# 后端与 Agent 独立验证记录

## 2026-09-30：PPT 设计与独立提示词库

- 学习用户提供的 Kimi slides skill，将场景分类、逐页目标、内容密度、实质图片和渲染校验应用于现有 AgentScope/PPTX 流程。未运行 Kimi 专用 CLI，未实现 PPTD。
- 提示词集中在 `prompts/ppt/`，每任务快照、下次任务热加载；版本和场景随工作记录保存并写入生成 trace。
- 设计、配图、工作记录、tracing 共 27 个不同回归用例通过：初轮 26 项见 `runtime/test-results/design-final-tests.log`；最终排版/相关性修正后重跑设计与配图 17 项见 `design-final-tests-2.log`。前端 ESLint/Prettier 与最终生产构建通过（`design-build-final.log`，保留已有大 chunk 提示）。
- 使用真实 MiniMax-M3 生成 6 页，策划包含逐页目标、场景与版式：任务 `49b7e08fa6ab464f8973ea48867193af`，trace `8d15e89a89183e7b087b4c165b78e28e`，提示词版本 `7f1819f5fc0304cc`。
- 浏览器验证：更改版式和内容来源、刷新后仍保留；单页移除图片后重新导出未自动补回。最终实际生成稿 artifact `a0bde928b99b4afc8a7c61ac413dc5f8`，仅第 3 页无图后回退版式提示，无文字缺失或页面边界警告。真实内容和最终文件备份于 `runtime/test-results/design-live-final/`。
- 六种原生版式另用人工整理样张验证；最终 PPTX、PDF 和 PNG 位于 `runtime/test-results/design-layouts/`。排版警告与 PDF 审计均为空，人工检查统一字号、无连线阴影、图片比例、流程和对比布局。拼图 `docs/ppt-design-preview.jpg`，编辑区截图 `docs/studio-design-editor.png`。
- Bing 初次返回了一张无关人物照片，已从临时验收稿移除，并增加候选标题/来源与关键词匹配过滤及回归用例。此筛选只基于文本元数据，不能宣称已具备图片视觉语义验证。
- 临时验收工作记录 `design-live-verification-20260930` 验收后软删除；用户两条已有工作记录和公司模板保留。详见 [设计与提示词维护](ppt-design.md)。

## 2026-09-30：AgentScope Studio tracing

- 原因：AgentScope 2.0.9 的 Agent 未挂载 `TracingMiddleware`，服务也没有初始化 OTLP exporter。Studio 前端 `5175/tracing` 的实际后端是 `3001`，已配置 `AGENTSCOPE_STUDIO_URL` 并重启服务。
- 新增 `telemetry.py`，使用官方中间件和 OTLP HTTP exporter，按任务创建独立根 trace，工作记录 ID 作为 conversation ID，任务接口返回 traceId。异步生成不再继承已结束的 HTTP 请求 span。
- 4 项 tracing 测试通过：并发调用父子层级与会话隔离、输入输出与 token 采集、模型错误、取消、后台任务独立于 HTTP 请求。6 项工作记录回归测试通过。
- 真实 MiniMax-M3 策划、写作和编辑调用均已由 Studio 接收。最终编辑验收 trace：`1f8f0c46ced5345a3cc9985a8f14e2b9`，包含 `PPT revise → invoke_agent PPTEditor → chat MiniMax-M3` 和结构化输出工具节点，模型耗时 10.02 秒、1,730 tokens、状态 OK。
- 浏览器在用户提供的监控页打开了该调用详情，输入输出可见；截图 `docs/studio-tracing.png`。真实返回数据保存在 `runtime/test-results/tracing-live-revise-result.json`。临时 PPT 验收工作记录已软删除，监控验收 trace 保留供查看。

配置及操作边界见 [监控说明](tracing.md)。

## 2026-09-30：内容配图

- 项目虚拟环境中执行配图和工作记录测试：16 项通过；补充 Bing 前排图库过滤回归测试，单独执行通过。覆盖真实本地 MCP HTTP 握手、图片下载校验、鉴权隔离、检索降级、PPTX 嵌入与删除后不自动补回。
- 前端生产构建、Studio ESLint 与 TypeScript 检查通过。
- 当前配置的 MiniMax-M3 真实生成了配图关键词；Bing 真实网络检索、图片缓存及两页 PPTX/PDF/PNG 生成通过。原始验证记录保存在 `runtime/test-results/image-integration/`。
- 浏览器实测：搜索候选图片、替换单页配图、更新实际 PPT 预览成功；移除第二页图片后刷新，仍显示「本页已关闭配图」，第一页面的选图不受影响。
- 最终两页产物 `runtime/studio/artifacts/afb46f09a315454b8c75a960149d11ec/`：每页均含一张内嵌图片，PDF 已生成，无渲染告警；来源分别为 Acciona 与 CGTN。页面截图为 `docs/studio-image-preview.png`。临时验证工作记录已移入可恢复删除区，保留用户既有演示与公司模板。
- Tavily、Serper、SearXNG 使用模拟服务响应验证适配；未配置真实凭证，未声称已验证其线上服务。MCP 已用本地测试服务验证，尚未配置用户的搜索 MCP 地址。
- 用户 WeKnora 地址与密钥已写入服务端环境配置。实际接口返回 `409 TENANT_REQUIRED`，尚需 `WEKNORA_TENANT_ID`，因此本地知识库的真实图片检索未完成验证。

