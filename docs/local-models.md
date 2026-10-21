# 模型配置

配置保存在项目根目录 `.env`，密钥仅供服务端读取。2026-10-21 的当前配置为：文本生成使用 `MiniMax-M3`，视觉审稿使用 `MiniMax-M3.1-Flash`。两者均通过 OpenAI 兼容接口接入，界面 provider 为 `openai`，实际服务以表中地址为准。

| 用途                 | 模型               | 地址                                           | 接入状态                                            |
| -------------------- | ------------------ | ---------------------------------------------- | --------------------------------------------------- |
| PPT 规划、生成、修改 | MiniMax-M3         | `https://api.minimaxi.com/v1`                  | 已启用，实际文字请求通过                            |
| AI 视觉审稿          | MiniMax-M3.1-Flash | `https://api.minimaxi.com/v1`                  | 已启用，2026-10-21 随机图片识别校验通过             |
| 向量                 | qwen3-vl-embedding | `http://192.168.0.11:8001/v1/embeddings`       | 仅保存，暂不接入业务；实测返回 2048 维向量          |
| 重排                 | qwen3-vl-reranker  | `http://192.168.0.11:8002/score`               | 仅保存，暂不接入业务；实测返回评分                  |
| OCR                  | PaddleOCR-VL-1.5   | `http://192.168.0.11:8118/v1/chat/completions` | 仅保存，暂不接入业务；2026-10-10 检查时端口拒绝连接 |

主模型读取 `PPT_LLM_PROVIDER`、`PPT_LLM_MODEL`、`OPENAI_BASE_URL`、`OPENAI_API_KEY`，`OPENAI_MODEL` 同步保存相同名称。视觉审稿读取 `PPT_VISION_PROVIDER`、`PPT_VISION_MODEL`、`PPT_VISION_BASE_URL`、`PPT_VISION_API_KEY`。视觉模型标识必须与服务端 `/models` 返回值完全一致；模型名中的空格、大小写或连字符错误会返回 400。通过的随机图片能力校验会按 provider/model/endpoint 缓存。

当前保留流式接收（`PPT_LLM_STREAM=true`），`PPT_LLM_EXTRA_BODY={}` 已清除 Qwen 专用扩展；MiniMax 的输出模式沿用现有官方接口适配。此前本地 Qwen 使用的 `chat_template_kwargs.enable_thinking=false` 只适用于已验证的 vLLM 服务，不发送给 MiniMax。

切换后文字探测 1.82 秒、随机图片识别 3.01 秒，均通过；这是连通性与图片输入测试，不是整稿生成耗时。MiniMax 模型接口未公布上下文上限，动态发现使用既有 128000 tokens 回退预算（不是服务端确认的上限），输出额度 32000、安全余量 4000。证据：`runtime/diagnostics/minimax-switch-2026-10-10/verification.json`。Qwen 及辅助服务的完整旧配置已备份至 `runtime/config-backups/env-before-minimax-m3-20261010-141949`。

随后修复了原始输入计数过度保守的问题：无 vLLM 分词接口时，自动探测同源标准 `responses/input_tokens` 能力，MiniMax 已实测支持。相同完整规划请求的 UTF-8 字节上界为 142307，服务端计数为 34388；加上 Chat 消息名称的保守字节余量，预算计入 34454，低于当前 92000 的输入限额。计数结果是服务端估算，不表述为字节数或模型窗口的精确测量。

`PPT_LLM_TIMEOUT_SECONDS=600` 同时限制 HTTP 读取和单次 PPTD 模型请求的总时长；流式返回不会无限延长请求。`PPT_LLM_MAX_TOKENS=32000` 为完整长规划提供输出空间。`PPTD_GENERATION_TIMEOUT_SECONDS=7200` 是整份文稿生成阶段（含逐页生成与修复）的上限，不是预计耗时。模型请求超时仍只执行一次显式重试，完成的页面继续保存在检查点中。

