# -*- coding: utf-8 -*-
"""
AI 双AI流水线 桌面启动器
双击此程序：静默启动全套服务（UA代理 + 平台 + 轮询），然后打开浏览器
"""
import subprocess
import time
import webbrowser
import sys
import os
import shutil
from pathlib import Path

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
AI_RELAY_DIR = APP_DIR / "ai-relay" if (APP_DIR / "ai-relay").is_dir() else APP_DIR
def find_python():
    if not getattr(sys, "frozen", False):
        return sys.executable
    configured = os.environ.get("AI_RELAY_PYTHON")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("python") or shutil.which("python3")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    installs = sorted((local / "Programs" / "Python").glob("Python*/python.exe"),
                      reverse=True)
    return str(installs[0]) if installs else None


PYTHON = find_python()
PLATFORM_URL = "http://127.0.0.1:8767"

# 隐藏控制台窗口的启动标志（仅 Windows）
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def is_service_up(url: str, timeout: float = 0.8) -> bool:
    """探测平台服务是否已经在跑"""
    try:
        import urllib.request
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def start_services():
    """后台启动全套服务（start_all.py 内部会做幂等检查，重复启动不会叠加进程）"""
    if not PYTHON:
        return
    cmd = [PYTHON, str(AI_RELAY_DIR / "start_all.py"), "--interval", "45"]
    try:
        subprocess.Popen(
            cmd,
            cwd=str(AI_RELAY_DIR),
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def open_browser(url: str):
    """打开浏览器，webbrowser 失败则用系统默认方式兜底"""
    try:
        webbrowser.open(url)
        return
    except Exception:
        pass
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", url],
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def show_start_error():
    message = ("本地平台没有启动成功。请确认 Python 3.10+ 已安装并加入 PATH，"
               "或设置 AI_RELAY_PYTHON 指向 python.exe。")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "AI 多 Agent 流水线", 0x10)
    except Exception:
        pass


def main():
    # 1. 先探测服务是否已在跑（避免重复启动叠加进程）
    if not is_service_up(PLATFORM_URL):
        start_services()
        # 等服务起来（最多等 8 秒）
        for _ in range(16):
            time.sleep(0.5)
            if is_service_up(PLATFORM_URL):
                break
    if not is_service_up(PLATFORM_URL):
        show_start_error()
        return
    # 2. 打开浏览器（同步执行，不依赖守护线程，保证一定能打开）
    open_browser(PLATFORM_URL)


if __name__ == "__main__":
    main()
