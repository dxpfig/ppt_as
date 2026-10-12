"""Reproduce the disposable PPTD editing/rendering/PPTX evaluation, not production migration."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUTPUT = ROOT / "runtime/pptd-v2-spike"


def run(*args):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True, timeout=120)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def hashes(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-browser-evidence", action="store_true", help="Use an already successful browser run after checking its source hashes; default reruns browser")
    args = parser.parse_args()
    node = shutil.which("node")
    office = shutil.which("soffice") or shutil.which("libreoffice")
    if not node or not office:
        raise SystemExit("This local probe requires existing Node and LibreOffice/soffice; no dependencies will be installed")
    run(sys.executable, HERE / "check_document.py")
    run(node, HERE / "yaml_probe.cjs")
    if not args.reuse_browser_evidence:
        run(node, HERE / "browser_probe.cjs")
    browser = read(OUTPUT / "browser/browser-report.json")
    if browser["status"] != "passed" or hashes(HERE / "fixture") != browser["fixtureHashes"]:
        raise SystemExit("Browser evidence is not successful or source fixture changed; rerun the browser probe")
    working_entry = Path(browser["workingEntry"])
    snapshot = OUTPUT / "edited-document"
    shutil.copytree(working_entry.parent, snapshot, dirs_exist_ok=True)
    assert hashes(snapshot) == hashes(working_entry.parent)
    imported = ROOT / "runtime/test-results/design-layouts/presentation.pptx"
    destination = OUTPUT / "pptx-edited"
    run(sys.executable, HERE / "pptx_probe.py", "--input", snapshot / working_entry.name, "--output", destination, "--import-pptx", imported)
    run(sys.executable, HERE / "verify_pptx_probe.py", "--input", snapshot / working_entry.name, "--output", destination, "--import-source", imported)
    run(node, HERE / "render_import.cjs")
    pdf = destination / "export.pdf"
    # Delete only this generated derivative to avoid using a stale PDF after a failed conversion.
    pdf.unlink(missing_ok=True)
    run(office, "-env:UserInstallation=" + (OUTPUT / "lo-profile").as_uri(), "--headless", "--convert-to", "pdf", "--outdir", destination, destination / "export.pptx")
    with pymupdf.open(pdf) as document:
        assert len(document) == 2
        pdf_fonts = []
        for index, page in enumerate(document):
            page.get_pixmap(matrix=pymupdf.Matrix(1, 1)).save(destination / f"export-slide-{index+1}.png")
            pdf_fonts.append([font[3] for font in page.get_fonts()])
        assert "编辑后仍以源文件为准" in document[0].get_text()
        assert "保存后继续核对" in document[1].get_text()
    # Side-by-side evidence deliberately includes visible incompatibilities on page 2.
    board = Image.new("RGB", (1956, 1184), "#E5E9E5")
    draw = ImageDraw.Draw(board)
    font = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 20)
    for index in range(2):
        y = index * 592
        for column, (label, file) in enumerate([
            ("浏览器呈现 · PPTD 编辑副本", OUTPUT / f"browser/slide-{index+1}.png"),
            ("PPTX 经 LibreOffice 渲染 · 非 PowerPoint 实测", destination / f"export-slide-{index+1}.png"),
        ]):
            x = 12 + column * 972
            draw.text((x, y + 12), f"{index+1} / {label}", fill="#17342D", font=font)
            with Image.open(file) as picture:
                board.paste(picture.convert("RGB").resize((960, 540)), (x, y + 44))
    board.save(OUTPUT / "comparison.png")
    imported_board = Image.new("RGB", (1304, 1240), "#E5E9E5")
    imported_draw = ImageDraw.Draw(imported_board)
    for index in range(6):
        x, y = (index % 2) * 652 + 8, (index // 2) * 412 + 8
        imported_draw.text((x, y), f"导入后浏览器呈现 · 第 {index+1} 页", fill="#17342D", font=font)
        with Image.open(OUTPUT / f"import-browser/slide-{index+1}.png") as picture:
            picture.thumbnail((640, 370))
            imported_board.paste(picture.convert("RGB"), (x, y + 30))
    imported_board.save(OUTPUT / "import-browser/contact-sheet.png")
    archive = shutil.make_archive(str(OUTPUT / "edited-pptd-project"), "zip", snapshot)
    export = read(destination / "export-report.json")
    imported_report = read(destination / "imported/import-report.json")
    report = {
        "status": "passed-with-documented-limitations",
        "scope": "two-page fixture subset, not full PPTD compatibility or production adoption",
        "sourceBrowserEntry": str(working_entry),
        "exportInput": str(snapshot / working_entry.name),
        "editedDocumentHashes": hashes(snapshot),
        "fixtureHashes": hashes(HERE / "fixture"),
        "documentChecks": read(OUTPUT / "document-report.json"),
        "browserReport": str(OUTPUT / "browser/browser-report.json"),
        "yamlCandidateReport": str(OUTPUT / "yaml-candidate-report.json"),
        "pptxVerification": read(destination / "verification.json"),
        "exportObjectSummary": export["summary"],
        "exportUnsupported": export["unsupported"],
        "independentImport": {"source": str(imported), "pages": imported_report["pageCount"], "elements": imported_report["importedElementCounts"], "report": str(destination / "imported/import-report.json")},
        "importBrowserReport": str(OUTPUT / "import-browser/render-report.json"),
        "officeVersion": subprocess.check_output([office, "--version"], text=True).strip(),
        "pdfFonts": pdf_fonts,
        "artifacts": {"pptx": str(destination / "export.pptx"), "pdf": str(pdf), "pptdZip": archive, "comparison": str(OUTPUT / "comparison.png")},
        "notVerified": ["Microsoft PowerPoint editing/playback", "Kimi visual equivalence", "complete PPTD schema/checker", "all 177 shapes/13 charts/22 animations", "production editor, IME, drag/resize, undo/redo, comments, versions", "arbitrary third-party PPTX import fidelity"],
    }
    (OUTPUT / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "evidence": str(OUTPUT / "evaluation.json"), "artifacts": report["artifacts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
