"""Bounded context packets with lossless, allowlisted reads of original sources."""
import copy
import hashlib
import json
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
            else:
                payload = {'request': context.get('request', ''),
                           'source': {'id': key, 'offset': index * 12000, 'text': chunk}}
                for attempt in range(2):
                    answer = await ask('source', payload, scenario=context.get('scenario', 'auto'),
                                       response_schema=MaterialDigest)
                    answer = MaterialDigest.model_validate(answer, strict=True).model_dump()
                    if all(text in chunk for text in answer['excerpts']) and sum(map(len, answer['excerpts'])) <= 6000:
                        break
                    payload['validationError'] = 'Return only verbatim excerpts from this chunk, at most 6000 characters in total. Never invent or rewrite facts.'
                else:
                    raise ValueError('长资料摘录未通过原文一致性检查；已保留原文与完成的分块，可继续恢复。')
                digests[key] = {'sha256': _hash(chunk), 'digest': answer}
                await save()
            if not all(text in chunk for text in answer['excerpts']) or sum(map(len, answer['excerpts'])) > 6000:
                raise ValueError('材料检查点摘录与原文不一致。')
            excerpts.append({'offset': index * 12000, 'excerpts': answer['excerpts']})
        materials.append({'sourceId': source_id, 'characters': len(original), 'chunks': excerpts,
                          'note': '这些是逐块原文摘录，并非完整原文；需要细节时用 contextRequest 按偏移读取。'})
    result.update(materials=materials, _contextSources=registry)
    return result