三个辅助服务分别预留 `PPT_EMBEDDING_*`、`PPT_RERANK_*`、`PPT_OCR_*`，每组包含 `URL`、`MODEL` 和 `API_KEY`。其中 `URL` 是完整接口地址。当前没有业务代码调用这些辅助配置，也没有修改 WeKnora 的模型设置。

修改配置后执行 `./start.sh --restart`，用 `./status.sh` 检查当前模型。切换前的配置保存在 `runtime/config-backups/`，仅供本机恢复，不应提交或分享。

## 动态上下文预算

`PPT_LLM_CONTEXT_MODE=auto`（默认）在每次创建生成任务时重新查询当前模型的能力，不按模型名称写死 256K。

- OpenAI 兼容服务读取 `/v1/models` 中与当前模型 ID 完全匹配的记录，识别 `max_model_len`、`context_length`、`context_window`、`max_context_length`（也支持 `limits` 内字段）；同时读取 `max_output_tokens` / `max_completion_tokens`。
- Anthropic 使用 Models Retrieve 的 `max_input_tokens` 和 `max_tokens`，使用原生 `messages.count_tokens` 计数。输入窗口内还预留输出空间，属于保守预算。
- vLLM 自动发现同源 `/tokenize`，计数包含真实消息、工具及 `chat_template_kwargs`。tokenizer 报告的更小窗口优先。已发现的分词服务不可用时明确停止并保留检查点。
- 其他 OpenAI 兼容服务在 auto 模式下，用合成短文本探测同源 `/responses/input_tokens`，无模型名称特判。支持时逐次传入完整消息、工具声明和调用记录进行服务端估算，缓存键覆盖完整原请求。文本/图片按协议转换，消息名称单独加保守字节余量，无法无损计入的字段明确报错。请求禁止重定向；已发现计数能力后发生故障或异常响应会停止生成，不降级放行。
- 服务不提供能力字段时，使用显式 `PPT_LLM_CONTEXT_TOKENS`；未配置时回退预算为 128000，事件和检查点明确标记 `fallback`。它不是服务端确认的上限，需按服务实际限制配置。无分词接口的 OpenAI 文本请求按 UTF-8 字节数保守估算；图片请求不能用文本估算替代真实计数。

`PPT_LLM_CONTEXT_TOKENS` 在 auto 下只作为人工上限，不能覆盖服务端更小的上限。`configured` 跳过能力发现；`off` 显式停用本地预算。`PPT_LLM_MAX_TOKENS` 是输出额度上限，实际值不超过服务输出限制及上下文的 1/4；安全余量不超过 `PPT_LLM_CONTEXT_MARGIN`（默认 8192）及上下文的 1/32。请求发送前检查输入、输出预留与余量，同时在 AgentScope 自动压缩阈值之前拒绝超限请求，保留完整规则。规则本身无法放入的小窗口模型会明确失败，不能靠裁掉规则适配。

页面生成携带完整规划和最近两页完整 JSON，其余已完成页面按目录引用读取。超过 24000 字符的参考资料每 12000 字符无损分块；默认由宿主按段落覆盖度选择逐字原文摘录，避免让模型在 JSON 字符串中重写长中文引文而产生未转义引号、换行或截断。原文和每块完成状态保存到检查点。必要时模型可以通过受限 `contextRequest` 读取原文（每次最多 12000 字符，每个请求最多 8 次），不能访问任意文件。格式/超时重试使用新 Agent 状态并重新发送完整要求，避免坏响应累积。页面默认并发 2 个，并对 429/5xx 做最多 6 次、约 31 秒总退避的重试；并发可通过 `PPTD_PAGE_CONCURRENCY` 调整，重试次数通过 `PPT_LLM_TRANSIENT_RETRIES` 调整。原始 Kimi 规则和全部验收门禁保持完整。旧模型摘录模式可通过 `PPT_MATERIAL_LLM_EXCERPTS=1` 显式启用，并在模型输出格式失败时自动回退到本地无损摘录。

