# PPTD 公式、图标、动画、图表与字体实现

日期：2026-10-09。用户本轮要求的五类能力已经接入 PPTD 预览、编辑后的导出和验收工具。文档仍由 `.pptd`、`.page` 与资源文件定义；原始 Kimi 规则的 13 个文件保持 SHA-256 一致。本实现依据公开规范，未使用不可取得的 Kimi 私有引擎。

## 当前实现

| 能力 | 浏览器 | PPTX / PDF |
|---|---|---|
| 公式 | KaTeX 0.16.47；行内、独立段落、表格中公式；分数、根式、上下标、矩阵、积分、求和；只继承颜色和字号 | 同一 KaTeX 解析为 MathML 后转换为可编辑 OMML；数学字体使用带 MATH 表的 STIX Two Math。PDF 使用 Chromium 的真实文字/矢量排版。 |
| 图标 | Font Awesome 7.3.1 Free 的 solid / regular / brands；2,883 个可识别名称（含别名），真实 SVG 路径 | 原生可编辑 `a:custGeom` 路径；支持填色/渐变/图片填充、边框、翻转、旋转、透明度及阴影。 |
| 动画 | 22 种效果；点击/与上一项同时/上一项之后；延迟、时长、缓动、重复、颜色/透明度/路径；播放、下一步、重置 | 原生 `p:timing`，引用实际形状 ID；独立检查微软 OpenXML SDK 定义的节点顺序、属性及基数。静态 PDF 输出文档的原始可见状态。 |
| 图表 | ECharts 6.1.0；全部 13 类型；组合、双轴、堆叠、空值、过滤、OHLC/HLC、瀑布总计、树及有向无环图 | 8 类可对应 Office 原生图表并嵌入 XLSX。其余类型及不能忠实映射的参数组合，需明确启用可编辑矢量出口；保留数据与源语义。 |
| 字体 | 浏览器从服务端缓存加载同一实际资源；区分 Latin / East Asian 字体；检查家族、样式及实际使用文字的字形覆盖 | 全量、未压缩 EOT `application/x-fontdata` 字体部件及嵌入关系；支持 TTF/OTF、TTC 选字面、WOFF 解码与可变字体实例化；遵守嵌入许可位。 |

原生图表类型为 `bar`、`line`、`area`、`scatter`、`bubble`、`candlestick`、`pie`、`radar`。`waterfall`、`heatmap`、`treemap`、`sunburst`、`sankey` 使用可编辑矢量。流式堆叠、Office 无法等价表示的柱宽/间距/标记等参数，也会进入有报告的矢量分支；默认 CLI 会拒绝这种转换，必须给出 `--allow-vector-charts`。界面的对应开关和说明始终可见。

22 个效果：appear、fade-in、fly-in、zoom-in、wipe-in、float-in、peek-in、rise-in、pulse、grow-shrink、spin、teeter、fill-color、transparency、color-pulse、disappear、fade-out、fly-out、zoom-out、wipe-out、float-out、motion-path。

## 运行与使用

在仓库根目录运行：

```bash
pnpm install --ignore-scripts
uv pip install --python .venv/bin/python -r agent_service/requirements.txt
pnpm dev:pptd
```

打开 `http://192.168.0.10:8891`（本机可用 `http://127.0.0.1:8891`）。默认展示 18 页高级能力样例，支持预览、动画播放、文字/坐标编辑、PPTX 和 PDF 导出以及导出报告。现有工作副本会续读，避免覆盖先前修改。

本次验收服务使用 **8893**，工作副本在 `runtime/advanced-validation/advanced-working/`。它是 PPTD 能力入口；原先的 Agent 主题/大纲生成工作流仍使用其已有入口，尚未迁移到这份 PPTD 文档核心。

单独导出或恢复源文件：

```bash
.venv/bin/python -m pptd_engine \
  --input experiments/pptd-v2/advanced-fixture/advanced.pptd \
  --output runtime/my-pptd-export --allow-vector-charts

.venv/bin/python -m pptd_engine recover-source \
  runtime/my-pptd-export/export.pptx --output runtime/recovered-pptd
```

恢复的是 **导出时的原始 PPTD 快照**，不是 PowerPoint 后续编辑的逆向转换。它保留原始文件字节、图表数据、公式源、动画、主题及资源引用。导入任意第三方 PPTX 的高级对象仍属于另一条待扩充链路。

运行依赖：Node 22、Python 3.12、Fontconfig、Noto Sans CJK SC 与 Chromium。Playwright 已列入项目开发依赖；`PPTD_CHROMIUM` 可指定 Chromium 可执行文件，默认本机 `/snap/bin/chromium`。可用 `PPTD_NODE_MODULES` 指定外部 Node 模块目录。首次使用自定义字体需访问声明的字体源；下载缓存放在 `runtime/fonts/`，与 PPTD 文件分开。字号以文档规定的 72 PPI 映射为 Office pt，PDF 保持相同物理尺寸。

## 验证与证据

可复现检查：

```bash
pnpm build:pptd
pnpm test:pptd
node experiments/pptd-v2/advanced_browser_probe.cjs \
  http://127.0.0.1:8893 runtime/advanced-validation/final-browser
.venv/bin/python experiments/pptd-v2/run_probe.py
pnpm build
```

