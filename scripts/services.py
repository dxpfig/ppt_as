#!/usr/bin/env python3
"""Local Linux service launcher. Model configuration comes only from project .env."""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runtime' / 'services'
STATE = RUN / 'processes.json'
PORTS = {'agent': 8001, 'backend': 3002, 'frontend': 5174}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def identity(pid: int) -> str | None:
    try:
        # Fields after comm begin at field 3; starttime is field 22.
        text = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if text[0] == 'Z' else text[19]
    except (OSError, IndexError):
        return None


def alive(record: dict) -> bool:
    return identity(record['pid']) == record['starttime']


def records() -> dict:
    if not STATE.exists():
        return {}
    data = json.loads(STATE.read_text())
    for name, entry in data.items():
        if name not in PORTS or not isinstance(entry, dict) or not isinstance(entry.get('pid'), int) or entry['pid'] <= 1 or not isinstance(entry.get('starttime'), str):
            raise RuntimeError('进程记录格式不正确，请检查 runtime/services/processes.json。')
    return data


def write_state(data: dict) -> None:
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(STATE)


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def health(url: str) -> dict | None:
    try:
        with OPENER.open(url, timeout=2) as response:
            if response.status != 200:
                return None
            if response.headers.get_content_type() == 'application/json':
                return json.load(response)
            return {'status': 'ok'}
    except (OSError, ValueError):
        return None


def show_urls() -> None:
    print('\n本机访问：http://127.0.0.1:5174/')
    try:
        addresses = subprocess.check_output(['hostname', '-I'], text=True).split()
        for address in addresses:
            if address.startswith(('192.168.', '10.')):
                print(f'局域网访问：http://{address}:5174/')
    except (OSError, subprocess.CalledProcessError):
        pass
    print(f'日志目录：{RUN}')
    print('查看状态：./status.sh   停止：./stop.sh   重新加载配置：./start.sh --restart')


