"""Search adapters and a local, validated image cache. Provider secrets stay server-side."""
import asyncio
from dataclasses import dataclass
import hashlib
import html
from io import BytesIO
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from lxml import html as html_tree
from PIL import Image, ImageOps

from .models import SlideImage

MAX_BYTES = 8 * 1024 * 1024
SEARCH_LIMIT = asyncio.Semaphore(3)
USER_AGENT = "PPTStudio/1.0 (presentation image search)"


class ImageSearchError(ValueError):
    pass


@dataclass
class Candidate:
    url: str
    title: str
    provider: str
    source_url: str = ""
    author: str = ""
    license: str = "来源未标注许可"
    fallback_url: str = ""
    private: bool = False


def plain(value):
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()[:500]


def origin(url):
    p = urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


def web_url(url):
    try:
        p = urlsplit(url)
        return p.scheme in ("http", "https") and bool(p.hostname) and not p.username and not p.password
    except ValueError:
        return False


def weknora_url():
    base = os.getenv("WEKNORA_BASE_URL", "").rstrip("/")
    return base if base.endswith("/api/v1") else base + "/api/v1"


def weknora_headers():
    headers = {"X-API-Key": os.getenv("WEKNORA_API_KEY", "")}
    if os.getenv("WEKNORA_TENANT_ID"):
        headers["X-Tenant-ID"] = os.environ["WEKNORA_TENANT_ID"]
    return headers


def configured_providers():
    available = {
        "mcp": bool(os.getenv("PPT_WEB_MCP_URL")),
        "tavily": bool(os.getenv("TAVILY_API_KEY")),
        "serper": bool(os.getenv("SERPER_API_KEY")),
        "searxng": bool(os.getenv("SEARXNG_BASE_URL")),
        "bing": True, "commons": True,
    }
    order = os.getenv("PPT_IMAGE_WEB_PROVIDERS", "mcp,tavily,serper,searxng,bing,commons").split(",")
    return [p.strip() for p in order if available.get(p.strip())]


def capabilities():
    return {"weknoraConfigured": bool(os.getenv("WEKNORA_BASE_URL") and os.getenv("WEKNORA_API_KEY")),
            "webProviders": configured_providers(), "mcpConfigured": bool(os.getenv("PPT_WEB_MCP_URL"))}


async def public_url(url):
    if not web_url(url):
        raise ImageSearchError("图片链接格式不支持。")
    parsed = urlsplit(url)
    allowed = {h.strip() for h in os.getenv("PPT_IMAGE_ALLOWED_HOSTS", "").split(",") if h.strip()}
    if parsed.hostname in allowed:
        return
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ImageSearchError("图片链接指向未配置的内网地址。")


async def fetch_bytes(url, *, private=False, max_bytes=MAX_BYTES):
    # Credentials only go to the configured WeKnora files proxy, never to search results/CDNs.
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), trust_env=False) as client:
        for _ in range(4):
            local_proxy = private and origin(url) == origin(weknora_url()) and urlsplit(url).path == urlsplit(weknora_url()).path + "/files"
            if not local_proxy:
                await public_url(url)
            headers = {"User-Agent": USER_AGENT}
            if local_proxy:
                headers.update(weknora_headers())
            async with client.stream("GET", url, headers=headers) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                data = bytearray()
                async for block in response.aiter_bytes():
                    data.extend(block)
                    if len(data) > max_bytes:
                        raise ImageSearchError("图片文件过大，已跳过。")
                return bytes(data)
    raise ImageSearchError("图片链接重定向过多。")


async def api_json(method, url, **kwargs):
    async with httpx.AsyncClient(timeout=httpx.Timeout(18, connect=5), trust_env=False, follow_redirects=False) as client:
        response = await client.request(method, url, **kwargs)
        if response.status_code == 409 and 'TENANT_REQUIRED' in response.text:
            raise ImageSearchError("WeKnora 需要工作空间 ID，请配置 WEKNORA_TENANT_ID。")
        if response.status_code in (401, 403):
            raise ImageSearchError("检索服务鉴权失败，请检查服务端密钥与访问权限。")
        response.raise_for_status()
        if len(response.content) > 4 * 1024 * 1024:
            raise ImageSearchError("检索结果过大。")
        return response.json()


