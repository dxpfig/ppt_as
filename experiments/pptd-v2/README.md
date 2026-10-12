# PPTD 工作台与验收

这部分提供 PPTD v2 工作副本编辑、预览、动画播放、PPTX/PDF 导出及可复现测试。可复用实现位于仓库根目录的 `pptd_engine/` 与 `packages/pptd-runtime/`。具体覆盖范围、格式依据与真实限制见[高级能力实现报告](../../docs/kimi-ppt/advanced-capabilities.md)。

```bash
pnpm build:pptd
pnpm dev:pptd
```

默认运行 18 页高级样例，监听 8891；已有工作副本会续读。自定义输入：

```bash
.venv/bin/python experiments/pptd-v2/server.py \
  --input path/to/project.pptd --workspace runtime/your-working-copy \
  --host 127.0.0.1 --port 8893
```

输入目录之外的独立工作副本保存所有修改。只写入指定元素的 text/bounds，重开后核验；未改文件逐字节保留。App 元数据在文档目录之外。编辑页 YAML 注释与格式会规范化，alias 明确拒绝。预览与导出共用 YAML 1.2 core 标量规则及字体缓存。

| 文件 | 用途 |
|---|---|
| `advanced-fixture/`、`make_advanced_fixture.py` | 18 页高级能力验收源与确定性生成器。 |
| `fixture/` | 早期两页、14 元素编辑保存回归源。 |
| `server.py`、`web/index.html` | 加载/保存、能力与字体资源接口、实际预览/动画与导出按钮。 |
| `export_pdf.cjs` | Chromium 输出每页真实文字/矢量 PDF；服务用 PyMuPDF 合并。 |
| `advanced_browser_probe.cjs` | 18 页实际预览、截图、源数据对照以及 PPTX/PDF 按钮下载验收。 |
| `browser_probe.cjs`、`check_document.py` | 编辑保存/重开及非法编辑、源文件不变量回归。 |
| `pptx_probe.py` | `pptd_engine.pptx` 的兼容 CLI。 |
| `verify_pptx_probe.py` | 独立核验 OOXML、嵌入数据及既有 PPTX 导入结果。 |
| `render_import.cjs`、`run_probe.py` | 原两页编辑导出及独立六页导入回归；包括 LibreOffice 转换对照。 |

```bash
pnpm test:pptd
node experiments/pptd-v2/advanced_browser_probe.cjs http://127.0.0.1:8893 runtime/advanced-validation/final-browser
.venv/bin/python experiments/pptd-v2/run_probe.py
```

最后一个回归需要既有独立样例 `runtime/test-results/design-layouts/presentation.pptx`，使用端口 8891/8894（可用 `PPTD_PROBE_PORT` / `PPTD_IMPORT_PORT` 更改）。`--reuse-browser-evidence` 只适用于浏览器源码没有变化且已有新鲜结果的续跑。

数学公式的 PDF 必须从浏览器入口导出：当前 LibreOffice 转 PPTX 时会丢失原生 Office 数学。PPTX 中的公式是可编辑 OMML；动画是原生时间树，但没有 Microsoft PowerPoint 实机验证。复杂图表的矢量分支必须显式启用，图表数据保留在源快照中。不要把技术工作台等同于 Kimi 全部 App 封装或已迁移的 Agent 生产入口。
