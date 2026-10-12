"""pptx 包的共享读取层：部件普查 + 幻灯片文本索引。

三个脚本共用这一份，避免「prep 说有 12 段、edit 只认 11 段」这种各写一份必然出现的漂移。

**只用标准库**。刻意不依赖 python-pptx：那一层保存时会把整个包重新序列化，未建模的部件
（加载项私有数据、内嵌工作簿、内嵌字体）能不能原样活下来取决于它的实现；本 skill 的全部
价值就是「除了你点名的那几个字，别的一个字节都不动」。

## 定位与改写必须用**同一个枚举**（2026-08-26 跨模型审阅实测的教训）

第一版用 ElementTree 只遍历 `p:sp` 建段落索引，改写却按整张 slide XML 里所有 `<a:t>` 的
出现序号落刀。于是**表格**（`p:graphicFrame/a:tbl`）里的文字把后面每一个下标顶偏一位：
用户点名改标题，新文字被写进表格单元格，标题原封不动，而 prep / edit / verify 三道全部
返回成功——正是这个 skill 声称要消灭的静默错误交付。

现在两边都以「文档序里的**每一个** `a:p`」为准（表格单元格、页眉页脚里的段落一并计入），
并在建索引时用 `assert_run_index_alignment` 硬校验「树里的 `a:t` 个数 == 字节里匹配到的
`<a:t>` 个数」——不等就**拒绝**，绝不带着可能错位的下标落刀。

## 页号是放映顺序，不是部件名里的数字

`ppt/slides/slide7.xml` 的 7 只是部件名。用户在 PowerPoint 里拖动页序或删页后，
`slide7.xml` 完全可能是放映的第 3 页——而用户说的「第 3 页」永远是放映顺序。所以
`read_slides` 按 `ppt/presentation.xml` 的 `p:sldIdLst` 顺序、经 `r:id` →
`presentation.xml.rels` 解析到真实部件，页号 = 该列表里的位置（从 1 起）；
`SlideDoc.part` 保留真实部件名供 `edit.py` 写包。按文件名排序曾让「改第 1 页」改到
别的页，prep / edit / verify 共用同一错误索引，全部报告成功。列表读不出直接拒绝
（fail-closed），不回退到文件名排序。

## 软换行（`a:br`）算行分隔，不是零宽度

段内的 `<a:br/>` 在 PowerPoint 里是实实在在的一次换行。早先把一段的文字按
`iter(a:t)` 直接拼起来，两行「第三季度」「营收同比」变成 `第三季度营收同比` ——
模型据此写 `find` 必然匹配不上，而按 id 整段改写又会把新文字全塞进第一个 run、
留下一个孤零零的 `<a:br/>` 变成空行。所以段落文本用 `\\n` 表示 `a:br`，改写时
**按行对位**：新文本的行数必须与原段一致，第 i 行落进第 i 个换行槽。
"""

from __future__ import annotations

import io
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
#: `r:id` 属性所在的命名空间（关系引用，与上面那个「关系清单文件」的命名空间不同）。
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
# `<a:t>文本</a:t>` 与自闭合 `<a:t/>`。
#
# 前缀写死 `a:` 是**有意的**：这里按字节落刀，而前缀不属于 XML 语义，生成方完全可以用别的
# 前缀声明同一个 namespace。所以不靠正则去猜，而是靠 assert_run_index_alignment 把「树里数
# 出来的 t 节点数」与「这里匹配到的个数」对上；对不上就拒绝改写（fail-closed）。
# `<a:tbl>` / `<a:tc>` 不会被误匹配：`<a:t` 之后必须紧跟空白、`>` 或 `/>`。
RUN_TEXT_RE = re.compile(rb"<a:t(?P<attrs>\s[^>]*?)?(?:/>|>(?P<text>.*?)</a:t>)", re.S)

#: 管线不建模、且导出器自己不会产生的部件类别（与桌面端 slides-addin-scan 同口径）。
UNMODELLED_PREFIXES = ("ppt/tags/", "ppt/embeddings/", "ppt/webextensions/")
#: 另外单独盯住的几类：内嵌字体（丢了换机器就掉字体）、图表、媒体、母版版式。
EXTRA_PREFIXES = ("ppt/fonts/", "ppt/charts/", "ppt/media/", "ppt/slideLayouts/", "ppt/slideMasters/")


