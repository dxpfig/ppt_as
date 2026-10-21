"""Real research evidence; provider absence is explicit, never synthetic citations."""
import asyncio
import os
import re
from lxml import html
from .images import api_json, fetch_bytes, web_url
from . import knowledge


async def research_context(request):
    sources, warnings = [], []
    references = request.references or request.project.references or ""
    if references:
        sources.append({"title": "用户提供的参考资料", "url": "", "text": references, "provider": "user"})
    if knowledge.configured():
        try:
            company = await knowledge.search(
                request.prompt or request.project.title,
                getattr(request, 'knowledgeBaseIds', None),
            )
            sources.extend(company['sources'])
            warnings.extend(company['warnings'])
        except knowledge.KnowledgeError as exc:
            warnings.append(str(exc))
    # Only explicit user links are fetched; search snippets keep their actual URL.
    links = list(dict.fromkeys(re.findall(r'https?://[^\s<>"）)\]]+', (request.prompt or '') + '\n' + references)))
    limit = asyncio.Semaphore(3)
    async def extract(url):
        try:
            async with limit:
                raw = await fetch_bytes(url, max_bytes=8 * 1024 * 1024)
            if raw.startswith(b'%PDF'):
                import pymupdf
                with pymupdf.open(stream=raw, filetype='pdf') as pdf:
                    title = pdf.metadata.get('title') or url
                    content = '\n\n'.join(page.get_text() for page in pdf)
            else:
                tree = html.fromstring(raw)
                for node in tree.xpath('//script|//style|//nav|//footer'):
                    node.drop_tree()
                title = ' '.join(tree.xpath('//title/text()'))[:300] or url
                content = re.sub(r'\s+', ' ', tree.text_content()).strip()
            if not content.strip():
                warnings.append('参考链接未取得可读文本（可能是扫描件或动态页面）：' + url)
            return {"title": title, "url": url, "text": content, "provider": "user-link"}
        except Exception:
            warnings.append("无法读取用户链接：" + url + "；不将该链接视作已核实内容。")
    extracted = await asyncio.gather(*(extract(url) for url in links))
    sources.extend(item for item in extracted if item and item["text"])
    provider = None
    if getattr(request, 'allowResearch', True):
        query = (request.prompt or request.project.title)[:500]
        try:
            if os.getenv("TAVILY_API_KEY"):
                provider = 'tavily'
                data = await api_json("POST", "https://api.tavily.com/search", headers={"Authorization": "Bearer " + os.environ["TAVILY_API_KEY"]}, json={"query": query, "max_results": 6, "include_raw_content": True})
                rows = [{"title": r.get("title"), "url": r.get("url"), "text": r.get("raw_content") or r.get("content")} for r in data.get("results", [])]
            elif os.getenv("SERPER_API_KEY"):
                provider = 'serper'
                data = await api_json("POST", "https://google.serper.dev/search", headers={"X-API-KEY": os.environ["SERPER_API_KEY"]}, json={"q": query, "num": 6})
                rows = [{"title": r.get("title"), "url": r.get("link"), "text": r.get("snippet")} for r in data.get("organic", [])]
            elif os.getenv("SEARXNG_BASE_URL"):
                provider = 'searxng'
                data = await api_json("GET", os.environ["SEARXNG_BASE_URL"].rstrip('/') + '/search', params={"q": query, "format": "json"})
                rows = [{"title": r.get("title"), "url": r.get("url"), "text": r.get("content")} for r in data.get("results", [])[:6]]
            else:
                rows = []
                warnings.append("未配置文本检索服务；仅使用用户素材和通用知识，不编造来源或最新数据。")
            sources.extend({**r, "text": str(r['text']), "provider": provider} for r in rows if r.get('text') and web_url(r.get('url', '')))
        except Exception:
            warnings.append("文本检索调用失败；仅使用已取得的资料，不编造检索结果。")
    return {"status": "available" if sources else "unavailable" if getattr(request, 'allowResearch', True) else "disabled", "sources": sources, "warnings": warnings}
