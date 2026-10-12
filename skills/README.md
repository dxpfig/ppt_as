# 逆向 kimi-slides 资产与三条路由

来源：`experiments/kimi_slide_research/Kimi做PPT功能逆向报告.rar`（用户提供）。
解包后的 `kimi-slides` 资产与仓库既有的 `docs/kimi-ppt/original/` **逐字节一致**，
因此提示词侧无需重复引入；这里只补执行机制。

## 路由（逆向报告 §二）

| 场景 | 走哪条 | 本仓库实现 |
| --- | --- | --- |
| 新建 deck | `kimi-slides`：模型写 `.pptd` + `check`/`screenshot` 闭环 | `agent_service/pptd_generation.py` + `pptd_engine/` |
| **改现有 pptx** | `pptx-surgical`：只重写点名段落，其余部件逐字节克隆 | `agent_service/surgical.py` + `skills/pptx-surgical/` |
| **用公司模板做新 deck** | 读模板 → 选页 → 只改文字 | `agent_service/template_job.py` |

逆向报告的原话：**「永远不要为了改一份 pptx 去 convert」**。转换是有损的——加载项
（`ppt/tags/`）、内嵌工作簿、内嵌字体都不会保留，而且不报错。公司模板几乎都带加载项，
所以模板路线必须**在原件上动刀**，不能重建。

## 目录

- `pptx-surgical/`：逐字节引入的上游 skill（`SKILL.md` + 5 个标准库脚本，哈希与逆向包一致）。
  `scripts/request_fold.py` 只对 Kimi 桌面端有效（走本机 IPC），本仓库不用它。
- `kimi-slides/` 的提示词资产即 `docs/kimi-ppt/original/`，由 `agent_service/kimi_rules.py`
  按角色注入；两者同源，改动需同步。

## 本仓库新增的封装

- `agent_service/surgical.py`
  - `inspect()`：页面清单 + 段落稳定地址（`s4#p0`）+ 碰不得的部件
  - `apply()`：ops → `edit.py` → `verify.py`（`--self` 与 `--before/--after`）
  - `select()`：在保留其余部件逐字节前提下选页/换序（只重写 `p:sldIdLst`、它的关系与
    `[Content_Types].xml`；不要动母版/主题关系，否则版面继承的版权行会消失）
- `agent_service/template_job.py`：清单 → 模型选页与写文案 → `apply` + `select` → 发布

## 实测（19 页公司模板）

- 只改封面三行文字：第 2–19 页与模板**逐像素一致**
- 选 6 页重建：5 页 0.00%、1 页 0.12% 差异
- AI 端到端（真实模型，6 页）：每页差异 0.03%–0.72%，全部来自改动的文字
