"""Bounded context packets with lossless, allowlisted reads of original sources."""
import copy
import hashlib
import json
import os
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class MaterialDigest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    excerpts: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(max_length=12)


class ContextRead(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=100)
    offset: int = Field(ge=0)
    length: int = Field(default=8000, ge=1, le=12000)


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def split_text(text, size=12000):
    # Character offsets (not bytes) never split a Unicode code point. Every
    # character, including whitespace, belongs to exactly one recoverable chunk.
    return [text[start:start + size] for start in range(0, len(text), size)]


def exact_material_excerpts(text, *, limit=6000, max_items=12):
    """Select exact source paragraphs spread across one material chunk.

    This is the deterministic default for long references. It avoids asking an
    OpenAI-compatible model to re-emit thousands of Chinese characters inside a
    JSON string, which is prone to unescaped quotes, newlines and truncated
    output. The planner still receives representative facts from every region
    of the supplied source, and the complete original remains readable on demand.
    """
    if not isinstance(text, str) or not text:
        return []
    paragraphs = []
    for value in re.split(r'\n\s*\n', text):
        value = value.strip()
        if not value:
            continue
        if len(value) <= 2000:
            paragraphs.append(value)
            continue
        # Some DOCX exports return one very long paragraph. Split it at nearby
        # punctuation so later regions can still be represented.
        cursor = 0
        while cursor < len(value):
            end = min(len(value), cursor + 1800)
            if end < len(value):
                boundary = max(value.rfind(mark, cursor + 900, end)
                               for mark in ('。', '；', '！', '？', '\n'))
                if boundary >= 0:
                    end = boundary + 1
            piece = value[cursor:end].strip()
            if piece:
                paragraphs.append(piece)
            cursor = end
    if not paragraphs:
        paragraphs = [text.strip()]
    located, cursor = [], 0
    for paragraph in paragraphs:
        start = text.find(paragraph, cursor)
        if start < 0:
            continue
        end = start + len(paragraph)
        located.append((start, end))
        cursor = end
    units, headings = [], []
    for start, end in located:
        paragraph = text[start:end]
        heading = (
            len(paragraph) <= 120
            and not re.search(r'[。！？]', paragraph)
            and (
                bool(re.match(r'^(?:[一二三四五六七八九十]+、|\d+(?:\.\d+)*\s*)', paragraph))
                or len(paragraph) <= 48
            )
        )
        if heading:
            headings.append((start, end))
            if len(headings) >= 3:
                units.append((headings[0][0], headings[-1][1]))
                headings = []
            continue
        units.append((headings[0][0] if headings else start, end))
        headings = []
    if headings:
        units.append((headings[0][0], headings[-1][1]))
    if not units:
        units = [(0, len(text))]
    # Keep a stable one-to-one distribution across the chunk instead of taking
    # only its opening pages. Short headings are useful, but full body paragraphs
    # that contain dates, metrics and strategy terms receive a higher score.
    count = min(max_items, len(units))
    buckets = [units[index * len(units) // count:(index + 1) * len(units) // count]
               for index in range(count)]

    def score(unit):
        start, end = unit
        paragraph = text[start:end]
        value = 15 if len(paragraph) >= 80 else 0
        value += 12 if re.search(r'\d|%|％|亿元|万元|万片|TOPS|POPS', paragraph) else 0
        value += sum(6 for term in (
            '战略', '需求', '目标', '规划', '产品', '能力', '市场', '技术', '保障', '建议', '风险'
        ) if term in paragraph)
        value += 10 if re.match(r'^(?:[一二三四五六七八九十]+、|\d+(?:\.\d+)*\s*)', paragraph) else 0
        return value

    per_item = max(1, min(2000, limit // max(1, count)))
    selected = []
    for bucket in buckets:
        if not bucket:
            continue
        start, _ = max(bucket, key=score)
        excerpt = text[start:min(len(text), start + per_item)].strip()
        if excerpt and excerpt not in selected:
            selected.append(excerpt)
    remaining = limit - sum(map(len, selected))
    if remaining > 0:
        ranked = sorted(units, key=score, reverse=True)
        for start, _ in ranked:
            excerpt = text[start:min(len(text), start + min(per_item, remaining))].strip()
            if excerpt and excerpt not in selected:
                selected.append(excerpt)
                remaining -= len(excerpt)
            if remaining <= 0 or len(selected) >= max_items:
                break
    return selected[:max_items]


def material_llm_excerpts_enabled():
    return os.getenv('PPT_MATERIAL_LLM_EXCERPTS', '0').strip().lower() in ('1', 'true', 'yes')


def read_context(sources, query):
    request = ContextRead.model_validate(query, strict=True)
    if request.id not in sources:
        raise ValueError('原文引用不存在，请使用材料目录中的 sourceId。')
    text = sources[request.id]
    if request.offset >= len(text):
        raise ValueError('原文读取位置超出范围。')
    end = min(len(text), request.offset + request.length)
    return {'id': request.id, 'offset': request.offset, 'text': text[request.offset:end],
            'nextOffset': end if end < len(text) else None, 'totalCharacters': len(text)}


def page_context(context, plan, complete, index):
    paths = plan['document']['pages']
    prior = [(i, path) for i, path in enumerate(paths[:index]) if path in complete]
    registry = dict(context.get('_contextSources', {}))
    catalog = []
    for i, path in prior:
        source_id = 'page-' + _hash(path)[:20]
        registry[source_id] = json.dumps(complete[path], ensure_ascii=False, separators=(',', ':'))
        item = plan['pages'][i]
        catalog.append({'path': path, 'sourceId': source_id, 'title': item['title'],
                        'visualPlan': item.get('visualPlan', '')})
    result = {**context, 'plan': plan, 'pageIndex': index, 'pagePath': paths[index],
              'pagePlan': plan['pages'][index],
              'previousPages': [copy.deepcopy(complete[path]) for _, path in prior[-2:]],
              'pageCatalog': catalog, '_contextSources': registry}
    # Template mode: hand the writer the exact shell it must reproduce.
    chosen = (plan['pages'][index] or {}).get('templatePage')
    raw = registry.get(chosen) if chosen else None
    if raw:
        try:
            template = json.loads(raw)
            result['templatePage'] = template
            # Name every text slot that has to be filled: a slot the model misses
            # keeps the company template's own words, which must never ship.
            height = plan['document'].get('size', [960, 540])[1]
            result['templateTextSlots'] = [
                {'elementId': element['elementId'], 'bounds': element.get('bounds'),
                 'current': re.sub(r'<[^>]+>', '', str((element.get('content') or {}).get('text', '')))[:60],
                 'characters': len(re.sub(r'<[^>]+>', '', str((element.get('content') or {}).get('text', ''))).strip()),
                 'limit': len(re.sub(r'<[^>]+>', '', str((element.get('content') or {}).get('text', ''))).strip()) + 2}
                for element in template.get('elements') or []
                if element.get('elementType') == 'text' and element.get('elementId')
                and not _band(element.get('bounds'), height)
            ]
        except ValueError:
            pass
    return result


def _band(bounds, height):
    """Top/bottom bands hold logo, footer and page-number text."""
    if not isinstance(bounds, list) or len(bounds) != 4 or not isinstance(height, (int, float)):
        return False
    top, size = bounds[1], bounds[3]
    if not isinstance(top, (int, float)) or not isinstance(size, (int, float)):
        return False
    return top + size <= height * 0.12 or top >= height * 0.88


async def prepare_materials(context, state, ask, save):
    """Read long sources chunk-by-chunk; persist verified exact excerpts.

    The user request, design system, images, and short materials are untouched.
    Digests do not substitute for originals: every original is retained and
    can be read again by sourceId/character offset during planning or authoring.
    """
    result = copy.deepcopy(context)
    registry = dict(result.get('_contextSources', {}))
    pending = {}

    def collect(value, field=''):
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if key in ('text', 'extractedText', 'references') and isinstance(child, str) and len(child) > 24000:
                    source_id = 'material-' + _hash(child)[:24]
                    pending[source_id] = child
                    value[key] = {'sourceId': source_id, 'characters': len(child), 'summaryIn': 'materials'}
                elif key not in ('request', 'designSystem', 'imageData', '_contextSources', 'existingDocument'):
                    collect(child, key)
        elif isinstance(value, list):
            for child in value:
                collect(child, field)

    collect(result)
    if not pending:
        return result
    digests = state.setdefault('materialDigests', {})
    originals = state.setdefault('materialSources', {})
    materials = []
    for source_id, original in pending.items():
        registry[source_id] = original
        originals[source_id] = original
        await save()
        excerpts = []
        for index, chunk in enumerate(split_text(original)):
            key = source_id + ':' + str(index)
            cached = digests.get(key)
            if cached and cached.get('sha256') == _hash(chunk):
                answer = MaterialDigest.model_validate(cached['digest'], strict=True).model_dump()
                digest_mode = cached.get('mode', 'model')
            elif not material_llm_excerpts_enabled():
                answer = {'excerpts': exact_material_excerpts(chunk)}
                digests[key] = {'sha256': _hash(chunk), 'digest': answer, 'mode': 'local-exact'}
                await save()
            else:
                digest_mode = 'model'
                payload = {'request': context.get('request', ''),
                           'source': {'id': key, 'offset': index * 12000, 'text': chunk}}
                try:
                    for attempt in range(2):
                        answer = await ask('source', payload, scenario=context.get('scenario', 'auto'),
                                           response_schema=MaterialDigest)
                        answer = MaterialDigest.model_validate(answer, strict=True).model_dump()
                        if all(text in chunk for text in answer['excerpts']) and sum(map(len, answer['excerpts'])) <= 6000:
                            break
                        payload['validationError'] = 'Return only verbatim excerpts from this chunk, at most 6000 characters in total. Never invent or rewrite facts.'
                    else:
                        raise ValueError('长资料摘录未通过原文一致性检查；已保留原文与完成的分块，可继续恢复。')
                except Exception as exc:
                    if getattr(exc, 'failure_kind', None) not in ('format', 'output-limit', 'timeout'):
                        raise
                    answer = {'excerpts': exact_material_excerpts(chunk)}
                    digest_mode = 'local-exact'
                digests[key] = {'sha256': _hash(chunk), 'digest': answer, 'mode': digest_mode}
                await save()
            if not all(text in chunk for text in answer['excerpts']) or sum(map(len, answer['excerpts'])) > 6000:
                raise ValueError('材料检查点摘录与原文不一致。')
            excerpts.append({'offset': index * 12000, 'excerpts': answer['excerpts']})
        materials.append({'sourceId': source_id, 'characters': len(original), 'chunks': excerpts,
                          'note': '这些是逐块原文摘录，并非完整原文；需要细节时用 contextRequest 按偏移读取。'})
    result.update(materials=materials, _contextSources=registry)
    return result
