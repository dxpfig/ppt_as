"""Template and research context boundary shared by generation modes."""
from __future__ import annotations

from pathlib import Path

from . import template_reference
from .research import research_context


def load_template(root: Path | None, template_id: str | None) -> dict | None:
    return template_reference.load(root, template_id)


async def research(request) -> dict:
    return await research_context(request)
