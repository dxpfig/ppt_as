# Kimi App 与配套工具的封装清单

这份材料区分三个证据层级：

- **明示契约**：原文写出了用户可见行为、格式或命令，但没有因此获得其源码。
- **实现需求（推断）**：为了复现明示行为，本项目需要某种能力；不能据此断言 Kimi 内部用了特定服务或架构。
- **未知实现**：现有文件没有提供接口、算法、兼容矩阵或运行包。

“工具环境能力”也不等于“kimi-slides 包内部能力”。搜索、浏览器、图片生成、视觉模型、Python 和文件工具在原文中被使用，但它们的供应商与实现没有给出。

## 1. 按宿主能力列出的完整工作清单

| ID | 能力 / 归属 | 原文明示的行为和证据 | 迁移所需封装及未知边界 |
|---|---|---|---|
| H01 | Skill 路由与上下文 / Agent 宿主 | SKILL L3、18–47：任务进入 skill，阅读全部上下文，判定任务、设计方法、资料类型和页数 | 需要规则加载与分支执行能力。原文没有规定系统提示词结构、Agent 数量、任务状态机或模型。 |
| H02 | 附件与文档读取 / 工具环境 | SKILL L19、26、35–38、49–58：读取完整文档、PPTX、图片/PDF | 需要附件访问、文本/图片抽取和必要的 OCR 等能力。OCR 是可选实现机制，不是原文点名的固定 API；格式支持矩阵和大小限制未知。 |
| H03 | 网页读取与视觉浏览 / 工具环境 | SKILL L19、80：读取URL内容；风格参考URL必须看视觉，不能只提取文字 | 需要网页加载、适用时截图、页面资源提取与引用。浏览器引擎、登录处理、滚动/长页策略未知。 |
| H04 | 事实资料研究 / 工具环境 | SKILL L38；分析场景 L13：搜索数据、案例和材料，标来源和口径 | 需要内容搜索与来源记录，不是图片搜索的别名。检索提供商、排序、核验算法及接口未知。 |
| H05 | 图片检索、生成、下载 / 工具环境 | SKILL L83–90：来源优先级、真实图优先、必要时生成图、批量素材准备 | 需要用户素材复用、可信来源筛选、检索/生成工具和素材落盘。未给生成模型、搜索 API 或具体打分算法。 |
| H06 | 看图、拼图、裁图 / 工具环境 | SKILL L50–52、56、74–81：网格、放大、总览、重点页、裁出复杂素材 | 需要图像理解和图像处理。未给视觉模型，未说明当前 MiniMax-M3 接口是否具备此能力。 |
| H07 | 工作区与并行文件工具 / Agent 宿主 | SKILL L115、118、120：稳定路径、并行 write_file、保存依赖 | 需要文件读取/写入与独立页面并行作业。并发提交、失败清理、原子发布与资源持久化实现未知。 |
| H08 | PPTD 解析与序列化 / 文档引擎 | pptd L9–148、319–510：YAML、多文件、主题、页面、元素、路径与图层 | 需要读写、schema、默认值、样式合并和稳定标识。现有材料是规范，没有提供库。 |
| H09 | PPTX 导入转换 / CLI | cli L3–31：convert 抽出页面和媒体，改写相对路径；SKILL L58：可能有损 | 需要 OOXML→文档模型转换，必要时回读原始文件。母版展开、复杂对象支持和保真矩阵未知；不能承诺无损。 |
| H10 | 静态结构与版面检查 / CLI | cli L33–81：check、14 类基础问题、部分确定修复；pptd 另有图表检查 | 需要结构/引用/主题/范围/文本检查器。文字使用模拟尺寸启发式，除 underfill 50% 外大多阈值未公开。 |
| H11 | PPTD 截图 / CLI | cli L83–111；SKILL L104：选页渲染、模拟截图可能与编辑器不同 | 需要可从模型渲染页面图像的实现。渲染库、缩放、像素密度、动画截取时刻和字体等待策略未知。 |
| H12 | 字体获取与排版 / 文档引擎及资源服务 | fonts 全文；pptd L143–146、452–458：系统字体、Kimi CDN、Google Fonts、精确名称 | 需要字体加载、缓存、名称处理、中英文字体与缺字行为。文档未提供具体排版引擎、字体嵌入及导出兼容实现。 |
| H13 | 形状、路径与图标 / 文档引擎 | pptd L674–877；shapes 全文：177 种预置形状、SVG path、Font Awesome 7.x Free | 需要形状几何、OOXML 调整参数、裁剪/描边、图标资源解析。形状表不是形状引擎源码，动作按钮形状也不等于可点击动作。 |
| H14 | 富文本、超链接与公式 / 文档引擎 | pptd L515–670：有限 HTML 标签/样式、列表、链接、LaTeX 混排 | 需要富文本解析、行布局和公式呈现。不能解释为支持任意 HTML/CSS；公式导出成原生对象还是图像未知。 |
| H15 | 表格 / 文档引擎 | pptd L882–951：比例列宽/行高、合并格、富文本、边框与主题 | 需要表格布局、合并与样式继承。整个表格明确不支持旋转/翻转和全局透明度。 |
| H16 | 图表 / 文档引擎 | pptd L956–1517：13 类图表、数据、系列、轴、标签、混合与校验 | 需要图表语义、绘制与导出映射。文档提 ECharts philosophy 不证明内部用了 ECharts；不明确所有图表导出后是否为原生可编辑图表。 |
| H17 | 动画编排 / 文档引擎与播放器 | pptd L1521–1644：22 个效果、点击分组、延迟/时长/路径等 | 需要播放器及目标文件映射。截图如何取时刻、PPTX 动画保真、播放状态如何保存未知。 |
| H18 | 资产引用协议 / Kimi App | SKILL L108–111、119：KIMI_REF 的 type/path 被渲染为资产卡片 | 明确需要宿主解析或等价资产事件。路径如何关联会话、读取权限和文档标识为推断的实现需求，内部 API 未公开。 |
| H19 | 卡片与编辑器打开 / Kimi App | SKILL L111：点卡片打开编辑器，用户可编辑和批注 | 需要文档路由和可编辑界面。具体工具栏、拖拽手柄、选区、撤销、快捷键等未在 skill 中定义，不能假定一比一 UI 已有规范。 |
| H20 | 编辑保存与模型继续修改 / Kimi App | SKILL L107、111、119：GUI 编辑，继续多轮精修 | 将 GUI 更改保存到可继续修改的文档是合理推断；具体保存协议、冲突处理及 Agent 如何读到改动未知。 |
| H21 | 批注 / Kimi App | SKILL L111 明示 GUI 可 annotated | 批注对象/页坐标锚点、状态、消息协议、是否直接回流 Agent、是否写入 PPTX 均未知。需要先定义本项目可观测行为。 |
| H22 | PPTX 与图片导出 / Kimi App | SKILL L111–112：右上角编辑器导出，禁止 CLI 导出 | 导出行为明示，但前端/后端实现、原生可编辑性矩阵和网络 API 未公开。PDF 不在这里公开的交付清单中；保留本项目 PDF 属扩展。 |
| H23 | 版本管理 / Kimi App | SKILL L120：同路径自动留每版，前端预览/恢复 | 需要稳定文档身份和历史状态。快照触发时机、全文/增量、是否包含所有媒体/字体、回滚后引用算法未知。不能只快照一份入口文件就假定足够。 |
| H24 | 资源生命周期 / 宿主存储 | SKILL L115、120；pptd L137–146：稳定文档、相对依赖、外部字体/图片例外 | 需要处理主文件、pages、media、字体依赖及原始 PPT。实际使用文件系统还是对象存储、版本资源去重与删除政策未知。 |
| H25 | 导出失败反馈 / Kimi App | SKILL L112：多次不能导出时引导反馈 | Kimi 反馈渠道与故障恢复 API 未公开；本项目应定义相应错误与反馈路径，不能引导到不存在的 Kimi 编辑器。 |

