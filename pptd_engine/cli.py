"""kimi-slides command line: convert / check / screenshot.

Linux-native parity for the Windows ``kimi-slides.exe`` described in
``docs/kimi-ppt/original/reference/cli.md``. ``convert`` reuses the existing
PPTD importer, ``check`` reuses the structural validator, and ``screenshot``
reuses the Chromium print renderer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from pptd_engine.document import load_yaml
from pptd_engine import check as check_module


def _convert_deck(source: Path, output: Path) -> dict:
    """Import a PPTX into a CLI-compatible PPTD directory and return its report."""
    from pptd_engine.pptx import Importer

    source = source.resolve()
    output = output.resolve()
    stem = source.stem
    with tempfile.TemporaryDirectory(prefix=".kimi-convert-", dir=output.parent) as tmp:
        temp = Path(tmp) / "imported"
        report = Importer(source, temp).run()
        _normalize_import(temp, output, stem)
    return report


def _normalize_import(temp: Path, output: Path, stem: str) -> None:
    """Map the importer's internal layout to the documented CLI layout."""
    entry = load_yaml(temp / "doc.pptd")
    page_map = {}
    for index, ref in enumerate(entry.get("pages", []), 1):
        page_map[ref] = f"pages/page-{index}.page"
    media_map = {}
    media_dir = temp / "media"
    if media_dir.is_dir():
        for path in media_dir.iterdir():
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            media_map[f"media/{path.name}"] = f"media/{digest}{path.suffix}"
    (output / "pages").mkdir(parents=True, exist_ok=True)
    (output / "media").mkdir(parents=True, exist_ok=True)
    for old_ref, new_ref in page_map.items():
        page = load_yaml(temp / old_ref)
        _rewrite_media(page, media_map)
        (output / new_ref).write_text(yaml.safe_dump(page, allow_unicode=True, sort_keys=False), encoding="utf-8")
    entry["pages"] = [page_map[ref] for ref in entry.get("pages", [])]
    (output / f"{stem}.pptd").write_text(yaml.safe_dump(entry, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for old_ref, new_ref in media_map.items():
        shutil.copy2(temp / old_ref, output / new_ref)
    for path in (temp / "media").iterdir() if (temp / "media").is_dir() else []:
        new_ref = media_map.get(f"media/{path.name}")
        if new_ref is None:
            shutil.copy2(path, output / "media" / path.name)


def _rewrite_media(page: dict, media_map: dict[str, str]) -> None:
    for element in page.get("elements", []):
        if isinstance(element, dict) and element.get("src") in media_map:
            element["src"] = media_map[element["src"]]


def _resolve_chromium():
    configured = os.getenv("PPTD_CHROMIUM")
    if configured:
        return configured if Path(configured).is_file() else None
    for candidate in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _renderer_ready() -> bool:
    from pptd_engine.workbench import RUNTIME_DIR
    return bool(shutil.which("node") and (RUNTIME_DIR / "pptd-runtime.js").is_file() and (_resolve_chromium() or any((Path.home() / ".cache/ms-playwright").glob("chromium-*"))))


def _screenshot(entry: Path, output: Path, page_spec: str | None, scale: float, fmt: str, font_dir: str | None) -> list[str]:
    import pymupdf
    from pptd_engine.workbench import snapshot_server, WEB_DIR

    if not _renderer_ready():
        raise RuntimeError("渲染器不可用：请先执行 `pnpm build:pptd` 并安装 Chromium（或设置 PPTD_CHROMIUM）。")
    entry = entry.resolve()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    browser_dir = output / ".pdf"
    browser_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    browser = _resolve_chromium()
    if browser:
        env["PPTD_CHROMIUM"] = browser
    if font_dir:
        env["KIMI_SLIDES_FONTS_DIR"] = str(font_dir)
    try:
        with snapshot_server(entry, 0) as url:
            proc = subprocess.run(
                ["node", str(WEB_DIR / "screenshot.cjs"), url, str(browser_dir), page_spec or ""],
                env=env, capture_output=True, text=True, timeout=360,
            )
            if proc.returncode:
                raise RuntimeError("screenshot 渲染失败：" + (proc.stderr or proc.stdout)[-5000:])
        report = json.loads((browser_dir / "pdf-pages.json").read_text(encoding="utf-8"))
        pages_dir = output / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for item in report["pages"]:
            number = item["index"]
            with pymupdf.open(browser_dir / item["file"]) as pdf:
                pix = pdf[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            ext = "jpg" if fmt == "jpg" else "png"
            dest = pages_dir / f"page-{number}.{ext}"
            if fmt == "jpg":
                pix.save(str(dest), jpg_quality=90)
            else:
                pix.save(str(dest))
            written.append(dest.name)
        return written
    finally:
        shutil.rmtree(browser_dir, ignore_errors=True)


def _cmd_convert(args) -> int:
    source = Path(args.source)
    if not source.is_file() or source.suffix.lower() != ".pptx":
        raise SystemExit("convert 需要一个 .pptx 文件")
    output = Path(args.output) if args.output else source.with_suffix("")
    report = _convert_deck(source, output)
    print(json.dumps({"output": str(output / f"{source.stem}.pptd"), "pages": report.get("pageCount"), "unsupportedCount": len(report.get("unsupported", []))}, ensure_ascii=False, indent=2))
    return 0


def _cmd_check(args) -> int:
    issues = check_module.check_deck(Path(args.deck), args.page, args.severity, args.level)
    for issue in issues:
        print(check_module.format_issue(issue))
    return 0


def _cmd_screenshot(args) -> int:
    deck = Path(args.deck)
    output = Path(args.output) if args.output else None
    if deck.is_dir():
        entry = check_module._find_entry(deck)
        if output is None:
            output = deck.parent / f"{deck.name}-screenshots"
        written = _screenshot(entry, output, args.page, args.scale, args.format, args.fontDir)
    elif deck.is_file() and deck.suffix.lower() == ".pptx":
        if output is None:
            output = deck.parent / f"{deck.stem}-screenshots"
        with tempfile.TemporaryDirectory(prefix=".kimi-shot-", dir=deck.parent) as tmp:
            _convert_deck(deck, Path(tmp) / "deck")
            entry = check_module._find_entry(Path(tmp) / "deck")
            written = _screenshot(entry, output, args.page, args.scale, args.format, args.fontDir)
    else:
        raise SystemExit("screenshot 需要一个 .pptd 目录或 .pptx 文件")
    print(json.dumps({"output": str(output), "pages": written}, ensure_ascii=False, indent=2))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kimi-slides", description="PPTD presentation convert/check/screenshot")
    sub = parser.add_subparsers(dest="command", required=True)

    convert = sub.add_parser("convert", help="Convert a PPTX file to a PPTD directory")
    convert.add_argument("source")
    convert.add_argument("-o", "--output")
    convert.set_defaults(func=_cmd_convert)

    check = sub.add_parser("check", help="Validate and optionally repair a PPTD directory")
    check.add_argument("deck")
    check.add_argument("-p", "--page")
    check.add_argument("-s", "--severity", default="all")
    check.add_argument("--level", default="keep", choices=("keep", "auto"))
    check.set_defaults(func=_cmd_check)

    shot = sub.add_parser("screenshot", help="Render a PPTD directory or PPTX file to images")
    shot.add_argument("deck")
    shot.add_argument("-p", "--page")
    shot.add_argument("-o", "--output")
    shot.add_argument("--scale", type=float, default=1.0)
    shot.add_argument("--fontDir", dest="fontDir")
    shot.add_argument("--format", default="png", choices=("png", "jpg"))
    shot.add_argument("--compare", nargs="?", const="", help="best-effort: reference screenshot directory")
    shot.add_argument("--threshold", type=float)
    shot.add_argument("--diffOut", dest="diffOut")
    shot.add_argument("--native-charts", action="store_true", help=argparse.SUPPRESS)
    shot.set_defaults(func=_cmd_screenshot)
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
