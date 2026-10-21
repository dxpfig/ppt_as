"""Performance report comparison and merge-gate helpers."""
from __future__ import annotations


def compare_reports(baseline: dict, candidate: dict, max_regression: float = 0.2) -> dict:
    if not 0 <= max_regression <= 10:
        raise ValueError('max_regression must be between 0 and 10')
    regressions = []
    improvements = []

    def compare_metric(group: str, name: str, old_value, new_value):
        old = (old_value or {}).get('p95')
        new = (new_value or {}).get('p95')
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            return
        if old <= 0:
            return
        change = (new - old) / old
        item = {
            'group': group,
            'metric': name,
            'baselineP95': round(old, 3),
            'candidateP95': round(new, 3),
            'change': round(change, 4),
        }
        if change > max_regression:
            regressions.append(item)
        elif change < 0:
            improvements.append(item)

    compare_metric(
        'duration',
        'total',
        baseline.get('durationSeconds'),
        candidate.get('durationSeconds'),
    )
    for group in ('byNode', 'milestones'):
        before = baseline.get(group) or {}
        after = candidate.get(group) or {}
        for name in sorted(set(before) & set(after)):
            compare_metric(group, name, before.get(name), after.get(name))
    return {
        'passed': not regressions,
        'maxRegression': max_regression,
        'regressions': regressions,
        'improvements': improvements,
    }
