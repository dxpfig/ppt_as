# PPT 内容配图

已接入无模板演示的自动配图：Agent 为每页生成具体主体和场景的 `imageQuery`，生成 PPT 时检索、校验并缓存图片，使用图文分栏排版。图片嵌入 PPTX，实际 PDF 与 PNG 预览使用相同图片，原网站之后失效不会影响已生成文件。

## 页面操作

右侧「内容配图」可选自动（知识库优先、网络补充）、仅知识库、仅网络或不配图。编辑单页时可调整图片关键词、搜索候选图、选择替换、移除，或指定本页不配图；点击「应用修改并更新预览」写入 PPT。关键词、来源、选图及逐页禁用状态跟随工作记录保存。单页删除图片后不会在下次生成时自动补回，选择新图可重新启用。

公司模板只填充明确的图片占位符，不覆盖模板已有文字、标志或插图。没有图片占位符时，素材仍保留在编辑区，生成检查中提示未插入。

## WeKnora

服务端 `.env` 中配置以下项，然后 `./start.sh --restart`：

```dotenv
WEKNORA_BASE_URL=http://192.168.0.10
WEKNORA_API_KEY=your-key
WEKNORA_TENANT_ID=your-workspace-id
# 可选，逗号分隔；留空检索当前工作空间下可访问的知识库。
WEKNORA_KNOWLEDGE_BASE_IDS=
```

使用 `X-API-Key`，配置工作空间 ID 时附加 `X-Tenant-ID`。平台类型的密钥必须提供工作空间 ID。调用知识搜索接口读取分块的 `image_info` 与 Markdown 图片引用；`resource://` 和本地存储路径通过鉴权的 `/api/v1/files` 获取，网络直链按公开图片方式读取。密钥只发送至配置的 WeKnora 文件接口，不会转发给重定向目标或图片 CDN。

知识库需已解析出图片或图片描述；纯文字分块无法凭空变成照片。仅知识库模式不会调用公网搜索。自动模式的公网检索只发送页面配图关键词，不发送知识库分块原文。

## 网络搜索方案

默认顺序：`mcp,tavily,serper,searxng,bing,commons`。未配置的提供方跳过；连接失败、无匹配或图片不可用时尝试下一个。只有找到可下载、可解码的图片才采用。

生成 Agent 还会标记每页 `imageIntent`：纯文字/结构页面不自动检索，用户手动选定的图片仍优先保留。Bing 结果根据标题及来源信息与关键词的匹配度筛选、排序，过滤明显无关的候选。这是文本元数据启发式检查，不能替代图片内容的视觉判断；用户仍可在编辑区替换或移除图片。

| 提供方            | 配置               | 说明                                                             |
| ----------------- | ------------------ | ---------------------------------------------------------------- |
| MCP               | `PPT_WEB_MCP_URL`  | 优先发现图片搜索工具，其次 Web 搜索；支持 Streamable HTTP 和 SSE |
| Tavily            | `TAVILY_API_KEY`   | 请求图片及图片描述；需服务商凭证                                 |
| Serper            | `SERPER_API_KEY`   | Google Images 搜索接口；需服务商凭证                             |
| SearXNG           | `SEARXNG_BASE_URL` | 可自行部署，需开启 JSON 返回格式及图片引擎                       |
| Bing 图片         | 无密钥             | 使用公开搜索结果；过滤部分常见商业图库预览来源，网站限制时会降级 |
| Wikimedia Commons | 无密钥             | 保留来源提供的作者和许可；当前网络需能访问 Wikimedia             |

可通过 `PPT_IMAGE_WEB_PROVIDERS` 调整顺序或禁用某个来源。MCP 配置示例：

```dotenv
PPT_WEB_MCP_URL=http://your-search-server/mcp
PPT_WEB_MCP_TRANSPORT=http
PPT_WEB_MCP_HEADERS={"Authorization":"Bearer your-mcp-key"}
# 默认自动发现；参数不标准时显式配置：
PPT_WEB_MCP_TOOL=image_search
PPT_WEB_MCP_QUERY_KEY=query
PPT_WEB_MCP_ARGS={}
```

模型的 Chat Completions / Anthropic `base_url` 本身不会公开它在其他客户端绑定的 MCP 列表，因此必须在本项目提供 MCP 地址，才能发现和调用工具。配置后优先使用，无需更换 LLM。通用网页搜索工具若没有直接返回图片，会尝试读取前几个页面的 `og:image`；无法提供可用图片时继续降级。

## 缓存和来源

图片保存在 `STUDIO_DATA_DIR/images`，以内容哈希索引；前端通过同源 `/api/studio/images/{id}` 显示。限制文件大小和像素数，拒绝 HTML、不可解码素材及未明确允许的内网图片地址。内部对象存储直链可通过 `PPT_IMAGE_ALLOWED_HOSTS` 显式允许主机；不会向其转发 WeKnora 密钥。图片按比例完整放入版面，避免裁掉知识库架构图。

来源、作者和许可信息展示在编辑区，并写入 PPT 讲稿。未标注许可的来源会保持「来源未标注许可」，不会被视为免费授权。没有合适图片时仍生成文字页，并在生成检查中说明原因。

## 验证

```bash
.venv/bin/python -m unittest agent_service.test_images agent_service.test_work_history -v
pnpm build:frontend
```

覆盖 WeKnora 图片引用与租户头、鉴权重定向隔离、实际 MCP HTTP 握手/发现/调用、Tavily/Serper/SearXNG 请求与结果适配、图片校验、PPTX 内嵌图片、单页禁用和失败降级。外部付费提供方测试使用模拟响应；只有配置真实凭证后才能确认服务商连通性。
