"""Complete AI geometry using measured text and bounded linear constraints.

This is a private candidate transformation, never an acceptance gate. It retains
the model's widths, typography and relative reading order; the browser must
measure changed wrapping again and the AI must review the resulting pixels.
"""
from __future__ import annotations

import copy
import math

import kiwisolver as kiwi


def complete_geometry(candidate, measured_source, layouts, paths):
    result = copy.deepcopy(candidate)
    width, height = result['document']['size']
    measured_pages = {p['path']: p['data'] for p in measured_source['pages']}
    for page in result['pages']:
        path = page['path']
        if path not in paths:
            continue
        originals = {e['elementId']: e for e in measured_pages[path]['elements']}
        measurements = {m['elementId']: m for m in layouts.get(path, {}).get('elements', [])}
        boxes = [e for e in page['data']['elements'] if e['elementType'] in ('text', 'table')
                 and not e.get('rotation', 0) % 360 and not any(e.get('flip', []))]
        solver = kiwi.Solver()
        variables = {}
        before = {e['elementId']: copy.deepcopy(e['bounds']) for e in boxes}
        try:
            for e in boxes:
                key = e['elementId']
                x, y, w, h = e['bounds']
                vx, vy, vh = (kiwi.Variable(f'{key}.{axis}') for axis in ('x', 'y', 'h'))
                variables[key] = (vx, vy, vh)
                m = measurements.get(key)
                old = originals[key]
                minimum = h
                if (m and e['elementType'] == 'text' and w == old['bounds'][2]
                        and e['content'] == old['content']):
                    minimum = math.ceil(m['measuredTextSize'][1] + m.get('paddingSize', [0, 0])[1]) + 1
                for constraint in (vx >= 0, vy >= 0, vx + w <= width, vy + vh <= height, vh >= minimum):
                    solver.addConstraint(constraint)
                # Keep whitespace when possible, but never at the expense of
                # measured glyphs. Position preferences yield to non-overlap.
                solver.addConstraint((vh == max(h, minimum)) | kiwi.strength.weak)
                solver.addConstraint((vx == x) | kiwi.strength.strong)
                solver.addConstraint((vy == y) | kiwi.strength.medium)
            for index, a in enumerate(boxes):
                ax, ay, aw, ah = a['bounds']
                avx, avy, avh = variables[a['elementId']]
                for b in boxes[index + 1:]:
                    bx, by, bw, bh = b['bounds']
                    bvx, bvy, bvh = variables[b['elementId']]
                    # Preserve the AI's intended row/column relationship. In
                    # an overlap, normalized center distance selects its axis.
                    overlap = min(ax + aw, bx + bw) - max(ax, bx)
                    horizontal = overlap <= min(aw, bw) * .2
                    if horizontal:
                        first, second, span = (avx, bvx, aw) if ax <= bx else (bvx, avx, bw)
                        solver.addConstraint(second >= first + span + 4)
                    else:
                        first, second, span = (avy, bvy, avh) if ay <= by else (bvy, avy, bvh)
                        solver.addConstraint(second >= first + span + 4)
            solver.updateVariables()
        except kiwi.UnsatisfiableConstraint as exc:
            raise ValueError(f'{path}: measured text cannot fit this arrangement; reflow into wider or additional columns without deleting content') from exc
        for e in boxes:
            vx, vy, vh = variables[e['elementId']]
            e['bounds'] = [round(vx.value(), 4), round(vy.value(), 4), e['bounds'][2], round(vh.value(), 4)]
        # Small adjacent bullets/icons follow their nearest text anchor. Large
        # illustrations, charts, containers and connectors remain model-owned.
        for e in page['data']['elements']:
            if e['elementType'] not in ('shape', 'icon', 'line'):
                continue
            x, y, w, h = e['bounds']
            if max(w, h) > (32 if e['elementType'] == 'line' else 24):
                continue
            # If the model moved text but omitted its bullet, retain the
            # original attachment; otherwise honor its newly placed marker.
            anchor_bounds = ({key: value['bounds'] for key, value in originals.items()}
                             if e['bounds'] == originals[e['elementId']]['bounds'] else before)
            def attached(t):
                tx, ty, _, _ = anchor_bounds[t['elementId']]
                return ((abs(tx - x) <= 32 and 0 <= ty - y - h <= 24)
                        if e['elementType'] == 'line' else
                        (0 <= tx - x - w <= 24 and abs(ty - y) <= 16))
            anchors = [t for t in boxes if attached(t)]
            if not anchors:
                continue
            anchor = min(anchors, key=lambda t: abs(anchor_bounds[t['elementId']][1] - y))
            old = anchor_bounds[anchor['elementId']]
            e['bounds'] = [x + anchor['bounds'][0] - old[0], y + anchor['bounds'][1] - old[1], w, h]
    return result
