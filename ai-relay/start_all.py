#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键启动 ai-relay 全套服务。

为什么需要它：
  Codex 要能用，UA 注入代理必须一直在跑；
  流水线要自动推进，看板和轮询也得在跑。
  三个进程手动启太麻烦，这里一把梭。

用法：
  python start_all.py                 # 代理 + 看板 + 轮询（默认 30 秒一轮）
  python start_all.py --no-watch      # 只起代理和看板
  python start_all.py --interval 60   # 轮询间隔改成 60 秒

Ctrl+C 一次性停掉全部进程。
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

PROCS: list[subprocess.Popen] = []


def port_in_use(port: int) -> bool:
    """探测端口是否已被占用（占用 = 服务已经在跑）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pid_alive(pid: int) -> bool:
    """判断进程是否存活（Windows + POSIX 通用）"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        import os
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pid_file_alive(name: str) -> bool:
    """检查 _state/<name>.pid 记录的进程是否仍在跑"""
    f = ROOT / "_state" / f"{name}.pid"
    if not f.exists():
        return False
    try:
        pid = int(f.read_text().strip())
    except (ValueError, OSError):
        return False
    return pid_alive(pid)


def start(script: str, *args: str, port: int | None = None, pid_name: str | None = None) -> subprocess.Popen | None:
    # 幂等检查 1：端口已被占用 → 服务已在跑
    if port is not None and port_in_use(port):
        print(f"  跳过 {script}（端口 {port} 已被占用，服务已在运行）")
        return None
    # 幂等检查 2：pid 文件记录的进程仍存活 → 已在跑（用于无固定端口的 watch）
    if pid_name is not None and pid_file_alive(pid_name):
        print(f"  跳过 {script}（已有进程在运行，pid 见 _state/{pid_name}.pid）")
        return None
    p = subprocess.Popen([PY, str(ROOT / script), *args], cwd=str(ROOT))
    PROCS.append(p)
    if pid_name is not None:
        f = ROOT / "_state" / f"{pid_name}.pid"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(p.pid))
    print(f"  启动 {script} (pid={p.pid})")
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description="一键启动 ai-relay")
    ap.add_argument("--no-watch", action="store_true", help="只起代理和看板，不跑自动轮询")
    ap.add_argument("--interval", type=int, default=30, help="轮询间隔（秒）")
    args = ap.parse_args()

    print("启动 ai-relay：")

    # 1. UA 注入代理 —— Codex 能连上中转的前提
    p1 = start("codex_ua_proxy.py", "--port", "8787", port=8787)
    time.sleep(1.5)

    # 2. 平台（网页界面）
    p2 = start("platform.py", "--port", "8767", port=8767)
    time.sleep(0.5)

    # 3. 流水线轮询（真正让任务自己跑起来的那个）
    # watch 没有固定端口，用 pid 文件去重
    if not args.no_watch:
        start("orchestrator.py", "watch", "--interval", str(args.interval), pid_name="watch")

    print("\n全部就绪：")
    print("  平台    http://127.0.0.1:8767")
    print("  代理    127.0.0.1:8787 -> tokken.cc")
    if not args.no_watch:
        print(f"  轮询    每 {args.interval} 秒推进一次流水线")

    # 如果没有任何新进程被启动（服务全都在跑），直接退出，不留空转进程
    if not PROCS:
        print("\n（所有服务已在运行，本次未启动新进程，直接退出）")
        return

    print("\nCtrl+C 停止全部")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止...")
        for p in PROCS:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(1)
        for p in PROCS:
            if p.poll() is None:
                p.kill()
        print("已停止")


if __name__ == "__main__":
    main()
