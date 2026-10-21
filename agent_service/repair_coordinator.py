"""Issue fingerprints, stall detection and bounded repair coordination."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .execution_policy import quick_policy
from .visual_repair import repair_deck as visual_repair_deck


def issue_fingerprint(issues) -> str:
    values = sorted(
        (
            str(issue.get('pagePath') or issue.get('page')),
            str(issue.get('code')),
            ','.join(sorted(issue.get('elementIds') or [issue.get('elementId', '')])),
        )
        for issue in issues
    )
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def stalled(previous_fingerprints: list[str], issues) -> bool:
    return issue_fingerprint(issues) in previous_fingerprints


async def repair_deck(*args, **kwargs):
    return await visual_repair_deck(*args, **kwargs)


async def repair_deck_batched(
    request,
    state,
    issues,
    *,
    on_event=None,
    rendered_pages=None,
    layout_measurements=None,
    validate_candidate=None,
    on_audit=None,
    repair_fn=None,
):
    """Keep each repair request within the provider context budget."""
    repair = repair_fn or repair_deck
    issue_pages = {
        issue.get('pagePath') or issue.get('page')
        for issue in issues
        if issue.get('pagePath') or issue.get('page')
    }
    ordered = [page['path'] for page in state['pages'] if page['path'] in issue_pages]
    limit = quick_policy().visual_repair_page_batch
    if len(ordered) <= limit:
        return await repair(
            request,
            state,
            issues,
            on_event=on_event,
            rendered_pages=rendered_pages,
            layout_measurements=layout_measurements,
            validate_candidate=validate_candidate,
            on_audit=on_audit,
        )
    repaired = deepcopy(state)
    for start in range(0, len(ordered), limit):
        paths = set(ordered[start:start + limit])
        batch_issues = [
            issue for issue in issues
            if (issue.get('pagePath') or issue.get('page')) in paths
        ]
        repaired = await repair(
            request,
            repaired,
            batch_issues,
            on_event=on_event,
            rendered_pages=rendered_pages,
            layout_measurements=layout_measurements,
            validate_candidate=None,
            on_audit=on_audit,
        )
    return repaired
