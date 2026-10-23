# Windows 本地部署指南

本项目自带的 `start.sh` / `stop.sh` / `status.sh` 和 `scripts/services.py` 依赖
`/proc`、`fcntl.flock`、`os.killpg`，**只能在 Linux 上运行**。Windows 用户请使用本文
提供的 PowerShell 脚本，功能与 Linux 版本一一对应。

## 环境要求

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.12 或更高 | 只用于创建虚拟环境；依赖全部装进项目内的 `.venv` |
| Node.js | 22 LTS 或更高 | 前端 Vite 8 与后端 ts-node 均要求 Node 22+ |
| pnpm | 9 或更高 | 若 PATH 中没有，安装脚本会自动装到 `.cache\pnpm-home` |
| LibreOffice | 可选 | 不装则 PPTX / PDF 导出不可用，页面预览仍正常 |

## 磁盘占用约定

所有安装产物与缓存都落在项目所在盘（当前为 `G:`），**不写入 C 盘**：

| 路径 | 内容 |
| --- | --- |
| `.venv\` | Python 虚拟环境（`Scripts\python.exe`） |
| `.cache\pip\` | pip 下载缓存 |
| `.cache\npm\` | npm 缓存 |
| `.cache\pnpm-store\` | pnpm 全局 store（与项目同盘，才能建立硬链接） |
| `.cache\pnpm-home\` | 未预装 pnpm 时的本地 pnpm |
| `.cache\tmp\` | 安装期临时目录 |
| `frontend\node_modules\`、`backend\node_modules\` | Node 依赖 |

系统全局 Python 不会被写入任何包。安装脚本会遍历 `py -0p` 列出的全部解释器以及 PATH
上的 `python`，逐个实际运行一次，选用第一个可用且版本不低于 3.12 的作为虚拟环境基底
（`py` 启动器可能保留已卸载解释器的注册项，所以必须实际运行才能确认可用）。

## 三步启动

### 1. 安装依赖

双击 `scripts\setup-windows.cmd`，或在 PowerShell 中执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
```

该脚本会依次完成：重定向缓存目录 → 创建 `.venv` → 安装 Python 依赖 → 安装 Node 依赖
→ 构建 PPTD 运行时 → 由 `.env.example` 生成 `.env`。

### 2. 填写模型配置

编辑项目根目录的 `.env`，至少需要这几项（模型走 OpenAI 兼容协议时）：

```dotenv
PPT_LLM_PROVIDER=openai
PPT_LLM_MODEL=<你的模型名>
OPENAI_API_KEY=<你的 API Key>
OPENAI_BASE_URL=<你的服务地址>
```

视觉审稿需要额外的视觉模型配置（必须支持真实图片输入）：

```dotenv
PPT_VISION_PROVIDER=openai
PPT_VISION_MODEL=<你的视觉模型名>
PPT_VISION_BASE_URL=<你的服务地址>
PPT_VISION_API_KEY=<你的 API Key>
```

> `.env` 已在 `.gitignore` 中，不会被提交。不要把密钥写进前端代码。

### 3. 启动服务

双击 `scripts\start-windows.cmd`，或执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\services-windows.ps1 -Action start
```

启动后访问 **http://127.0.0.1:5174/**

三个服务的端口与 Linux 版本完全一致：

| 服务 | 端口 | 说明 |
| --- | --- | --- |
| agent | 8001 | FastAPI + AgentScope 智能体服务 |
| backend | 3002 | Express BFF |
| frontend | 5174 | Vite 开发服务器，`/api` 反向代理到 3002 |

## 日常操作

```powershell
# 查看状态
powershell -ExecutionPolicy Bypass -File scripts\services-windows.ps1 -Action status

# 停止
powershell -ExecutionPolicy Bypass -File scripts\services-windows.ps1 -Action stop

# 重启（改了 .env 之后用）
powershell -ExecutionPolicy Bypass -File scripts\services-windows.ps1 -Action restart
```

日志位于 `runtime\services\`，每个服务一对文件：

- `agent.log` / `backend.log` / `frontend.log` —— 标准输出
- `agent.err.log` / `backend.err.log` / `frontend.err.log` —— 标准错误

每次启动会覆盖同名日志文件，需要保留旧日志时请先改名。

## 与 Linux 版的差异

- 进程状态记录在 `runtime\services\processes.json`，格式与 Linux 版一致。
- 停止服务时只终止本脚本登记过的进程；脚本**不会**杀掉占用端口的其他程序。
- 启动时会剥离宿主环境中的模型凭据变量（`ANTHROPIC_*`、`OPENAI_*`、`PPT_*` 等），
  与 `services.py` 行为一致，确保项目 `.env` 是唯一配置来源。
- 脚本内的提示信息一律使用 ASCII。Windows PowerShell 5.1 会按系统 ANSI 代码页
  （中文系统为 GBK）解析无 BOM 的文件，若脚本内直接写中文会出现乱码。

## 常见问题

**提示 `pnpm install failed` 或出现符号链接权限错误**
Windows 默认不允许普通用户创建符号链接。启用「设置 → 系统 → 系统信息 → 高级系统设置
→ 环境变量 → 用户变量 → Path」中的 `C:\Program Files\nodejs` 目录写入权限，或在
管理员 PowerShell 中执行一次 `npm config set legacy-peer-deps true`。pnpm 在工作区
内部使用 junction，通常不受影响。

**提示端口已被占用**
8001 / 3002 / 5174 三个端口必须空闲。可用
`netstat -ano | findstr :5174` 找到占用进程后自行结束。脚本不会替你终止未登记的进程。

**`rendererAvailable` 为 false**
未检测到 LibreOffice。安装 LibreOffice 后重启服务即可启用 PPTX / PDF 导出；
也可以在 `.env` 中配置 `PPT_RENDERER_IMAGE` 改用 Docker 渲染器。

**Python 依赖安装时出现编译错误**
`requirements.txt` 里部分包对 Python 版本有 wheel 覆盖差异。安装脚本要求 Python 3.12
或更高（`numpy 2.5.3` 声明 `Requires-Python >=3.12`）；若某个包缺少预编译包，需要安装
C++ 生成工具（Visual Studio Build Tools）后再重试。

**安装日志里出现 `uvloop` 构建失败**
`uvloop` 只支持 Linux / macOS，且没有 Windows wheel。`requirements.txt` 已用
`sys_platform != "win32"` 标记在 Windows 上跳过它；应用代码不导入 uvloop，Windows 上
uvicorn 会直接使用 asyncio 默认事件循环。

**脚本报 `SetConsoleWindowTitle` / `HostException`**
把脚本输出重定向到文件时，PowerShell 控制台宿主会尝试应用 pnpm 输出的 ANSI 窗口标题
序列并失败。安装脚本已改用 `append-only` 报告器并捕获该宿主异常，不会因此中断。

**导出报 `Font 'xxx' is not installed`**
导出器需要系统字体。Windows 上它从注册表枚举已安装字体并用字体自身的 name 表校验家族名，
因此 Windows 必须装有对应字体：中文正文需要 `Noto Sans SC`（本机通常随
`Noto Sans SC` 静态或可变字体安装），衬线用 `Noto Serif SC`，拉丁用 `Arial`。
文档里保留的是规范名（`Noto Sans CJK SC`），导出器会映射到平台实际安装的字体并记录替换。
若某个字体确实缺失，可在文档的 `customFonts` 里用显式来源声明。
