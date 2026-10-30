#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai-relay 平台服务端
==================
提供完整 REST API + 单页应用界面。零第三方依赖，`python platform.py` 直接跑。

端口默认 8767（与旧版平台 8766 错开，允许新旧版本并行）。

API:
  GET  /api/status                 全局状态：Codex、代理、统计
  GET  /api/tasks                  任务列表（含派生字段与文件索引）
  GET  /api/task/<id>              单任务详情（含状态流转历史）
  GET  /api/file?path=<相对路径>    读取 ai-relay 内的文件（越界拒绝）
  GET  /api/logs                   最近的日志
  GET  /api/probe                  主动探测 Codex 连通性
  POST /api/task                   新建任务
  POST /api/task/<id>/advance      推进一步
  POST /api/task/<id>/action       干预：reset / giveup / reroute
"""

from __future__ import annotations

import json
import queue
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import orchestrator as orch
import agents
import workbuddy_config

ROOT = orch.ROOT
HTML_FILE = ROOT / "platform.html"
GAME_FILE = ROOT / "game.html"
GAME_2048_FILE = ROOT / "2048.html"
PROXY_PORT = 8787
DEFAULT_PORT = 8767

# FlClash 可执行文件（用于「一键启动代理」）
FLCLASH_EXE = r"D:\Program Files\FlClash\FlClash.exe"

# ---- 实时事件总线：Codex 的每个事件都会广播给所有订阅者 ----
# 结构：{ "bus_id": {"queue": queue.Queue, "last_seen": timestamp} }
# 用线程安全队列做 pub/sub，SSE 订阅者从队列取事件推给浏览器。
_EVENTS: dict[str, dict] = {}
_EVENTS_LOCK = threading.Lock()

# 有订阅者在等的总线（用于清理）
def subscribe(bus_id: str) -> "queue.Queue":
    with _EVENTS_LOCK:
        b = _EVENTS.setdefault(bus_id, {"q": queue.Queue(), "last_seen": 0.0})
        return b["q"]


def publish(bus_id: str, evt: dict) -> None:
    if evt.get("type") == "reasoning":
        return
    with _EVENTS_LOCK:
        b = _EVENTS.get(bus_id)
        if not b:
            return
        import time as _t
        b["last_seen"] = _t.time()
        b["q"].put(evt)

STAGE_LABEL = {
    orch.NEW: "待出题",
    orch.PROMPT_READY: "待执行",
    orch.DONE: "待验收",
    orch.PASS: "已通过",
    orch.FAIL: "未通过",
    orch.GIVE_UP: "已终止",
    "BLOCKED": "已阻塞",
    orch.WAITING_OPTIMIZE: "等待出题",
    orch.WAITING_EXECUTE: "等待执行",
    orch.WAITING_REVIEW: "等待验收",
    orch.WAITING_CODEX: "等待 Codex",
    orch.AWAIT_MODEL_CONFIRM: "待确认模型",
    orch.ASSESSED: "已评估",
    orch.DELIBERATING: "方案博弈中",
    orch.DELIB_PAUSED: "博弈已暂停",
    orch.DELIB_READY: "待选方案",
}


# ---------------------------------------------------------------- 工具

def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def is_flclash_running() -> bool:
    """FlClash 是否在跑：进程存在，或本机任一翻墙端口在监听。"""
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq FlClash.exe"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        if "FlClash.exe" in out:
            return True
    except Exception:
        pass
    for p in (7890, 7891, 7897, 1080, 10808, 2080):
        if port_open(p):
            return True
    return False


def find_upstream_proxy() -> str | None:
    """探测本机可用的翻墙端口，返回第一个能用的。

    实测教训（2026-10-19）：gstatic 做金丝雀会误报——当前节点到 gstatic 不通，
    但到 tokken.cc 是通的，链路明明可用却被判"未通"，吓得人白排查。
    所以改成：优先直测真正的依赖 tokken.cc（带 token 打 /v1/models），
    gstatic 只作兜底参考。
    """
    import urllib.request, urllib.error
    token = ""
    try:
        import re
        cfg = Path.home() / ".codex" / "config.toml"
        m = re.search(r'experimental_bearer_token\s*=\s*"([^"]+)"', cfg.read_text(encoding="utf-8"))
        token = m.group(1) if m else ""
    except Exception:
        pass

    def _can_reach(px: str, url: str, hdr: dict | None = None) -> bool:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": px, "https": px}))
        try:
            req = urllib.request.Request(url, headers=hdr or {})
            opener.open(req, timeout=5)
            return True
        except urllib.error.HTTPError:
            # 401/403 也算"通"：能拿到 HTTP 响应就说明链路可达，只是没带凭证
            return True
        except Exception:
            return False

    hdrs = {"Authorization": f"Bearer {token}"} if token else {}
    for p in (7890, 7891, 7897, 1080, 10808, 2080):
        if not port_open(p):
            continue
        px = f"http://127.0.0.1:{p}"
        if _can_reach(px, "https://tokken.cc/v1/models", hdrs):
            return px
    # 兜底：tokken.cc 探测全部失败时，再看通用出墙
    for p in (7890, 7891, 7897, 1080, 10808, 2080):
        px = f"http://127.0.0.1:{p}"
        if port_open(p) and _can_reach(px, "https://www.gstatic.com/generate_204"):
            return px
    return None


def compute_stats(state: dict) -> dict:
    tasks = list(state.get("tasks", {}).values())
    stat = {
        "total": len(tasks), "active": 0, "passed": 0, "failed": 0,
        "waiting": 0, "giveup": 0, "pass_rate": None,
        "avg_rounds": 0, "total_calls": 0,
    }
    if not tasks:
        return stat

    def cnt(st):
        return sum(1 for t in tasks if t.get("status") == st)

    passed, failed = cnt(orch.PASS), cnt(orch.FAIL)
    judged = passed + failed
    stat.update({
        "passed": passed,
        "failed": failed,
        "giveup": cnt(orch.GIVE_UP),
        "waiting": sum(1 for t in tasks
                       if t.get("status") in orch.WAITING_STATES or t.get("status") == orch.WAITING_CODEX),
        "active": sum(1 for t in tasks
                      if t.get("status") not in (orch.PASS, orch.GIVE_UP, "BLOCKED")),
        "pass_rate": round(passed / judged * 100) if judged else None,
        "avg_rounds": round(sum(t.get("round", 1) for t in tasks) / len(tasks), 1),
        "total_calls": sum(t.get("calls", 0) or 0 for t in tasks),
    })
    return stat


def task_view(t: dict) -> dict:
    """补充派生字段，前端拿来就能渲染。"""
    v = dict(t)
    v.pop("last_codex_msg", None)
    tid = t["id"]
    files: dict = {}

    p = orch.PROMPTS / f"{tid}.md"
    if p.exists():
        files["prompt"] = str(p.relative_to(ROOT))

    d = orch.OUTPUT / tid
    if d.exists():
        files["outputs"] = [str(x.relative_to(ROOT))
                            for x in sorted(d.rglob("*")) if x.is_file()]

    r = orch.REVIEW / f"{tid}.md"
    if r.exists():
        files["review"] = str(r.relative_to(ROOT))

    if t.get("waiting_agent") == "trae":
        handoff = orch.handoff_file("trae", tid)
        if handoff.exists():
            files["handoff"] = str(handoff.relative_to(ROOT))

    v["files"] = files
    v["stage_cn"] = STAGE_LABEL.get(t.get("status"), t.get("status"))
    v["on_hold"] = t.get("status") in orch.WAITING_STATES or t.get("status") == orch.WAITING_CODEX
    v["max_calls"] = orch.MAX_CALLS
    v["max_rounds"] = orch.MAX_ROUNDS
    return v


AGENT_ROLES = {
    "assess": ("难度评估", "assess"),
    "optimize": ("提示词优化", "optimize"),
    "execute": ("任务执行", "execute"),
    "review": ("独立验收", "review"),
    "chat": ("实时对话", "chat"),
}

WAITING_ROLE = {
    orch.WAITING_OPTIMIZE: "optimize",
    orch.WAITING_EXECUTE: "execute",
    orch.WAITING_REVIEW: "review",
}


def workbuddy_save(payload: dict) -> tuple[int, dict]:
    """保存 WorkBuddy 配置。返回 (HTTP 状态码, 响应体)。

    抽成函数而不是写在 handler 里，是为了能直接被单测调用 —— 走 HTTP 测一遍
    既慢又要处理端口占用，覆盖不到"校验失败"这类分支。
    """
    try:
        cfg = workbuddy_config.save(payload if isinstance(payload, dict) else {})
    except workbuddy_config.ConfigError as exc:
        orch.log(f"WorkBuddy 配置写入被拒绝：{exc.message}")
        return 400, exc.as_dict()
    except Exception as exc:  # 兜底：任何异常都不能让服务 500 掉线
        return 500, {"ok": False, "error": f"保存失败：{type(exc).__name__}: {exc}"}

    orch.log(f"WorkBuddy 配置已更新：model={cfg.get('model') or '(未指定)'} "
             f"poll={cfg.get('poll_seconds')}s "
             f"timeout={cfg.get('handoff_timeout_minutes')}min "
             f"auto_resume={cfg.get('auto_resume')}")
    # 改了配置立刻重建待办队列：模型名写进了交接指令，不重建就还是旧值
    try:
        with orch.FileLock(orch.LOCK_FILE):
            orch.refresh_pending(orch.load_state())
    except Exception as exc:
        return 200, {"ok": True, "config": cfg,
                     "warning": f"配置已保存，但待办队列重建失败：{exc}"}
    return 200, {"ok": True, "config": cfg}


def workbuddy_reset() -> tuple[int, dict]:
    try:
        cfg = workbuddy_config.reset()
    except workbuddy_config.ConfigError as exc:
        return 400, exc.as_dict()
    orch.log("WorkBuddy 配置已恢复默认")
    return 200, {"ok": True, "config": cfg}


def workbuddy_resume() -> tuple[int, dict]:
    summary = orch.restore_unfinished()
    _LAST_RESUME.clear()
    _LAST_RESUME.update(summary)
    payload = {"ok": bool(summary.get("ok")), "resume": summary}
    try:
        payload["queue"] = orch.pending_queue_info()
    except Exception as exc:
        payload["queue"] = {"error": str(exc)}
    return 200, payload


# 最近一次"启动恢复"的结果，供界面显示（进程内缓存，重启即重置）
_LAST_RESUME: dict = {}


def workbuddy_view() -> dict:
    """WorkBuddy 配置视图：值 + 字段说明 + 文件位置 + 告警 + 恢复状态。

    读失败不抛异常：load() 已经做过降级，这里把告警原样带回界面，
    让用户看到"用的是默认值"而不是以为自己设的模型生效了。
    """
    cfg, warnings = workbuddy_config.ensure_file()
    try:
        queue = orch.pending_queue_info()
    except Exception as exc:
        queue = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "config": cfg,
        "fields": workbuddy_config.describe(),
        "defaults": dict(workbuddy_config.DEFAULTS),
        "file": workbuddy_config.status(),
        "warnings": warnings,
        "queue": queue,
        "last_resume": _LAST_RESUME,
    }


def agent_activity_view(cfg: dict, state: dict, activity: dict) -> dict:
    """Expose only observable call and handoff state; never infer activity from config."""
    backends = cfg.get("backends", {})
    configured_roles = cfg.get("roles", {})
    active = list((activity.get("active") or {}).values())
    last_calls = activity.get("last") or {}
    totals = activity.get("totals") or {}
    tasks = list((state.get("tasks") or {}).values())
    tasks.sort(key=lambda t: t.get("updated", t.get("created", "")), reverse=True)

    codex_waiting: dict[str, dict] = {}
    for task in tasks:
        if task.get("status") != orch.WAITING_CODEX:
            continue
        resume = task.get("resume_status", orch.NEW)
        if resume == orch.NEW:
            role = "assess" if orch.model_routing_on(task) and not task.get("model_locked") else "optimize"
        else:
            role = {orch.PROMPT_READY: "execute", orch.DONE: "review"}.get(resume)
        if role and role not in codex_waiting:
            codex_waiting[role] = task

    rows = []
    for role, (role_label, config_key) in AGENT_ROLES.items():
        backend_name = ("codex" if role == "chat"
                        else configured_roles.get(config_key, "codex"))
        backend = backends.get(backend_name, {})
        matching = [call for call in active if call.get("role") == role]
        matching.sort(key=lambda c: c.get("started_epoch", 0), reverse=True)
        item = {
            "role": role,
            "role_label": role_label,
            "backend": backend_name,
            "backend_label": backend.get("label", backend_name),
            "model": backend.get("model", ""),
            "status": "idle",
            "task_id": "",
            "phase": "",
            "detail": "",
            "updated_at": "",
            "started_epoch": None,
            "count": int(totals.get(role, 0)),
            "active_count": len(matching),
        }

        if matching:
            call = matching[0]
            call_backend = call.get("backend", backend_name)
            item.update({
                "status": "running",
                "backend": call_backend,
                "backend_label": backends.get(call_backend, {}).get("label", call_backend),
                "model": call.get("model") or item["model"],
                "task_id": call.get("task_id", ""),
                "phase": call.get("phase", ""),
                "detail": "正在向模型发送请求并等待响应",
                "updated_at": call.get("started_at", ""),
                "started_epoch": call.get("started_epoch"),
            })
        else:
            waiting = next((t for t in tasks
                            if WAITING_ROLE.get(t.get("status")) == role
                            and t.get("waiting_agent")), None)
            if waiting:
                handoff = waiting.get("waiting_agent", "workbuddy")
                handoff_label = backends.get(handoff, {}).get("label", handoff)
                item.update({
                    "status": "waiting",
                    "backend": handoff,
                    "backend_label": handoff_label,
                    "model": "",
                    "task_id": waiting.get("id", ""),
                    "phase": role,
                    "detail": f"已登记给 {handoff_label}；外部程序的内部运行状态无法从此处确认",
                    "updated_at": waiting.get("updated", waiting.get("created", "")),
                })
            elif role in codex_waiting:
                waiting = codex_waiting[role]
                item.update({
                    "status": "waiting_codex",
                    "backend": "codex",
                    "backend_label": backends.get("codex", {}).get("label", "Codex"),
                    "task_id": waiting.get("id", ""),
                    "phase": role,
                    "detail": "任务已排队，等待 Codex 服务恢复后继续",
                    "updated_at": waiting.get("updated", waiting.get("created", "")),
                })
            else:
                last = last_calls.get(role) or {}
                status = last.get("status", "idle")
                item.update({
                    "status": status,
                    "backend": last.get("backend", backend_name),
                    "backend_label": backends.get(
                        last.get("backend", backend_name), {}).get(
                            "label", last.get("backend", backend_name)),
                    "model": last.get("model") or item["model"],
                    "task_id": last.get("task_id", ""),
                    "phase": last.get("phase", ""),
                    "detail": {
                        "success": "最近一次调用已完成",
                        "error": "最近一次调用失败",
                        "interrupted": "最近一次调用中断或超时",
                    }.get(status, "尚无实际调用记录"),
                    "updated_at": last.get("finished_at", last.get("started_at", "")),
                })
        rows.append(item)

    return {"updated_at": orch.now(), "roles": rows}


def safe_read(rel: str) -> tuple[bool, str]:
    """只允许读 ai-relay 目录内的文件，挡住 ../ 之类的越界。"""
    try:
        p = (ROOT / rel).resolve()
    except Exception:
        return False, "路径非法"
    try:
        p.relative_to(ROOT.resolve())
    except ValueError:
        return False, "越界访问被拒绝"
    parts = p.relative_to(ROOT.resolve()).parts
    if parts and parts[0] == "_logs":
        return False, "内部诊断日志不通过网页文件接口公开"
    if len(parts) > 1 and parts[0] == "_state" and parts[1] == "events":
        return False, "任务事件请通过过程记录接口读取"
    if not p.is_file():
        return False, "文件不存在或不是普通文件"
    try:
        return True, p.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"读取失败：{e}"


# ---------------------------------------------------------------- 处理

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, data, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _html(self):
        data = HTML_FILE.read_bytes() if HTML_FILE.exists() else b"<h1>platform.html missing</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # 不禁缓存的话，浏览器会拿旧页面：用户改了代码却"看不到更新"（实测踩过）
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _game(self):
        data = GAME_FILE.read_bytes() if GAME_FILE.exists() else b"<h1>game.html missing</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _game_2048(self):
        data = GAME_2048_FILE.read_bytes() if GAME_2048_FILE.exists() else b"<h1>2048.html missing</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------------- GET

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path in ("/", "/platform", "/index.html"):
            return self._html()

        if path in ("/game", "/game.html"):
            return self._game()

        if path in ("/2048", "/2048.html"):
            return self._game_2048()

        if path == "/api/status":
            return self._json({
                "codex_available": orch.codex_available(),
                "codex_bin": str(orch.CODEX_BIN) if orch.CODEX_BIN else None,
                "base_url": orch.get_codex_base_url(),
                "proxy_running": port_open(PROXY_PORT),
                "proxy_port": PROXY_PORT,
                "stats": compute_stats(orch.load_state()),
                "server_time": orch.now(),
                "max_rounds": orch.MAX_ROUNDS,
                "max_calls": orch.MAX_CALLS,
                "flclash_running": is_flclash_running(),
                "upstream_proxy": find_upstream_proxy(),
                "model_candidates": orch.MODEL_CANDIDATES,
                "model_confirm_window": orch.MODEL_CONFIRM_WINDOW,
            })

        if path == "/api/tasks":
            st = orch.load_state()
            views = [task_view(t) for t in st.get("tasks", {}).values()]
            views.sort(key=lambda x: x.get("created", ""), reverse=True)
            return self._json({"tasks": views, "stats": compute_stats(st)})

        # ---- 可核验的任务阶段事件回放 ----
        # 注意：必须放在通用 /api/task/<id> 分支之前，否则 "xxx/events"
        # 会被当成任务 ID 撞 404（实测踩过）。
        if path.startswith("/api/task/") and path.endswith("/events"):
            tid = path[len("/api/task/"):-len("/events")]
            ev_file = orch.EVENTS_DIR / f"{tid}.jsonl"
            events = []
            if ev_file.exists():
                try:
                    lines = ev_file.read_text(encoding="utf-8").splitlines()
                    for ln in lines[-400:]:
                        try:
                            evt = json.loads(ln)
                            if evt.get("type") != "reasoning":
                                events.append(evt)
                        except Exception:
                            pass
                except Exception:
                    pass
            return self._json({"ok": True, "events": events})

        # ---- Open the current TRAE file-handoff workspace ----
        if path.startswith("/api/task/") and path.endswith("/trae/open"):
            tid = path[len("/api/task/"):-len("/trae/open")]
            task = orch.load_state().get("tasks", {}).get(tid)
            if not task:
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            if task.get("waiting_agent") != "trae" or task.get("status") not in orch.WAITING_STATES:
                return self._json({"ok": False, "error": "该任务当前没有等待 TRAE 接力"}, 409)
            ok, message = agents.open_trae(orch.ROOT)
            return self._json({"ok": ok, "msg": message}, 200 if ok else 503)

        # ---- 一键打开产出文件夹（资源管理器）----
        if path.startswith("/api/task/") and path.endswith("/open"):
            tid = path[len("/api/task/"):-len("/open")]
            out_dir = orch.OUTPUT / tid
            if not out_dir.exists():
                return self._json({"ok": False, "error": "该任务还没有产出目录"})
            import os
            os.startfile(str(out_dir))  # Windows 资源管理器打开
            return self._json({"ok": True, "path": str(out_dir)})

        # ---- 产出文件原文服务（html 可直接浏览器预览）----
        m = re.match(r"^/api/task/([^/]+)/file$", path)
        if m:
            tid = m.group(1)
            rel = (qs.get("name") or [""])[0]
            base = (orch.OUTPUT / tid).resolve()
            if not base.exists():
                return self._json({"error": "产出目录不存在"}, 404)
            try:
                p = (base / rel).resolve()
                p.relative_to(base)
            except (ValueError, OSError):
                return self._json({"error": "路径非法"}, 400)
            if not p.is_file():
                return self._json({"error": "文件不存在"}, 404)
            import mimetypes
            ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            if p.suffix.lower() in (".md", ".markdown", ".log", ".txt", ".json"):
                ctype = "text/plain; charset=utf-8"
            elif p.suffix.lower() in (".html", ".htm"):
                ctype = "text/html; charset=utf-8"
            data = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # ---- 通用任务详情（放在各子路由之后兜底）----
        if path.startswith("/api/task/"):
            tid = path[len("/api/task/"):]
            st = orch.load_state()
            t = st.get("tasks", {}).get(tid)
            if not t:
                return self._json({"error": "任务不存在"}, 404)
            return self._json({"task": task_view(t)})

        if path == "/api/file":
            rel = (qs.get("path") or [""])[0]
            ok, content = safe_read(rel)
            return self._json({"ok": ok, "content": content, "path": rel})

        if path == "/api/logs":
            logs = sorted(orch.LOGS.glob("*.log"), reverse=True)
            out = []
            for f in logs[:3]:
                try:
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                    out.append({"file": f.name, "lines": lines[-200:]})
                except Exception:
                    pass
            return self._json({"logs": out})

        if path == "/api/probe":
            ok, msg = orch.probe_codex()
            if ok:
                orch.mark_codex_ok()
                orch.OFFLINE.update(on=False, reason="", last_probe=0.0)
            return self._json({"ok": ok, "msg": msg})

        if path == "/api/flclash":
            return self._json({
                "running": is_flclash_running(),
                "exe": FLCLASH_EXE,
                "proxy": find_upstream_proxy(),
            })

        # ---- 实时监控：SSE 事件流 ----
        if path == "/api/events":
            bus_id = (qs.get("bus") or ["global"])[0]
            q = subscribe(bus_id)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            try:
                while True:
                    try:
                        evt = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    data = json.dumps(evt, ensure_ascii=False)
                    self.wfile.write(f"event: msg\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        if path == "/api/health":
            return self._json({"ok": True})

        # ---- 多 Agent 实时活动 ----
        if path == "/api/agents/activity":
            return self._json(agent_activity_view(
                agents.load_config(), orch.load_state(), orch.load_agent_activity()))

        # ---- 多 Agent 配置（api_key 打码返回）----
        if path == "/api/agents":
            cfg = agents.load_config()
            for b in cfg.get("backends", {}).values():
                k = b.get("api_key") or ""
                if k:
                    b["api_key"] = (k[:4] + "****") if len(k) > 8 else "****"
            return self._json({"ok": True, **cfg})

        # ---- WorkBuddy 配置（模型 / 轮询 / 超时 / 自动恢复）----
        if path == "/api/workbuddy":
            return self._json(workbuddy_view())

        # ---- 未完成任务恢复状态（只读）----
        if path == "/api/workbuddy/resume":
            return self._json({
                "ok": True,
                "queue": orch.pending_queue_info(),
                "last_resume": _LAST_RESUME,
            })

        return self._json({"error": "not found"}, 404)

    # ---------------- POST

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        # ---- 投递本地文件到 00_inbox 并（可选）建任务 ----
        # 两种方式：
        #   1) 浏览器上传：传 content（base64 文件字节）+ name
        #   2) 本地脚本/Electron：传 src（绝对路径）让服务端去拷
        if path == "/api/drop":
            import base64
            import shutil
            name = (payload.get("name") or "").strip()
            req = (payload.get("requirement") or "").strip()
            if not name:
                return self._json({"ok": False, "error": "缺少文件名"})
            try:
                (orch.INBOX / name).parent.mkdir(parents=True, exist_ok=True)
                dst = orch.INBOX / name
                if payload.get("content"):
                    dst.write_bytes(base64.b64decode(payload["content"]))
                elif payload.get("src"):
                    sp = Path(payload["src"].strip())
                    if not sp.is_file():
                        return self._json({"ok": False, "error": "源文件不存在或不是文件"})
                    shutil.copy2(sp, dst)
                else:
                    return self._json({"ok": False, "error": "缺少文件内容或来源路径"})
            except Exception as e:
                return self._json({"ok": False, "error": f"写入失败：{e}"})
            orch.log(f"平台投递文件 {name} -> {dst}")
            if req:
                with orch.FileLock(orch.LOCK_FILE):
                    st = orch.load_state()
                    tid = orch.new_id(st)
                    task = {
                        "id": tid, "requirement": req,
                        "optimizer": "auto", "executor": "auto", "reviewer": "auto",
                        "status": orch.NEW, "round": 1, "calls": 0,
                        "created": orch.now(), "updated": orch.now(), "history": [],
                        "dropped_file": name,
                    }
                    st["tasks"][tid] = task
                    orch.save_state(st)
                orch.log(f"平台投递创建任务 {tid}（绑定 {name}）", tid)
                return self._json({"ok": True, "file": name, "id": tid})
            return self._json({"ok": True, "file": name})

        if path == "/api/flclash/start":
            import subprocess
            if is_flclash_running():
                return self._json({"ok": True, "msg": "FlClash 已在运行"})
            try:
                subprocess.Popen([FLCLASH_EXE], shell=False)
                return self._json({"ok": True, "msg": "已启动 FlClash，等待几秒后它会开始监听端口"})
            except Exception as e:
                return self._json({"ok": False, "msg": f"启动失败：{e}"})

        # ---- 直接对话 Codex（不走流水线，实时回显）----
        if path == "/api/chat":
            msg = (payload.get("message") or "").strip()
            if not msg:
                return self._json({"ok": False, "error": "消息不能为空"})
            bus = payload.get("bus", "chat")

            def _run():
                publish(bus, {"type": "system", "message": f"已发送给 Codex：{msg[:60]}"})
                ok, reply = orch.run_agent_call(
                    "chat", "codex", "", "chat/direct", "", 600,
                    lambda: orch.call_codex(
                        msg, orch.ROOT, "chat/direct", timeout=600,
                        on_event=lambda evt: publish(bus, evt),
                    ))
                publish(bus, {"type": "system", "message": "调用结束",
                              "ok": ok})
                if not ok:
                    publish(bus, {"type": "error", "message": reply[:300]})

            threading.Thread(target=_run, daemon=True).start()
            return self._json({"ok": True, "bus": bus, "msg": "已开始"})

        # ---- 保存多 Agent 配置（角色绑定 + 后端参数）----
        if path == "/api/agents":
            cfg = agents.load_config()
            roles = payload.get("roles")
            if isinstance(roles, dict):
                for r in ("assess", "optimize", "execute", "review"):
                    v = str(roles.get(r, "")).strip()
                    if not v:
                        continue
                    backend = cfg["backends"].get(v, {})
                    allowed = (v in agents.EXECUTE_ALLOWED if r == "execute"
                               else backend.get("type") in ("codex_cli", "openai"))
                    if not allowed:
                        allowed_hint = "/".join(agents.EXECUTE_ALLOWED) if r == "execute" else "文本型后端"
                        return self._json({"ok": False,
                                           "error": f"{r} 角色只能绑定 {allowed_hint}"})
                    cfg["roles"][r] = v
            backends = payload.get("backends")
            if isinstance(backends, dict):
                for name, fields in backends.items():
                    b = cfg["backends"].get(name)
                    if not isinstance(b, dict) or not isinstance(fields, dict):
                        continue
                    for k in ("model", "base_url", "label"):
                        if fields.get(k):
                            b[k] = str(fields[k]).strip()
                    k = str(fields.get("api_key") or "")
                    # 打码值/空值 = 保持不变，避免把 **** 存成真 key
                    if k and "****" not in k:
                        b["api_key"] = k
            agents.save_config(cfg)
            orch.log(f"多 Agent 配置已更新：{cfg.get('roles')}")
            return self._json({"ok": True, "roles": cfg["roles"]})

        # ---- 保存 WorkBuddy 配置（模型 / 轮询 / 超时 / 自动恢复）----
        if path == "/api/workbuddy":
            status, body = workbuddy_save(payload)
            return self._json(body, status)

        # ---- 恢复默认 ----
        if path == "/api/workbuddy/reset":
            status, body = workbuddy_reset()
            return self._json(body, status)

        # ---- 手动触发"恢复未完成任务"----
        if path == "/api/workbuddy/resume":
            status, body = workbuddy_resume()
            return self._json(body, status)

        # ---- 后端连通性自检 ----
        if path == "/api/agents/test":
            name = (payload.get("backend") or "").strip()
            ok, msg = agents.test_backend(name)
            return self._json({"ok": ok, "msg": msg})

        if path == "/api/task":
            req = (payload.get("requirement") or "").strip()
            if not req:
                return self._json({"ok": False, "error": "需求不能为空"})
            preset = dict(orch.MODE_PRESETS.get(payload.get("mode"), {}))
            if payload.get("on_unavailable"):
                preset["on_unavailable"] = payload["on_unavailable"]
            deliberate = bool(payload.get("deliberate"))

            with orch.FileLock(orch.LOCK_FILE):
                st = orch.load_state()
                tid = orch.new_id(st)
                task = {
                    "id": tid, "requirement": req,
                    "optimizer": preset.get("optimizer", payload.get("optimizer", "auto")),
                    "executor": preset.get("executor", payload.get("executor", "auto")),
                    "reviewer": preset.get("reviewer", payload.get("reviewer", "auto")),
                    "status": orch.NEW, "round": 1, "calls": 0,
                    "created": orch.now(), "updated": orch.now(), "history": [],
                    "model_route": payload.get("model_route", orch.MODEL_ROUTING_DEFAULT),
                }
                if "on_unavailable" in preset:
                    task["on_unavailable"] = preset["on_unavailable"]
                if deliberate:
                    task["status"] = orch.DELIBERATING
                    orch.init_deliberation(task)
                st["tasks"][tid] = task
                orch.save_state(st)
            orch.log(f"平台创建任务 {tid}" + ("（先方案博弈）" if deliberate else ""), tid)
            if deliberate:
                threading.Thread(target=self._delib_bg, args=(tid,), daemon=True).start()
            return self._json({"ok": True, "id": tid, "deliberate": deliberate})

        if path.startswith("/api/task/") and path.endswith("/advance"):
            tid = path[len("/api/task/"):-len("/advance")]
            threading.Thread(target=self._advance_bg, args=(tid,), daemon=True).start()
            return self._json({"ok": True, "msg": "已在后台推进"})

        # ---- 多 AI 协作 · 方案博弈：开始 / 叫停 / 继续 / 采纳 ----
        if path.startswith("/api/task/") and path.endswith("/deliberate"):
            tid = path[len("/api/task/"):-len("/deliberate")]
            with orch.FileLock(orch.LOCK_FILE):
                st = orch.load_state()
                t = st.get("tasks", {}).get(tid)
                if not t:
                    return self._json({"ok": False, "error": "任务不存在"})
                if t.get("status") in (orch.PASS, orch.GIVE_UP):
                    return self._json({"ok": False, "error": "任务已结束，无法再博弈"})
                if not t.get("deliberation"):
                    orch.init_deliberation(t)
                t["hold"] = False
                t["status"] = orch.DELIBERATING
                t["updated"] = orch.now()
                t.setdefault("history", []).append(
                    {"t": orch.now(), "from": "manual", "to": orch.DELIBERATING, "action": "deliberate"})
                orch.save_state(st)
            threading.Thread(target=self._delib_bg, args=(tid,), daemon=True).start()
            return self._json({"ok": True, "msg": "多 AI 协作已启动"})

        if path.startswith("/api/task/") and path.endswith("/halt"):
            tid = path[len("/api/task/"):-len("/halt")]
            with orch.FileLock(orch.LOCK_FILE):
                st = orch.load_state()
                t = st.get("tasks", {}).get(tid)
                if not t:
                    return self._json({"ok": False, "error": "任务不存在"})
                t["hold"] = True
                if t.get("status") == orch.DELIBERATING:
                    t["status"] = orch.DELIB_PAUSED
                t["updated"] = orch.now()
                orch.save_state(st)
            orch.log("人工叫停方案博弈", tid)
            return self._json({"ok": True, "status": orch.DELIB_PAUSED})

        if path.startswith("/api/task/") and path.endswith("/resume"):
            tid = path[len("/api/task/"):-len("/resume")]
            with orch.FileLock(orch.LOCK_FILE):
                st = orch.load_state()
                t = st.get("tasks", {}).get(tid)
                if not t:
                    return self._json({"ok": False, "error": "任务不存在"})
                t["hold"] = False
                t["status"] = orch.DELIBERATING
                t["updated"] = orch.now()
                orch.save_state(st)
            threading.Thread(target=self._delib_bg, args=(tid,), daemon=True).start()
            return self._json({"ok": True, "msg": "已继续博弈"})

        if path.startswith("/api/task/") and path.endswith("/adopt"):
            tid = path[len("/api/task/"):-len("/adopt")]
            key = str(payload.get("key") or "").strip().upper()
            with orch.FileLock(orch.LOCK_FILE):
                st = orch.load_state()
                t = st.get("tasks", {}).get(tid)
                if not t:
                    return self._json({"ok": False, "error": "任务不存在"})
                d = t.get("deliberation") or {}
                cands = d.get("candidates") or []
                chosen = next((c for c in cands if c.get("key") == key), None)
                if not chosen and d.get("synthesis"):
                    chosen = {"key": key or "自定义", "text": d["synthesis"]}
                if not chosen:
                    return self._json({"ok": False, "error": "还没有可用方案，请先等博弈产出"})
                t["requirement"] = (t.get("requirement", "") +
                                    "\n\n【已选用的设计方案】\n" + chosen["text"])
                d["adopted"] = chosen.get("key", key)
                t["deliberation"] = d
                t["status"] = orch.NEW
                t["round"] = 1
                t["calls"] = 0
                t["model_route"] = False
                t["model_locked"] = True
                t.pop("hold", None)
                t["updated"] = orch.now()
                t.setdefault("history", []).append(
                    {"t": orch.now(), "from": "manual", "to": orch.NEW, "action": f"adopt:{chosen.get('key','')}"})
                orch.save_state(st)
            orch.log(f"采纳方案 {chosen.get('key','')}，进入建造流水线", tid)
            threading.Thread(target=self._advance_bg, args=(tid,), daemon=True).start()
            return self._json({"ok": True, "msg": f"已采纳方案 {chosen.get('key','')}，开始建造"})

        # ---- 人工确认难度路由的选用模型 ----
        if path.startswith("/api/task/") and path.endswith("/model"):
            tid = path[len("/api/task/"):-len("/model")]
            model = payload.get("model") or None
            with orch.FileLock(orch.LOCK_FILE):
                st = orch.load_state()
                ok, msg = orch.confirm_model(st, tid, model)
                if ok:
                    orch.save_state(st)
                    orch.refresh_model_confirm(st)
                    orch.save_state(st)
                else:
                    return self._json({"ok": False, "error": msg})
            # 确认后后台继续推进，省去等下一轮询
            threading.Thread(target=self._advance_bg, args=(tid,), daemon=True).start()
            return self._json({"ok": True, "msg": msg})

        if path.startswith("/api/task/") and path.endswith("/action"):
            tid = path[len("/api/task/"):-len("/action")]
            action = payload.get("action")
            with orch.FileLock(orch.LOCK_FILE):
                st = orch.load_state()
                t = st.get("tasks", {}).get(tid)
                if not t:
                    return self._json({"ok": False, "error": "任务不存在"})

                if action == "reset":
                    t["status"] = orch.NEW
                    t["round"] = 1
                    t["calls"] = 0
                    orch.reset_prompt_optimization(t)
                    t.pop("verdict", None)
                    orch.log("人工重置到起点", tid)
                elif action == "giveup":
                    t["status"] = orch.GIVE_UP
                    t["giveup_reason"] = "人工终止"
                    orch.log("人工终止", tid)
                elif action == "reroute":
                    for k in ("optimizer", "executor", "reviewer"):
                        if payload.get(k):
                            t[k] = payload[k]
                    if payload.get("on_unavailable"):
                        t["on_unavailable"] = payload["on_unavailable"]
                    orch.log(f"人工改派：{payload}", tid)
                else:
                    return self._json({"ok": False, "error": f"未知动作 {action}"})

                t["updated"] = orch.now()
                t.setdefault("history", []).append(
                    {"t": orch.now(), "from": "manual", "to": t["status"], "action": action}
                )
                orch.save_state(st)
                orch.refresh_pending(st)
                orch.refresh_model_confirm(st)
                orch.save_state(st)
            return self._json({"ok": True, "status": t["status"]})

        return self._json({"error": "not found"}, 404)

    def _advance_bg(self, tid: str):
        try:
            with orch.FileLock(orch.LOCK_FILE):
                st = orch.load_state()
                t = st.get("tasks", {}).get(tid)
                if t:
                    orch.advance_task(t, st)
                    orch.save_state(st)
        except RuntimeError as e:
            orch.log(f"跳过推进：{e}", tid)
        except Exception as e:
            orch.log(f"推进出错：{e}", tid)

    def _delib_bg(self, tid: str):
        """方案博弈后台线程：逐角色/汇总/红队推进；模型调用在锁外，写完再上锁。

        每一步都重新读状态并检查 hold，用户叫停后立即停止（保留已产出）。
        """
        while True:
            with orch.FileLock(orch.LOCK_FILE):
                st = orch.load_state()
                t = st.get("tasks", {}).get(tid)
                if not t or t.get("status") != orch.DELIBERATING or t.get("hold"):
                    return
                step = orch.next_delib_step(t)
                if step.get("kind") == "done":
                    t["status"] = orch.DELIB_READY
                    t["updated"] = orch.now()
                    orch.save_state(st)
                    orch.log("博弈产出就绪，等待采纳方案", tid)
                    return
            # —— 锁外做模型调用，避免长时间占锁 ——
            ok, text = orch.call_model(step["messages"], step.get("model"))
            with orch.FileLock(orch.LOCK_FILE):
                st = orch.load_state()
                t = st.get("tasks", {}).get(tid)
                if not t:
                    return
                d = t.setdefault("deliberation", {})
                if not ok:
                    d["error"] = text
                    t["status"] = orch.DELIB_PAUSED
                    t["updated"] = orch.now()
                    orch.save_state(st)
                    orch.log(f"博弈调用失败：{text[:120]}", tid)
                    return
                orch.apply_delib_result(t, step, text)
                t["updated"] = orch.now()
                if t.get("hold"):
                    t["status"] = orch.DELIB_PAUSED
                orch.save_state(st)
                if t.get("status") == orch.DELIB_PAUSED:
                    return


def main():
    import argparse
    ap = argparse.ArgumentParser(description="ai-relay 平台")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ai-relay 平台已启动： http://127.0.0.1:{args.port}/")

    # 启动即恢复现场：state.json 只是记录，_PENDING_FOR_WORKBUDDY.md 才是
    # WorkBuddy 会话真正读的东西 —— 队列文件丢了任务就"看着还在、实际没人做"。
    try:
        if workbuddy_config.get("auto_resume"):
            summary = orch.restore_unfinished()
            _LAST_RESUME.clear()
            _LAST_RESUME.update(summary)
            if summary.get("ok"):
                print(f"  恢复：共 {summary['total']} 个任务，"
                      f"{summary['unfinished']} 个未完成，"
                      f"其中 {summary['waiting_workbuddy']} 个等 WorkBuddy 接力")
            else:
                print(f"  恢复失败（不影响服务）：{summary.get('error')}")
        else:
            print("  启动恢复已关闭（可在「WorkBuddy 配置」里打开）")
    except Exception as exc:
        print(f"  恢复失败（不影响服务）：{exc}")

    print(f"  Codex: {orch.CODEX_BIN or '未找到'}")
    print(f"  base_url: {orch.get_codex_base_url() or '(未配置)'}")
    print(f"  代理(8787): {'运行中' if port_open(PROXY_PORT) else '未运行 —— Codex 可能连不上'}")
    print("Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