Studio 的参考资料预算也使用当前模型动态发现结果，但资料文件大小与模型上下文分开计算：`.docx/.txt/.md/.csv/.json` 单文件默认最高 50 MB，浏览器解析出的正文再按 `min(PPT_REFERENCE_MAX_CHARACTERS, 可用输入 tokens × PPT_REFERENCE_CHARS_PER_TOKEN)` 限制。默认系数为 2，累计文本硬顶为 200 万字符；页面会显示模型窗口、可用文本上限和已用字数。模型上下文扣除输出预留、安全余量及 AgentScope 自动压缩阈值后，才计算可用输入 tokens。长资料仍由上述 12000 字符分块和原文引用机制处理，因此这里限制的是单个工作记录可保留和排队的资料规模，不是把整份资料一次性塞入单次请求。修改模型后刷新页面或重新进入工作台即可读取新的预算。

2026-10-10 切换前检查：本地 Qwen 服务公布 `max_model_len=262144`，不是应用固定配置。vLLM `enable_prefix_caching=True`，无需重启模型服务；保留稳定系统提示前缀以复用缓存。token 数下降不等于同等幅度的端到端耗时下降。以下 Qwen 验证记录为此前结果。

本轮验证：179 项回归测试通过；真实服务支持动态发现和原文读取。对失败检查点的第 14 页请求，17 个完整历史页改为最近两页及目录后，输入由 90704 降至 49630 tokens（约 45.28%）；这是输入体积实测，不是整稿耗时承诺。已有 17 页通过原有恢复接口继续生成。

## JSON 输出约束

检测到支持结构化输出的 vLLM 服务后，PPTD 生成、修复等请求使用 OpenAI 兼容的 `response_format=json_schema` 约束返回对象；约束来自各阶段的真实 Pydantic schema。存在可读原文时，允许返回受限 `contextRequest` 对象。每个请求使用独立上下文保存 schema，避免并行页面、规划和修复串用返回格式。其他未确认支持的接口继续使用提示词和严格校验，不盲目发送扩展参数。JSON 约束不能替代内容、结构、渲染及 AI 视觉验收。

2026-10-10 修复记录：5 页任务已完成内容生成，在导出发现缺失字体及未支持字距后进入修复。相同第 4 页输入可复现模型返回说明文字、没有 JSON 的问题；当前 vLLM 已通过实际 JSON Schema 能力探测。原始失败时监控端口 3001 不可达，定位依据是任务事件、导出问题和独立复现，不是完整历史响应。

同一故障还暴露出导出器缺少字距映射：现已将 PPTD `letterSpacing` 写入 DrawingML 字符间距（`spc`，单位为 1/100 点），覆盖主题继承、直接覆盖、负间距和零值。保持原始设计，不通过丢弃属性绕过验收；其他尚未支持的样式仍然会阻止发布。第 4 页独立复测已通过 JSON、结构、字体与原生导出检查。

## 本地 Qwen 检查（2026-10-21）

本地 Qwen profile 位于 `config/profiles/qwen.env.example`，当前服务参数为：

- 模型：`Qwen3.8-27B`
- 地址：`http://192.168.0.11:8000/v1`
- 推理服务：vLLM 0.27
- 上下文：262144 tokens
- tokenizer：`/tokenize` 可用
- 结构化输出：JSON Schema 可用
- 图片输入：可用

使用以下命令检查模型列表、tokenizer、结构化输出和可选图片输入：

```bash
pnpm check:qwen -- --env .env
pnpm check:qwen -- --env .env --image runtime/sample-slide.png
```

2026-10-21 使用历史本地配置实测通过，项目自身动态发现结果为：

- provider：`openai`
- contextTokens：262144
- inputTokens：221952
- outputTokens：32000
- counting：`server-tokenizer`
- structuredOutput：`json-schema`

当前 `.env` 仍使用 MiniMax；Qwen profile 尚未自动覆盖运行配置。切换前应备份 `.env`，并使用固定样例完成快速模式、专家模式、页面操作和导出回归。
