# 预设设计系统（第三方资源）

来源：`open-kimi-ppt-skill` v1.2.0（MIT，作者 binaryify，
<https://github.com/binaryify/open-kimi-ppt-skill>），随镜像解包于
`experiments/open-kimi-ppt-skill/`。文件按原样复制，未做改写。

- `specs/` — 设计系统规范（`*.md`）。两套目录互补：
  - `<分类>/<风格名>/design.md`：`theme.md` 索引引用的主目录；
  - `0N_<分类>/<序号>/en/<风格名>.md`：较新的编号目录，同名条目更完整。
  两者同名时目录里只保留较长的一份（`agent_service/design_systems.py`）。
- `theme.md` / `theme_EN.md` — 上游的预设主题索引（人读）。
- `general-poster.md` — 上游的海报/信息图规则（本仓库暂只作为参考文本）。

用途：本项目「指定设计规范」的**预设选项**。选定后其正文作为 `designSystem`
注入生成请求；用户仍可在此基础上改写。