@dataclass
class Paragraph:
    """一段文字（`a:p`）在整份 slide XML 里的位置。"""

    pid: str
    """稳定地址，形如 `s3#p7`（第 3 页、文档序第 7 段）。**下标覆盖整页所有段落**，
    包括表格单元格里的——这正是第一版错位的根因，不要再按形状分段编号。"""
    shape: str
    """最近的祖先形状名（`p:cNvPr@name`），没有就是 ''。"""
    in_table: bool
    """这一段是不是表格单元格里的文字。"""
    has_field: bool
    """段内含 `a:fld`（页码 / 日期这类自动字段）。改这种段落是无效功——PowerPoint
    渲染时会用自动值覆盖，所以 edit.py 直接拒绝。"""
    text: str
    """该段的文字。段内 `<a:br/>` 渲染成 `\\n` —— 所见即模型要匹配的字符串。"""
    run_range: tuple[int, int]
    """该段占用的 `<a:t>` **全局**序号区间 [start, end)。"""
    line_ranges: list[tuple[int, int]]
    """按 `a:br` 切开的每一行各自的 run 区间；没有软换行时就是 `[run_range]`。
    改写按它逐行落刀，所以行数即 `text.count('\\n') + 1`。"""

    @property
    def has_break(self) -> bool:
        """段内有没有软换行。"""
        return len(self.line_ranges) > 1


@dataclass
class SlideDoc:
    number: int
    """放映页号（`p:sldIdLst` 里的位置，从 1 起）——用户口中的「第 N 页」。"""
    part: str
    """真实部件名（`ppt/slides/slideM.xml`）；M 与 number 无关，写包按它定位。"""
    xml: bytes
    paragraphs: list[Paragraph] = field(default_factory=list)


def census(zf: zipfile.ZipFile) -> dict[str, int]:
    """逐类清点部件。**不认识任何加载项**——只问「这一类还剩几个」。"""
    counts: dict[str, int] = {}
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        for prefix in UNMODELLED_PREFIXES + EXTRA_PREFIXES:
            if name.startswith(prefix):
                counts[prefix] = counts.get(prefix, 0) + 1
        if SLIDE_RE.match(name):
            counts["ppt/slides/"] = counts.get("ppt/slides/", 0) + 1
    return counts


def addin_markers(path: str) -> list[str]:
    """认得出名字的加载项痕迹（think-cell 等）。认不出但有 `p:tag` 的报 'unknown-addin'。"""
    found: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        for name in names:
            if not name.startswith("ppt/tags/"):
                continue
            try:
                text = zf.read(name).decode("utf-8", "replace")
            except KeyError:  # pragma: no cover - 目录项
                continue
            upper = text.upper()
            if "THINKCELL" in upper or "TCLAYOUT" in upper:
                found.append("think-cell")
            elif "<p:tag" in text:
                found.append("unknown-addin")
        if any(n.startswith("ppt/webextensions/") for n in names):
            found.append("web-extension")
    return sorted(set(found))


def _scan_paragraph(node, first_run: int) -> tuple[str, list[tuple[int, int]], int]:
    """按文档序走一遍段落，切出 `(文本, 每行的 run 区间, 段末 run 游标)`。

    `a:t` 与 `a:br` 一起按 `iter()` 的先序取出——两者交错的顺序就是渲染顺序，
    只数 `a:t` 就看不出换行落在哪两个 run 之间。
    """
    lines: list[str] = []
    ranges: list[tuple[int, int]] = []
    current: list[str] = []
    line_start = first_run
    cursor = first_run
    for element in node.iter():
        if element.tag == f"{A_NS}t":
            current.append(element.text or "")
            cursor += 1
        elif element.tag == f"{A_NS}br":
            lines.append("".join(current))
            ranges.append((line_start, cursor))
            current = []
            line_start = cursor
    lines.append("".join(current))
    ranges.append((line_start, cursor))
    return "\n".join(lines), ranges, cursor


def assert_run_index_alignment(xml: bytes, tree_run_count: int) -> None:
    """树里的 `a:t` 个数必须与字节里匹配到的 `<a:t>` 个数相等，否则**拒绝**。

    这是「用树定位、按字节落刀」的唯一前提。不等意味着两边看到的不是同一批节点
    （非 `a:` 前缀、CDATA、我们没想到的写法），此时任何下标都可能指向别的节点。
    """
    raw_count = len(RUN_TEXT_RE.findall(xml))
    if raw_count != tree_run_count:
        raise ValueError(
            f"这一页的文本节点对不上（树 {tree_run_count} 个 / 字节 {raw_count} 个），"
            "拒绝按下标定位——这一页请改用编辑器处理"
        )


