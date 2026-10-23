"""Shared, content-addressed template conversion cache."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def cache_root(data_root: Path, template_pptx: Path) -> Path:
    digest = _sha256(template_pptx)
    return Path(data_root) / 'template-cache' / digest[:20]


def _run_cli(command: str, arguments: list[str], cwd: Path) -> None:
    process = subprocess.run(
        [
            sys.executable,
            '-m',
            'pptd_engine.cli',
            command,
            *arguments,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if process.returncode:
        detail = (process.stderr or process.stdout).strip()[-3000:]
        raise ValueError(f'template {command} failed: {detail}')


def prepare(
    data_root: Path,
    template_pptx: Path,
    *,
    screenshots: bool,
) -> dict:
    """Convert a template once and return cached PPTD and optional screenshots."""
    data_root = Path(data_root).resolve()
    template_pptx = Path(template_pptx).resolve()
    root = cache_root(data_root, template_pptx)
    pptd_dir = root / 'template-pptd'
    shots_dir = root / 'template-screenshots'
    entries = sorted(pptd_dir.glob('*.pptd')) if pptd_dir.is_dir() else []
    if not entries:
        root.mkdir(parents=True, exist_ok=True)
        _run_cli(
            'convert',
            [str(template_pptx), '-o', str(pptd_dir)],
            ROOT,
        )
        entries = sorted(pptd_dir.glob('*.pptd'))
    if not entries:
        raise ValueError('模板转换未生成 PPTD 入口。')
    entry = entries[0]
    if screenshots and not any((shots_dir / 'pages').glob('*.png')):
        _run_cli(
            'screenshot',
            [str(pptd_dir), '-o', str(shots_dir)],
            ROOT,
        )
    profile = {
        'available': True,
        'hostApplies': [
            'background',
            'cover',
            'corporate elements',
            'closing',
        ],
    }
    try:
        from pptd_engine.document import load_yaml

        document = load_yaml(entry)
        profile['size'] = document.get('size', [960, 540])
        profile['templatePages'] = len(document.get('pages', []))
    except (OSError, ValueError):
        pass
    metadata = {
        'sourceSha256': _sha256(template_pptx),
        'entry': str(entry),
        'screenshotsReady': any((shots_dir / 'pages').glob('*.png')),
        'profile': profile,
    }
    (root / 'analysis.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return {
        'pptx': str(template_pptx),
        'pptd': str(pptd_dir),
        'entry': str(entry),
        'screenshots': str(shots_dir),
        'profile': profile,
    }