使用方式与全部配置项见 [配图说明](illustrations.md)。

验证日期：2026-09-30。所有 Python 检查均使用项目内 `.venv/bin/python`。

## 已确认

- `pnpm --filter backend test`：5 个测试全部通过；覆盖 multipart 原始字节、二进制下载、状态码/错误透传、SSE 首事件立即送达、客户端断开时取消上游连接、不可用上游返回 502、非法上游配置拒绝。
- BFF 代码审查：移除标准及 `Connection` 指定的逐跳头；固定上游 origin；删除不可信转发头；请求和响应采用流式传输，未缓存整个上传或下载。
- 项目虚拟环境安装 `agentscope==2.0.9`。通过 `inspect.signature` 实际确认 `Agent(..., react_config=..., context_config=...)`、`Agent.reply(..., structured_schema=...)`、模型 `credential` / `parameters` / `client_kwargs` API 均存在。
- 真实模型检查：使用服务环境中的 Anthropic 兼容模型 `MiniMax-M3[1M]`，实际调用策划与写作 Agent 生成两页信息安全演示，再调用编辑 Agent 修改指定页面。断言通过：两页输出、原页面 ID 保留、未选中页面完全不变、新增“密码管理器”建议存在。没有替代模型输出或模拟成功。
- 使用局域网地址 `http://192.168.0.10:5174` 完整经过 Vite → BFF → Python：健康检查、保存/读取项目、非法模板返回 400、页数超限返回 422、非法资源 ID 返回 400、已生成 PPTX/PDF/PNG 文件字节及附件下载响应头均通过。
- 独立执行 `pnpm --filter frontend build` 成功；工作台 `index.tsx`、`model.ts`、`api.ts`、`useStudio.ts` 的 ESLint 检查成功。构建仍有已有依赖 `mime-types` 的 Node `path` 浏览器兼容告警和大型分包告警，未导致构建失败。
- Python 实现者执行 `agent_service/test_service.py` 的 5 个集成验收测试通过，覆盖服务器项目保存/读取、普通演示真实渲染、PPTX/POTX 公司模板、错误和输入限制、任务取消。模板测试核对背景颜色、标题字体、主题 XML 及品牌图片字节；独立验证者另外通过完整 HTTP 代理链下载并检查了这些真实产物。

## API 来源

实际安装的包源码，而非猜测 API：

- `.venv/lib/python3.12/site-packages/agentscope/agent/_agent.py`：`Agent.__init__`、`Agent.reply`。
- `.venv/lib/python3.12/site-packages/agentscope/agent/_config.py`：`ReActConfig`、`ContextConfig`。
- `.venv/lib/python3.12/site-packages/agentscope/message/_base.py`：消息正文为内容块列表，`structured_output` 为结构化结果。
- `.venv/lib/python3.12/site-packages/agentscope/model/_anthropic/_model.py` 与 `_openai_chat/_model.py`：实际模型构造与客户端实现。

## 审查发现及处理

- 首次真实模型检查在发送网络请求前发现 `Msg.content` 不能传字符串。已修复为文本内容块列表；随后真实模型调用通过。
- 前端共享项目加载与自动保存竞态、项目标题修改未使旧产物失效已修复。加载完成前暂停自动保存；显式共享项目及根页面都读取服务器版本；不同的本机草稿可恢复。
- 后续大纲对话原先因 `phase` 判断重新生成全文；现改用成功生成标记，首次取消/失败后重试仍为大纲生成。版本保存并恢复项目标题，避免旧产物与新标题配对。
- 模板解析异常现在统一返回 400 并清理失败上传；损坏 XML、实体声明和除超链接之外的外部资源关系会被拒绝。
- 后端说明中的 SSE 完成态过度表述已更正：当前工作台每秒轮询进度，BFF 仅预留流式事件代理能力。

## 验证边界

- 已验证当前 Anthropic 兼容模型；OpenAI 兼容配置的构造参数已对照实际包 API，但未另行调用该提供方。
- 模板验证使用包含标题/正文占位符、品牌图片、背景和字体的真实测试文件，不能证明所有复杂公司模板都能自动适配。图表/表格保留示例内容，任意拖拽排版、动画和切换效果不是当前实现范围。
- 独立验证者未进行并发多人同时编辑同一个项目的冲突测试；当前没有多人协作版本合并机制。
- 页面上的完整点击操作、模板预览尺寸与播放的视觉验收由主任务继续执行；本记录不将 HTTP 检查称作浏览器验收。

审查过程中发现的阻塞问题已修复，并以真实调用或最终构建复核。

## 2026-09-30：空白首页与工作记录

- 前端生产构建、Studio 相关文件 ESLint/Prettier 检查通过。
- `.venv/bin/python -m unittest agent_service.test_work_history -v`：6 项通过，使用独立临时目录与模拟生成调用。覆盖完整记录恢复、继续编辑、删除与撤销、延迟保存防复活、活动任务取消、浏览器离线时结果持久化、失败后可继续编辑、生成版本持久化。
- 浏览器实测：打开 A，修改标题与讲稿，立即切换 B，再切回 A，内容保留；两份记录的对话、配色、页数与使用场景正确隔离，未发送输入不串入另一份记录。
- 浏览器实测：删除其他记录、撤销删除、删除当前记录后回到空白页均成功。两条临时 UI 测试记录均已删除，刷新后项目与模板列表为空。
- 本次未重新调用用户的本地模型；验证重点是记录保存、恢复和页面交互。
