# PPTD v2 编辑、呈现与 PPTX 适配候选评估

研究日期与全部网络资料访问日期：**2026-10-09**。范围：D03 编辑器与 D04 呈现、导入、导出的联合可行性研究。**本文不作最终技术选型，也不代表候选已通过运行验收。**

本文记录资料/源码研究。并行开展的自建路线子集实测另见[联合选型验证](engine-evaluation.md)；两者证据范围不同，下面的“未运行”不否定该独立实验，也不表示其他候选已经经过同等实测。

前提来自 [README](README.md) 与 [gap-and-decisions.md](gap-and-decisions.md)：PPTD v2 是唯一权威文档；内部编辑对象、渲染结果和 PPTX 均由它派生。Kimi 配套引擎无法取得，本次没有获取或调用 Kimi 私有引擎、接口或 CLI。

本次用已发现的 `MiniMax.web_search` 查找公开资料，再直接读取上游官方文档、GitHub 源码和发布元数据。搜索摘要、第三方转载和 README 中作者估计的保真百分比均不作为实测证据。没有安装或运行下述候选编辑器，没有执行用户样例的导入、GUI 编辑或导出往返；本文所有运行时保真结论均标为待测。

## 1. 证据级别与版本基线

- **源码确认**：在注明版本/提交的实现或类型中发现具体行为；只能证明该代码路径，不能证明最终 Office 显示结果。
- **文档确认**：上游明确描述能力或限制；不能替代夹具实测。
- **工程判断**：根据接口和模型作出的整合推论；尚未实现。
- **待测/未知**：尚无运行证据，或公开资料不能证明符合 PPTD 契约。