**最终运行：Node/Chromium 56/56、Python 56/56 通过；前端 TypeScript/Vite 与后端 TypeScript 构建通过；相关包配置的 Prettier 检查通过。** 构建仍有既有大 bundle 和 mime-types 浏览器外部化提示。

18 页验收源包含 6 个公式、3 种图标前缀、15 个图表（13 类型 + 组合 + 流图）和 22 条动画。导出的 86 个顶层对象中，80 个为原生对象，6 个为可编辑图表矢量组；包括 9 个原生图表及 9 份嵌入工作簿。样例没有整页图片替代。PDF 实测为 18 页、每页 960×540pt，全部页面没有图片对象，公式符号和文字可提取。

- [独立产物复核](../../runtime/advanced-validation/final-browser/artifact-verification.json) / [18页总览](../../runtime/advanced-validation/final-browser/contact-sheet.png)
- [最终测试日志](../../runtime/advanced-validation/tests.log)
- [浏览器、交互与下载报告](../../runtime/advanced-validation/final-browser/browser-report.json)
- [PPTX 样例](../../runtime/advanced-validation/final-browser/ui-export.pptx) / [导出报告](../../runtime/advanced-validation/final-browser/ui-export-pptx-report.json)
- [PDF 样例](../../runtime/advanced-validation/final-browser/ui-export.pdf) / [导出报告](../../runtime/advanced-validation/final-browser/ui-export-pdf-report.json)
- [旧编辑链路回归](../../runtime/pptd-v2-spike/evaluation.json)：9 组文档不变量、10 组浏览器编辑检查、216 项独立 OOXML/内容断言及既有 6 页 PPTX 导入呈现。

还验证了本机未安装的 JetBrains Mono：该字体实际嵌入 PPTX 后，LibreOffice 转出的 PDF 字体清单包含 JetBrainsMono-Regular；EOT 解码出的 sfnt 字节与缓存字体 SHA-256 一致。不能把这一结果外推为所有 Office 版本的排版完全一致。

## 已知边界

- **没有 Microsoft PowerPoint 实机播放/编辑验收。** 动画已有 Chromium 行为与独立 OOXML 结构证据，原生 Transparency 组合仍需 PowerPoint 实机验证。Office 毫秒量化使退出可见性设置最多有 1ms 的尾端差。
- **本机 LibreOfficeDev 26.8 会丢弃 `a14:m` 原生公式。** 因此正式 PDF 入口使用 Chromium；不要用 LibreOffice 转 PPTX 的结果替代公式 PDF 验收。原生公式仍保留在 PPTX 中，未改成截图。
- 未支持或非法的 TeX/MathML 节点、未知图标、非法图表数据、缺字和禁止编辑嵌入的字体均应定位报错。TeX 宏包全集不在公开 PPTD 文档定义中，本实现不声称支持任意 TeX 扩展。
- 字体只有常规字面时，粗体/斜体由显示引擎合成，报告明确标记；不会伪造字体文件。`思源黑体` 使用明确记录的 `Noto Sans CJK SC` 别名。浏览器加载和导出均检查实际文本的覆盖，不能把隐式回退当加载成功。
- 图表的罕见 `bar.symbol` Office 预设形状尚依赖完整形状引擎；当前基本形状及自定义 SVG path 可用，其余明确报错。矢量出口的非矩形裁切使用包围矩形，细小色标圆角可有差异；跨裁切曲线以不超过 0.05px 的误差近似为可编辑线段。Office 原生图表仍采用自身的自动标签布局。
- 既有普通表格边框、177 形状库、任意富文本、竖排、撤销/协作等没有因本次五项扩充而自动实现；报告不会将它们计入已完成。
- 服务当前是本地 PPTD 工作台，未实现生产级多用户隔离、版本并发或 Agent 全流程迁移；原始 Kimi skill 的完整流程迁移与五类渲染/导出能力是不同验收范围。

## 代码边界与格式依据

- `packages/pptd-runtime/src/`：浏览器/Node 共用 KaTeX、FA7、图表解析和动画播放器。
- `pptd_engine/`：共享 YAML 读取、OMML、可编辑路径、图表/工作簿、动画时间树、EOT、源快照及 PPTX 入口。
- `experiments/pptd-v2/`：工作台、导出 HTTP 接口、浏览器 PDF、验收源与脚本；旧 `pptx_probe.py` 仅保留兼容入口。

格式资料：[Microsoft TextMath](https://learn.microsoft.com/dotnet/api/documentformat.openxml.office2010.drawing.textmath)、[PowerPoint 字体格式说明](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-oe376/1663dabc-5d98-463f-889e-bcd9b77c3d34)、[EOT 规范](https://www.w3.org/submissions/EOT/)、[LibreOffice EOTConverter](https://raw.githubusercontent.com/LibreOffice/core/master/vcl/source/font/EOTConverter.cxx)。动画测试附带微软 OpenXML SDK 相关 schema 片段与许可证，避免只用与实现同源的断言验证结构。
