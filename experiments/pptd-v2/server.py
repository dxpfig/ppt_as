#!/usr/bin/env python3
"""Disposable PPTD v2 browser probe. YAML files remain the only document source.

Run with the repository's Python environment (PyYAML required):
  .venv/bin/python experiments/pptd-v2/server.py --input <fixture.pptd> \
      --workspace runtime/pptd-v2-spike/working --host 127.0.0.1 --port 8891

This is a subset renderer/editing probe, not the Kimi engine or a PPTD validator.
Saving preserves YAML values, including unknown fields, but rewrites formatting
and comments in the edited .page. Unedited files are not serialized.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import mimetypes
import re
import shutil
import tempfile
import threading
import subprocess
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pptd_engine.fonts import FontResolver, document_font_usage
import yaml


from pptd_engine.document import load_yaml, ProbeDumper, Yaml12SafeLoader


def safe_path(root: Path, reference: str):
    if not isinstance(reference, str) or not reference or "\\" in reference:
        raise ValueError("A non-empty relative project path is required")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("PPTD paths must remain inside the project directory")
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("PPTD paths must remain inside the project directory")
    return result


def atomic_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".probe-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Workspace:
    def __init__(self, source: Path, destination: Path):
        self.source = source.resolve()
        self.root = destination.resolve()
        self.lock = threading.RLock()
        if self.source.suffix != ".pptd" or not self.source.is_file():
            raise ValueError("--input must name an existing .pptd main entry")
        if self.root == self.source.parent or self.root.is_relative_to(self.source.parent):
            raise ValueError("--workspace must be outside the fixture directory")
        self.metadata_path = self.root.with_name(self.root.name + ".app-metadata.json")
        if not self.root.exists() or not any(self.root.iterdir()):
            self.root.parent.mkdir(parents=True, exist_ok=True)
            # Do not follow links to data outside the self-contained fixture.
            for item in self.source.parent.rglob("*"):
                if item.is_symlink():
                    raise ValueError("The self-contained probe fixture must not contain symbolic links")
            shutil.copytree(self.source.parent, self.root, dirs_exist_ok=True)
            self.metadata = {"source": str(self.source), "entry": self.source.name, "createdAt": self.timestamp(), "edits": []}
            self.save_metadata()
        else:
            if not self.metadata_path.is_file():
                raise ValueError("Existing non-empty workspace lacks probe app metadata; use a fresh directory")
            self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            if self.metadata.get("source") != str(self.source):
                raise ValueError("Workspace belongs to a different fixture")
        self.entry = self.root / self.metadata["entry"]
        self.read()

    @staticmethod
    def timestamp():
        return datetime.now(timezone.utc).isoformat()

    def save_metadata(self):
        atomic_text(self.metadata_path, json.dumps(self.metadata, ensure_ascii=False, indent=2) + "\n")

    def read(self):
        with self.lock:
            document = load_yaml(self.entry)
            if document.get("version") != "v2":
                raise ValueError("This probe only accepts PPTD version: v2")
            if not isinstance(document.get("pages"), list):
                raise ValueError("PPTD pages must be a list of relative .page paths")
            pages = []
            for reference in document["pages"]:
                path = safe_path(self.root, reference)
                if path.suffix != ".page":
                    raise ValueError("All page references must point to .page files")
                page = load_yaml(path)
                elements = page.get("elements")
                if not isinstance(elements, list):
                    raise ValueError(f"Missing elements array: {reference}")
                ids = [element.get("elementId") for element in elements]
                if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
                    raise ValueError(f"Unique non-empty stable elementId required: {reference}")
                pages.append({"path": reference, "data": page})
            return {"document": document, "pages": pages}

    def edit(self, request):
        if not isinstance(request, dict):
            raise ValueError("Edit body must be an object")
        allowed = {"page", "pageIndex", "elementId", "text", "bounds"}
        if set(request) - allowed:
            raise ValueError("Unknown edit keys: " + ", ".join(sorted(set(request) - allowed)))
        if "text" not in request and "bounds" not in request:
            raise ValueError("Provide text and/or bounds")
        with self.lock:
            current = self.read()
            reference = request.get("page")
            if reference is None:
                index = request.get("pageIndex")
                if type(index) is not int or not 0 <= index < len(current["pages"]):
                    raise ValueError("Provide page path or zero-based pageIndex")
                reference = current["pages"][index]["path"]
            matches = [page for page in current["pages"] if page["path"] == reference]
            if len(matches) != 1:
                raise ValueError("Page must be a unique main-entry page reference")
            page = copy.deepcopy(matches[0]["data"])
            elements = [element for element in page["elements"] if element["elementId"] == request.get("elementId")]
            if len(elements) != 1:
                raise ValueError("elementId not found on selected page")
            element = elements[0]
            changed = []
            if "text" in request:
                if element.get("elementType") != "text" or not isinstance(request["text"], str):
                    raise ValueError("text edits require a text element and a string")
                if len(request["text"]) > 100_000:
                    raise ValueError("Text exceeds probe limit (100,000 characters)")
                element.setdefault("content", {})["text"] = request["text"]
                changed.append("content.text")
            if "bounds" in request:
                bounds = request["bounds"]
                if not isinstance(bounds, list) or len(bounds) != 4 or any(type(value) not in (int, float) or not math.isfinite(value) for value in bounds):
                    raise ValueError("bounds must contain four finite numbers: [x, y, width, height]")
                if bounds[2] < 0 or bounds[3] < 0:
                    raise ValueError("width and height must be non-negative")
                element["bounds"] = bounds
                changed.append("bounds")
            serialized = yaml.dump(page, Dumper=ProbeDumper, allow_unicode=True, sort_keys=False, width=1000)
            if yaml.load(serialized, Loader=Yaml12SafeLoader) != page:
                raise ValueError("Serialization changed YAML values; edit refused")
            target = safe_path(self.root, reference)
            atomic_text(target, serialized)
            reread = load_yaml(target)
            if reread != page:
                raise RuntimeError("Post-write YAML verification failed")
            self.metadata["edits"].append({"at": self.timestamp(), "page": reference, "elementId": element["elementId"], "fields": changed})
            self.save_metadata()
            return {"ok": True, "verified": True, "page": reference, "elementId": element["elementId"], "changedFields": changed, "data": reread}

    def fonts(self):
        state = self.read()
        resolver = FontResolver(ROOT / "runtime/fonts", state["document"].get("customFonts", []))
        assets, errors = [], []
        for (family, weight, italic), chars in document_font_usage(state["document"], [p["data"] for p in state["pages"]]).items():
            try:
                asset = resolver.resolve(family, weight, italic)
                asset.check_coverage("".join(chars))
                assets.append(asset.public())
            except Exception as error: errors.append({"family": family, "weight": weight, "style": "italic" if italic else "normal", "error": str(error)})
        return {"fonts": assets, "errors": errors}

    def capabilities(self):
        return {
            "label": "PPTD v2 高级能力预览",
            "authority": "PPTD v2 YAML (.pptd + .page + resources)",
            "renderer": "Independent KaTeX / Font Awesome 7 Free / ECharts 6 / Web Animations runtime",
            "advanced": {"math": "KaTeX + native editable Office Math", "icons": "FA 7 Free solid/regular/brands SVG and editable paths", "animations": "22 browser effects + native PPTX timing", "charts": "13 types; native Office charts where compatible, editable vectors with explicit policy otherwise", "fonts": "Shared cached font resources + full EOT embedded fonts, with permission and glyph checks"},
            "unsupported": ["Complete Kimi/PPTX visual equivalence", "Full 177-shape library and arbitrary HTML/CSS", "Full independent PPTX import of advanced objects", "Microsoft PowerPoint device-level animation validation"],
            "persistence": {"untouchedFiles": "byte-for-byte unchanged", "editedPage": "YAML values retained; comments/formatting normalized", "appMetadata": "separate sidecar outside document directory"},
        }


def make_handler(workspace: Workspace):
    html = Path(__file__).parent / "web" / "index.html"

    class Handler(BaseHTTPRequestHandler):
        def json_response(self, status, value):
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            try:
                route = unquote(urlsplit(self.path).path)
                if route == "/api/identity":
                    return self.json_response(200, {"workspace": str(workspace.root), "entry": workspace.entry.name})
                if route == "/api/document":
                    return self.json_response(200, workspace.read())
                if route == "/api/fonts":
                    return self.json_response(200, workspace.fonts())
                if route == "/api/capabilities":
                    return self.json_response(200, workspace.capabilities())
                if route in ("/", "/index.html"):
                    path = html
                elif route.startswith("/runtime/"):
                    path = safe_path(ROOT / "packages/pptd-runtime/build", route[len("/runtime/"):])
                elif route.startswith("/fonts/"):
                    reference = route[len("/fonts/"):]
                    if not re.fullmatch(r"[a-f0-9]{64}\.(ttf|otf)", reference):
                        return self.json_response(404, {"error": "Font not found"})
                    path = safe_path(ROOT / "runtime/fonts", reference)
                elif route.startswith("/exports/"):
                    path = safe_path(workspace.root.with_name(workspace.root.name + "-export"), route[len("/exports/"):])
                elif route.startswith("/resources/"):
                    path = safe_path(workspace.root, route[len("/resources/"):])
                elif route.startswith("/media/"):
                    path = safe_path(workspace.root, route.lstrip("/"))
                else:
                    return self.json_response(404, {"error": "Not found"})
                if not path.is_file():
                    return self.json_response(404, {"error": "File not found"})
                raw = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(raw)
            except (ValueError, OSError) as error:
                self.json_response(400, {"error": str(error)})

        def do_POST(self):
            route = urlsplit(self.path).path
            if route not in ("/api/edit", "/api/export", "/api/export-pdf"):
                return self.json_response(404, {"error": "Not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("Request body must be between 1 byte and 1 MB")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict): raise ValueError("Request body must be an object")
                if route == "/api/export-pdf":
                    import pymupdf
                    out = workspace.root.with_name(workspace.root.name + "-export")
                    out.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="pdf-", dir=out) as directory:
                        result = subprocess.run(["node", str(Path(__file__).parent / "export_pdf.cjs"), f"http://127.0.0.1:{self.server.server_port}", directory], capture_output=True, text=True, timeout=240)
                        if result.returncode: raise ValueError("PDF rendering failed: " + result.stderr[-2000:])
                        pages = sorted(Path(directory).glob("slide-*.pdf"))
                        if not pages: raise ValueError("PDF renderer produced no pages")
                        merged = pymupdf.open()
                        for page in pages:
                            with pymupdf.open(page) as document: merged.insert_pdf(document)
                        destination = out / "export.pdf"
                        temporary = out / (".pdf-" + uuid.uuid4().hex)
                        merged.save(temporary); merged.close(); temporary.replace(destination)
                        report = {**json.loads((Path(directory) / "pdf-pages.json").read_text()), "engine": "Chromium print", "pageCount": len(pages), "formulas": "KaTeX vector/text rendering", "animationState": "authored static appearance", "wholeSlideRasterization": False}
                        atomic_text(out / "pdf-report.json", json.dumps(report, ensure_ascii=False, indent=2))
                    return self.json_response(200, {"url": "/exports/export.pdf", "reportUrl": "/exports/pdf-report.json", "report": report})
                if route == "/api/export":
                    from pptd_engine.pptx import Exporter
                    with workspace.lock:
                        report = Exporter(workspace.entry, workspace.root.with_name(workspace.root.name + "-export"), allow_vector=request.get("allowVectorCharts") is True).run()
                    return self.json_response(200, {"url": "/exports/export.pptx", "reportUrl": "/exports/export-report.json", "report": report})
                return self.json_response(200, workspace.edit(request))
            except (ValueError, OSError, TypeError, yaml.YAMLError, subprocess.TimeoutExpired) as error:
                return self.json_response(400, {"error": str(error)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8891)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    workspace = Workspace(args.input, args.workspace)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(workspace))
    print(f"PPTD subset probe: http://{args.host}:{args.port} | source={workspace.source} | working={workspace.root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
