# 部署说明

本文档对应当前已落地的源码、Docker 和离线部署骨架。完整离线验收仍依赖本地 Qwen、公司知识库和 Agent 镜像的最终验证。

## Docker 部署

前置条件：

- Docker 29 或兼容版本
- Docker Compose 2.40 或兼容版本
- 可访问模型和知识库的项目 `.env`
- 默认对外端口 `8080`

配置：

```bash
cp .env.example .env
chmod 600 .env
```

在 `.env` 中填写模型、视觉模型、知识库和渲染器配置。密钥只通过 `env_file` 注入容器，不写入镜像。

启动：

```bash
docker compose -f deploy/docker/compose.yml up -d --build
```

访问 `http://<server>:8080`。数据卷 `ppt-runtime` 保存项目、PPTX/PDF、模板和运行数据。

查看状态：

```bash
docker compose -f deploy/docker/compose.yml ps
curl -fsS http://127.0.0.1:8080/api/health
curl -fsS http://127.0.0.1:8080/api/studio/health
```

停止：

```bash
docker compose -f deploy/docker/compose.yml down
```

数据卷默认保留。只有明确需要删除全部运行数据时才使用 `down -v`。

### 容器

| 容器     | 作用                            | 是否暴露宿主机 |
| -------- | ------------------------------- | -------------- |
| frontend | Nginx 静态站点与 `/api` 代理    | 是，默认 8080  |
| backend  | Express BFF                     | 否             |
| agent    | FastAPI、AgentScope、渲染与导出 | 否             |

前端、BFF 镜像已验证构建；BFF 和前端容器已实际启动并通过 HTTP 检查。Agent 镜像包含 LibreOffice、Chromium 和 Python 依赖，构建体积较大。

## 源码部署

前置条件：

- Node.js 22
- pnpm
- Python 3.11+
- uv
- Chromium
- LibreOffice 或兼容渲染镜像

安装：

```bash
./deploy/source/install.sh
```

如果 `.env` 不存在，脚本会从模板创建并停止，等待填写配置。

启动和状态：

```bash
./start.sh
./status.sh
```

升级：

```bash
./deploy/source/upgrade.sh
```

`ppt-as.service.example` 可作为 systemd 单元的起点。当前 `start.sh` 使用项目内置进程管理器和 Vite 开发服务器，生产环境优先使用 Docker 或反向代理静态构建产物。

## 离线部署

构建离线包：

```bash
./deploy/offline/build-bundle.sh 2026.10.21 /path/to/output
```

离线包包含：

- 三个 Docker 镜像
- 源码归档
- Python wheels
- Docker、源码和离线部署脚本
- `.env.example`
- SHA-256 校验清单

构建机需要已经能够拉取容器基础镜像和 Python 包。构建完成后将整个输出目录转移到离线服务器。

安装：

```bash
cd ppt-as-2026.10.21
cp .env.example .env
chmod 600 .env
./install-offline.sh
./verify-offline.sh
```

离线安装脚本会加载带版本号的镜像并执行 `docker compose up --no-build`。如果 `.env` 不存在，脚本会先停止并要求填写配置。

## 安全约束

- `.env`、模型密钥、知识库密钥和用户数据不进入镜像或 Git。
- 只有前端端口需要对外暴露；Agent 和 BFF 仅在 Compose 内网访问。
- Nginx 上传限制为 64 MB，SSE 和长任务代理关闭缓冲。
- 运行数据使用持久化卷，删除卷前必须备份。
- 离线包必须校验 `SHA256SUMS` 后才能加载镜像。
