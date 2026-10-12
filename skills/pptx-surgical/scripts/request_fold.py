#!/usr/bin/env python3
"""request_fold.py — 请求桌面端把借出的 PPTD 工作副本折回同名 .pptx，并等待终态。

AI 改完工作副本（working files/decks/<stem>-*/）后**必须**运行本脚本，直到拿到
`done`；`folded` ∪ `synced` 里含目标 pptx 才算折回完成。

通道：桌面 main 进程的 app-context 本地服务（endpoint 在环境变量
KIMI_WORK_CONTEXT_IPC；macOS/Linux 是 0600 unix socket，Windows 是命名管道），
协议为一行 UTF-8 JSON 请求 → 一行 JSON 响应。折回要走云端导出（最长约 98s），
超过 app-context 单次 3s 处理上限，所以 op 是「提交即返 + 轮询取结果」两段式：
本脚本每次调用拿到 `running` 就间隔重发，直到 `done` 或总超时。

用法：
    python3 request_fold.py [工作区根绝对路径，默认 cwd] [总超时秒，默认 180]

退出码：0 = 无 conflicts 且无 stuck（folded/synced 情况都正常，含「本无欠账」）；
2 = 有 conflicts 或 stuck（折回未完成，如实把 stderr 的原因告知用户）；
1 = 通道/协议错误（桌面端不可用，同样如实告知，不得当作已折回）。
"""

import json
import os
import socket
import sys
import time

OP = "flush_slides_decks"
CLIENT_ID = "pptx-fold"
POLL_INTERVAL_S = 2.0


def exchange(endpoint: str, payload: dict) -> dict:
    """发一行 JSON、收一行 JSON。每次调用都是新连接（服务端一问一答后即关闭）。"""
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    if os.name == "nt":
        # Windows：命名管道按普通文件打开（字节流模式），与服务端行协议一致。
        with open(endpoint, "r+b", buffering=0) as pipe:
            pipe.write(line)
            return json.loads(_read_line(pipe))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(10)
        sock.connect(endpoint)
        sock.sendall(line)
        return json.loads(_read_line(sock))
    finally:
        sock.close()


def _read_line(io_obj) -> str:
    buf = bytearray()
    while True:
        chunk = io_obj.read(1) if hasattr(io_obj, "read") else io_obj.recv(1)
        if not chunk:
            break
        if chunk == b"\n":
            break
        buf += chunk
        if len(buf) > 64 * 1024:
            raise RuntimeError("response too large")
    return buf.decode("utf-8")


def main() -> int:
    endpoint = os.environ.get("KIMI_WORK_CONTEXT_IPC", "").strip()
    if not endpoint:
        print("KIMI_WORK_CONTEXT_IPC 未设置：桌面端上下文通道不可用", file=sys.stderr)
        return 1
    root = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.getcwd()
    deadline = time.monotonic() + (float(sys.argv[2]) if len(sys.argv) > 2 else 180.0)

    while True:
        try:
            resp = exchange(endpoint, {"op": OP, "root": root, "client_id": CLIENT_ID})
        except Exception as err:  # 连接/协议错误：桌面端不可用
            print(f"折回请求失败（桌面端通道异常）: {err}", file=sys.stderr)
            return 1

        if "error" in resp:
            print(f"折回请求被拒: {resp.get('error')}: {resp.get('message')}", file=sys.stderr)
            if resp.get("error") == "access_denied":
                # 这不是会话权限限制：root 不在桌面端登记表里。多为 root 传错（默认取
                # cwd，而 cwd 不是工作区根）——提示把根作第一个参数显式重试。
                print(
                    "提示：access_denied 表示传入的 root 不是桌面端登记过的工作区根"
                    f"（当前 root={root}）。这不是会话权限问题——"
                    "请把工作区根的绝对路径作为第一个参数重试；"
                    "若仍被拒，说明该 root 不是当前登录账号任一会话的工作区根，"
                    "改为在回答里如实告知用户折回未执行即可。",
                    file=sys.stderr,
                )
            return 1
        status = resp.get("status")
        if status == "done":
            print(json.dumps(resp, ensure_ascii=False))
            conflicts = resp.get("conflicts") or []
            stuck = resp.get("stuck") or []
            if conflicts:
                print(
                    "conflict：deck 与 pptx 两边都动过，桌面端已停手（不自动合并、不挑边覆盖）: "
                    + ", ".join(conflicts),
                    file=sys.stderr,
                )
            if stuck:
                print(
                    "stuck：有改动未能折回（文件被占用/导出失败），需要下次再试: " + ", ".join(stuck),
                    file=sys.stderr,
                )
            return 2 if conflicts or stuck else 0
        if status != "running":
            print(f"折回响应形态未知: {resp!r}", file=sys.stderr)
            return 1
        if time.monotonic() >= deadline:
            print("折回等待超时：桌面端仍未完成导出", file=sys.stderr)
            return 1
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