def _slide_id_targets(zf: zipfile.ZipFile) -> list[tuple[str, str | None]]:
    """`p:sldIdLst` 按放映顺序 → `[(r:id, 解析到的部件名或 None)]`。

    读不出 presentation.xml / 它的关系清单时抛 `ValueError`。这是 `read_slides`
    与 `_slide_id_problems` 共用的唯一解析口径：页号来源与包一致性校验不能各算各的。
    """
    try:
        presentation = ET.fromstring(zf.read("ppt/presentation.xml"))
        rels = ET.fromstring(zf.read("ppt/_rels/presentation.xml.rels"))
    except (KeyError, ET.ParseError) as error:
        raise ValueError(f"读不出 ppt/presentation.xml 或它的关系清单：{error}") from error
    by_id = {rel.get("Id") or "": rel.get("Target") or "" for rel in rels.findall(f"{REL_NS}Relationship")}
    out: list[tuple[str, str | None]] = []
    for slide_id in presentation.iter(f"{P_NS}sldId"):
        rid = slide_id.get(f"{R_NS}id") or ""
        target = by_id.get(rid)
        out.append((rid, None if target is None else _resolve_target("ppt", target)))
    return out


def show_order(zf: zipfile.ZipFile) -> list[str]:
    """放映顺序里的 slide 部件名（`p:sldIdLst` 序）。

    列表里指向不存在部件的条目一律拒绝（`ValueError`）：那是个坏包，按残缺列表编页号
    会让「第 N 页」指错；`verify --self` 会把具体哪条断了报出来。
    """
    names = set(zf.namelist())
    parts: list[str] = []
    for rid, part in _slide_id_targets(zf):
        if part is None or part not in names:
            raise ValueError(f"p:sldIdLst 里的 {rid} 指向不存在的页（{part}）——包不一致，请先 verify --self")
        parts.append(part)
    return parts


def read_slides(path: str) -> list[SlideDoc]:
    """按**放映顺序**读出每一页，并建立**全页**段落索引（含表格单元格）。

    页号 = `p:sldIdLst` 里的位置（从 1 起）；不在列表里的 slide 部件放映时不存在，
    不进索引（`verify --self` 会点名）。
    """
    docs: list[SlideDoc] = []
    with zipfile.ZipFile(path) as zf:
        for number, part in enumerate(show_order(zf), start=1):
            xml = zf.read(part)
            doc = SlideDoc(number=number, part=part, xml=xml)
            root = ET.fromstring(xml)
            parents = {child: parent for parent in root.iter() for child in parent}
            run_cursor = 0
            # ⚠️ 遍历**所有** a:p（文档序），不按形状筛：表格单元格等位置同样贡献
            # `<a:t>`，漏掉它们就会让后面每个下标整体偏移。
            for para_index, para in enumerate(root.iter(f"{A_NS}p")):
                start = run_cursor
                text, line_ranges, run_cursor = _scan_paragraph(para, run_cursor)
                shape = ""
                in_table = False
                node = parents.get(para)
                while node is not None:
                    if node.tag == f"{A_NS}tbl":
                        in_table = True
                    if shape == "":
                        name_node = node.find(f"./{P_NS}nvSpPr/{P_NS}cNvPr")
                        if name_node is not None:
                            shape = name_node.get("name") or ""
                    node = parents.get(node)
                doc.paragraphs.append(
                    Paragraph(
                        pid=f"s{number}#p{para_index}",
                        shape=shape,
                        in_table=in_table,
                        has_field=any(True for _ in para.iter(f"{A_NS}fld")),
                        text=text,
                        run_range=(start, run_cursor),
                        line_ranges=line_ranges,
                    )
                )
            assert_run_index_alignment(xml, run_cursor)
            docs.append(doc)
    return docs