async def knowledge_bases():
    data = await api_json("GET", weknora_url() + "/knowledge-bases", headers=weknora_headers())
    rows = data.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("knowledge_bases") or []
    return [{"id": str(row["id"]), "name": str(row.get("name", "知识库"))} for row in rows if row.get("id")]


async def knowledge_search(query):
    ids = [v.strip() for v in os.getenv("WEKNORA_KNOWLEDGE_BASE_IDS", "").split(",") if v.strip()]
    if not ids:
        ids = [item["id"] for item in await knowledge_bases()]
    if not ids:
        raise ImageSearchError("WeKnora 当前工作空间没有可检索的知识库。")
    data = await api_json("POST", weknora_url() + "/knowledge-search", headers=weknora_headers(),
                          json={"query": query, "knowledge_base_ids": ids})
    candidates = []
    for item in (data.get("data") or [])[:15]:
        title = item.get("knowledge_title") or item.get("knowledge_filename") or "知识库配图"
        info = item.get("image_info") or []
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except ValueError:
                info = []
        if isinstance(info, dict):
            info = [info]
        for entry in info if isinstance(info, list) else []:
            if isinstance(entry, dict):
                ref = entry.get("url") or entry.get("original_url")
                if ref:
                    candidates.append(knowledge_candidate(ref, title + (" · " + entry["caption"] if entry.get("caption") else "")))
        for caption, ref in re.findall(r"!\[([^\]]*)\]\(([^\s)]+)(?:\s+[^)]*)?\)", item.get("content") or ""):
            candidates.append(knowledge_candidate(ref, title + (" · " + caption if caption else "")))
    return candidates


def knowledge_candidate(ref, title):
    # Fetch through the authenticated proxy, preserving resource:// handles and local storage paths.
    if web_url(ref):
        return Candidate(ref, plain(title), "weknora", author="本地知识库", license="内部素材 · 按原文件授权使用")
    return Candidate(weknora_url() + "/files?" + urlencode({"file_path": ref}), plain(title), "weknora",
                     author="本地知识库", license="内部素材 · 按原文件授权使用", private=True)


async def bing_search(query):
    data = await fetch_bytes("https://www.bing.com/images/search?" + urlencode({"q": query + " -site:vecteezy.com -site:freepik.com -site:gettyimages.com -site:shutterstock.com", "first": 1}), max_bytes=3 * 1024 * 1024)
    tree = html_tree.fromstring(data)
    results = []
    for node in tree.xpath('//a[contains(concat(" ", normalize-space(@class), " "), " iusc ")][@m]')[:60]:
        try:
            row = json.loads(node.get("m"))
            if any(site in (row.get("purl", "") + row.get("murl", "")).lower() for site in ("gettyimages", "istockphoto", "shutterstock", "alamy.com", "stock.adobe", "vecteezy", "freepik", "dreamstime", "123rf", "depositphotos", "pngtree", "pond5")):
                continue
            if web_url(row.get("murl", "")):
                title = plain(row.get("t") or node.get("aria-label") or "")
                score = relevance_score(query, title + " " + row.get("purl", "") + " " + row["murl"])
                if score > 0:
                    results.append((score, Candidate(row["murl"], title or "检索配图", "bing",
                                                     row.get("purl", ""), fallback_url=row.get("turl", ""))))
        except (ValueError, TypeError):
            continue
    return [candidate for _, candidate in sorted(results, key=lambda item: item[0], reverse=True)]


