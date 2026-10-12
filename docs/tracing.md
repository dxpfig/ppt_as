# AgentScope Studio 调用监控

PPT 助手使用 AgentScope 2.0.9。该版本通过 `TracingMiddleware` 采集 Agent 回复、模型调用和工具调用，另需初始化 OpenTelemetry Provider 与 OTLP Exporter；仅创建 Agent 不会上报监控数据，也不使用旧版 `agentscope.init(studio_url=...)`。

项目 `.env` 配置：

```dotenv
AGENTSCOPE_STUDIO_URL=http://192.168.0.10:3001
```

运行 `./start.sh --restart` 生效。当前 Studio 的页面是 `http://192.168.0.10:5175/tracing`，HTTP 数据接收地址是 `http://192.168.0.10:3001/v1/traces`。两者端口不同；不要把前端页面地址填写为上报地址。删除或留空此配置并重启可关闭本项目的上报。

## 查看记录

在 Traces 中搜索 `PPT outline`（策划和写作）或 `PPT revise`（编辑），打开后查看下级节点：

- 新 PPTD 流程的 `invoke_agent PPTDPlan`、`PPTDPage`、`PPTDRepair`、`PPTDEdit` 等阶段；旧项目兼容路径保留 `PPTPlanner`、`PPTWriter`、`PPTEditor`。
- `chat <模型名>`：模型输入输出、token 用量、耗时和错误状态；token 字段取决于模型服务返回的 usage。

每次生成任务拥有独立 trace；`gen_ai.conversation.id` 对应 PPT 工作记录 ID，`ppt.job.id` 对应任务 ID。任务接口也返回 `traceId`，可用于精确定位。继续编辑同一份 PPT 会产生新的 trace，但保持相同会话 ID。

上报包含提示词与生成内容，便于在用户指定的 Studio 内查看对话。不主动采集模型 API Key、WeKnora 密钥或请求鉴权头。后台批量发送，任务完成时刷新队列，关闭服务时清空待发送数据；监控服务不可用时不阻止模型执行。健康接口的 `tracing.enabled` 仅表示已启用，真实投递是否成功应在 Studio 中验证。

## 哪些操作不会产生模型调用

规划与生成 PPTD 页面、通过对话修改内容会调用模型；生成中的结构或渲染错误也可能触发修复调用。模板导入、直接元素编辑、版本恢复，以及手动编辑后单独导出 PPTX/PDF 使用已保存内容，不调用模型。主链路 PDF 使用 Chromium；旧项目兼容导出仍可使用 LibreOffice。这些导出任务可以有根 trace，但不会新增 `chat` 模型节点。

启用之前的历史调用无法补报完整追踪；需要重新发起一次模型任务。Studio 的 Projects 注册与 Traces 上报是不同功能，本次接入 Traces。

## 验证

```bash
.venv/bin/python -m unittest agent_service.test_telemetry -v
.venv/bin/python -m unittest agent_service.test_work_history -v
```

前者通过真实 AgentScope 中间件与本地确定性模型响应验证父子调用、并发会话隔离、输入输出、token、错误和取消状态；不调用外部模型。
