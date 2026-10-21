# PPT AS

基于 AgentScope 的 AI 演示文稿助手。项目在 AgentScope Web UI 基础上扩展了 PPT Studio，支持主题输入、内容规划、页面生成、模板复用、PPTX/PDF 导出、页面修复、AI 视觉审稿和单页重新设计。

## 当前状态

- 主线分支：`kimi`
- 权威文档格式：PPTD v2
- 生成模式：快速模式和专家模式
- 主模型：配置在项目 `.env`，当前环境为 MiniMax
- 本地 Qwen：已完成接口、结构化输出和快速模式冒烟，专家模式调试暂缓
- 公司知识库：WeKnora 连接代码已存在，真实正文检索等待租户配置
- Docker：BFF 和前端镜像已验证；Agent 镜像构建暂缓
- 重构前测试、黄金场景清单和性能基线已建立

## 核心功能

- 从提示词生成完整 PPTD 演示，并导出 PPTX、PDF、页面 PNG 和源文件 ZIP。
- 快速模式：结构化规划、并发生成页面、自动视觉审稿与导出。
- 专家模式：Agent 使用文件、检查、截图等工具完成长文档和模板创作。
- 公司模板复用：封面、致谢、背景、公司标识和正文设计。
- 页面级操作：页面修复、AI 视觉检视、单页重新设计。
- 设计候选稿：预览、应用或放弃，不直接覆盖当前版本。
- 版本历史、revision 冲突保护、任务取消、失败恢复和 SSE 进度。
- 知识库配图和可选的正文知识检索能力。

## 技术栈

| 层    | 技术                                                    |
| ----- | ------------------------------------------------------- |
| 前端  | React 19、TypeScript、Vite、Tailwind v4、TanStack Query |
| BFF   | Express、TypeScript                                     |
| Agent | Python 3.11+、FastAPI、AgentScope                       |
| PPTD  | 自研 PPTD v2 文档、浏览器渲染器和原生 PPTX 导出         |
| 测试  | Python unittest、Node test、Playwright 浏览器契约测试   |
| 部署  | 源码进程管理、Docker Compose、离线镜像与 wheels 包      |

## 目录

```text
frontend/                 React 工作台
backend/                  Express BFF
agent_service/            FastAPI、生成、审稿、修复和导出
pptd_engine/              PPTD 导入、校验、渲染和 PPTX 导出
packages/pptd-runtime/    浏览器 PPTD 运行时
deploy/                   源码、Docker 和离线部署脚本
tests/golden/             黄金测试场景清单
docs/                     架构、部署、模型和验证文档
runtime/                  运行数据，不提交 Git
```

运行数据默认位于 `runtime/studio`：

- `projects/`：项目元数据
- `documents/`：当前权威 PPTD 文档指针
- `document_versions/`：不可变文档版本
- `jobs/`：任务状态和事件
- `checkpoints/`：生成检查点
- `artifacts/`：PPTX、PDF、预览和导出报告
- `templates/`：公司模板
- `design-candidates/`：待应用或放弃的页面设计稿

## 快速开始

前置条件：

- Node.js 22
- pnpm
- Python 3.11+
- uv
- Chromium
- LibreOffice 或兼容渲染镜像

安装：

```bash
cp .env.example .env
chmod 600 .env
pnpm install
uv venv .venv
uv pip install --python .venv/bin/python -r agent_service/requirements.txt
pnpm build
./start.sh
```

状态和停止：

```bash
./status.sh
./stop.sh
```

开发模式：

```bash
pnpm dev
```

## 配置

模型和外部服务都通过 `.env` 配置：

- 文本模型：`PPT_LLM_PROVIDER`、`PPT_LLM_MODEL`、对应 API Key/Base URL
- 视觉模型：`PPT_VISION_PROVIDER`、`PPT_VISION_MODEL`
- 本地 Qwen profile：`config/profiles/qwen.env.example`
- 公司知识库：`WEKNORA_BASE_URL`、`WEKNORA_API_KEY`、`WEKNORA_TENANT_ID`
- 页面操作超时：`PPTD_PAGE_ACTION_TIMEOUT_SECONDS`
- 视觉审稿并发：`PPTD_VISUAL_REVIEW_CONCURRENCY`
- 数据目录：`STUDIO_DATA_DIR`

不要提交 `.env`、模型密钥或公司知识库凭证。

## 测试

```bash
pnpm test:fast
pnpm test:contract
pnpm test:pptd
pnpm test:all
```

浏览器页面操作测试需要先启动前端：

```bash
pnpm test:browser:page-actions
```

检查本地 Qwen：

```bash
pnpm check:qwen -- --env .env
pnpm check:qwen -- --env .env --image runtime/sample-slide.png
```

性能摘要：

```bash
pnpm perf:summary
pnpm perf:report -- --output runtime/diagnostics/performance.json
```

## 部署

详见 [部署说明](docs/deployment.md)。

```bash
docker compose -f deploy/docker/compose.yml up -d --build
```

默认访问 `http://<server>:8080`。只有前端端口需要对外暴露。

## 关键文档

- [项目工作清单](docs/work-plan.md)
- [部署说明](docs/deployment.md)
- [后端与 Agent](docs/backend-agent.md)
- [前端工作台](docs/frontend-studio.md)
- [模型配置](docs/local-models.md)
- [内容配图与 WeKnora](docs/illustrations.md)
- [Tracing](docs/tracing.md)
- [视觉与生成验证](docs/verification.md)

## 已知限制

- 公司知识库真实检索需要 `WEKNORA_TENANT_ID`。
- 专家模式完整本地 Qwen 调试暂缓。
- 离线部署尚未在真正断网服务器完成验收。
- Agent Docker 镜像构建暂缓。
- 完全隔离模式下的字体、素材和知识库仍需按部署环境准备。