def relevance_score(query, description):
    """Reject obvious unrelated public-image results; this is not visual verification."""
    stopwords = {"the", "and", "with", "for", "from", "view", "photo", "image", "aerial", "photography", "high", "quality", "beautiful", "close", "wide", "angle"}
    aliases = {"solar": ("太阳能", "光伏", "photovoltaic"), "photovoltaic": ("太阳能", "光伏", "solar"),
               "rooftop": ("屋顶", "屋面"), "wind": ("风机", "风电", "风力"), "turbines": ("风机", "风电"),
               "industrial": ("工业", "产业"), "park": ("园区", "公园"), "architecture": ("架构", "建筑")}
    words = set(re.findall(r"[a-z]{3,}", query.lower())) - stopwords
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    words.update(chunk[i:i+2] for chunk in chunks for i in range(len(chunk)-1))
    text = description.lower()
    hits = sum(word in text or any(alias in text for alias in aliases.get(word, ())) for word in words)
    return hits / max(1, len(words))


async def commons_search(query):
    data = await api_json("GET", "https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "format": "json", "generator": "search", "gsrsearch": query,
        "gsrnamespace": 6, "gsrlimit": 8, "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 1400}, headers={"User-Agent": USER_AGENT})
    results = []
    for row in (data.get("query", {}).get("pages", {})).values():
        for info in row.get("imageinfo", []):
            meta = info.get("extmetadata", {})
            results.append(Candidate(info.get("thumburl") or info["url"], plain(row.get("title", "").removeprefix("File:")), "commons",
                                      info.get("descriptionurl", ""), plain(meta.get("Artist", {}).get("value")),
                                      plain(meta.get("LicenseShortName", {}).get("value")) or "来源未标注许可"))
    return results


def normalize_images(data, provider):
    results = []
    if isinstance(data, dict):
        rows = data.get("images") or data.get("results") or []
    else:
        rows = data if isinstance(data, list) else []
    for row in rows[:20]:
        if isinstance(row, str):
            row = {"image_url": row}
        if not isinstance(row, dict):
            continue
        url = row.get("imageUrl") or row.get("image_url") or row.get("img_src") or row.get("url")
        if url and web_url(url):
            results.append(Candidate(url, plain(row.get("title") or row.get("description") or "检索配图"), provider,
                                      row.get("link") or row.get("source_url") or row.get("sourceUrl") or row.get("url") or url))
    return results


async def mcp_search(query):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.sse import sse_client
    from contextlib import AsyncExitStack
    headers = json.loads(os.getenv("PPT_WEB_MCP_HEADERS", "{}"))
    url = os.environ["PPT_WEB_MCP_URL"]
    async with AsyncExitStack() as stack:
        if os.getenv("PPT_WEB_MCP_TRANSPORT", "http") == "sse":
            streams = await stack.enter_async_context(sse_client(url, headers=headers, timeout=10, sse_read_timeout=20))
        else:
            client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=20, trust_env=False))
            streams = await stack.enter_async_context(streamable_http_client(url, http_client=client))
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        tools = (await session.list_tools()).tools
        configured = os.getenv("PPT_WEB_MCP_TOOL")
        candidates = [tool for tool in tools if tool.name == configured] if configured else [tool for tool in tools if re.search(r"search", tool.name, re.I)]
        candidates.sort(key=lambda tool: 0 if re.search(r"image", tool.name, re.I) else 1 if re.search(r"web", tool.name, re.I) else 2)
        if not candidates:
            raise ImageSearchError("MCP 未提供搜索工具，请设置 PPT_WEB_MCP_TOOL。")
        tool = candidates[0]
        props = tool.inputSchema.get("properties", {})
        args = json.loads(os.getenv("PPT_WEB_MCP_ARGS", "{}"))
        query_key = os.getenv("PPT_WEB_MCP_QUERY_KEY") or next((k for k in ("query", "q", "search_query", "query_text") if k in props), None)
        if not query_key:
            raise ImageSearchError("MCP 搜索参数不兼容，请设置 PPT_WEB_MCP_QUERY_KEY。")
        args[query_key] = query
        for key in ("include_images", "include_image_descriptions"):
            if key in props:
                args[key] = True
        for key in ("count", "max_results"):
            if key in props and key not in args:
                args[key] = 8
        if any(key not in args for key in tool.inputSchema.get("required", [])):
            raise ImageSearchError("MCP 搜索缺少参数，请补充 PPT_WEB_MCP_ARGS。")
        result = await session.call_tool(tool.name, args)
        if result.isError:
            raise ImageSearchError("MCP 搜索调用失败。")
        data = result.structuredContent
        texts = "\n".join(getattr(block, "text", "") for block in result.content)
        if not data:
            try:
                data = json.loads(texts)
            except ValueError:
                data = {"images": [{"url": u, "title": t} for t, u in re.findall(r"!\[([^\]]*)\]\((https?://[^\s)]+)\)", texts)]}
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            data = data["result"]
        if isinstance(data, dict) and not data.get("images"):
            direct = [row for row in data.get("results", []) if isinstance(row, dict) and (any(row.get(k) for k in ("image_url", "imageUrl", "img_src")) or re.search(r"\.(png|jpe?g|webp)(?:[?#]|$)", row.get("url", ""), re.I))]
            rows = normalize_images(direct, "mcp")
        else:
            rows = normalize_images(data, "mcp")
        # Generic web-search MCPs return webpages. Resolve only explicit og:image metadata.
        pages = data.get("organic") or data.get("results") or [] if isinstance(data, dict) else []
        if not (isinstance(data, dict) and data.get("images")):
            for row in pages[:3]:
                if not isinstance(row, dict):
                    continue
                page = row.get("link") or row.get("url")
                if not page or not web_url(page):
                    continue
                try:
                    tree = html_tree.fromstring(await fetch_bytes(page, max_bytes=2 * 1024 * 1024))
                    urls = tree.xpath('//meta[@property="og:image"]/@content')
                    if urls:
                        rows.append(Candidate(urljoin(page, urls[0]), plain(row.get("title") or query), "mcp", page))
                except Exception:
                    continue
        return rows


