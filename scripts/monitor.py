#!/usr/bin/env python3
"""Check (and start) the AgentScope Studio trace backend our agent exports to.

The agent ships OpenTelemetry spans to ``AGENTSCOPE_STUDIO_URL/v1/traces``. When
that backend is not running every exported batch fails and `agent.log` fills with
connection errors, so every project start checks it here instead of leaving the
monitoring gap to be noticed later.

Studio itself is optional infrastructure: if it cannot be started, the project
still starts and this script explains exactly what to run.
"""
from __future__ import annotations

import argparse
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
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runtime' / 'services'
STATE = RUN / 'monitor.json'
LOG = RUN / 'studio.log'
DEFAULT_URL = 'http://127.0.0.1:3001'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def dotenv() -> dict[str, str]:
    """Minimal .env reader: the launcher owns the environment on purpose."""
    values: dict[str, str] = {}
    path = ROOT / '.env'
    if not path.is_file():
        return values
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def configuration() -> dict:
    base = dotenv().get('AGENTSCOPE_STUDIO_URL', '').strip().rstrip('/')
    if not base:
        return {'configured': False, 'base': None, 'host': None, 'port': None}
    parsed = urlsplit(base if '://' in base else f'http://{base}')
    if not parsed.hostname:
        return {'configured': False, 'base': None, 'host': None, 'port': None}
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return {'configured': True, 'base': f'{parsed.scheme}://{parsed.netloc}', 'host': parsed.hostname, 'port': port}


def _identity(pid: int) -> str | None:
    try:
        text = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if text[0] == 'Z' else text[19]
    except (OSError, IndexError):
        return None


def managed() -> dict | None:
    """The Studio this script started, if it is still alive."""
    if not STATE.exists():
        return None
    try:
        record = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get('pid'), int):
        return None
    return record if _identity(record['pid']) == record.get('starttime') else None


def port_open(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def http_ok(base: str) -> bool:
    try:
        with OPENER.open(base + '/', timeout=2) as response:
            return response.status < 500
    except (OSError, ValueError):
        return False


def status() -> dict:
    config = configuration()
    record = managed()
    result = {**config, 'running': False, 'managed': record is not None, 'pid': (record or {}).get('pid')}
    if not config['configured']:
        result['detail'] = '未配置 AGENTSCOPE_STUDIO_URL，本次运行不上报追踪。'
        return result
    if port_open(config['host'], config['port']):
        result['running'] = True
        result['detail'] = 'HTTP 就绪' if http_ok(config['base']) else '端口已监听'
    else:
        result['detail'] = '未运行'
    return result


def launcher() -> tuple[list[str], str] | None:
    """Prefer an installed Studio CLI, otherwise fall back to npx."""
    binary = shutil.which('as_studio')
    if binary:
        return [binary], binary
    npx = shutil.which('npx')
    if npx:
        return [npx, '-y', '@agentscope/studio'], f'{Path(npx).name} (npx @agentscope/studio)'
    return None


def start(timeout: float = 300.0) -> dict:
    """Start Studio when the configured backend is down; never raise for infra."""
    current = status()
    if not current['configured']:
        return current
    if current['running']:
        return current
    command = launcher()
    if command is None:
        return {**current, 'started': False,
                'detail': '未找到 as_studio 或 npx；请安装 Node.js 后执行 npx -y @agentscope/studio。'}
    RUN.mkdir(parents=True, exist_ok=True)
    argv, label = command
    try:
        with LOG.open('ab') as log:
            log.write(f'\n--- 启动 AgentScope Studio {time.strftime("%Y-%m-%d %H:%M:%S")} ---\n'.encode())
            log.flush()
            process = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
    except OSError as error:
        return {**current, 'started': False, 'detail': f'启动 {label} 失败：{error}'}
    stamp = _identity(process.pid)
    if stamp is None:
        return {**current, 'started': False, 'detail': f'{label} 启动后立即退出，详见 {LOG}。'}
    STATE.write_text(json.dumps({'pid': process.pid, 'starttime': stamp, 'launcher': label,
                                 'url': current['base'], 'startedAt': time.time()}, indent=2))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return {**current, 'started': False, 'detail': f'{label} 已退出，详见 {LOG}。'}
        if port_open(current['host'], current['port']):
            return {**status(), 'started': True, 'launcher': label,
                    'detail': f'已启动（{label}，日志 {LOG}）'}
        time.sleep(0.5)
    return {**current, 'started': False,
            'detail': (f'{label} 已启动但 {timeout:.0f} 秒内没有监听 {current["host"]}:{current["port"]}；'
                       f'Studio 使用自己的配置端口（见 {LOG}），请确认 AGENTSCOPE_STUDIO_URL 的端口与之一致。')}


def stop() -> None:
    record = managed()
    if record is None:
        STATE.unlink(missing_ok=True)
        return
    try:
        if os.getpgid(record['pid']) == record['pid']:
            os.killpg(record['pid'], signal.SIGTERM)
        else:
            os.kill(record['pid'], signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 10
    while _identity(record['pid']) is not None and time.monotonic() < deadline:
        time.sleep(0.1)
    if _identity(record['pid']) is not None:
        try:
            os.killpg(record['pid'], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    STATE.unlink(missing_ok=True)


def summary(state: dict) -> str:
    if not state['configured']:
        return '监控：未配置（.env 里没有 AGENTSCOPE_STUDIO_URL，追踪不上报）'
    where = f'{state["host"]}:{state["port"]}'
    if state['running']:
        return f'监控：运行中（{where}{"，由本脚本管理" if state["managed"] else "，由外部进程提供"}）'
    return f'监控：未运行（{where}）——{state["detail"]}；手动启动：npx -y @agentscope/studio'


def main() -> int:
    parser = argparse.ArgumentParser(description='AgentScope Studio 监控后台检查')
    parser.add_argument('action', choices=['check', 'start', 'stop', 'status'], nargs='?', default='check')
    parser.add_argument('--timeout', type=float, default=300.0, help='等待 Studio 就绪的秒数')
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'stop':
        stop()
        print('监控：已停止由本脚本启动的 Studio（外部进程未受影响）')
        return 0
    state = status() if args.action == 'status' else start(args.timeout)
    print(summary(state))
    if args.action in ('check', 'start') and state['configured'] and not state['running']:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
