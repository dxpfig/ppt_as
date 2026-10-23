# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project: ppt_as — AI 演示文稿(PPT)助手

基于 **AgentScope 框架** 构建的 AI PPT 生成助手。前端复用 AgentScope 官方 `examples/web_ui/`(React 19 + TypeScript + Vite + Tailwind + shadcn)作为基础界面,在此之上扩展 PPT 主题、大纲、幻灯片预览与生成等业务视图。后端使用 `backend/`(Express + TS)桥接到 AgentScope Python 侧的智能体服务。

## 技术栈

| 层         | 选型                                                                                           |
| ---------- | ---------------------------------------------------------------------------------------------- |
| 包管理     | pnpm(workspace)                                                                                |
| 前端       | React 19 + TypeScript + Vite + Tailwind v4 + shadcn/ui + TanStack Query + i18next + streamdown |
| 后端(BFF)  | Express + TypeScript(`ts-node` + `nodemon`)                                                    |
| Agent 框架 | `@agentscope-ai/agentscope`(Python 侧,见 AgentScope 主仓库)                                    |
| 代码规范   | ESLint(React + 类型检查)+ Prettier + Husky + lint-staged                                       |

## 项目结构

```
ppt_as/
├── frontend/                 # React + Vite 前端
│   ├── src/
│   │   ├── api/              # 后端 API 调用封装
│   │   ├── components/       # 通用组件
│   │   ├── pages/            # 路由级页面
│   │   ├── context/          # React Context
│   │   ├── hooks/
│   │   ├── i18n/             # 多语言
│   │   ├── lib/
│   │   ├── types/
│   │   └── utils/
│   └── ...
├── backend/                  # Express BFF
│   └── src/index.ts
├── package.json              # workspace 根脚本
├── pnpm-workspace.yaml
└── pnpm-lock.yaml
```

## 常用命令

```bash
# 安装依赖(首次或 lockfile 变更后)
pnpm install

# 开发:并行跑前端(5173)+ 后端(3000)
pnpm dev

# 仅前端 / 仅后端
pnpm dev:frontend
pnpm dev:backend

# 构建
pnpm build                  # 全部
pnpm build:frontend
pnpm build:backend

# 代码格式化与检查
pnpm format                 # prettier + eslint --fix
pnpm format:check
```

## 单页模板基准门禁(强制)

页面生成相关改动必须通过固定的 10 个用户模板单页基准用例。基线定义：

- 用例说明与时间预算：`docs/single-page-template-test-cases.md`
- 机器可读用例清单：`benchmarks/single-page-template/cases.v1.json`
- 优化目标与实施顺序：`docs/single-page-template-optimization-goals.md`
- 固定用户模板：`d962b90ffef240d0be9dde7fee39750b`
- 模板 SHA-256：`e197ff456d0678dd15d022eee1814869db469965606422b02c5d96397b792113`

触发条件：

- 修改 `agent_service/layout_engine.py`
- 修改页面生成提示词、页面 Agent 或页面门禁
- 修改 `agent_service/kimi_template.py` 或模板处理
- 修改单页渲染、AI 视觉检视或视觉修复
- 修改 PPTD 页面结构、元素统计或导出相关逻辑

执行要求：

- 日常开发：10 个用例各运行 1 次 smoke。
- 合并或发布前：10 个用例各运行 3 次，记录中位数和硬上限。
- 基准门禁要求每个用例首轮直接通过；发生修复的运行只能用于诊断，不能记为基准通过。
- 任意用例失败、超时或相对上一基线中位耗时回退超过 10%，不得宣称迭代通过。
- 基线结果写入 `runtime/benchmarks/single-page-template/`，不得覆盖历史结果。
- 不得删除或替换固定模板、用例 ID、元素预算和时间预算来规避失败；用例变更必须单独评审。

## 运行时目录(不要提交)

- `.remember/` — Remember 插件产物(已在 .gitignore)
- `.traces/` — Codex trace hook 产物(已在 .gitignore)
- `frontend/node_modules/`、`backend/node_modules/` — 由 `pnpm install` 生成

## 当前业务方向(占位,待与产品对齐)

PPT 助手核心流程预期为:**主题/大纲输入 → AgentScope 多智能体拆解与生成 → 实时预览幻灯片 → 导出 PPTX/PDF**。前端将在 AgentScope 原生对话/工作流视图之上新增:

- 主题输入与大纲编辑视图
- 幻灯片网格预览与单页编辑
- 导出与历史记录
- 与 AgentScope Python 侧 PPT 智能体的 API 桥接

## 后续待补

- Python 侧 AgentScope 智能体定义(主题拆分 / 大纲生成 / 页面合成)
- 后端 BFF 与 Python 服务的连接方式(HTTP / stdio / 长连接)
- 导出服务的实现策略(本地 LibreOffice / 云端 / 浏览器端)