async def provider_search(provider, query):
    if provider == "weknora":
        return await knowledge_search(query)
    if provider == "mcp":
        return await mcp_search(query)
    if provider == "bing":
        return await bing_search(query)
    if provider == "commons":
        return await commons_search(query)
    if provider == "tavily":
        data = await api_json("POST", "https://api.tavily.com/search", headers={"Authorization": "Bearer " + os.environ["TAVILY_API_KEY"]},
                              json={"query": query, "include_images": True, "include_image_descriptions": True, "max_results": 8})
    elif provider == "serper":
        data = await api_json("POST", "https://google.serper.dev/images", headers={"X-API-KEY": os.environ["SERPER_API_KEY"]}, json={"q": query, "num": 8})
    elif provider == "searxng":
        data = await api_json("GET", os.environ["SEARXNG_BASE_URL"].rstrip("/") + "/search", params={"q": query, "categories": "images", "format": "json", "safesearch": 1})
    else:
        return []
    return normalize_images(data, provider)


def asset_path(root, asset_id):
    if not re.fullmatch(r"[a-f0-9]{64}", asset_id):
        raise ImageSearchError("图片 ID 无效。")
    return root / "images" / (asset_id + ".jpg")


def normalize_bitmap(data):
    with Image.open(BytesIO(data)) as image:
        if image.width * image.height > 20_000_000 or min(image.size) < 120 or max(image.size) < 320:
            raise ImageSearchError("图片分辨率不适合演示，已跳过。")
        image = ImageOps.exif_transpose(image)
        image.thumbnail((2000, 2000))
        if image.mode in ("RGBA", "P"):
            rgba = image.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            image = rgb
        else:
            image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue(), image.width, image.height


