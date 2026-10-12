#!/usr/bin/env python3
"""读一份 pptx：页面清单、每段文字的稳定地址、以及「这份文件里有什么是碰不得的」。

改之前先跑它——`edit.py` 的 op 用的就是这里打印的 `pid`。

    python3 prep.py "<deck.pptx>"                # 全部页
    python3 prep.py "<deck.pptx>" --slide 4      # 只看第 4 页
    python3 prep.py "<deck.pptx>" --json         # 机器读
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile

from pptx_parts import addin_markers, census, read_slides


def main() -> int:
    parser = argparse.ArgumentParser(description="读 pptx 的页面 / 段落 / 部件清单")
    parser.add_argument("pptx")
    parser.add_argument("--slide", type=int, default=None, help="只看这一页（放映顺序页号，从 1 起）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    with zipfile.ZipFile(args.pptx) as zf:
        counts = census(zf)
    markers = addin_markers(args.pptx)
    try:
        slides = read_slides(args.pptx)
    except ValueError as error:
        print(f"读不出页面列表：{error}", file=sys.stderr)
        return 2
    if args.slide is not None:
        slides = [s for s in slides if s.number == args.slide]
        if not slides:
            print(f"没有第 {args.slide} 页", file=sys.stderr)
            return 2

    if args.json:
        print(
            json.dumps(
                {
                    "census": counts,
                    "addins": markers,
                    "slides": [
                        {
                            "number": s.number,
                            "paragraphs": [
                                {
                                    "id": p.pid,
                                    "shape": p.shape,
                                    "text": p.text,
                                    "in_table": p.in_table,
                                    "has_field": p.has_field,
                                    # 软换行数 + 1。改这一段时新文本必须给出同样的行数。
                                    "lines": len(p.line_ranges),
                                }
                                for p in s.paragraphs
                            ],
                        }
                        for s in slides
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"页数: {counts.get('ppt/slides/', 0)}")
    print("部件清点: " + (", ".join(f"{k}{v}" for k, v in sorted(counts.items())) or "（空）"))
    if markers:
        print(
            "⚠️ 检测到加载项 / 扩展内容: "
            + ", ".join(markers)
            + " —— 这类东西 PPTD 装不下，**不要**把这份文件转成 deck 再改；就地 surgical 改。"
        )
    for slide in slides:
        print(f"\n── 第 {slide.number} 页 ──")
        for para in slide.paragraphs:
            if para.text.strip() == "":
                continue
            shape = f" [{para.shape}]" if para.shape else ""
            # 表格单元格与自动字段必须标出来：前者容易被误当成正文形状，后者改了也白改。
            # 软换行同样要标：改这一段时新文本得给出同样的行数，看不见就必然写错。
            tags = "".join(
                t
                for t in (
                    " {表格}" if para.in_table else "",
                    " {自动字段·不可改}" if para.has_field else "",
                    f" {{{len(para.line_ranges)} 行}}" if para.has_break else "",
                )
            )
            # `\n` 打成 `⏎`，一段仍占一行——否则带软换行的段落会把清单撑散、地址对不上文字。
            print(f"  {para.pid}{shape}{tags}  {para.text.replace(chr(10), ' ⏎ ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
