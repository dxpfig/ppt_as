# 部署说明

本文档对应当前已落地的源码、Docker 和离线部署骨架。W12 重构后的四个 Job Runner、统一 JobStore/SSE、渲染、审稿和导出服务均包含在同一模块化单体中。完整离线验收仍依赖本地 Qwen、公司知识库和 Agent 镜像的最终验证。

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

`PPT_VISION_MODEL` 必须使用服务端实际支持的模型标识。部署后先执行随机图片识别校验，健康接口应显示模型、视觉、知识库和渲染器 provider 状态。

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

源码开发验证：

```bash
pnpm test:all
pnpm build
pnpm --filter frontend lint
```

性能门禁：

```bash
pnpm perf:report -- \
  --data-dir runtime/studio \
  --output runtime/diagnostics/performance.json
pnpm perf:compare -- \
  --baseline docs/performance-baseline-2026-10-21.json \
  --candidate runtime/diagnostics/performance.json \
  --max-regression 0.2
```

### 容器

| 容器     | 作用                            | 是否暴露宿主机 |
| -------- | ------------------------------- | -------------- |
| frontend | Nginx 静态站点与 `/api` 代理    | 是，默认 8080  |
| backend  | Express BFF                     | 否             |
| agent    | FastAPI、AgentScope、渲染与导出 | 否             |

前端、BFF 镜像已验证构建；BFF 和前端容器已实际启动并通过 HTTP 检查。Agent 镜像包含 LibreOffice、Chromium 和 Python 依赖，构建体积较大。
当前 Agent 镜像完整构建与三容器 Compose 全链路仍待验收，不能仅凭前端和 BFF 容器成功就宣称整个生产栈已部署完成。

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

源码模式的数据、检查点和缓存均位于 `STUDIO_DATA_DIR`。备份至少应覆盖 `projects/`、`documents/`、`document_versions/`、`jobs/`、`checkpoints/`、`templates/`、`reference-files/` 和 `artifacts/`。`render-cache/`、`review-cache/`、`export-cache/` 可重建，不属于权威数据。

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

## 已知限制与故障排查

- 视觉模型必须真实支持图片输入；只返回“看过图片”文本的模型会被随机像素校验拒绝。
- 429/529 会触发模型请求退避；持续限流时应降低页面并发和视觉审稿并发。
- 文档 revision 冲突返回 409，必须重新加载后操作，不能强制覆盖。
- Chromium 或 LibreOffice 不可用时不会伪造 PPTX/PDF 成功，应检查渲染器健康状态、字体和镜像。
- 5/20 页真实任务可能因视觉服务限流、超时或格式异常而中止发布并保留检查点；恢复后可继续，不会伪造审稿成功。
- 两轮安全修复后仅剩主观样式提醒时允许发布，提醒会写入任务结果、项目元数据和导出报告。
- 公司知识库真实检索需要租户配置；离线环境不得退化为伪造引用。