## 2. 已公开的工具调用契约

以下是原文已有调用，不是拟议新 API：

```bash
kimi-slides convert path/deck.pptx -o path/output/
kimi-slides check path/deck -p 1,3 -s all --level keep
kimi-slides check path/deck -p 2-10 --level auto
kimi-slides screenshot path/deck -p 1,3,5 -o path/screenshots/
```

- `convert` 接受 PPTX，`-o/--output` 为输出目录；默认同级去扩展名目录。产生 `.pptd`、`pages/*.page`、`media/<hash>.png`。
- `check` 与 `screenshot` 接受含 `.pptd` 的目录；页码从 1 开始，支持单页、逗号列表、闭区间。
- `check -s/--severity` 支持 `all/error/warning` 或具体 issue 列表；`keep` 只检查，`auto` 尝试确定性修复。
- `auto` 包括简单类型转换、删除非法可选字段、**删除不能安全修复的非法元素**和重写 YAML。原文称 safe repair 不意味着只会移动文字；后续必须审阅修复 diff，怎样留版本与回滚待决策。
- 输出为 `[fixed: false][Error:InvalidType] ... id="..." ...` 一类文本。没有公开 JSON 模式、退出码约定、DPI 参数或 `export` 命令。
- 截图默认输出同级 `<deck>-screenshots/`，典型文件为 `pages/page-1.png`。

基础检查的 14 类：`YamlParseError`、`FileReadError`、`MissingField`、`InvalidType`、`OutOfRange`、`InvalidTheme`、`PageNotFound`、`UnknownField`、`BoundsOutside`、`SrcNotFound`、`TextOverflow`、`TextUnderFill`、`TextOcclusion`、`TextDrift`。