async def cache_candidate(root, candidate):
    urls = [candidate.url] + ([candidate.fallback_url] if candidate.fallback_url else [])
    for url in urls:
        try:
            data = await fetch_bytes(url, private=candidate.private)
            normalized, width, height = await asyncio.to_thread(normalize_bitmap, data)
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            if url == urls[-1]:
                raise ImageSearchError("部分检索图片无法下载或格式不适用，已跳过。") from None
    identifier = hashlib.sha256(normalized).hexdigest()
    path = asset_path(root, identifier)
    path.parent.mkdir(exist_ok=True)
    await asyncio.to_thread(path.write_bytes, normalized)
    source = candidate.source_url if web_url(candidate.source_url) else ""
    metadata = SlideImage(assetId=identifier, url=f"/api/studio/images/{identifier}", title=plain(candidate.title),
                          provider=candidate.provider, sourceUrl=source, author=plain(candidate.author), license=plain(candidate.license), width=width, height=height)
    path.with_suffix(".json").write_text(metadata.model_dump_json(), encoding="utf-8")
    return metadata


async def search_images(root, query, source="auto", limit=4, knowledge_query=None):
    providers = []
    configured = capabilities()
    if source in ("auto", "knowledge"):
        if configured["weknoraConfigured"]:
            providers.append("weknora")
        elif source == "knowledge":
            return [], ["尚未配置 WeKnora。"]
    if source in ("auto", "web"):
        providers.extend(configured_providers())
    warnings, results, seen = [], [], set()
    async with SEARCH_LIMIT:
        for provider in providers:
            try:
                candidates = await asyncio.wait_for(provider_search(provider, (knowledge_query or query) if provider == "weknora" else query), timeout=25)
                if not candidates:
                    warnings.append(f"{provider} 未找到匹配图片。")
                    continue
                for candidate in candidates[:8]:
                    if candidate.url in seen:
                        continue
                    seen.add(candidate.url)
                    try:
                        image = await asyncio.wait_for(cache_candidate(root, candidate), timeout=18)
                        if image.assetId not in {item.assetId for item in results}:
                            results.append(image)
                    except (ImageSearchError, asyncio.TimeoutError):
                        continue
                    if len(results) >= limit:
                        return results, list(dict.fromkeys(warnings))
                if results:
                    return results, list(dict.fromkeys(warnings))
                warnings.append(f"{provider} 返回的图片暂时无法使用。")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                warnings.append(str(exc) if isinstance(exc, ImageSearchError) else f"{provider} 检索暂不可用，已尝试其他来源。")
    return results, list(dict.fromkeys(warnings))


async def illustrate_project(root, project, progress):
    project = project.model_copy(deep=True)
    if project.imageSource == "none":
        return project, {}, []
    warnings = []
    images = {}
    async def illustrate(index, slide):
        if slide.imageDisabled:
            return
        if slide.image:
            path = asset_path(root, slide.image.assetId)
            if path.is_file() and path.with_suffix(".json").is_file():
                # Trust cached provenance, not arbitrary client-supplied URLs or captions.
                slide.image = SlideImage.model_validate_json(path.with_suffix(".json").read_text())
                images[slide.id] = path
                return
        if slide.imageIntent == "none":
            return
        progress(1, f"正在为第 {index + 1}/{len(project.slides)} 页检索配图…")
        query = slide.imageQuery or slide.title
        try:
            found, notes = await asyncio.wait_for(search_images(root, query, project.imageSource, limit=1, knowledge_query=slide.title + " " + query), timeout=70)
        except asyncio.TimeoutError:
            found, notes = [], ["配图检索超时。"]
        warnings.extend(notes)
        if found:
            slide.image = found[0]
            images[slide.id] = asset_path(root, found[0].assetId)
        else:
            slide.image = None
            warnings.append(f"第 {index + 1} 页未找到可用配图，保留文字版式。")
    workers = asyncio.Semaphore(3)
    async def worker(index, slide):
        async with workers:
            await illustrate(index, slide)
    try:
        await asyncio.wait_for(asyncio.gather(*(worker(i, s) for i, s in enumerate(project.slides))), timeout=180)
    except asyncio.TimeoutError:
        warnings.append("部分页面配图检索超时，未完成的页面使用文字版式。")
    return project, images, list(dict.fromkeys(warnings))