| 对象 | 本次核实的基线 | 版本边界 |
|---|---|---|
| PPTD | 本仓库 [原始规范](original/reference/pptd.md)，1,644 行；文档模型版本 v2 | 契约基线，不是已执行引擎。原文“无损”与主 skill 的有损提示存在 C01 歧义，不能据此承诺无损。 |
| PPTist | `package.json` 为 **2.0.0**；master 提交 **`1d5dc853f77e99c0d3d9485ea9e52774f870ee78`**，提交时间 2026-10-08；[E01–E04](#证据索引) | 源码与依赖范围已读。`pptxgenjs ^3.12.0`、`pptxtojson ^2.2.0` 是依赖范围，不是本次安装解析版本。 |
| PptxGenJS | 发布 **v4.0.1**，GitHub 发布时间 2025-06-26；该 tag 的 README 与 `gen-xml.ts`，以及访问日版本号为 4.0.1 的 package/types；[E05](#证据索引) | 未安装运行；不要把 PPTist 使用的 3.x 当成此 4.x。 |
| python-pptx | PyPI 发布 **1.0.2**；官方在线文档仍显示 **1.0.0 documentation**；[E06](#证据索引) | 文档和包版本不一致，具体补丁版本必须在验证环境锁定。 |
| ONLYOFFICE Docs | GitHub latest release 返回 **v9.4.0**，发布时间 2026-05-19；读取该 tag 的 Readme 与 LICENSE；[E08](#证据索引) | API 与 Help Center 是访问日动态文档，不能自动视为全部适用于 v9.4.0 的已运行证据。 |
| Collabora Online / CODE | `online` 仓库 main 提交 **`5bce075cee48496de3e817ea73d76fd2a37d289e`**，2026-10-06；官方源码镜像 `online.mirror` main **`c5ef0c56cbd49d2114277dc8f31196a9481eb32c`**，2026-10-09；[E12](#证据索引) | 未锁定/安装服务端发行构建。GitHub latest release 返回 `25.04.7-mobile`，因此没有将它冒充 CODE 服务端版本。 |

## 2. 不能被候选默认模型取代的 PPTD 契约

| 契约 | 原文位置 | 对选型的直接影响 |
|---|---|---|
| YAML 1.2、多文件入口、相对资源路径、自包含项目；72 PPI 下 `1px = 1pt` | `pptd.md` L9–16、L127–148、L317–368 | Office 的 EMU、网页 CSS px 与候选画布坐标需显式转换。`.page` 不能单独成为项目入口。 |
| 文本/表格/图表各自的继承、默认值、浅合并语义；主题引用 | L18–123、L370–451 | 将当前解析后的颜色、字体全部固化回文档，会破坏主题和后续编辑语义；仅“看起来一样”不足。 |
| `FontFamily = string \| {latin, ea}`；段落/行内样式优先级；固定行高、竖排、分散对齐 | L161–182、L515–655 | 中文可显示不等于中西文分字体、断行和布局一致。编辑器清洗 HTML 时必须保留规范允许的语义。 |
| **七类**元素：text / shape / line / image / icon / table / chart | L499–510 | 公式不是第八类元素。视频、音频和 group 也不是已公开的 PPTD 元素类型。 |
| LaTeX 通过 `\(...\)` 嵌入文本或表格富文本，只继承颜色和字号 | L657–672、L900–925 | 候选的独立公式框、SVG 图片或 OMML 均需明确映射；不能把可编辑 LaTeX 改成不可逆图片后静默保存。 |
| 177 预置形状及 OOXML adjustments；自定义 SVG path；shape 无内嵌文字 | L674–744、`shapes.md` | 候选的“形状内文本”不能直接作为 PPTD shape 保存；需要独立 text。自定义形状须保留参数与路径。 |
| 图像处理顺序 `crop → fit → cropShape`；允许负裁切透明扩展 | L784–843 | 一般图片编辑器的裁切框不等价，尤其是负裁切和形状遮罩。 |
| Font Awesome **7.x free** 图标身份与样式 | L845–878 | 仅保存 SVG 外观会丢失 `iconName`；图标资源及许可是独立依赖。 |
| 表格比例宽高、合并格省略被占位置、逐边边框与样式优先级 | L882–954 | 常见二维矩阵、占位格和候选表格样式要双向转换；普通单元格样式不足以证明富文本支持。 |
| **13 类**图表、统一 cols/rows + encode、缺值、混合类型、双轴及各类限制 | L956–1517 | 不能把 PPTD 数据降成简单 `labels + series:number[][]` 后当主模型。热图、层级图、Sankey 等需单独出口策略。 |
| **22 个**动画效果、触发组、时长/延迟/缓动/重复、颜色/透明度和运动路径 | L1521–1644 | 网页可播放动画不等于可以写出 PowerPoint 时间树；“支持动画”必须逐参数验证。 |

规范中已登记的 C05–C09 等歧义仍以 [差距文档](gap-and-decisions.md) 为准；候选库的默认行为不能自动成为歧义答案。

## 3. 三条路线的核心比较

| 维度 | A：自建 React + DOM/SVG；Python/PptxGenJS + OOXML 适配 | B：复用开源演示编辑器，以 PPTist 为代表 | C：嵌入 Office 套件，以 ONLYOFFICE / Collabora 为代表 |
|---|---|---|---|
| PPTD 权威模型 | **工程判断：最直接可控制**。加载和编辑命令以 PPTD 为入口，DOM、编辑状态和导出对象均为派生。 | **工程判断：需要改造**。PPTist 的 Slide/PPTElement、Pinia store 是自己的编辑模型；不能默认全量序列化该 store 覆盖 PPTD。 | **工程判断：冲突最明显**。套件打开、编辑和保存的是 Office 文件；嵌入 UI 不会获得 PPTD 原生编辑能力。 |
| 已有 GUI 能力 | 选区、拖拽、缩放、撤销、富文本编辑、表格及动画 UI 等均要组合/实现。 | 有现成选择、对齐、缩放、分组、撤销、富文本、图表表格及演示交互，文档与源码可见。 | 有 Office 编辑、审阅/协同等完整产品界面；功能仍受产品版本和格式限制。 |
| PPTD 语义覆盖 | 可以按规范完整设计，但目前只是路线，**不是已实现能力**。 | 已有明确差集：图表、公式、动画字段、字体模型、图标身份、主题继承等。 | 支持 Office 对象不等于支持 PPTD；PPTD→PPTX→编辑→PPTD 的映射是额外工程。 |
| 预览与截图 | 可共享同一浏览器呈现实现，等待字体和资源完成后截图；不保证与 PowerPoint 排版一致。 | 页面和导出图片可复用现成呈现，但规范语义仍要补；PPTX 是另一条转换链。 | 可用套件呈现/转换 Office 文件；仍需比较套件预览、导出 PDF 与 PowerPoint。 |
| PPTX 导入 | Python 可读 OOXML，另有 MIT 的 pptxtojson 可研究；没有现成 PPTD 导入器。 | 导入经 pptxtojson 后转换成 PPTist 模型，源码存在信息降级；不是直接导入为 PPTD。 | 产品原生工作流较接近 PPTX，但**转换回 PPTD**依然没有现成契约。 |
| PPTX 导出 | 基本文字、图像、形状、表格、部分原生图表有库支持；混合/复杂图表、公式和动画需专门适配。 | 用 PptxGenJS；公式、特殊形状存在图片化，动画出口缺口没有消失。 | 能保存 PPTX，但需先把 PPTD 转成套件可编辑文件，且往返稳定性待测。 |
| 本项目接入 | React/TS 和 Python 与现有栈接近；主要成本在编辑和排版、映射实现。 | Vue 3 + Pinia + ProseMirror；可独立嵌入或移植/重构，均增加边界适配。 | 独立 Office 服务、文件存储接口、会话和部署资源；不只是引入一个 React 包。 |
| 本次状态 | 公共库能力与明确缺口已核实；联合路径未实测。 | 类型、导入/导出关键路径已核实；联合路径未实测。 | 官方产品/API/许可已核实；没有部署与样例测试。 |

## 4. 路线 A：自建编辑呈现与 OOXML 适配

### 4.1 可被证据支持的能力

- **PptxGenJS 是 PPTX 生成库**，官方 README 展示 Node/React/浏览器使用和文字、表格、形状、图片、图表、SVG、亚洲字体；公开类型提供 `addText`、`addShape`、`addImage`、`addTable`、`addChart` 等入口。[E05] 这不是现成编辑器、PPTD 解析器或 PPTX 排版渲染器；本次未发现其公开 API 提供通用 PPTX 导入。
- **python-pptx 可以创建、读取和修改 PPTX**，官方文档声明无需安装 PowerPoint，支持文字、图片、形状、表格和多类图表。[E06] 文档所称“round-trip any Open XML presentation”描述保留原 Office 文件的能力，不能推导为经 PPTD 重建仍无损。
- **python-pptx 官方明确不支持新建 multi-plot 混合图表**，但可访问已有混合图表的多个 plot；文档提供 scatter 和 bubble 示例。[E06] 因此用已有模板保留混合图表、从 PPTD 新建混合图表是不同能力。
- **PptxGenJS `addChart` 的类型接受多图表配置**；其 `CHART_NAME` 只有 area / bar / bar3D / bubble / doughnut / line / pie / radar / scatter。[E05] 不能据此宣称 13 类 PPTD 图表全部有原生导出。
- **PptxGenJS 字体接口仍需适配**：v4.0.1 `gen-xml.ts` 的表格段落结束属性把同一个 `fontFace` 写入 `a:latin`、`a:ea`、`a:cs`。[E05] 这条源码路径不足以直接表达 PPTD `{latin, ea}` 的不同值，需检查所有文本写入路径并验证 OOXML 修正。

### 4.2 必须独立建设的部分

| 项目 | 可行实现方向，尚未选库/实现 | 未解决的问题 |
|---|---|---|
| PPTD 核心 | YAML 读写与节点身份、schema、样式解析、资源解析、编辑命令；保留主题引用和没有被此次编辑触及的字段 | 如何保留 YAML 表达与语义；规范歧义；所有修改入口与版本的原子保存 |
| 自由坐标与七类元素 | React 管理编辑交互，DOM 呈现富文本，SVG 呈现路径、线条、图标及部分图表 | SVG geometry/adjustments 全目录、裁切顺序、表格合并和覆盖顺序；不能只实现几个常用形状 |
| 中文富文本 | 从规范富文本解析出语义，字体加载后测量；展示和截图共享呈现 | 浏览器 CSS px 与 72 PPI 的转换、中西文双字体、IME、断行、竖排、固定行高、分散对齐、字体替换 |
| 公式 | 浏览器公式排版器作为派生渲染；PPTD 保留原始 LaTeX | 与正文混排基线、公式只继承颜色/字号、LaTeX↔OMML 子集；SVG 图片不等于 Office 原生公式 |
| 图表 | 按 PPTD 数据与继承规则适配图表渲染器或 SVG；导出层单独决定原生对象映射 | 13 类逐项出口，混合/双轴、null、总计、树/图约束；库没有直接类型时不可静默改图类 |
| 动画 | 浏览器按 PPTD 页面序列执行；PPTX 层写入 OOXML 时间树、对象 ID 与运动路径 | 22 效果与全部参数、首次自动播放、组边界、持久颜色/透明度；库的默认导出没有证明覆盖 |
| PPTX 导入 | 读取 OOXML/库对象，解开母版和版式继承，建立到 PPTD 的可定位映射；原文件作为旁路证据保存 | 未知/不支持 Office 对象、占位符、嵌套组、图表、公式和时序的报告与修复；原文件不是第二个主模型 |

PPTX 中可编辑性必须逐对象记录：**原生 Office 对象、可编辑矢量组合、SVG 图片、位图**是四种不同结果。某类 OOXML 不存在等价原生对象或库未实现，不代表可直接降低交付要求；应先记录缺口及可选处理，由联合验证决定可接受边界。

## 5. 路线 B：PPTist 的实质复用范围

### 5.1 已经核实的模型差异

| PPTD 要求 | PPTist 源码/文档事实 | 适配影响 |
|---|---|---|
| 七类元素，icon 保留身份 | `ElementTypes` 有 text/image/shape/line/chart/table/latex/video/audio，没有 icon。[E02] | 图标可以有视觉载体，但需要保留 `iconName`；不能用候选类型表替换 PPTD schema。 |
| 富文本与字体继承 | `PPTTextElement` 有 HTML 字符串、`defaultFontName:string`、lineHeight、wordSpace、vertical 等；`TextAlign` 不含 distributed。[E02] | 基础富文本有复用价值，但双字体、主题链、固定行高和分散对齐不能直接假定支持。 |
| LaTeX 是富文本的一部分 | `PPTLatexElement` 为独立元素；PPTX 导入的 `math` 分支创建 `type:'image'`；导出的 `latex` 分支生成 SVG 并调用 `addImage`。[E02–E04] | 现成链路不是正文内可往返编辑的公式链路。pptxtojson 已输出部分 `latex`，不代表 PPTist 使用了它。 |
| 13 类及混合图表 | `ChartType` 为 bar/column/line/pie/ring/area/radar/scatter；`ChartData` 为 labels/legends/series:number[][]；没有 PPTD encode/cols/rows 结构。[E02] | 名称数量也不一一对应：column 是方向区分、ring 是饼图变体。缺少 bubble/candlestick/waterfall/heatmap/treemap/sunburst/sankey 及通用混合语义。 |
| bubble 大小通道 | PPTX 导入把 `bubbleChart` 与 `scatterChart` 都映射为 `scatter`。[E03] | 存在源码确认的图类语义降级，必须报告和改造；不能只比较截图。 |
| 动画完整参数 | `PPTAnimation` 只有 id/elId/effect/type/duration/trigger；trigger 对应 click/meantime/auto。[E02] | delay、easing、repeat、path、color、amount 没有等价类型字段；触发顺序可借鉴，完整契约仍缺。 |
| 图形与导出可编辑性 | shape 支持 SVG path；类型注释及导出实现对 special 等分支使用图片。[E02、E04] | 自定义路径/渐变等应按分支检查；“形状在画布可编辑”不等于“PPTX 中仍为原生形状”。 |
| 合并富文本表格 | 有 rowspan/colspan 和单元格级样式；`TableCell.text` 为 string，有限 style 字段。[E02] | 不能从 string 类型推断支持 PPTD 全部富文本和逐层样式；需实际编辑/导出夹具。 |

PPTist README 宣称约 **85%+ 导入、95%+ 导出**，同时提醒动画、特殊图表、深层嵌套和高级样式可能不同。[E01] 这些数字没有与本项目夹具绑定，本次不采用为指标。旧 `doc/Q&A.md` 仍写导入尚无合适方案，而当前 README 和 `useImport.ts` 已有导入；本报告以当前代码判断“存在导入功能”，并保留文档陈旧这一证据差异。FAQ 对 PptxGenJS 动画出口不足及样式差异的提醒，与本次关键源码观察一致。[E07]

### 5.2 保持 PPTD 权威需要怎样的改造

以下是工程判断，不是已验证的插件接口：

1. PPTD 原对象与字段必须保存在文档核心；PPTist store 只是可重建的编辑投影。
2. 移动、改字、改色等操作需转换为针对原 PPTD 节点的命令或补丁。仅在投影中保存“未支持字段的旁路副本”，随后整页反向序列化，仍可能破坏继承与复合对象。
3. 对投影暂不能表达的属性，至少完整保留并在 UI 上明确状态；这不等于该属性已经获得编辑和呈现能力。
4. 在正文内公式、复杂图表、主题和动画上，需要扩展编辑模型/呈现/导出，或者绕过相应 PPTist 子系统。工作量不能仅按“加一个 YAML 导入按钮”估算。
5. 该项目是 Vue 应用，不是 React 原生组件库。独立界面嵌入、组件移植、整体 fork 是不同方案，需测焦点、快捷键、资源生命周期和版本升级成本。

## 6. 路线 C：Office 套件的能力与边界

### 6.1 ONLYOFFICE Docs

**文档确认：**公开 Docs API 的 document 配置支持 `pptx` 等演示文件类型，未列出 `.pptd`。编辑结果通过 `callbackUrl` 返回文件 URL，由宿主下载保存；文档说明编辑结果会组装到 `.docx/.xlsx/.pptx/.pdf` 等原生格式，forcesave 同样返回文件。[E09] 因此保存回调不是 PPTD 元素级修改流。

**文档确认：**Presentation Editor 有公式编辑与 Office Math ML 转换说明；动画包含入口、强调、退出、运动路径，并有 on click / with previous / after previous、duration、delay、repeat 和颜色参数。[E10] 这比 PPTist 当前公开动画字段更广，但尚未逐一核实 PPTD 22 效果、缓动和运动路径坐标的对应。

**文档确认的图表边界：**官方演示编辑器帮助列出 Column/Line/Pie/Bar/Area/Stock/Scatter/Radar/Combo，且明确将 **Waterfall 等列为仅能打开的类型**。[E10] “Office 套件支持图表”不能推出 13 类 PPTD 图表全部可创建、可编辑、可往返。Sankey/heatmap/treemap/sunburst 的 PPTD 语义覆盖在本次资料中没有得到完整证明。

**公式资料有内部差异：**同一帮助页同时提供 Unicode/LaTeX 与 Professional/Linear 设置说明，又保留“currently, equations cannot be entered using the linear format”的注释。[E10] 不能只取其中一句承诺任意 LaTeX 输入；需用锁定版本实际测试正文混排和保存后的 OMML。

**未知/待测：**PPTD 样式链和字体引用经 Office 编辑后怎样反解；原元素 ID 如何稳定匹配；主题变更是否被固化；用户新增的 PPTD 未定义 Office 对象如何处理；图表和公式语义是否保留；导出和 PowerPoint 实际播放是否一致。

### 6.2 Collabora Online / CODE

**文档确认：**官方产品页列出 Impress 支持 `.odp/.ppt/.pptx`，源码 README 列出文档渲染引擎、服务端 daemon、渲染进程和浏览器组件。[E12–E13] 它是基于 Office 文档的服务端编辑/呈现体系，不是直接加载 PPTD 的前端画布。

**文档确认：**官方 CODE 安装页要求把 Collabora 地址接入宿主的 WOPI 配置，官方集成页描述 WebSocket、WOPISrc 与有状态文档会话。[E13] 本次未核实完整的当前 WOPI / PostMessage API 表；SDK 两个页面返回反机器人页面，不能把它们算作读到了 API 正文。

**来源变化：**`CollaboraOnline/online` 官方 README 明确表示活跃源码和代码评审已移到 Gerrit，GitHub 原仓库主要保留 issue 和发行构建；官方提供 `online.mirror` 只读镜像。[E12] 本次没有将旧仓库缺失文件误判为该功能不存在，也没有以 mobile release 作为服务端版本。

**未知/待测：**当前服务端构建对 PPTD 所需七元素、富文本混排公式、13 类图表、22 动画的逐项可编辑/可导出矩阵；所有中文字体指标、字体替换、动画播放和 PPTX 往返。仅依据 LibreOffice 技术背景不能宣称这些项目已满足。

### 6.3 对 PPTD 权威性的共同要求

Office 路线可以构造为 `PPTD → 临时 PPTX → Office 编辑 → 解析修改 → PPTD`，但这个箭头图只是待实现架构。准入条件是：能识别改了什么、可定位地写回 PPTD、保留没有改过的语义，并对不可表达修改明确处理。**如果日常编辑直接以保存后的 PPTX 为最终真相、PPTD 只做备份或导出副本，就不符合已确认的 D01。**

可以单独评估 Office 套件作为 PPTX 导入参考呈现、兼容性查看或导出验证工具；这不自动意味着采用它作为主编辑器。两种角色应分开给出验证结果。

## 7. 许可证与部署事实

| 候选 | 已核实许可/发行约束 | 部署依赖与需决策项 |
|---|---|---|
| PptxGenJS | MIT，package 与官方仓库明确。[E05] | Node 或浏览器；生成 PPTX 不要求 Office 安装。实际 package 列有 JSZip、image-size 等依赖，不能把 README 的“zero runtime dependencies”营销句当依赖清单。截图另需浏览器与字体环境。 |
| python-pptx | MIT；官方包与 LICENSE。[E06] | Python；包依赖包含 lxml、Pillow、XlsxWriter 等；没有因此获得渲染/GUI。PPTX 呈现需浏览器或 Office/LibreOffice 等额外引擎。 |
| pptxtojson | MIT，官方 README。[E14] | PPTX→自有 JSON 的解析器，非 PPTD 转换器；公式 LaTeX 文档限定 common structures。若采用必须锁定依赖版本并逐对象核查。 |
| PPTist | 当前源码 AGPL-3.0；README 提供另行商业授权渠道，也提到 2022-05 停止维护的旧 Apache-2.0 版本。[E01、E07] | Vue 3/TS/Vite、Pinia、ProseMirror、ECharts、hfmath、html-to-image；README 要求 Node ≥20。现有 AGPL 版本与旧 Apache 版本不能混用功能结论；商用闭源路线需先明确许可安排。 |
| ONLYOFFICE Docs | v9.4.0 LICENSE 为 AGPLv3 **加附加条款**，涉及署名、修改提示、UI 法律声明、商标；部分非代码内容为 CC BY-SA 4.0。[E08] | 独立 Document Server + 宿主文件 URL/保存回调/令牌；官方 Docker 文档最低建议双核 2 GHz、4 GB RAM、40 GB 可用空间、4 GB swap。商业集成授权、品牌修改权限和具体 edition 限制须按目标方案核实，不能认为 iframe 消除了许可义务。 |
| Collabora Online / CODE | 官方源码 README 写主要为 MPLv2，部分组件另有许可；COPYING 为 MPL 2.0，不能把整套发行物概括为单一许可证。[E12] | 独立有状态服务、引擎、WOPI 宿主、WebSocket/反向代理和字体；Docker/Linux 包可部署。CODE 官方定位测试/家庭/小团队，**不建议生产环境**；正式产品支持/订阅与自构建部署需分别评估。[E13] |

字体和图标资产的许可不随上述代码库许可自动覆盖。PPTD 的字体来源和 Font Awesome 7 免费图标契约需要各自记录来源、可部署性及导出使用条件；本次没有下载、安装字体或图标资产。

## 8. 联合验证应怎样裁定候选，而不是看演示截图

以下是**待执行**的比较条件，不是已完成的测试结果。每条路线使用相同 PPTD 与 PPTX 夹具、固定字体环境、固定浏览器/Office 版本，记录构建哈希与输出文件。

| 验证组 | 最小观察 | 不能冒充通过的情形 |
|---|---|---|
| 权威模型 | PPTD 加载→只改一个文本/坐标→保存→重载；主题引用、未改页面、资源、动画、公式源和图表数据语义均保留 | 只保存编辑器 JSON；把所有继承固化；旁路保存原稿但下一次编辑仍以 PPTX 为主 |
| 七类元素 | 元素 ID、坐标、图层、形状参数、自定义路径、FA 图标身份、图片三步裁切、表格合并均往返 | 截图看似一致却已经整页/整图图片化 |
| 中文富文本 | Latin/EA 双字体、中文标点和中英混排、缺字、列表、上下标、链接、竖排、固定行高、distributed、单元格样式优先级 | 只用默认系统字体测试；浏览器字体尚未加载就测量截图 |
| 公式 | 文本与表格内独立/混排公式加载编辑保存；PPTX 中记录原生 OMML 或明确降级类型 | 可见 SVG 公式就记作“原生可编辑公式通过” |
| 图表 | 13 类分别建表，另加 mixed/双轴/null/堆叠/总计/层级/DAG 样例；编辑数据后检查 PPTD 与导出图表数据 | bubble 变 scatter；复杂图表变图形或图片但报告仍写 native chart |
| 动画 | 22 效果与参数组；第一次自动播放、withPrevious/afterPrevious、重复、路径、效果后保留状态；检查 PPTX 时间树并在目标播放器播放 | 只在网页播放成功；只检查 `p:timing` 存在而不播放 |
| PPTX 导入 | 普通 Office 文档和复杂母版/嵌套组/公式/混合图表/动画样本；导入报告能定位每个丢失/转换对象 | 用作者总体保真率推定通过；解析库忽略对象却没有报告 |
| 导出与呈现 | 同一 PPTD 的编辑器截图、导出图片、PPTX 在目标 Office 的截图和语义对象检查 | 只有 `pptx` 文件能解压或 LibreOffice 能打开就认为全部可编辑/保真 |
| 会话与回写 | 对 Office 路线额外测试同文档版本、保存回调、宿主并发修改、ID 匹配、不可表示新增对象 | 套件成功保存 Office 文件就认为 PPTD 已一致更新 |

每条结果至少有：契约项、输入、执行版本、步骤、预期语义、实际 PPTD diff、导出对象类型、截图/播放证据及已知差异。静态源码已经证明的差集可优先测，以尽早判断改造量。

## 9. 当前可用于下一轮决策的结论

1. **没有发现可直接替换 Kimi 引擎、又原生满足全部 PPTD v2 的现成候选。**这仅限本文调查对象，不是对整个开源生态不存在此实现的断言。
2. **A 允许直接围绕 PPTD 实现语义，但编辑器、排版和 OOXML 完整映射都需要实作。**python-pptx/PptxGenJS 只能减少部分基础对象适配工作。
3. **B 能复用成熟编辑交互，但已确认有实质模型差集。**公式图片化、图表模型、动画参数、主题与字体语义都需要改造；不能以 AGPL 项目现成 UI 当完整迁移证明。
4. **C 的 Office 文件操作能力更完整，但不能仅凭嵌入 API 满足 PPTD 权威性。**必须先证明 Office 编辑结果能受控回写 PPTD；也可以只承担兼容查看或验证角色。
5. **当前尚不能报告哪条路线“已跑通”或提供保真百分比。**最终选择应由共同夹具的往返结果、未覆盖项的改造范围、许可与部署条件决定，不由本报告单方面决定。

## 证据索引

下列均为一手来源。全部访问于 2026-10-09；未标 tag 的动态网页按访问日快照理解。

- **E01 — PPTist 项目与依赖。**[README](https://github.com/pipipi-pikachu/PPTist/blob/1d5dc853f77e99c0d3d9485ea9e52774f870ee78/README.md)、[package.json](https://github.com/pipipi-pikachu/PPTist/blob/1d5dc853f77e99c0d3d9485ea9e52774f870ee78/package.json)。涵盖技术栈、功能清单、作者保真估计和商业渠道。
- **E02 — PPTist 模型。**[src/types/slides.ts](https://github.com/pipipi-pikachu/PPTist/blob/1d5dc853f77e99c0d3d9485ea9e52774f870ee78/src/types/slides.ts)。本次重点核查 ElementTypes、PPTTextElement、ChartData/ChartType、TableCell、PPTLatexElement、PPTAnimation。
- **E03 — PPTist 导入。**[src/hooks/useImport.ts](https://github.com/pipipi-pikachu/PPTist/blob/1d5dc853f77e99c0d3d9485ea9e52774f870ee78/src/hooks/useImport.ts#L876)。`math`→image L876–888；图表类型转换 L1223–1285。
- **E04 — PPTist 导出。**[src/hooks/useExport.ts](https://github.com/pipipi-pikachu/PPTist/blob/1d5dc853f77e99c0d3d9485ea9e52774f870ee78/src/hooks/useExport.ts#L948)。LaTeX→SVG→addImage L948–970；形状图片分支 L626 起；原生图表导出 L762–852。
- **E05 — PptxGenJS。**[v4.0.1 发布](https://github.com/gitbrent/PptxGenJS/releases/tag/v4.0.1)、[README](https://github.com/gitbrent/PptxGenJS/blob/v4.0.1/README.md)、[类型入口](https://github.com/gitbrent/PptxGenJS/blob/v4.0.1/types/index.d.ts)、[OOXML 写入源码](https://github.com/gitbrent/PptxGenJS/blob/v4.0.1/src/gen-xml.ts#L1312)、[package.json](https://github.com/gitbrent/PptxGenJS/blob/v4.0.1/package.json)。文档能力、MIT、图表类型和字体写入路径。
- **E06 — python-pptx。**[官方概述](https://python-pptx.readthedocs.io/en/latest/)、[Working with charts](https://python-pptx.readthedocs.io/en/latest/user/charts.html)、[PyPI](https://pypi.org/project/python-pptx/)、[LICENSE](https://github.com/scanny/python-pptx/blob/master/LICENSE)、[pyproject](https://github.com/scanny/python-pptx/blob/master/pyproject.toml)。文档明确 multi-plot 创建限制，读取/生成和依赖/许可已核实。
- **E07 — PPTist 限制与许可。**[Q&A](https://github.com/pipipi-pikachu/PPTist/blob/1d5dc853f77e99c0d3d9485ea9e52774f870ee78/doc/Q%26A.md)、[LICENSE](https://github.com/pipipi-pikachu/PPTist/blob/1d5dc853f77e99c0d3d9485ea9e52774f870ee78/LICENSE)。旧导入说明不用于否认当前源码能力；导出限制与许可证单独核实。
- **E08 — ONLYOFFICE 版本与发行。**[v9.4.0 发布](https://github.com/ONLYOFFICE/DocumentServer/releases/tag/v9.4.0)、[Readme](https://github.com/ONLYOFFICE/DocumentServer/blob/v9.4.0/Readme.md)、[LICENSE](https://github.com/ONLYOFFICE/DocumentServer/blob/v9.4.0/LICENSE)。组件构成、产品版本和附加许可条款。
- **E09 — ONLYOFFICE 接入与保存。**[document config](https://api.onlyoffice.com/docs/docs-api/usage-api/config/document/)、[Saving file](https://api.onlyoffice.com/docs/docs-api/get-started/how-it-works/saving-file/)。文件类型、URL、callbackUrl、forcesave 与原生格式保存。
- **E10 — ONLYOFFICE 演示功能。**[Insert equations](https://helpcenter.onlyoffice.com/docs/userguides/presentation_editor/InsertEquation.aspx)、[Insert charts](https://helpcenter.onlyoffice.com/docs/userguides/presentation_editor/InsertCharts.aspx)、[Adding animations](https://helpcenter.onlyoffice.com/docs/userguides/presentation_editor/AddingAnimations.aspx)。公式、图表编辑/仅打开差别、动画时序及运动路径。
- **E11 — ONLYOFFICE 部署。**[Installing Docs Community using Docker](https://helpcenter.onlyoffice.com/docs/installation/docs-community-install-docker.aspx)。资源、Linux/Docker、持久化与令牌配置。
- **E12 — Collabora 源码来源与许可。**[原仓库说明](https://github.com/CollaboraOnline/online/blob/5bce075cee48496de3e817ea73d76fd2a37d289e/README.md)、[官方镜像 README](https://github.com/CollaboraOnline/online.mirror/blob/c5ef0c56cbd49d2114277dc8f31196a9481eb32c/README.md)、[COPYING](https://github.com/CollaboraOnline/online.mirror/blob/c5ef0c56cbd49d2114277dc8f31196a9481eb32c/COPYING)。源码迁移、引擎/服务/浏览器组件、主要 MPLv2 与混合许可证说明。
- **E13 — Collabora 产品与部署。**[CODE](https://www.collaboraonline.com/code/)、[Collabora Online](https://www.collaboraonline.com/collabora-online/)、[集成介绍](https://www.collaboraonline.com/integrate-collabora-online/)。PPTX/ODP、WOPI 接入、有状态部署和 CODE 非生产建议。
- **E14 — pptxtojson。**[官方 README](https://github.com/pipipi-pikachu/pptxtojson/blob/master/README.md)。MIT、数据格式、math.picBase64 与仅支持常见结构的 LaTeX 字段；本文未独立验证其转换正确性。

检索中遇到的限制：Collabora SDK 的 `how_to_integrate.html` 与 `postmessage_api.html` 返回反机器人页面；部分初次 HTTP 请求超时后改从官方 raw/tag 获取。上述失败不被解释为功能不存在。未使用搜索摘要中的旧版百分比或第三方生成文档填补这些证据空白。