格式文档还列出图表问题：`DuplicateColumnError`、`EmptyColumnError`、`RowLengthError`、`UnknownColumnError`、`NonNumericValueError`、`InvalidValueError`、`StackModeMismatchError`、`WaterfallTotalMismatchWarning`、`CyclicGraphError`。完整实现不能只覆盖基础 14 类。

## 3. 不能被六类布局替代的文档模型

| 能力组 | 必须保留的契约 |
|---|---|
| 主文档 | YAML 1.2；`version: v2`、size、theme、customFonts、pages；坐标左上原点，px，72 PPI 下等于 pt。 |
| 页面 | pageType 是分类标签，不决定版式，允许自定义字符串；background、notes、elements、animations。 |
| 元素身份 | 页内唯一 elementId、七类 elementType、任意 bounds；数组后项图层更高。 |
| 样式 | HEX6/HEX8/主题引用；中英字体；边框、渐变、图片填充、阴影；按文本/表格/图表各自的优先级合并。 |
| 富文本 | p/span/strong/em/u/s/sup/sub/a/ul/ol/li 和明确支持的样式；列表、行高/字距、链接、LaTeX；不支持任意 HTML/CSS。 |
| 几何 | 177 种 shapeName 与 OOXML adjustments；自定义 viewBox/path；shape 不内嵌文字，另建 text；线条/箭头/曲线。 |
| 图片 | 裁剪顺序 `crop→fit→cropShape`，cover/contain/fill 与旋转翻转透明度；设计规则禁止拉伸变形，但不能删除格式的 fill 语义。 |
| 图标 | Font Awesome 7.x Free，`fas/far/fab:name`，颜色、边框、阴影及变换。 |
| 表格 | columnWidths/rowHeights 是比例，分别合计 1；合并格被覆盖位置从数组省略，不用 null 占位；逐格边框和行列优先级。 |
| 图表 | 数据列/行、encode、series、axes、legend、dataLabels、seriesDefaults、色阶和混合类型规则；不同图表限制各异。 |
| 动画 | effect、trigger、elementId、direction、durationMs、delayMs、easing、repeat、path/color/amount；数组顺序与点击组语义。 |

13 类图表：`bar / line / area / scatter / bubble / candlestick / pie / radar / waterfall / heatmap / treemap / sunburst / sankey`。

22 个动画效果：`appear / fade-in / fly-in / zoom-in / wipe-in / float-in / peek-in / rise-in / pulse / grow-shrink / spin / teeter / fill-color / transparency / color-pulse / disappear / fade-out / fly-out / zoom-out / wipe-out / float-out / motion-path`。

重要语义包括：

- 文本继承顺序、表格边框 null 的清除语义、固定行高优先、`wrap:false` 允许超框而非自动缩字。
- 图表默认配色与系列覆盖规则、对象一层浅合并/数组整替换；seriesDefaults 的类型范围、禁止 type/encode。
- 柱/线/面积/散点/气泡混用规则；蜡烛图混用范围；其他图种限制、同类型单堆叠组、主次轴对应关系、雷达共享 category。
- 图表数字格式只支持列举的 Excel 子集；系列 fill 不支持 ImageFill；Sankey 只允许 DAG。
- 表格/图表没有整对象旋转、翻转及全局 opacity 的公开支持；目录注释中的 video 不构成视频元素契约。
- 自定义字体 Google Fonts 只支持默认字重；字体名需精确匹配，CDN 路径空格需编码；缺字回退/导出嵌入细节未公开。

以上细节的完整字段表和全部示例见 [pptd.md](original/reference/pptd.md)、[shapes.md](original/reference/shapes.md)、[fonts.md](original/reference/fonts.md)，不能以本表取代 schema。

## 4. 必须保留为未知的 Kimi 内部实现

目前没有证据确认其使用 Canvas、SVG、WebGL、浏览器打印、LibreOffice、某个 OOXML 库、ECharts 或特定对象存储。也没有公开以下契约：

1. CLI 包的安装来源、版本、可分发二进制、服务依赖、反向导出命令。
2. 编辑器加载/保存/导出 API、批注 schema、选区或撤销重做协议。
3. 复杂图表、公式、动画、字体在 PPTX 中的表达方式和可编辑性/保真矩阵。
4. 精确文字测量、遮挡阈值、检查容差、渲染和导出差异处理。
5. 图片/字体资源缓存、版本快照算法、文档生命周期与存储结构。
6. 研究、搜索、视觉、图片生成与编辑器如何编排，具体调用什么模型。

因此，可以完整保存和逐项实现公开行为；不能通过复制 Markdown 获得这些私有封装。用户已确认配套引擎无法取得，后续讨论按公开规范独立实现所需能力，不再依赖取得Kimi工具或猜测私有API。独立实现可以复用通用库，具体库及兼容方案尚未选择。
