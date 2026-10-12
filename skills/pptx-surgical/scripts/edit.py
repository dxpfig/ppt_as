#!/usr/bin/env python3
"""定点改一份 pptx：只重写你点名的那几页，其余部件逐字节原样搬过去。

    python3 edit.py "<deck.pptx>" --ops ops.json            # 就地改
    python3 edit.py "<deck.pptx>" --ops ops.json --dry-run  # 只看会改什么
    python3 edit.py "<deck.pptx>" --ops ops.json -o out.pptx

ops.json 是一个数组，两种写法可混用：

    [
      {"id": "s4#p0", "text": "新的标题"},
      {"slide": 4, "find": "旧口径", "replace": "新口径"}
    ]

* `id` 来自 `prep.py`，**精确**改一整段；
* `find`/`replace` 在指定页内按段落全文匹配（子串），命中多段时全部替换，
  一段都没命中即报错退出——**绝不静默无改动**，那是最容易被当成「改好了」的失败。

## 内置保护（调用方不需要任何外围 shell）

- 同一文件的编辑经 OS 级文件锁互斥：另一任务正在改时报错退出，不排队；
  锁状态在内核句柄表里，进程死亡（含 kill -9 / 断电）自动释放，无残留锁。
- 就地覆盖前自动在同目录留 `.<文件名>.before.pptx`（恒定一份，下次编辑覆盖）。
- 写盘前复检源文件的 mtime 与大小：编辑期间被其它程序改动则放弃写盘，原文件不动。

## 写盘前的三道自检（缺一条都可能静默交付错东西）

1. 改过的 XML 必须仍能解析；
2. 部件类别不许减少、页数不许变；
3. **逐 op 复核**：重新读一遍产出的包，确认每个 op 点名的那一段**真的**变成了预期文本。
   第 3 条是 2026-08-26 跨模型审阅之后加的——在那之前，表格造成的下标错位会让新文字落进
   别的节点，而 1、2 两条全都通过，prep / edit / verify 一齐报成功。
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import stat as stat_mod
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

from pptx_parts import census_of, dropped, read_slides, repack, rewrite_paragraph


def _load_ops(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        ops = json.load(handle)
    if not isinstance(ops, list) or not ops:
        raise SystemExit("ops 必须是非空数组")
    return ops


def _acquire_edit_lock(target: str) -> int:
    """同一目标文件的编辑互斥：拿不到锁就失败，不排队。

    锁状态在内核的句柄表里（fcntl flock / msvcrt locking），进程无论怎么死——含 kill -9
    与断电——都由内核自动释放；磁盘上只留下一个 0 字节空文件，不存在残留锁，也不需要
    过期判定。锁文件放系统临时目录，不在工作区留痕。
    """
    key = hashlib.sha256(os.path.realpath(target).encode()).hexdigest()[:16]
    lock_path = os.path.join(tempfile.gettempdir(), f"pptx-surgical-{key}.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError as error:
        raise SystemExit(f"创建不了编辑锁文件（{error}）——为安全起见本次不修改文件") from error
    for attempt in range(3):
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBRLCK, 1)
            else:
                raise SystemExit("当前平台不支持文件锁——为安全起见本次不修改文件")
            return fd
        except BlockingIOError:
            pass  # 锁被别人持有，等一等再试
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise SystemExit(f"文件锁不可用（{error}）——为安全起见本次不修改文件") from error
        if attempt < 2:
            time.sleep(4)
    raise SystemExit("这份文件正被另一个编辑任务修改，请等它完成后重试")


def _backup_for_inplace_edit(target: str) -> str:
    """就地覆盖前留一份对照：与目标同目录、随目标命名，恒定一份（下次编辑覆盖）。

    核对失败时它是恢复原件的唯一依据——现在没有任何版本管理机制了。
    """
    directory = os.path.dirname(os.path.abspath(target))
    backup = os.path.join(directory, f".{os.path.basename(target)}.before.pptx")
    try:
        shutil.copy2(target, backup)
        os.chmod(backup, stat_mod.S_IRUSR | stat_mod.S_IWUSR)
    except OSError as error:
        raise SystemExit(f"留备份失败（{error}）——为安全起见本次不修改文件") from error
    return backup


def _resolve_ops(ops: list[dict], slides: dict) -> dict[str, tuple[int, list[tuple[int, int]], str, str]]:
    """把 op 解析成 `pid → (页号, 每行的 run 区间, 新文本, 人话描述)`。

    **按 pid 去重并拒绝冲突**：同一段被两个 op 命中时（一个 id op + 同页 find/replace 命中
    同一段），两次改写各自基于原始文本算，后写的整段覆盖先写的，最终结果不是任何一个 op 的
    语义，日志却照样打印「改动 2 段」。宁可报错让调用方拆成两轮。
    """
    by_pid = {para.pid: (slide.number, para) for slide in slides.values() for para in slide.paragraphs}
    resolved: dict[str, tuple[int, list[tuple[int, int]], str, str]] = {}

    def claim(pid: str, number: int, lines: list[tuple[int, int]], old_text: str, new_text: str) -> None:
        if new_text == old_text:
            # 目标文本与现状一致就**不碰这一页**：改写会把多 run 段落合并进首个 run，
            # 文本没变字节却变了，verify.py 会把这页判成「未点名的条目被改动」。
            print(f"  {pid} 已是 {new_text!r}，跳过")
            return
        if pid in resolved and resolved[pid][2] != new_text:
            raise SystemExit(f"两个 op 都要改 {pid} 且结果不同（{resolved[pid][2]!r} vs {new_text!r}）——请拆成两轮")
        resolved[pid] = (number, lines, new_text, f"{pid}: {old_text!r} → {new_text!r}")

    for op in ops:
        if "id" in op:
            target = by_pid.get(str(op["id"]))
            if target is None:
                raise SystemExit(f"找不到段落 {op['id']}（先跑 prep.py 取当前地址）")
            number, para = target
            if para.has_field:
                raise SystemExit(f"{para.pid} 含自动字段（页码 / 日期），改它无效——PowerPoint 会用自动值覆盖")
            claim(para.pid, number, para.line_ranges, para.text, str(op.get("text", "")))
            continue
        if "find" in op:
            missing = [key for key in ("slide", "replace") if key not in op]
            if missing:
                raise SystemExit(f"find op 缺少 {'/'.join(missing)}：{op}")
            try:
                number = int(op["slide"])
            except (TypeError, ValueError) as error:
                raise SystemExit(f"slide 必须是页号整数：{op}") from error
            slide = slides.get(number)
            if slide is None:
                raise SystemExit(f"没有第 {number} 页")
            needle = str(op["find"])
            replacement = str(op["replace"])
            if needle == replacement:
                raise SystemExit(f"第 {number} 页的 find 与 replace 相同（{needle!r}）——不会有任何改动")
            hits = [p for p in slide.paragraphs if needle in p.text and not p.has_field]
            if not hits:
                raise SystemExit(f"第 {number} 页里没有含「{needle}」的可改段落——没有任何改动发生")
            for para in hits:
                claim(para.pid, number, para.line_ranges, para.text, para.text.replace(needle, replacement))
            continue
        raise SystemExit(f"看不懂的 op（需要 id 或 find）：{op}")
    return resolved


def _verify_written(path: str, expected: dict[str, str]) -> list[str]:
    """重新读产出的包，逐 op 核对目标段落**真的**变成了预期文本。返回不符的清单。"""
    fresh = {para.pid: para.text for slide in read_slides(path) for para in slide.paragraphs}
    return [f"{pid}: 期望 {text!r}，实际 {fresh.get(pid)!r}" for pid, text in expected.items() if fresh.get(pid) != text]


def _finalize(tmp_path: str, out_path: str) -> None:
    """把临时文件顶到目标位置：先还原权限，再原子替换。"""
    # mkstemp 给的是 0600；就地覆盖时要把原文件的权限还回去，否则同步盘 / 共享目录里别的
    # 账号突然读不到这份 pptx，而脚本报的是「已写入」。
    if os.path.exists(out_path):
        shutil.copymode(out_path, tmp_path)
    else:
        os.chmod(tmp_path, stat_mod.S_IRUSR | stat_mod.S_IWUSR | stat_mod.S_IRGRP | stat_mod.S_IROTH)
    try:
        os.replace(tmp_path, out_path)
    except OSError as error:
        # Windows 上目标被 PowerPoint 以 deny-write 打开时就是这一支；吐 traceback 只会让人
        # 以为脚本崩了，说清楚「文件正被占用」才有下一步。
        raise SystemExit(f"写盘失败（目标可能正被其它程序打开）：{error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="对 pptx 做定点（surgical）修改")
    parser.add_argument("pptx")
    parser.add_argument("--ops", required=True, help="ops JSON 文件路径")
    parser.add_argument("-o", "--out", default=None, help="输出路径（默认就地覆盖）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要发生的改动")
    args = parser.parse_args()

    ops = _load_ops(args.ops)
    lock_fd = _acquire_edit_lock(args.pptx)  # 持有引用即持锁；进程退出（含被杀）内核自动释放
    _ = lock_fd
    source_stat = os.stat(args.pptx)
    try:
        slides = {slide.number: slide for slide in read_slides(args.pptx)}
    except ValueError as error:
        raise SystemExit(f"读不出页面列表，拒绝改写：{error}") from error
    resolved = _resolve_ops(ops, slides)
    if not resolved:
        print("所有 op 的目标文本都与现状一致——没有改动，未写盘")
        return 0

    pending: dict[int, list[tuple[list[tuple[int, int]], str, str, str]]] = {}
    for pid, (number, line_ranges, new_text, description) in resolved.items():
        pending.setdefault(number, []).append((line_ranges, new_text, description, pid))

    replaced: dict[str, bytes] = {}
    for number, edits in sorted(pending.items()):
        slide = slides[number]
        xml = slide.xml
        # 顺序无所谓：改写只换 `<a:t>` 的内容与属性、**不增删节点**，所以下标是序号而非
        # 字节偏移，前一次改写不会让后一次错位。仍按倒序跑只是让 dry-run 与实跑的日志同序。
        for line_ranges, text, _, pid in sorted(edits, key=lambda item: item[0][0][0], reverse=True):
            try:
                xml = rewrite_paragraph(xml, line_ranges, text)
            except ValueError as error:
                # 拒绝改写是**给调用方看的**结论（行数不对位、这一行没有文本 run）。
                # 让 ValueError 裸奔出去只会打一屏 traceback，读起来像脚本崩了。
                raise SystemExit(f"{pid} 改不了：{error}") from error
        try:
            ET.fromstring(xml)
        except ET.ParseError as error:  # pragma: no cover - 防御
            raise SystemExit(f"第 {number} 页改完之后 XML 解析不过：{error}") from error
        replaced[slide.part] = xml

    print(f"将改动 {len(replaced)} 页 / {len(resolved)} 段：")
    for number, edits in sorted(pending.items()):
        for _, _, description, _pid in edits:
            print(f"  [第 {number} 页] {description}")
    if args.dry_run:
        print("（--dry-run：没有写盘）")
        return 0

    out_path = args.out or args.pptx
    before = census_of(args.pptx)
    if os.path.abspath(out_path) == os.path.abspath(args.pptx):
        _backup_for_inplace_edit(args.pptx)
    handle, tmp_path = tempfile.mkstemp(suffix=".pptx", dir=os.path.dirname(os.path.abspath(out_path)) or ".")
    os.close(handle)
    try:
        repack(args.pptx, tmp_path, replaced)
        after = census_of(tmp_path)
        lost = dropped(before, after)
        if lost:
            raise SystemExit(f"自检不过：这次会丢掉部件 {lost}——已放弃写盘，原文件未动")
        if before.get("ppt/slides/") != after.get("ppt/slides/"):
            raise SystemExit("自检不过：页数变了——已放弃写盘，原文件未动")
        mismatched = _verify_written(tmp_path, {pid: item[2] for pid, item in resolved.items()})
        if mismatched:
            raise SystemExit("自检不过：改动没落到点名的段落上——已放弃写盘，原文件未动\n  " + "\n  ".join(mismatched))
        current = os.stat(args.pptx)
        if (current.st_mtime_ns, current.st_size) != (source_stat.st_mtime_ns, source_stat.st_size):
            raise SystemExit(
                "编辑期间这份文件被其它程序改动过——已放弃写盘，原文件保持对方的新内容；"
                "请基于现状重新跑一遍"
            )
        _finalize(tmp_path, out_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    print(f"已写入 {out_path}；逐段复核通过；部件清点前后一致：{sorted(before.items())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