def rewrite_paragraph(xml: bytes, line_ranges: list[tuple[int, int]], text: str) -> bytes:
    """把某段的文字换成 `text`，**其余字节一个不动**。

    做法：每一行的第一个 `<a:t>` 承载该行全部新文字（因此它的 `a:rPr` 格式被保留），
    同行其余 `<a:t>` 清空。刻意不删 run —— 删了就要动 `a:rPr`、编号、以及可能挂在
    run 上的超链接/书签关系，那是「整段重写」的开始。

    **行必须对位。** 原段有几行（`a:br` 数 + 1），新文本就得有几行（按 `\\n` 切）。
    不一致时拒绝：多了没地方放（我们不新增 `a:br`），少了会留下渲染成空行的孤立
    `<a:br/>`——两种都会静默交付一份排版被改坏的 pptx。

    段内原本有多种格式（一行里粗体一半、常规一半）时，该行新文字会整体继承**该行
    第一个 run** 的格式。这是有意的取舍：想保住行内分段格式就分别改每个 run 的文字。
    """
    if not any(end > start for start, end in line_ranges):
        raise ValueError("这一段没有任何文字 run，无法就地改写（先在编辑器里给它一个文本框）")
    lines = text.split("\n")
    if len(lines) != len(line_ranges):
        raise ValueError(
            f"这一段有 {len(line_ranges)} 行（{len(line_ranges) - 1} 个软换行），"
            f"新文本给了 {len(lines)} 行——行数必须一致；要增删换行请在编辑器里做"
        )
    # 全局 run 序号 → `(改写后的内容, 要不要 xml:space="preserve")`；空 bytes = 清空。
    payloads: dict[int, tuple[bytes, bool]] = {}
    for line_no, ((start, end), line) in enumerate(zip(line_ranges, lines), start=1):
        if end <= start:
            if line != "":
                raise ValueError(f"第 {line_no} 行原本没有文本 run，放不下「{line}」——请在编辑器里补一个")
            continue
        payloads[start] = (escape(line).encode("utf-8"), line != line.strip())
        for index in range(start + 1, end):
            payloads[index] = (b"", False)
    out = bytearray()
    cursor = 0
    for index, match in enumerate(RUN_TEXT_RE.finditer(xml)):
        entry = payloads.get(index)
        if entry is None:
            continue
        payload, needs_preserve = entry
        attrs = match.group("attrs") or b""
        # 前后有空白就必须显式 preserve。原节点写着 `xml:space="default"` 时也要**改掉**它
        # ——第一版只在「没有这个属性」时才补，于是 default 那种情况静默丢掉首尾空格。
        if needs_preserve:
            attrs = re.sub(rb'\s+xml:space="[^"]*"', b"", attrs) + b' xml:space="preserve"'
        out += xml[cursor : match.start()]
        out += b"<a:t" + attrs + b">" + payload + b"</a:t>"
        cursor = match.end()
    out += xml[cursor:]
    return bytes(out)


def repack(src_path: str, dst_path: str, replaced: dict[str, bytes]) -> None:
    """按原顺序重写 zip：`replaced` 里点名的条目换内容，其余条目**原样拷贝**。

    逐条克隆 `ZipInfo` 的**全部**字段（压缩方式、时间戳、内外属性、flag_bits、extra、
    comment、版本号），所以没被点名的部件在新包里与原包字节一致。只复制一部分字段是不够
    的：漏掉 flag_bits / extra 会让带 UTF-8 标记或 NTFS 时间扩展的条目在新包里变形，
    而文件头注承诺的是**字节级**不变。加密条目直接拒绝，不猜。
    """
    with zipfile.ZipFile(src_path) as src, zipfile.ZipFile(dst_path, "w") as dst:
        for info in src.infolist():
            if info.flag_bits & 0x1:
                raise ValueError(f"包内有加密条目，拒绝改写：{info.filename}")
            data = replaced.get(info.filename, src.read(info.filename))
            clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            clone.compress_type = info.compress_type
            clone.external_attr = info.external_attr
            clone.internal_attr = info.internal_attr
            clone.create_system = info.create_system
            clone.create_version = info.create_version
            clone.extract_version = info.extract_version
            clone.flag_bits = info.flag_bits
            clone.extra = info.extra
            clone.comment = info.comment
            dst.writestr(clone, data)


def census_of(path: str) -> dict[str, int]:
    with zipfile.ZipFile(path) as zf:
        return census(zf)


def dropped(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """只看**减少**：多出来的不算损失（图表工作簿被补回来就是这种情况）。"""
    out: dict[str, int] = {}
    for key, count in before.items():
        left = after.get(key, 0)
        if count > left:
            out[key] = count - left
    return out


def _rels_base(rels_name: str) -> str:
    """`.rels` 文件路径 → 它所描述的那个部件**所在目录**（Target 就相对这个目录解析）。

    `ppt/slides/_rels/slide1.xml.rels` → `ppt/slides`；`_rels/.rels` → `''`（包根）。
    """
    return posixpath.dirname(posixpath.dirname(rels_name))


def _resolve_target(base: str, target: str) -> str:
    """把一条关系的 `Target` 解析成包内绝对路径（不含前导 `/`）。"""
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(base, target)) if base else posixpath.normpath(target)


