#!/usr/bin/env python3
"""改完之后回答两个问题：包还好着吗？我是不是只改了我想改的那几页？

    python3 verify.py --self "<deck.pptx>"                  # 结构自检
    python3 verify.py --before old.pptx --after new.pptx    # 与改前对照

`--before` 那份从哪来：就地编辑时 `edit.py` 会自动在同目录留 `.<文件名>.before.pptx`
（恒定一份，下次编辑覆盖）；用 `-o` 写到新文件时源文件未动，直接拿源文件当 `--before`。

退出码非 0 = 有问题，别把这次改动当成功交付。
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
import zipfile

from pptx_parts import (
    addin_markers,
    census,
    census_of,
    dropped,
    package_problems,
    read_slides,
    untouched_parts_identical,
)


def self_check(path: str) -> list[str]:
    problems: list[str] = []
    with zipfile.ZipFile(path) as zf:
        broken = zf.testzip()
        if broken is not None:
            problems.append(f"zip 校验失败，第一个坏条目：{broken}")
        for name in zf.namelist():
            if not name.endswith(".xml") and not name.endswith(".rels"):
                continue
            try:
                ET.fromstring(zf.read(name))
            except ET.ParseError as error:
                problems.append(f"{name} XML 解析失败：{error}")
        counts = census(zf)
        if counts.get("ppt/slides/", 0) == 0:
            problems.append("一页都没有（ppt/slides/slideN.xml 缺失）")
    # 「每个 XML 都能解析」离「这是一份 PowerPoint 打得开的包」还差三条：部件有没有
    # 内容类型、内部关系断没断、页列表与页部件对不对得上。
    problems += package_problems(path)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="pptx 改动后的结构与内容核对")
    parser.add_argument("--self", dest="self_path", default=None)
    parser.add_argument("--before", default=None)
    parser.add_argument("--after", default=None)
    args = parser.parse_args()

    failed = False
    target = args.self_path or args.after
    if target is None:
        parser.error("至少要给 --self 或 --after")

    problems = self_check(target)
    if problems:
        failed = True
        print("结构自检不过：")
        for problem in problems:
            print(f"  ✗ {problem}")
    else:
        print(f"结构自检通过：{target}")
    markers = addin_markers(target)
    if markers:
        print("这份文件带加载项 / 扩展内容：" + ", ".join(markers) + "（surgical 改法会原样保留它们）")

    if args.before and args.after:
        before = census_of(args.before)
        after = census_of(args.after)
        lost = dropped(before, after)
        if lost:
            failed = True
            print(f"✗ 部件丢失：{lost}")
        else:
            print("✓ 部件类别无减少")
        if before.get("ppt/slides/") != after.get("ppt/slides/"):
            failed = True
            print(f"✗ 页数变了：{before.get('ppt/slides/')} → {after.get('ppt/slides/')}")
        try:
            slides_before = read_slides(args.before)
            slides_after = read_slides(args.after)
        except ValueError as error:
            # 页列表读不出 / 指向不存在的页：self_check 已把具体问题列出来，这里不再堆 traceback。
            print(f"✗ 无法按放映顺序比对页面：{error}")
            return 1
        text_before = {s.number: [p.text for p in s.paragraphs] for s in slides_before}
        text_after = {s.number: [p.text for p in s.paragraphs] for s in slides_after}
        changed = sorted(n for n in text_after if text_before.get(n) != text_after[n])
        untouched = sorted(n for n in text_after if text_before.get(n) == text_after[n])
        print(f"✓ 文本有变化的页：{changed or '（无）'}；一字未动的页：{len(untouched)} 页")
        # surgical 的字面定义：没被点名的条目**字节相同**。只比类别计数会漏掉「同类部件被
        # 顶替」「内容被改写」这两种——2026-08-26 跨模型审阅点名的正是它。
        # 页号是放映序，部件名另有编号：点名的部件按 SlideDoc.part 取，不能拼 slide{n}.xml。
        changed_parts = {s.part for s in slides_after if s.number in changed}
        diverged = untouched_parts_identical(args.before, args.after, changed_parts)
        if diverged:
            failed = True
            print(f"✗ 未点名的条目被改动了：{diverged[:5]}{' …' if len(diverged) > 5 else ''}")
        else:
            print("✓ 未点名的条目字节全等")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
