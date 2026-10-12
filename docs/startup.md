# 启动与停止

适用于当前 Linux 主机。脚本从任意工作目录调用均可，所有 Python 命令使用项目 `.venv`。

```bash
cd /home/figodxp/workspace/ppt_as
./start.sh             # 后台启动三个服务，完成健康检查后返回终端
./status.sh            # 查看进程状态与当前模型配置
./start.sh --restart   # 修改 .env 后重启三个服务
./stop.sh              # 停止脚本管理的三个服务
```

工作台地址：<http://192.168.0.10:5174/>。脚本也会打印本机及检测到的局域网地址。

## 配置与依赖

模型和渲染配置读取项目根目录 `.env`。启动脚本会清除继承的 `ANTHROPIC_*`、`OPENAI_*`、`PPT_LLM_*` 以及渲染/存储配置环境变量，再由 Agent 加载 `.env`，避免使用其他工具的模型凭证。脚本不会输出 API Key，也不会发起付费模型测试；“就绪”表示服务和代理健康，不代表模型地址可访问或支持工具调用。

首次部署先安装依赖：

```bash
pnpm install
uv venv .venv
uv pip install --python .venv/bin/python -r agent_service/requirements.txt
cp .env.example .env
# 编辑 .env 后执行 ./start.sh
```

已有 `.env` 或 `.venv` 时不要重复覆盖。模型提供商、名称、服务地址和密钥需填写完整；本地服务不验证密钥时可使用占位值 `local`。模型运行在另一台机器时，地址应使用该机器局域网 IP。

## 日志

```bash
tail -f runtime/services/agent.log
tail -f runtime/services/backend.log
tail -f runtime/services/frontend.log
```

日志追加写入；目录与新日志使用仅当前用户可读写的权限。正常停止不会删除日志、项目、模板或生成的文件。

## 运行方式与端口

- 前端 Vite：`0.0.0.0:5174`，局域网可访问，端口冲突时拒绝自动换端口。
- Express：`127.0.0.1:3002`，通过前端同源代理访问。
- Python Agent：`127.0.0.1:8001`。

脚本将三个服务放入独立进程组并记录 PID 与启动标识。重复启动已健康的服务不会创建第二份进程；启动失败会清理本次启动的服务。停止时仅操作本脚本登记且身份匹配的进程，不会按端口强杀其他应用。

旧终端通过 `pnpm dev` 等方式启动的服务不属于脚本管理。若端口已占用，请在旧终端停止对应服务后再启动脚本；`./stop.sh` 不会终止这些未登记进程。

前端支持 Vite 热更新；后端和 Agent 以常驻进程运行，修改其代码或 `.env` 后使用 `./start.sh --restart`。这些脚本不注册开机自启服务，系统重启后需要重新运行 `./start.sh`。