def stop_services(data: dict) -> None:
    targets = []
    for name, entry in data.items():
        if not alive(entry):
            continue
        pid = entry['pid']
        try:
            if os.getpgid(pid) != pid:
                print(f'{name}: 进程组不匹配，未停止。')
                continue
            os.killpg(pid, signal.SIGTERM)
            targets.append((name, entry))
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while any(alive(entry) for _, entry in targets) and time.monotonic() < deadline:
        time.sleep(0.1)
    for name, entry in targets:
        if alive(entry):
            try:
                os.killpg(entry['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        print(f'{name}: 已停止')
    remaining = {name: entry for name, entry in data.items() if alive(entry)}
    if remaining:
        write_state(remaining)
    else:
        STATE.unlink(missing_ok=True)


def preflight() -> str:
    if not (ROOT / '.env').is_file():
        raise RuntimeError('缺少 .env。请先执行 cp .env.example .env 并填写模型配置。')
    node = shutil.which('node')
    if not node:
        raise RuntimeError('未找到 Node.js，请安装 Node.js 22。')
    for path in ('frontend/node_modules/vite/bin/vite.js', 'backend/node_modules/ts-node/dist/bin.js'):
        if not (ROOT / path).is_file():
            raise RuntimeError('缺少 Node 依赖，请在项目目录执行 pnpm install。')
    if not (ROOT / 'packages/pptd-runtime/build/pptd-runtime.js').is_file():
        raise RuntimeError('缺少 PPTD 浏览器运行时，请先执行 pnpm build:pptd。')
    for module in ('uvicorn', 'fastapi', 'agentscope', 'pptx', 'dotenv', 'fitz'):
        if importlib.util.find_spec(module) is None:
            raise RuntimeError('缺少 Python 依赖，请执行 uv pip install --python .venv/bin/python -r agent_service/requirements.txt。')
    occupied = [f'{name}:{port}' for name, port in PORTS.items() if port_open(port)]
    if occupied:
        raise RuntimeError('端口已被其他运行进程占用：' + ', '.join(occupied) + '。请先停止旧服务；脚本不会终止未登记的进程。')
    return node


def start_services() -> None:
    data = records()
    running = [name for name, entry in data.items() if alive(entry)]
    if running:
        if len(running) == 3 and health('http://127.0.0.1:5174/api/studio/health'):
            print('服务已在运行，无需重复启动。')
            show_urls()
            return
        raise RuntimeError('已有部分服务在运行：' + ', '.join(running) + '。请使用 ./start.sh --restart。')
    node = preflight()
    environment = dict(os.environ)
    # Never implicitly inherit credentials/settings from Codex, Claude or other tools.
    for name in list(environment):
        if name.startswith(('ANTHROPIC_', 'OPENAI_', 'PPT_LLM_', 'PPT_VISION_', 'PPT_EMBEDDING_', 'PPT_RERANK_', 'PPT_OCR_', 'PPTD_', 'WEKNORA_', 'PPT_IMAGE_', 'PPT_WEB_MCP_', 'TAVILY_', 'SERPER_', 'SEARXNG_', 'OTEL_')) or name in ('PPT_RENDERER_IMAGE', 'LIBREOFFICE_BIN', 'STUDIO_DATA_DIR', 'AGENTSCOPE_STUDIO_URL'):
            environment.pop(name, None)
    environment['PYTHONUNBUFFERED'] = '1'
    specs = [
        ('agent', ROOT, [str(ROOT / '.venv/bin/python'), '-m', 'uvicorn', 'agent_service.app:app', '--host', '127.0.0.1', '--port', '8001'], 'http://127.0.0.1:8001/api/studio/health'),
        ('backend', ROOT / 'backend', [node, str(ROOT / 'backend/node_modules/ts-node/dist/bin.js'), 'src/index.ts'], 'http://127.0.0.1:3002/api/health'),
        ('frontend', ROOT / 'frontend', [node, str(ROOT / 'frontend/node_modules/vite/bin/vite.js'), '--host', '0.0.0.0', '--port', '5174', '--strictPort'], 'http://127.0.0.1:5174/'),
    ]
    started = {}
    try:
        for name, cwd, command, url in specs:
            child_env = dict(environment)
            if name == 'backend':
                child_env.update(PORT='3002', HOST='127.0.0.1', AGENT_SERVICE_URL='http://127.0.0.1:8001')
            with (RUN / f'{name}.log').open('ab') as log:
                log.write(f'\n--- 启动 {time.strftime("%Y-%m-%d %H:%M:%S")} ---\n'.encode())
                log.flush()
                process = subprocess.Popen(command, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            stamp = identity(process.pid)
            if stamp is None:
                process.wait()
                raise RuntimeError(f'{name} 启动失败，请查看 {RUN / (name + ".log")}。')
            started[name] = {'pid': process.pid, 'starttime': stamp}
            write_state(started)
            deadline = time.monotonic() + 90
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f'{name} 退出，详见 {RUN / (name + ".log")}。')
                if health(url) is not None:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(f'{name} 启动超时，详见 {RUN / (name + ".log")}。')
                time.sleep(0.4)
            print(f'{name}: 就绪（端口 {PORTS[name]}）', flush=True)
        state = health('http://127.0.0.1:5174/api/studio/health')
        if not state:
            raise RuntimeError('服务已启动，但前后端代理链路检查失败。')
        print('已启用模型配置：' + str(state.get('model', '未配置')))
        print('配置检查不代表模型连通性；生成时会实际调用你在 .env 中填写的服务。')
        if not state.get('llmConfigured'):
            print('提示：模型配置不完整，AI 生成暂不可用。')
        if not state.get('rendererAvailable'):
            print('提示：未发现可用的 LibreOffice 渲染器。')
        show_urls()
    except BaseException:
        stop_services(started)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description='PPT 助手服务管理（模型配置仅从项目 .env 读取）')
    parser.add_argument('action', choices=['start', 'stop', 'status', 'restart'])
    args = parser.parse_args()
    os.umask(0o077)
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / 'launcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('另一个启动/停止操作正在进行，请稍后重试。', file=sys.stderr)
            return 1
        if args.action in ('stop', 'restart'):
            data = records()
            stop_services(data)
            if not data:
                print('没有由脚本管理的运行进程。')
        if args.action in ('start', 'restart'):
            start_services()
        if args.action == 'status':
            data = records()
            for name, port in PORTS.items():
                managed = name in data and alive(data[name])
                state = '运行中（脚本管理）' if managed else '端口被其他进程占用' if port_open(port) else '未运行'
                print(f'{name:8} {port}: {state}')
            state = health('http://127.0.0.1:5174/api/studio/health')
            if state:
                print('模型配置：' + str(state.get('model', '未配置')))
            return 0 if all(name in data and alive(data[name]) for name in PORTS) else 1
    return 0


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as error:
        print(f'错误：{error}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('启动已取消。', file=sys.stderr)
        sys.exit(130)
