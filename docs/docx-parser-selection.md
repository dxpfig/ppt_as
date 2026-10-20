# DOCX 解析引擎评估

> 评估日期：2026-10-19。目标是让 Studio 的“添加文本资料”读取 `.docx` 正文，并作为 PPT 生成参考资料，不要求还原 Word 版式或做截图转文字。

## 结论

当前场景选择 **Mammoth 1.13.0**，在浏览器内调用 `extractRawText`。

它适合本页的核心原因是：

- 纯 JavaScript，可在浏览器离线运行，无需 Java、LibreOffice、Python 服务或系统字体。
- 直接读取 Office Open XML 的 WordprocessingML，保留正文的阅读顺序，并支持标题、列表、表格、脚注、文本框等常见结构。
- 文本来自 XML 的 Unicode 字符串，中文汉字、中文标点、全角字符和混排数字不经过额外编码转换，不需要中文分词模型。
- 输入是 ZIP 形式的 `.docx`，输出只需纯文本，避免引入 HTML 清洗和大体积渲染逻辑。
- 动态加载解析器，只有选择 DOCX 时才下载对应前端代码。

需要注意：Mammoth 是文本/语义抽取器，不是版式渲染器。字体、颜色、页眉页脚、浮动对象和图片内文字不应作为当前“参考资料”的正文来源；扫描版 DOCX 若正文实际是图片，需要后续 OCR 能力。

## 候选对比

| 引擎         | 部署            | DOCX 正文与结构                             | 中文影响                               | 适用性                                                                  |
| ------------ | --------------- | ------------------------------------------- | -------------------------------------- | ----------------------------------------------------------------------- |
| Mammoth      | Node / 浏览器   | 好；段落、标题、列表、表格、脚注、文本框    | 原生 Unicode，无分词依赖               | **选型**。轻量、离线、与当前前端读取流程一致                            |
| officeparser | Node / 浏览器   | 好；支持 Office、PDF 等多种格式             | 原生 Unicode                           | 能力更强但体积约 31 MB，包含 PDF、OCR 等依赖；当前只处理 DOCX，收益不足 |
| docx-preview | 浏览器          | 以渲染为主，不提供稳定的纯文本抽取接口      | 渲染依赖字体                           | 适合 Word 预览，不适合作为资料抽取器                                    |
| python-docx  | Python 后端     | 段落和表格稳定；更多 Word 语义需自行补      | 原生 Unicode                           | 适合已有后端上传链路；当前页面是本地即时读取，不引入新服务更简单        |
| Unstructured | Python 后端     | 最丰富，可输出带类型的文档元素，适合 RAG    | 原生 Unicode；切片可结合中文 tokenizer | 适合后续知识库和语义分块，不作为当前轻量流程                            |
| Apache Tika  | Java 服务       | 格式覆盖最广，支持元数据和 OCR              | 原生 Unicode                           | 企业文档网关方案，运行和资源成本高                                      |
| textract     | Node + 外部工具 | 支持 DOCX，但依赖较旧，部分格式依赖外部程序 | 本身无中文转码问题                     | 维护活跃度和浏览器适配不适合当前项目                                    |

## 中文支持说明

DOCX 中正文通常位于 `word/document.xml` 的 `<w:t>` 节点，文件本身声明 XML 编码，现代 Word、WPS 和 LibreOffice 保存的文档一般使用 UTF-8。中文是否正确，取决于 XML 解码和字符串拼接，而不是是否内置中文词典。因此 Mammoth、officeparser、python-docx、Tika 在这一层面的正文抽取能力相当。

中文体验的真正差异来自后处理：

- 是否跨 `run` 拼接同一段落；Mammoth 会按段落生成连续文本。
- 是否保留表格和列表的阅读顺序；Mammoth 支持这些常见结构。
- 是否错误地在汉字之间插入空格；当前实现只规范换行和行尾空白，不改写字符。
- 是否支持中文分句或检索切片；这属于后续 RAG 层，不应让解析器承担。

## 当前实现边界

- 支持 `.docx`，不支持旧版二进制 `.doc`。
- 单个资料文件默认上限 50 MB；解析后的正文按当前模型上下文动态计算上限，默认最多 200 万字符。
- 只加入正文文本，不把图片、颜色、字体或页面坐标传给模型。
- 加密、损坏或仅含扫描图片的 DOCX 会提示无法读取；扫描件需要后续接入 OCR。

## 参考

- [Mammoth npm](https://www.npmjs.com/package/mammoth)
- [Mammoth 源码与 README](https://github.com/mwilliamson/mammoth.js)
- [officeparser](https://www.npmjs.com/package/officeparser)
- [docx-preview](https://github.com/VolodymyrBaydalka/docxjs)
- [python-docx](https://python-docx.readthedocs.io/)
- [Unstructured](https://docs.unstructured.io/)
- [Apache Tika](https://tika.apache.org/)
