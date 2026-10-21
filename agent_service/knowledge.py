"""Company knowledge-base retrieval with source provenance."""
from __future__ import annotations

import json
import os
import re

from .images import api_json, knowledge_bases, plain, weknora_headers, weknora_url


class KnowledgeError(ValueError):
    pass


def configured() -> bool:
    return bool(os.getenv('WEKNORA_BASE_URL') and os.getenv('WEKNORA_API_KEY'))


def _content(item: dict) -> str:
    value = item.get('content') or item.get('text') or item.get('chunk_content') or ''
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    value = plain(value)
    return value[:12000]


async def search(query: str, knowledge_base_ids: list[str] | None = None, limit: int = 8) -> dict:
    if not configured():
        return {'status': 'unconfigured', 'sources': [], 'warnings': []}
    ids = [value for value in (knowledge_base_ids or []) if isinstance(value, str) and value.strip()]
    if not ids:
        ids = [value.strip() for value in os.getenv('WEKNORA_KNOWLEDGE_BASE_IDS', '').split(',') if value.strip()]
    if not ids:
        try:
            ids = [item['id'] for item in await knowledge_bases()]
        except Exception as exc:
            raise KnowledgeError('无法读取公司知识库列表。') from exc
    if not ids:
        raise KnowledgeError('当前工作空间没有可检索的知识库。')
    try:
        data = await api_json(
            'POST',
            weknora_url() + '/knowledge-search',
            headers=weknora_headers(),
            json={'query': str(query or '')[:500], 'knowledge_base_ids': ids},
        )
    except Exception as exc:
        raise KnowledgeError('公司知识库检索失败。') from exc
    sources = []
    for index, item in enumerate((data.get('data') or [])[:max(1, min(20, limit))]):
        if not isinstance(item, dict):
            continue
        text = _content(item)
        if not text:
            continue
        title = plain(
            item.get('knowledge_title')
            or item.get('knowledge_filename')
            or item.get('document_name')
            or f'公司知识库资料 {index + 1}'
        )
        sources.append({
            'title': title,
            'url': '',
            'text': text,
            'provider': 'weknora',
            'knowledgeBaseId': str(item.get('knowledge_base_id') or item.get('knowledge_base') or ''),
            'documentId': str(item.get('knowledge_id') or item.get('document_id') or item.get('id') or ''),
            'chunkId': str(item.get('chunk_id') or item.get('id') or ''),
            'page': item.get('page') or item.get('page_number'),
        })
    warnings = []
    if not sources:
        warnings.append('公司知识库未返回可读正文。')
    return {'status': 'available' if sources else 'empty', 'sources': sources, 'warnings': warnings}


def format_citation(source: dict) -> str:
    title = str(source.get('title') or '公司知识库资料')
    page = source.get('page')
    return f'{title}（第 {page} 页）' if page else title