def _content_type_problems(zf: zipfile.ZipFile, names: list[str]) -> list[str]:
    """每个部件都得有内容类型，否则 PowerPoint 直接判包损坏。"""
    try:
        content_types = ET.fromstring(zf.read("[Content_Types].xml"))
    except KeyError:
        return ["缺 [Content_Types].xml —— 这不是一个合法的 OPC 包"]
    except ET.ParseError as error:
        return [f"[Content_Types].xml 解析失败：{error}"]
    defaults = {(node.get("Extension") or "").lower() for node in content_types.findall(f"{CT_NS}Default")}
    overrides = {node.get("PartName") or "" for node in content_types.findall(f"{CT_NS}Override")}
    problems: list[str] = []
    for name in names:
        if name == "[Content_Types].xml" or f"/{name}" in overrides:
            continue
        extension = name.rsplit(".", 1)[-1].lower() if "." in posixpath.basename(name) else ""
        if extension not in defaults:
            problems.append(f"{name} 没有内容类型（既无 Override，也没有 .{extension} 的 Default）")
    return problems


def _rels_problems(zf: zipfile.ZipFile, names: list[str]) -> list[str]:
    """每条内部关系都得指向包里真实存在的部件——断了的关系就是打不开的图 / 丢掉的图表。"""
    present = set(names)
    problems: list[str] = []
    for name in names:
        if not name.endswith(".rels"):
            continue
        try:
            root = ET.fromstring(zf.read(name))
        except ET.ParseError as error:
            problems.append(f"{name} 解析失败：{error}")
            continue
        base = _rels_base(name)
        for rel in root.findall(f"{REL_NS}Relationship"):
            if (rel.get("TargetMode") or "Internal") == "External":
                continue
            target = rel.get("Target") or ""
            if target == "":
                problems.append(f"{name} 里有一条关系没有 Target（Id={rel.get('Id')}）")
                continue
            resolved = _resolve_target(base, target)
            if resolved not in present:
                problems.append(f"{name} 指向不存在的部件：{target} → {resolved}")
    return problems


def _slide_id_problems(zf: zipfile.ZipFile, names: list[str]) -> list[str]:
    """`p:sldIdLst` 与真实 slide 部件必须一一对上。

    对不上的两种后果都不会报错、只会静默变形：列表里多一条 → 打开即提示修复；
    有 slide 部件却没进列表 → 那一页在放映和缩略图里**根本不存在**。
    """
    try:
        targets = _slide_id_targets(zf)
    except ValueError as error:
        return [str(error)]
    present = set(names)
    problems: list[str] = []
    listed: set[str] = set()
    for rid, resolved in targets:
        if resolved is None:
            problems.append(f"p:sldIdLst 里的 {rid} 在 presentation.xml.rels 里没有对应关系")
            continue
        if resolved not in present:
            problems.append(f"p:sldIdLst 里的 {rid} 指向不存在的页：{resolved}")
            continue
        listed.add(resolved)
    for name in names:
        if SLIDE_RE.match(name) and name not in listed:
            problems.append(f"{name} 存在但没进 p:sldIdLst —— 这一页不会显示")
    return problems


def package_problems(path: str) -> list[str]:
    """OPC 包一致性：内容类型齐全、内部关系不断、页列表与页部件对得上。

    surgical 改写只动 `<a:t>` 的字节，理论上碰不到这三样；但 `verify.py` 是这条链上
    **唯一**的交付闸，只验「XML 能不能解析」会漏掉「关系断了 / 少一页 / 部件没类型」
    这类照样打得开、却已经变形的包（2026-08-26 跨模型审阅点名）。
    """
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        return _content_type_problems(zf, names) + _rels_problems(zf, names) + _slide_id_problems(zf, names)


def untouched_parts_identical(before_path: str, after_path: str, changed: set[str]) -> list[str]:
    """没点名的条目里字节**变了**的那些——正常应当为空。这是 surgical 的字面定义。"""
    diverged: list[str] = []
    with zipfile.ZipFile(before_path) as a, zipfile.ZipFile(after_path) as b:
        names_b = set(b.namelist())
        for name in a.namelist():
            if name in changed or name.endswith("/"):
                continue
            if name not in names_b:
                diverged.append(f"{name}（丢了）")
            elif a.read(name) != b.read(name):
                diverged.append(name)
    return diverged
