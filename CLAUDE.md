# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: ppt_as — AI 演示文稿(PPT)助手

基于 **AgentScope 框架** 构建的 AI PPT 生成助手。前端复用 AgentScope 官方 `examples/web_ui/`(React 19 + TypeScript + Vite + Tailwind + shadcn)作为基础界面,在此之上扩展 PPT 主题、大纲、幻灯片预览与生成等业务视图。后端使用 `backend/`(Express + TS)桥接到 AgentScope Python 侧的智能体服务。

## 技术栈

| 层 | 选型 |
|---|---|
| 包管理 | pnpm(workspace) |
| 前端 | React 19 + TypeScript + Vite + Tailwind v4 + shadcn/ui + TanStack Query + i18next + streamdown |
| 后端(BFF) | Express + TypeScript(`ts-node` + `nodemon`) |
| Agent 框架 | `@agentscope-ai/agentscope`(Python 侧,见 AgentScope 主仓库) |
| 代码规范 | ESLint(React + 类型检查)+ Prettier + Husky + lint-staged |

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

## 运行时目录(不要提交)

- `.remember/` — Remember 插件产物(已在 .gitignore)
- `.traces/` — Claude Code trace hook 产物(已在 .gitignore)
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