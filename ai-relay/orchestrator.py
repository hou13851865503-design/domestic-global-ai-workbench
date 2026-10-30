#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai-relay 编排器
================
把「文件夹」从死的消息总线，变成会自己推进的流水线。

设计要点：
  1. Codex 有 CLI  -> 可被脚本直接调起（全自动）
  2. WorkBuddy / TRAE 通过文件接力参与（见 executor=workbuddy|trae）
  3. 文件锁防止两个进程抢同一个任务
  4. 验收失败自动打回，最多 MAX_ROUNDS 轮，避免无限烧钱

用法：
  python orchestrator.py new      "你的原始需求" [--executor codex|workbuddy]
  python orchestrator.py list
  python orchestrator.py run      [TASK_ID]      # 推进一轮（可指定任务）
  python orchestrator.py watch    [--interval 30] # 常驻轮询
  python orchestrator.py serve    [--port 8765]   # 启动网页看板
  python orchestrator.py show     TASK_ID
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "00_inbox"
PROMPTS = ROOT / "02_prompts"
OUTPUT = ROOT / "03_output"
REVIEW = ROOT / "04_review"
STATE = ROOT / "_state"
LOGS = ROOT / "_logs"
PROMPT_VERSIONS = STATE / "prompt_versions"
EVALUATIONS = STATE / "evaluations"

STATE_FILE = STATE / "state.json"
LOCK_FILE = STATE / ".lock"
AGENT_ACTIVITY_FILE = STATE / "agent_activity.json"
AGENT_ACTIVITY_LOCK_FILE = STATE / ".agent_activity.lock"
PENDING_FILE = PROMPTS / "_PENDING_FOR_WORKBUDDY.md"
PENDING_TRAE_FILE = PROMPTS / "_PENDING_FOR_TRAE.md"

MAX_ROUNDS = 3
MAX_CALLS = 1 + MAX_ROUNDS * 3  # 一次难度评估 + 每轮优化/执行/验收
CODEX_TIMEOUT = 900  # 单次 codex 调用超时（秒）
CODEX_PROBE_TIMEOUT = 8  # 健康探测超时（秒）——探测必须便宜，否则不如别探

# 状态常量
NEW = "NEW"
PROMPT_READY = "PROMPT_READY"
DONE = "DONE"
PASS = "PASS"
FAIL = "FAIL"
GIVE_UP = "GIVE_UP"

# 等待 WorkBuddy 的三种情形：出题、执行、阅卷都可能交给它
WAITING_OPTIMIZE = "WAITING_OPTIMIZE"
WAITING_EXECUTE = "WAITING_EXECUTE"
WAITING_REVIEW = "WAITING_REVIEW"
WAITING_WORKBUDDY = WAITING_EXECUTE  # 旧状态别名，保持向后兼容

# 明确要求 Codex 但它当前不可用：排队等它恢复，而不是悄悄降级换人。
# 因为换人就等于放弃跨模型交叉验证——那正是这条流水线存在的理由。
WAITING_CODEX = "WAITING_CODEX"

WAITING_STATES = (WAITING_OPTIMIZE, WAITING_EXECUTE, WAITING_REVIEW)

# 状态 -> 该阶段由谁做、完成后要检查什么文件
STAGE_SPEC = {
    WAITING_OPTIMIZE: ("optimizer", "出题", lambda tid: PROMPTS / f"{tid}.md"),
    WAITING_EXECUTE:  ("executor",  "执行", lambda tid: OUTPUT / tid / "RESULT.md"),
    WAITING_REVIEW:   ("reviewer",  "阅卷", lambda tid: REVIEW / f"{tid}.md"),
}

# --------------------------------------------------------------------------
# 难度路由：Codex 先评任务难度，再按难度选模型，并给你 3 分钟人工确认
# --------------------------------------------------------------------------

# 难度分级 -> 默认推荐模型（候选来自中转站 /v1/models 实测清单）
MODEL_TIERS = {
    "easy":   "gpt-5.6-sol",   # 轻量、便宜、快
    "medium": "gpt-6-sol",     # 均衡
    "hard":   "gpt-6.1-sol",   # 最强（也是当前默认模型）
}

# 评估时交给 Codex 挑选的候选清单（只列 chat 模型，排除 image / auto-review）
MODEL_CANDIDATES = [
    "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra",
    "gpt-6-sol", "gpt-6-astra", "gpt-6.1-sol",
]

MODEL_ROUTING_DEFAULT = True   # 新任务默认开启「难度评估 + 模型确认闸门」
MODEL_CONFIRM_WINDOW = 180     # 人工确认窗口（秒）= 3 分钟
MODEL_ASSESS_TIMEOUT = 300     # 单次难度评估调用超时（秒）

# 新增状态：难度评估后的「等你确认模型」闸门，以及确认/超时后的放行态
AWAIT_MODEL_CONFIRM = "AWAIT_MODEL_CONFIRM"
ASSESSED = "ASSESSED"

MODEL_CONFIRM_FILE = PROMPTS / "_MODEL_CONFIRM.md"

# ---- 多 AI 协作 · 方案博弈阶段 ----
# 流程：DELIBERATING（多角色分头论证 → 汇总出 A/B/C 方案 → 红队压测）
#       ──人工叫停──> DELIB_PAUSED（保留已产出，可继续）
#       ──方案就绪──> DELIB_READY（等用户采纳某套方案）
# 采纳后把方案并入需求、回到 NEW，交给既有「出题→执行→验收」流水线。
DELIBERATING = "DELIBERATING"
DELIB_PAUSED = "DELIB_PAUSED"
DELIB_READY = "DELIB_READY"

DELIB_DEFAULT_MODEL = "gpt-6-sol"
DELIB_ROLES = [
    ("产品经理", "gpt-5.6-luna",
     "你是产品经理。输出：①目标用户与核心场景 ②核心价值一句话 ③MVP 边界（做什么 / 明确不做什么）"
     "④可量化的成功标准。务必简短，不超过 8 行。"),
    ("架构师", "gpt-5.6-sol",
     "你是架构师。输出：①技术选型（语言/库/存储）②模块划分 ③关键数据流（几句话）④最难的 1 个技术点。"
     "务必简短，不超过 8 行。"),
    ("风险官", "gpt-6-sol",
     "你是风险与合规官。输出：①合规/隐私风险 ②最可能失败的模式 2 条 ③每条一句缓解措施"
     "④整体可行性判断（高/中/低 + 一行理由）。务必简短，不超过 8 行。"),
    ("实现者", "gpt-5.6-terra",
     "你是实现者。输出：①落地步骤（3-5 步）②总工作量估算（人日）③环境与依赖 ④第一个可验证里程碑。"
     "务必简短，不超过 8 行。"),
]


# --------------------------------------------------------------------------
# Codex 可执行文件自动发现
# --------------------------------------------------------------------------

def find_codex() -> Path | None:
    """Codex 装在带哈希版本号的目录里，升级会变路径，所以必须动态发现。"""
    candidates: list[str] = []

    env = os.environ.get("CODEX_BIN")
    if env:
        candidates.append(env)

    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    if base.exists():
        candidates += sorted(glob.glob(str(base / "*" / "codex.exe")), reverse=True)

    candidates += [
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "codex.exe"),
        "codex",
    ]

    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if p.is_absolute():
            if p.exists():
                return p
        else:
            from shutil import which
            w = which(c)
            if w:
                return Path(w)
    return None


CODEX_BIN = find_codex()


# --------------------------------------------------------------------------
# 状态存取
# --------------------------------------------------------------------------

def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"tasks": {}, "seq": 0}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"tasks": {}, "seq": 0}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)  # 原子替换，避免写一半被读到


def _optimization_run(task: dict) -> str:
    """给每次任务重置分配独立目录，避免覆盖旧的提示词版本。"""
    if not task.get("optimization_run"):
        task["optimization_run"] = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return str(task["optimization_run"])


def snapshot_prompt_version(task: dict, source: Path, round_no: int | None = None) -> str | None:
    """保存一轮实际执行的 prompt，并冻结首轮评测标准。"""
    if not source.is_file():
        return None
    rnd = int(round_no or task.get("round", 1))
    run = _optimization_run(task)
    target = PROMPT_VERSIONS / task["id"] / f"run-{run}" / f"round-{rnd}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = source.read_text(encoding="utf-8")
    target.write_text(content, encoding="utf-8")
    rel = str(target.relative_to(ROOT))

    versions = task.setdefault("prompt_versions", [])
    versions[:] = [v for v in versions
                   if not (v.get("run") == run and v.get("round") == rnd)]
    versions.append({"run": run, "round": rnd, "file": rel, "created": now()})
    if rnd == 1:
        task["baseline_prompt_file"] = rel
        task["evaluation_criteria"] = parse_acceptance_criteria(content)
    return rel


def parse_acceptance_criteria(prompt_text: str) -> list[str]:
    """Read the simple YAML acceptance list emitted by the prompt stage."""
    lines = prompt_text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.match(r"^\s*acceptance\s*:\s*(?:#.*)?$", line)), None)
    if start is None:
        return []
    criteria = []
    for line in lines[start + 1:]:
        if not line.strip():
            if criteria:
                break
            continue
        match = re.match(r"^\s+-\s+(.+?)\s*$", line)
        if not match:
            if criteria or not line[:1].isspace():
                break
            continue
        value = match.group(1).strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except Exception:
                value = value.strip('"')
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        if value:
            criteria.append(value)
    return criteria


def parse_review_score(review_file: Path) -> int | None:
    """Parse the independent reviewer's bounded 0-100 score, if present."""
    try:
        text = review_file.read_text(encoding="utf-8")
    except Exception:
        return None
    match = re.search(r"(?im)^\s*overall_score\s*:\s*(\d{1,3})(?:\s*/\s*100)?\s*(?:#.*)?$", text)
    if not match:
        return None
    score = int(match.group(1))
    return score if 0 <= score <= 100 else None


def record_review_result(task: dict, review_file: Path) -> tuple[str, int | None]:
    """Archive a review and track whether its score improves on the best run."""
    verdict = parse_verdict(review_file)
    score = parse_review_score(review_file)
    rnd = int(task.get("round", 1))
    run = _optimization_run(task)
    archive = EVALUATIONS / task["id"] / f"run-{run}" / f"round-{rnd}.md"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(review_file.read_bytes())

    previous_best = task.get("best_score")
    improved = None if score is None else previous_best is None or score > previous_best
    version = next((v for v in task.get("prompt_versions", [])
                    if v.get("run") == run and v.get("round") == rnd), None)
    prompt_version = (version or {}).get("file") or task.get("prompt_file")
    history = task.setdefault("optimization_history", [])
    history[:] = [row for row in history
                  if not (row.get("run") == run and row.get("round") == rnd)]
    history.append({
        "run": run, "round": rnd, "score": score, "verdict": verdict,
        "improved": improved, "previous_best": previous_best,
        "prompt_file": prompt_version,
        "review_file": str(archive.relative_to(ROOT)),
    })
    task["last_score"] = score
    if score is not None and improved:
        task["best_score"] = score
        task["best_round"] = rnd
        task["best_prompt_file"] = prompt_version
    task["verdict"] = verdict
    task["review_file"] = str(review_file.relative_to(ROOT))
    task["status"] = PASS if verdict == PASS else FAIL
    return verdict, score


def _restore_best_prompt(task: dict) -> bool:
    """Restore the best-scoring prompt after a non-improving candidate."""
    best_rel = task.get("best_prompt_file")
    if not best_rel:
        return False
    best = ROOT / best_rel
    active = PROMPTS / f"{task['id']}.md"
    if not best.is_file():
        return False
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_bytes(best.read_bytes())
    task["prompt_file"] = str(active.relative_to(ROOT))
    return True


def reset_prompt_optimization(task: dict) -> None:
    """Start a clean optimization run while leaving prior snapshots intact."""
    task["optimization_run"] = datetime.now().strftime("%Y%m%d%H%M%S%f")
    for key in ("prompt_versions", "optimization_history", "evaluation_criteria",
                "baseline_prompt_file", "best_score", "best_round",
                "best_prompt_file", "last_score"):
        task.pop(key, None)


def log(msg: str, task_id: str = "") -> None:
    """写一行日志。

    日志写不进去不能把业务打死：`python platform.py` 直接启动时（README 里写的
    用法之一）工作目录可能是新克隆的，`_logs/` 还不存在 —— 原来这里直接 open
    会抛 FileNotFoundError，把正在返回的成功响应打成 500。
    """
    line = f"[{now()}] {'[' + task_id + '] ' if task_id else ''}{msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / f"{datetime.now():%Y-%m-%d}.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def ensure_workspace_dirs() -> None:
    """建齐运行时目录。platform.py / orchestrator.py 启动时都调一次。"""
    for directory in (INBOX, PROMPTS, OUTPUT, REVIEW, STATE, LOGS):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def new_id(state: dict) -> str:
    state["seq"] += 1
    return f"T-{datetime.now():%Y%m%d}-{state['seq']:03d}"


# --------------------------------------------------------------------------
# 文件锁（防并发踩踏）
# --------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    """进程还在吗？跨平台。"""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                k.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


class FileLock:
    def __init__(self, path: Path, stale_after: int = 1800):
        self.path = path
        self.stale_after = stale_after
        self.acquired = False

    def _reap_if_dead(self) -> bool:
        """锁的持有者还活着吗？死了就清掉。

        为什么必须查 pid：进程被强杀时不会执行 __exit__，锁文件会残留。
        只看时间戳的话要等满 30 分钟才超时，这期间整条流水线是停的，
        而且从外面看不出来是停了还是在忙 —— 最难受的一种故障。
        """
        try:
            pid = int(self.path.read_text(encoding="utf-8").split()[0])
        except Exception:
            self.path.unlink(missing_ok=True)
            return True
        if not pid_alive(pid):
            self.path.unlink(missing_ok=True)
            log(f"清理僵尸锁（持有进程 {pid} 已不存在）")
            return True
        try:  # 进程还活着，再看是否超时
            if time.time() - self.path.stat().st_mtime > self.stale_after:
                self.path.unlink(missing_ok=True)
                return True
        except Exception:
            pass
        return False

    def __enter__(self):
        for _ in range(50):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()}\n{now()}".encode())
                os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                if self._reap_if_dead():
                    continue
                time.sleep(0.2)
        raise RuntimeError("无法获得锁，另一个编排器进程可能正在运行")

    def __exit__(self, *a):
        if self.acquired:
            self.path.unlink(missing_ok=True)


def _read_agent_activity_unlocked() -> dict:
    try:
        data = json.loads(AGENT_ACTIVITY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("active", {})
            data.setdefault("last", {})
            data.setdefault("totals", {})
            return data
    except Exception:
        pass
    return {"active": {}, "last": {}, "totals": {}, "updated": time.time()}


def _write_agent_activity_unlocked(data: dict) -> None:
    AGENT_ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = AGENT_ACTIVITY_FILE.with_name(
        f".{AGENT_ACTIVITY_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, AGENT_ACTIVITY_FILE)


def start_agent_activity(role: str, backend: str, task_id: str = "",
                         phase: str = "", model: str = "",
                         timeout: int = CODEX_TIMEOUT) -> str | None:
    """Persist a real model invocation so the web monitor can observe other processes."""
    call_id = f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    try:
        AGENT_ACTIVITY_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(AGENT_ACTIVITY_LOCK_FILE):
            data = _read_agent_activity_unlocked()
            data["active"][call_id] = {
                "role": role, "backend": backend, "task_id": task_id,
                "phase": phase, "model": model, "pid": os.getpid(),
                "started_at": now(), "started_epoch": time.time(),
                "timeout": timeout,
            }
            data["totals"][role] = int(data["totals"].get(role, 0)) + 1
            data["updated"] = time.time()
            _write_agent_activity_unlocked(data)
        return call_id
    except Exception:
        return None


def finish_agent_activity(call_id: str | None, ok: bool) -> None:
    if not call_id:
        return
    try:
        AGENT_ACTIVITY_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(AGENT_ACTIVITY_LOCK_FILE):
            data = _read_agent_activity_unlocked()
            call = data["active"].pop(call_id, None)
            if call:
                call.update({
                    "status": "success" if ok else "error",
                    "finished_at": now(), "finished_epoch": time.time(),
                })
                data["last"][call["role"]] = call
                data["updated"] = time.time()
                _write_agent_activity_unlocked(data)
    except Exception:
        pass


def load_agent_activity() -> dict:
    """Read cross-process activity and turn dead/overdue calls into visible interruptions."""
    try:
        AGENT_ACTIVITY_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(AGENT_ACTIVITY_LOCK_FILE):
            data = _read_agent_activity_unlocked()
            changed = False
            current = time.time()
            for call_id, call in list(data["active"].items()):
                elapsed = current - float(call.get("started_epoch", current))
                dead = not pid_alive(int(call.get("pid", 0)))
                overdue = elapsed > int(call.get("timeout", CODEX_TIMEOUT)) + 60
                if dead or overdue:
                    data["active"].pop(call_id, None)
                    call.update({
                        "status": "interrupted", "finished_at": now(),
                        "finished_epoch": current,
                    })
                    data["last"][call.get("role", "unknown")] = call
                    changed = True
            if changed:
                data["updated"] = current
                _write_agent_activity_unlocked(data)
            return data
    except Exception:
        return {"active": {}, "last": {}, "totals": {}, "updated": time.time()}


def run_agent_call(role: str, backend: str, task_id: str, phase: str,
                   model: str, timeout: int, call):
    """Wrap a real backend call with fail-open monitoring bookkeeping."""
    activity_id = start_agent_activity(role, backend, task_id, phase, model, timeout)
    try:
        ok, result = call()
    except BaseException:
        finish_agent_activity(activity_id, False)
        raise
    finish_agent_activity(activity_id, bool(ok))
    return ok, result


# --------------------------------------------------------------------------
# 调用 Codex
# --------------------------------------------------------------------------

# 出现这些字样说明根本连不上模型，继续等只是白烧时间
NET_FAIL_PATTERNS = (
    "ERROR: Reconnecting",
    "waiting for network",
    "stream timed out",
    "upstream_error",
    "unexpected status 502",   # 代理通了但翻不出去（如 FlClash 内核没开）
    "Tunnel connection failed",
)


def slim_event(evt: dict) -> dict | None:
    """把 Codex 的 JSONL 事件瘦身成适合实时监控的精简形式。

    Codex 的 --json 事件结构庞杂，直接推给前端又大又难读。
    这里只提炼：谁在说什么（assistant 文本）、调了什么工具、有什么结果。
    """
    t = evt.get("type", "")
    item = evt.get("item") or {}
    itype = item.get("type")

    # assistant 消息（旧 schema：顶层 response_item / assistant_message）
    if t == "assistant_message" or (t == "response_item" and item):
        if item.get("type") == "message":
            role = item.get("role", "assistant")
            if role != "assistant":
                return None
            parts = item.get("content", [])
            texts = []
            for p in parts:
                if isinstance(p, dict) and p.get("type") in ("output_text", "input_text"):
                    texts.append(p.get("text", ""))
                elif isinstance(p, str):
                    texts.append(p)
            if texts:
                return {"type": "assistant", "role": role, "text": "".join(texts)}
        return None

    # Do not expose private reasoning text in event logs or the UI.
    if itype == "reasoning" or "reasoning" in t:
        return None

    # 工具调用 / 命令（含新 schema 的 command_execution / local_shell）
    if itype in ("function_call", "command_execution", "local_shell") \
            or "tool" in t or "function_call" in t or "command" in t:
        name = item.get("name") or itype or t
        args = item.get("arguments") or item.get("command") or ""
        if isinstance(args, str) and len(args) > 200:
            args = args[:200] + "…"
        return {"type": "tool", "name": str(name), "args": str(args)[:200]}

    # item.completed：按内部 item.type 分流（这是 Codex 新事件的主干）
    if t == "item.completed":
        if itype in ("function_call", "command_execution", "local_shell"):
            out = item.get("output") or item.get("content") or ""
            if isinstance(out, str):
                out = out[:240]
            return {"type": "result", "name": item.get("name", "") or itype, "out": str(out)[:240]}
        if itype == "agent_message":
            txt = item.get("text") or ""
            if txt:
                return {"type": "assistant", "text": str(txt)[:600]}
        elif itype == "file_change":
            ch = item.get("changes") or []
            paths = ", ".join(c.get("path", "") for c in ch[:3]) if isinstance(ch, list) else ""
            return {"type": "tool", "name": "file_change", "args": str(paths)[:160]}
        elif itype == "error":
            return {"type": "error", "message": str(item.get("message", ""))[:240]}
        return None  # 其它 completed（turn/thread/response）忽略，不再塞 done

    # 完成 / 错误
    if "error" in t:
        return {"type": "error", "message": str(evt.get("message", "")
                                                 or (item.get("message") if itype == "error" else ""))[:240]}
    if t == "response_completed":
        return {"type": "done", "message": "完成"}

    return None


def _dump_raw(tag: str, raw: str) -> None:
    """tag 形如 `T-xxx/optimize-r1`，含子目录，写日志前要先建目录。"""
    f = LOGS / f"{tag}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(raw, encoding="utf-8")


def _codex_env() -> dict:
    """给 codex.exe 一份干净的网络环境。

    Codex 只需要访问本地 UA 代理(127.0.0.1:8787)，绝不能继承宿主的 http_proxy ——
    实测 WorkBuddy 等程序注入的内部代理(端口还会漂移)会劫持对 127.0.0.1 的请求，
    造成"明明探测 200、Codex 却连不上"的诡异现象。
    （urllib 走 Windows 注册表规则会绕过 localhost，所以探测正常；
      codex 的 HTTP 客户端只认环境变量且不绕 localhost —— 两者行为差异是假象根源）
    """
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def call_codex(prompt: str, cwd: Path, tag: str, timeout: int = CODEX_TIMEOUT,
               on_event=None, model: str | None = None) -> tuple[bool, str]:
    """以非交互方式调起 Codex。

    踩过的两个坑，都在这里被处理掉了：
      1. 不切断 stdin —— Codex 非 TTY 下会打印 "Reading additional input from stdin..."
         然后无限等待，流水线静默卡死。故用 stdin=DEVNULL。
      2. 网络不通 —— Codex 会反复 "Reconnecting..."，不报错也不退出。
         故流式读输出，一旦识别到网络故障字样就快速失败，而不是干等满 timeout。

    on_event: 可选回调，签名为 on_event(dict)。每解析到一条 JSONL 事件就调用一次，
      用于把 Codex 的工具操作、最终回复和结果事件推给监控端。
    """
    if CODEX_BIN is None:
        return False, "未找到 codex.exe。请设置环境变量 CODEX_BIN 指向 codex.exe"

    cmd = [
        str(CODEX_BIN), "exec",
        "-C", str(cwd),
        "-s", "workspace-write",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--ephemeral",
        "--json",
        prompt,
    ]
    if model:
        # 难度路由：按任务难度选用不同档位的模型，省额度也保质量
        cmd[2:2] = ["--model", model]

    tid = tag.split("/")[0] if "/" in tag else ""
    log(f"调用 Codex ({tag})" + (f" [model={model}]" if model else ""), tid)

    import threading
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        env=_codex_env(),
    )

    chunks: list[str] = []

    def _emit(line: str) -> None:
        """把一条 JSONL 解析成事件回调出去。"""
        if not on_event:
            return
        line = line.strip()
        if not line.startswith("{"):
            # 非 JSON 行：通常是 Codex 的 stderr 提示（如「xxx is ignored」「deprecated」）。
            # 这类「配置提醒」无害，但原样当 error 推到控制台会变成红字吓人 ——
            # 这里只把含已知良性关键字的提示降级为灰色 system，其余非 JSON 行直接丢弃。
            _BENIGN = ("ignored", "deprecated", "unrecognized",
                       "configuration", "notice", "warning", "will be removed")
            low = line.lower()
            if any(k in low for k in _BENIGN):
                on_event({"type": "system", "message": line[:200]})
            return
        try:
            evt = json.loads(line)
        except Exception:
            return
        # 提取有用的片段，避免把整个巨大 payload 推出去
        slim = slim_event(evt)
        if slim:
            on_event(slim)

    def reader():
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                chunks.append(line)
                _emit(line)
        except Exception:
            pass

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    deadline = time.time() + timeout
    net_hit_at = None

    while proc.poll() is None:
        blob = "".join(chunks)
        if any(p in blob for p in NET_FAIL_PATTERNS):
            if net_hit_at is None:
                net_hit_at = time.time()
            # 给 20 秒自愈窗口，抖动能扛过去
            elif time.time() - net_hit_at > 20:
                proc.kill()
                raw = "".join(chunks)
                _dump_raw(tag, raw)
                msg = "Codex 连不上模型服务（网络/中转故障）。请检查 ~/.codex/config.toml 里的 base_url 是否可达"
                OFFLINE.update(on=True, reason="网络/中转故障",
                               last_probe=time.monotonic())
                mark_codex_fail()
                log(msg + " -> Codex 进入冷却，后续阶段自动改派 WorkBuddy", tid)
                if on_event:
                    on_event({"type": "error", "message": msg})
                return False, msg
        if time.time() > deadline:
            proc.kill()
            raw = "".join(chunks)
            _dump_raw(tag, raw)
            mark_codex_fail()
            return False, f"Codex 调用超时（{timeout}s）"
        time.sleep(1)

    th.join(timeout=5)
    raw = "".join(chunks)
    _dump_raw(tag, raw)

    summary = extract_last_message(raw)
    ok = proc.returncode == 0
    if ok:
        mark_codex_ok()
    else:
        mark_codex_fail()
        log(f"Codex 退出码 {proc.returncode}", tid)
    return ok, summary or raw[-2000:]


def extract_last_message(jsonl: str) -> str:
    last = ""
    for line in jsonl.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type", "")
        if t.endswith("message") or "item" in ev:
            item = ev.get("item") or ev
            txt = item.get("text") or item.get("content") or ""
            if isinstance(txt, list):
                txt = " ".join(x.get("text", "") for x in txt if isinstance(x, dict))
            if txt:
                last = txt
    return last


# --------------------------------------------------------------------------
# 任务事件落盘 + 多 Agent 角色路由
# --------------------------------------------------------------------------

EVENTS_DIR = STATE / "events"


def record_event(tid: str, phase: str | None = None):
    """Persist auditable stage events, excluding private reasoning text."""
    def _cb(evt: dict) -> None:
        try:
            if evt.get("type") == "reasoning":
                return
            EVENTS_DIR.mkdir(parents=True, exist_ok=True)
            with (EVENTS_DIR / f"{tid}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t": now(), "phase": phase, **evt}, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 落盘失败不影响主流程
    return _cb


def text_stage_call(task: dict, role: str, prompt: str, tag: str,
                    model: str | None = None,
                    timeout: int = CODEX_TIMEOUT) -> tuple[bool, str]:
    """文本型阶段（评估/出题/验收）的统一入口。

    按 _state/agents.json 里 roles[role] 绑定的后端路由：
    - openai 兼容后端（DeepSeek/豆包/tokken...）→ agents.call_openai
    - 其他（codex_cli）→ 本文件 call_codex（带难度路由的 model 参数）

    可核验的阶段事件会写入任务事件文件，供网页回放。
    """
    tid = task["id"]
    ev_cb = record_event(tid, role)
    import agents
    backend = agents.backend_for_role(role)
    name = (backend or {}).get("name", "codex")
    if agents.is_openai(backend):
        # HTTP 后端没有"难度选档"概念，model 参数忽略，用它配置里的模型
        actual_model = (backend or {}).get("model", "")
        call = lambda: agents.call_backend(
            backend, prompt, tag, timeout=timeout, on_event=ev_cb)
    else:
        actual_model = model or ""
        call = lambda: call_codex(prompt, ROOT, tag, timeout=timeout,
                                  on_event=ev_cb, model=model)
    ok, msg = run_agent_call(
        role, name, tid, role, actual_model, timeout, call)
    task[f"backend_{role}"] = name
    return ok, msg


# --------------------------------------------------------------------------
# 多 AI 协作 · 方案博弈
# --------------------------------------------------------------------------

def init_deliberation(task: dict) -> None:
    """初始化一个任务的方案博弈数据（幂等）。"""
    task["deliberation"] = {
        "phase": "coop",      # coop → debate → ready
        "roles": {},          # {角色名: {model, text}}
        "synthesis": "",      # 汇总原文
        "candidates": [],     # [{key, title, text}]
        "debate": [],         # [{round, text}]
        "recommended": "",    # "A"/"B"/"C"
        "adopted": "",        # 用户采纳的方案 key
        "error": "",
        "started": now(),
        "updated": now(),
    }
    task["hold"] = False


def call_model(messages: list[dict], model: str | None = None,
               timeout: int = 300) -> tuple[bool, str]:
    """直连 OpenAI 兼容 /chat/completions（默认走 tokken 后端，即 8787 转发）。

    与 agents.call_openai 的差别：支持 system+user 多消息与指定模型，
    便于让不同角色用不同模型；显式禁用环境代理（只打 127.0.0.1）。
    """
    import agents
    import urllib.request
    cfg = agents.load_config()
    b = cfg.get("backends", {}).get("tokken") or {}
    base = (b.get("base_url") or "http://127.0.0.1:8787/v1").rstrip("/")
    key = b.get("api_key") or agents.tokken_token()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _once(m: str) -> str:
        body = json.dumps({"model": m, "messages": messages, "stream": False},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            base + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"})
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""

    use = model or b.get("model") or DELIB_DEFAULT_MODEL
    try:
        return True, _once(use)
    except Exception as exc:  # 换备用模型再试一次
        if use != DELIB_DEFAULT_MODEL:
            try:
                return True, _once(DELIB_DEFAULT_MODEL)
            except Exception as exc2:
                return False, f"{type(exc2).__name__}: {exc2}"
        return False, f"{type(exc).__name__}: {exc}"


def parse_candidates(text: str) -> list[dict]:
    """从汇总文本里切出 A/B/C 三套方案（严格按 ### 方案X： 标题）。"""
    parts = re.split(r'(?m)^###\s*方案\s*([ABC])[：:]\s*(.*)$', text)
    out: list[dict] = []
    for i in range(1, len(parts) - 1, 3):
        key = parts[i].strip()
        title = parts[i + 1].strip()
        body = parts[i + 2].strip() if i + 2 < len(parts) else ""
        out.append({"key": key, "title": title,
                    "text": f"方案{key}：{title}\n{body}".strip()})
    return out


def next_delib_step(task: dict) -> dict:
    """返回下一步要做的动作：role / synth / debate / done。"""
    d = task.setdefault("deliberation", {})
    d.setdefault("roles", {})
    req = task.get("requirement", "")

    for name, model, role_prompt in DELIB_ROLES:
        if name not in d["roles"]:
            return {"kind": "role", "name": name, "model": model, "messages": [
                {"role": "system", "content": role_prompt},
                {"role": "user", "content": f"想法：{req}\n\n请给出你的专业意见。"},
            ]}

    if not d.get("synthesis"):
        material = "\n\n".join(
            f"【{n}】\n{(v.get('text') or '')}" for n, v in d["roles"].items())
        prompt = (
            f"用户想法：{req}\n\n以下是四位角色的意见：\n\n{material}\n\n"
            "请综合，产出 3 套风格不同的候选方案，严格按下面标题格式（每套一段）：\n"
            "### 方案A：<名称>\n- 一句话定位：\n- 技术要点：\n- 预估工作量：\n- 主要风险：\n"
            "- 可行性评分（1-10，含一句理由）：\n- 适合场景：\n"
            "### 方案B：<名称>\n- ...\n### 方案C：<名称>\n- ...\n"
            "最后单独一行：推荐：方案X —— 一句理由。"
        )
        return {"kind": "synth", "model": DELIB_DEFAULT_MODEL, "messages": [
            {"role": "system", "content": "你是总架构师，擅长在多个方案间做取舍，输出结构化、可比较，不啰嗦。"},
            {"role": "user", "content": prompt},
        ]}

    if not d.get("debate"):
        best = d.get("recommended") or "A"
        cand_text = next((c["text"] for c in d.get("candidates", []) if c["key"] == best),
                         d.get("synthesis", ""))
        return {"kind": "debate", "model": "gpt-5.6-sol", "messages": [
            {"role": "system", "content": "你是刻薄但专业的红队评审，只讲具体、可验证的问题，输出简短。"},
            {"role": "user", "content": f"想法：{req}\n\n拟采用的 {best} 方案：\n{cand_text}\n\n"
                                        "请指出 3 个最致命问题（每条写清后果），并给结论：可开工 / 需返工，"
                                        "再给 1 条最重要的修改建议。"},
        ]}

    return {"kind": "done"}


def apply_delib_result(task: dict, step: dict, text: str) -> None:
    """把一次模型输出写回任务。"""
    d = task.setdefault("deliberation", {})
    kind = step.get("kind")
    if kind == "role":
        d.setdefault("roles", {})[step["name"]] = {"model": step.get("model", ""), "text": text}
    elif kind == "synth":
        d["synthesis"] = text
        d["candidates"] = parse_candidates(text)
        m = re.search(r'推荐[：:]\s*方案\s*([ABC])', text)
        d["recommended"] = m.group(1) if m else (d["candidates"][0]["key"] if d["candidates"] else "")
        d["phase"] = "debate"
    elif kind == "debate":
        d.setdefault("debate", []).append({"round": len(d.get("debate", [])) + 1, "text": text})
        d["phase"] = "ready"
    d["updated"] = now()


# --------------------------------------------------------------------------
# 三个阶段
# --------------------------------------------------------------------------

CONSTITUTION = (ROOT / "01_分工与协议" / "ROLES.md").read_text(encoding="utf-8") \
    if (ROOT / "01_分工与协议" / "ROLES.md").exists() else ""


# Codex 不可用（未安装 / 网络不通）时置为 True，走本地模板，
# 保证流水线不至于彻底瘫痪：prompt 质量下降，但流程仍然推进。
OFFLINE = {"on": False, "reason": "", "last_probe": 0.0}


HEALTH_FILE = STATE / "codex_health.json"
CODEX_COOLDOWN = 1800  # 连败后停止重复生成请求；轻量恢复探测仍每 30 秒进行
CODEX_RECOVERY_INTERVAL = 30  # 离线期间每 30 秒轻量检查一次服务是否恢复


def load_health() -> dict:
    try:
        return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"fails": 0, "last_fail": 0.0}


def _save_health(h: dict) -> None:
    HEALTH_FILE.write_text(json.dumps(h), encoding="utf-8")


def mark_codex_fail() -> None:
    h = load_health()
    h["fails"] = h.get("fails", 0) + 1
    h["last_fail"] = time.time()
    _save_health(h)


def mark_codex_ok() -> None:
    _save_health({"fails": 0, "last_fail": 0.0})


def get_codex_base_url() -> str:
    """从 ~/.codex/config.toml 读出 base_url，供轻量探测使用。"""
    home = os.environ.get("CODEX_HOME")
    cfg = Path(home) / "config.toml" if home else Path.home() / ".codex" / "config.toml"
    try:
        txt = cfg.read_text(encoding="utf-8")
    except Exception:
        return ""
    import re
    m = re.search(r'^\s*base_url\s*=\s*["\']([^"\']+)', txt, re.M)
    return m.group(1) if m else ""


def get_codex_token() -> str:
    """从 config.toml 读取 bearer token，供探测时带上（否则会拿到 401）。"""
    home = os.environ.get("CODEX_HOME")
    cfg = Path(home) / "config.toml" if home else Path.home() / ".codex" / "config.toml"
    try:
        txt = cfg.read_text(encoding="utf-8")
    except Exception:
        return ""
    import re
    m = re.search(r'^\s*experimental_bearer_token\s*=\s*["\']([^"\']+)', txt, re.M)
    return m.group(1) if m else ""


def probe_codex() -> tuple[bool, str]:
    """轻量探测：直接测 base_url 通不通，不启动 Codex。

    为什么不直接跑一次 `codex exec` 来试探？
    因为它失败时要等 74 秒才确认，而探测只要 8 秒。
    在轮询场景里，这个差距是决定性的。
    """
    base = get_codex_base_url()
    if not base:
        return False, "config.toml 里没找到 base_url"
    try:
        import urllib.request
        # 探 /models 而不是根路径：根路径常被网关返回 403/404，
        # 会让我们误判成"服务挂了"。/models 只要服务活着就一定有响应。
        tok = get_codex_token()
        hdrs = {"Authorization": f"Bearer {tok}"} if tok else {}
        req = urllib.request.Request(base.rstrip("/") + "/models", headers=hdrs, method="GET")
        with urllib.request.urlopen(req, timeout=CODEX_PROBE_TIMEOUT) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # 收到 HTTP 响应（哪怕 401/403）就说明服务可达、网络通。
        # 真正"不可用"是连不上：URLError / timeout / DNS 失败。
        return True, f"HTTP {e.code}（服务可达）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def codex_available() -> bool:
    """Codex 值不值得再试一次。

    连续失败时暂停生成请求，但保留低频健康探测，避免离线标志永久卡住。
    """
    if CODEX_BIN is None:
        return False
    h = load_health()
    health_cooling = (h.get("fails", 0) >= 2
                      and (time.time() - h.get("last_fail", 0)) < CODEX_COOLDOWN)
    if OFFLINE["on"] or health_cooling:
        now_mono = time.monotonic()
        if now_mono - OFFLINE.get("last_probe", 0.0) >= CODEX_RECOVERY_INTERVAL:
            OFFLINE["last_probe"] = now_mono
            ok, msg = probe_codex()
            if ok:
                mark_codex_ok()
                OFFLINE.update(on=False, reason="", last_probe=0.0)
            else:
                OFFLINE.update(on=True, reason=msg)
        if OFFLINE["on"]:
            return False
        h = load_health()
        if (h.get("fails", 0) >= 2
                and (time.time() - h.get("last_fail", 0)) < CODEX_COOLDOWN):
            return False
    return True


def resolve_role(task: dict, kind: str) -> str:
    """决定某个阶段由谁执行。kind ∈ {optimizer, executor, reviewer}

    取值含义：
      auto     —— 使用 agents.json 对应阶段的分工；Codex 不可用时回退 WorkBuddy
      codex    —— 强制 Codex；不可用时停机报错，不悄悄降级
      workbuddy—— 交给 WorkBuddy，生成待办等定时任务唤起
      trae     —— 交给 TRAE，生成 IDE 文件接力待办
      template —— 用本地模板，不调任何 AI（最快，但无智能）
    """
    pref = task.get(kind, "auto")
    if pref == "auto":
        import agents
        role_name = {"optimizer": "optimize", "executor": "execute",
                     "reviewer": "review"}.get(kind, kind)
        backend = agents.backend_for_role(role_name)
        name = (backend or {}).get("name", "codex")
        btype = (backend or {}).get("type")
        if btype == "codex_cli":
            return "codex" if codex_available() else "workbuddy"
        if btype in ("workbuddy", "trae"):
            return btype
        if btype == "openai":
            return "workbuddy" if role_name == "execute" else name
        return "codex" if codex_available() else "workbuddy"
    if pref == "codex" and not codex_available():
        # 显式要求 Codex 却不可用：排队等它恢复，还是放弃改派人？
        #   wait     —— 停下等（保住跨模型交叉验证，这是流水线的核心价值）
        #   fallback —— 改派 WorkBuddy（能跑，但变成自证，可信度下降）
        return "wait_codex" if task.get("on_unavailable", "fallback") == "wait" else "unavailable"
    return pref


def wait_for_codex(task: dict, state: dict, resume_status: str) -> bool:
    """Codex 不可用且用户要求非它不可 —— 排队，不降级。"""
    task["resume_status"] = resume_status
    task["status"] = WAITING_CODEX
    log(f"Codex 不可用，任务排队等待（恢复后自动继续：{resume_status}）", task["id"])
    return False


def budget_ok(task: dict) -> bool:
    """单次任务调用预算。防止一个坏任务把额度烧穿。"""
    if task.get("calls", 0) >= MAX_CALLS:
        task["status"] = GIVE_UP
        task["giveup_reason"] = f"超出调用预算（上限 {MAX_CALLS} 次）"
        log(f"熔断：已达调用上限 {MAX_CALLS}", task["id"])
        return False
    return True


def ask_file_agent(task: dict, state: dict, waiting_state: str, what: str,
                   agent: str = "workbuddy") -> bool:
    """登记为文件接力待办；完成信号由约定交付路径上的文件提供。"""
    task["status"] = waiting_state
    task["waiting_for"] = what
    task["waiting_agent"] = agent
    refresh_pending(state)
    log(f"已登记待办：{what}（等待 {agent.upper()} 文件接力）", task["id"])
    return False  # 本轮到此，等它被唤起后再推进


def ask_workbuddy(task: dict, state: dict, waiting_state: str, what: str) -> bool:
    return ask_file_agent(task, state, waiting_state, what, "workbuddy")


def offline_optimize(task: dict) -> bool:
    """本地模板兜底生成 prompt。没有 AI 润色，但结构完整、可被验收。"""
    tid = task["id"]
    rnd = task.get("round", 1)
    out_file = PROMPTS / f"{tid}.md"
    out_dir = OUTPUT / tid

    feedback = ""
    if rnd > 1 and (REVIEW / f"{tid}.md").exists():
        feedback = f"\n\n## 上一轮验收意见（必须逐条回应）\n\n{(REVIEW / f'{tid}.md').read_text(encoding='utf-8')}"

    content = f"""# {tid} · 执行 Prompt（第 {rnd} 轮）

> ⚠️ 本 prompt 由本地模板生成（Codex 不可用：{OFFLINE['reason']}），未经 AI 润色。

## 1. 目标

{task['requirement']}

## 2. 上下文

- 任务 ID：{tid}
- 当前轮次：第 {rnd} 轮（最多 {MAX_ROUNDS} 轮）
- 流水线根目录：{ROOT}

## 3. 产出规格

- 所有产出必须写入目录：`{out_dir}`
- 必须包含 `RESULT.md`，说明：做了什么、产出文件清单、如何验证
- 产出路径示例：`{out_dir}/<你的文件名>`

## 4. 验收标准

- [ ] `RESULT.md` 存在，且说明了做了什么
- [ ] 产出文件确实存在于 `{out_dir}` 下
- [ ] 产出内容与需求「{task['requirement'][:30]}」直接相关
- [ ] 代码类产出可直接运行，无语法错误

## 5. 禁止事项

- 不要修改 `01_分工与协议/`、`orchestrator.py`
- 不要在本目录外创建文件
{feedback}

```yaml
acceptance:
  - "RESULT.md 存在且说明清楚做了什么"
  - "产出文件确实存在于指定输出目录"
  - "产出内容与原始需求直接相关"
  - "代码类产出无语法错误、可直接运行"
```
"""
    out_file.write_text(content, encoding="utf-8")
    snapshot_prompt_version(task, out_file, rnd)
    task["status"] = PROMPT_READY
    task["prompt_file"] = str(out_file.relative_to(ROOT))
    log(f"[离线模板] prompt 就绪 -> {out_file.name}", tid)
    return True


def model_routing_on(task: dict) -> bool:
    """该任务是否启用难度评估 + 模型确认闸门。"""
    return bool(task.get("model_route", MODEL_ROUTING_DEFAULT))


def _extract_json(text: str) -> dict | None:
    """从 Codex 返回的自由文本里尽量抠出一个 JSON 对象。"""
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # 退而求其次：取第一个 { 到最后一个 } 之间的内容
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            return None
    return None


def assess_difficulty(task: dict) -> dict | None:
    """让 Codex 给任务评级（easy/medium/hard）并推荐一个候选模型。

    返回 {difficulty, recommended_model, reason} 或 None（评估失败时）。
    评估失败时由调用方降级为默认模型放行，绝不阻塞流水线。
    """
    cand = "、".join(MODEL_CANDIDATES)
    tier_hint = "，".join(f"{k}→{v}" for k, v in MODEL_TIERS.items())
    prompt = f"""你是一个任务复杂度评估器。请只做评估，不要写任何文件、不要调用任何工具。

# 待评估任务（原始需求）
{task['requirement']}

# 你的任务
1. 判断这个任务的复杂度等级，只能取以下三档之一：
   - easy：明确、单一、几步就能搞定，几乎不会出错
   - medium：需要一些设计或多种步骤，有一定出错概率
   - hard：需求模糊/涉及多模块/需要较强推理或创造力
2. 从下面这份「可用模型清单」里挑一个最合适的（强任务用强模型，弱任务用轻模型省额度）：
   {cand}
   推荐映射参考（你可偏离，但要给理由）：{tier_hint}
3. 用一句话说明推荐理由。

# 输出格式（严格遵守，程序只解析这个 JSON）
只输出一个 JSON 对象，不要任何前后缀、不要代码块标记：
{{"difficulty": "easy|medium|hard", "recommended_model": "模型id", "reason": "一句话理由"}}
"""
    if not budget_ok(task):
        return None
    ok, msg = text_stage_call(task, "assess", prompt, f"{task['id']}/assess",
                              timeout=MODEL_ASSESS_TIMEOUT)
    task["calls"] = task.get("calls", 0) + 1
    task["last_codex_msg"] = msg[:500]
    if not ok:
        log(f"难度评估失败：{msg[:200]}（降级为默认模型放行）", task["id"])
        return None

    data = _extract_json(msg)
    if not data:
        log("难度评估返回无法解析为 JSON（降级放行）", task["id"])
        return None
    diff = str(data.get("difficulty", "")).lower()
    model = str(data.get("recommended_model", "")).strip()
    if diff not in MODEL_TIERS:
        diff = "medium"  # 档位非法时居中，不卡死
    if model not in MODEL_CANDIDATES:
        model = MODEL_TIERS[diff]  # 模型不在候选里就退回该档位默认
    return {"difficulty": diff, "recommended_model": model,
            "reason": str(data.get("reason", ""))[:200]}


def stage_model_gate(task: dict, state: dict) -> bool:
    """闸门阶段：评估难度 → 进入「等你确认模型」。

    已确认锁定（model_locked）的任务直接放行到 ASSESSED。
    """
    if task.get("model_locked"):
        task["status"] = ASSESSED
        return True

    log("进入难度评估闸门", task["id"])
    info = assess_difficulty(task)
    if info is None:
        # 评估失败：用默认最强模型兜底，直接放行，不阻塞
        task["difficulty"] = "medium"
        task["recommended_model"] = MODEL_TIERS["medium"]
        task["selected_model"] = MODEL_TIERS["medium"]
        task["model_status"] = "auto_fallback"
        task["model_locked"] = True
        task["status"] = ASSESSED
        log(f"评估失败，兜底选用 {MODEL_TIERS['medium']}", task["id"])
        return True

    task["difficulty"] = info["difficulty"]
    task["recommended_model"] = info["recommended_model"]
    task["model_reason"] = info["reason"]
    task["model_status"] = "pending_confirm"
    task["model_deadline"] = time.time() + MODEL_CONFIRM_WINDOW
    task["status"] = AWAIT_MODEL_CONFIRM
    log(f"难度={info['difficulty']} 推荐模型={info['recommended_model']} "
        f"（{MODEL_CONFIRM_WINDOW}s 内可手动确认，否则自动采用）", task["id"])
    return True


def check_model_gate(task: dict, state: dict) -> bool:
    """闸门待确认态：人工已确认则锁定并放行；超时未确认则自动选用推荐模型。"""
    if task.get("model_locked"):
        task["status"] = ASSESSED
        return stage_optimize(task, state)

    if time.time() >= task.get("model_deadline", 0):
        # 超时自决
        task["selected_model"] = task.get("recommended_model", MODEL_TIERS["medium"])
        task["model_status"] = "auto"
        task["model_locked"] = True
        task["status"] = ASSESSED
        log(f"⏰ {MODEL_CONFIRM_WINDOW}s 内未确认，自动选用 {task['selected_model']}",
            task["id"])
        return stage_optimize(task, state)

    # 仍在等待确认
    return False


def stage_optimize(task: dict, state: dict) -> bool:
    """阶段一：把原始需求改写成结构化 prompt。"""
    role = resolve_role(task, "optimizer")

    if role == "template":
        return offline_optimize(task)
    if role == "unavailable":
        task["status"] = "BLOCKED"
        log("optimizer 要求 Codex 但 Codex 不可用 -> 停机", task["id"])
        return False
    if role in ("workbuddy", "trae"):
        return ask_file_agent(task, state, WAITING_OPTIMIZE,
                              "出题：把原始需求改写为结构化 prompt", role)
    if role == "wait_codex":
        return wait_for_codex(task, state, NEW)

    tid = task["id"]
    out_file = PROMPTS / f"{tid}.md"
    rnd = task.get("round", 1)

    feedback = ""
    current_prompt = ""
    if rnd > 1:
        rf = REVIEW / f"{tid}.md"
        if rf.exists():
            feedback = f"\n\n## 上一轮验收意见（必须逐条回应）\n\n{rf.read_text(encoding='utf-8')}"
        if out_file.exists():
            current_prompt = out_file.read_text(encoding="utf-8")

    criteria = task.get("evaluation_criteria") or []
    frozen_rubric = "\n".join(f"- {item}" for item in criteria)
    if not frozen_rubric:
        frozen_rubric = "首轮 prompt 的 acceptance 列表；若缺失，则从原始需求建立并保持不变。"

    prompt = f"""你是「Prompt 工程师」角色。请阅读本目录下的分工宪法：

{CONSTITUTION}

---

# 原始需求（第 {rnd} 轮）

{task['requirement']}
{feedback}

## 固定评测标准（不得改写、删减或新增）
以下标准在第 1 轮冻结；后续版本只能改进执行指令，不能通过改变评分标准抬分：
{frozen_rubric}

## 当前提示词版本
{current_prompt or '第 1 轮基线：请直接从原始需求建立。'}

---

# 你的产出

把上面的原始需求改写为一份**可直接执行的结构化 prompt**，并写入文件：
`{out_file}`

该 prompt 必须包含这五段，缺一不可：

1. **目标** —— 一句话说清要什么
2. **上下文** —— 背景、约束、已有材料
3. **产出规格** —— 明确写出产出要落到哪个路径、什么格式
4. **验收标准** —— 必须是二值可判定的（是/否），写成勾选清单，**禁止**出现"尽量""较好"这类无法判定的表述
5. **禁止事项** —— 明确不要做什么

## 提示词优化规则

- 第 1 轮建立清晰基线；后续轮次先读上一轮验收反馈，只针对最主要的问题做**一个**改动。
- 始终保留上面的固定评测标准，`acceptance:` 列表必须与第 1 轮完全一致。
- 不要因为上一轮分数低就同时改多个方面；让下一轮评分能说明这一个改动是否有效。

## 写验收标准时的硬性约束（违反会造成流水线死循环）

- 标准必须**二值可判定**
- **绝对禁止**写入「不要创建额外文件」「只允许创建 X 个文件」「产出目录不得有其他文件」这类条款

  原因：流水线要求执行者写 `03_output/<ID>/RESULT.md` 作为**交付信号**。
  如果标准禁止额外文件，执行者写了 RESULT.md 会被判 FAIL，
  但不写又无法交付 —— 无解死循环（实测已发生）。
  `RESULT.md` 是流水线基础设施，不是任务产出，**永远不得列为扣分项**。

同时，请在文件**末尾**追加一个 YAML 块，供程序解析：

```yaml
acceptance:
  - "标准一"
  - "标准二"
```

只写这一个文件，不要创建其他文件。若当前后端不能直接写本地文件，请返回完整 Markdown 提示词，不要只回复“完成”。
"""
    if not budget_ok(task):
        return False
    previous_content = out_file.read_bytes() if out_file.exists() else None
    ok, msg = text_stage_call(task, "optimize", prompt, f"{tid}/optimize-r{rnd}",
                              model=task.get("selected_model"))
    task["calls"] = task.get("calls", 0) + 1
    task["last_codex_msg"] = msg[:500]
    if not ok:
        log(f"优化阶段失败：{msg[:200]}", tid)
        return False

    current_content = out_file.read_bytes() if out_file.exists() else None
    if current_content is None or current_content == previous_content:
        # API 型后端不能直接写本地文件；用返回的完整提示词作为兜底。
        out_file.write_text(
            f"# {tid} 优化后的 Prompt（第 {rnd} 轮）\n\n{msg}\n", encoding="utf-8"
        )
        log("优化后端未更新目标文件，已用返回内容兜底写入", tid)

    version = snapshot_prompt_version(task, out_file, rnd)
    if rnd == 1 and version and not task.get("evaluation_criteria"):
        task["evaluation_criteria"] = parse_acceptance_criteria(
            out_file.read_text(encoding="utf-8"))

    task["status"] = PROMPT_READY
    task["prompt_file"] = str(out_file.relative_to(ROOT))
    log(f"prompt 就绪 -> {out_file.name}", tid)
    return True


def stage_execute(task: dict, state: dict) -> bool:
    """阶段二：执行任务。

    executor=codex      -> 脚本直接调起，全自动
    executor=workbuddy  -> 只能标记等待，由定时任务唤起 WorkBuddy 接手
    """
    role = resolve_role(task, "executor")
    tid = task["id"]
    out_dir = OUTPUT / tid
    out_dir.mkdir(parents=True, exist_ok=True)

    if role == "unavailable":
        task["status"] = "BLOCKED"
        log("executor 要求 Codex 但 Codex 不可用 -> 停机", tid)
        return False
    # 执行环节没有"模板"可言，统一交给 WorkBuddy
    if role in ("workbuddy", "trae", "template"):
        task["output_dir"] = str(out_dir.relative_to(ROOT))
        agent = "workbuddy" if role == "template" else role
        return ask_file_agent(task, state, WAITING_EXECUTE,
                              "执行：按 prompt 完成任务，产出写入输出目录并写 RESULT.md",
                              agent)
    if role == "wait_codex":
        task["output_dir"] = str(out_dir.relative_to(ROOT))
        return wait_for_codex(task, state, PROMPT_READY)

    prompt_file = ROOT / task.get("prompt_file", f"02_prompts/{tid}.md")
    prompt_text = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""

    if role == "codex":
        p = f"""你是「执行者」角色。请严格按下面的 prompt 完成任务。

===== 待执行 Prompt =====
{prompt_text}
===== Prompt 结束 =====

硬性要求：
- 所有产出必须写入目录 `{out_dir}`
- 完成后写一个 `{out_dir}/RESULT.md`，说明你做了什么、产出文件有哪些
- 不要修改 01_分工与协议/ 和 orchestrator.py
"""
        if not budget_ok(task):
            return False
        selected_model = task.get("selected_model") or ""
        ok, msg = run_agent_call(
            "execute", "codex", tid, "execute", selected_model, CODEX_TIMEOUT,
            lambda: call_codex(
                p, ROOT, f"{tid}/execute-r{task.get('round', 1)}",
                model=task.get("selected_model"),
                on_event=record_event(tid, "execute")))
        task["calls"] = task.get("calls", 0) + 1
        task["last_codex_msg"] = msg[:500]
        if not ok:
            log(f"执行阶段失败：{msg[:200]}", tid)
            return False
        task["status"] = DONE
        task["output_dir"] = str(out_dir.relative_to(ROOT))
        log(f"产出已落盘 -> {out_dir.name}", tid)
        return True

    return False


def stage_review(task: dict, state: dict) -> bool:
    """阶段三：验收。"""
    role = resolve_role(task, "reviewer")

    if role == "unavailable":
        task["status"] = "BLOCKED"
        log("reviewer 要求 Codex 但 Codex 不可用 -> 停机", task["id"])
        return False
    # 阅卷若交给 WorkBuddy，仍能保留上下文隔离（独立会话、看不到执行过程）
    if role in ("workbuddy", "trae", "template"):
        agent = "workbuddy" if role == "template" else role
        return ask_file_agent(task, state, WAITING_REVIEW,
                              "阅卷：对照验收标准逐条判定，写验收报告并给出 PASS/FAIL",
                              agent)
    if role == "wait_codex":
        return wait_for_codex(task, state, DONE)

    tid = task["id"]
    out_dir = ROOT / task.get("output_dir", f"03_output/{tid}")
    prompt_file = ROOT / task.get("prompt_file", f"02_prompts/{tid}.md")
    review_file = REVIEW / f"{tid}.md"
    baseline_file = ROOT / task.get("baseline_prompt_file", "")
    baseline_text = (baseline_file.read_text(encoding="utf-8")
                     if baseline_file.is_file() else
                     prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else "")
    criteria = task.get("evaluation_criteria") or parse_acceptance_criteria(baseline_text)
    if not criteria:
        criteria = ["按原始需求完成目标，并满足 prompt 中的产出规格"]
    task["evaluation_criteria"] = criteria
    frozen_rubric = "\n".join(f"- {item}" for item in criteria)

    # 收集产出清单
    files = [str(p.relative_to(out_dir)) for p in out_dir.rglob("*") if p.is_file()] \
        if out_dir.exists() else []

    p = f"""你是「验收者」角色。你**没有参与执行**，请严格、挑剔地验收。

===== 本轮执行所用的 Prompt =====
{prompt_file.read_text(encoding='utf-8') if prompt_file.exists() else '(缺失)'}
=====

===== 固定评测标准（来自第 1 轮，所有轮次完全相同） =====
{frozen_rubric}
评测标准来源文件：{task.get('baseline_prompt_file', '(未归档；按当前任务首次评测冻结)')}
不要用本轮 prompt 中被改写的 acceptance 替代以上固定标准。
=====

===== 产出目录 =====
{out_dir}
产出文件清单：{files}
=====

## 验收三步（必须按顺序做完，不许跳步）

**第一步：先攻击，再判分。**
动笔判分之前，先写「如果我是来找茬的，我会攻击这三点」，必须列出 3 条具体攻击点。
不许写"暂无"——找不到攻击点，说明你还没真正读进去。

**第二步：逐条判定，每条必须附证据。**
对照验收标准逐条判 通过/不通过。每条判定必须带 `evidence:`，
指明依据的是产出里**哪一句话、哪个文件、哪一行**。没有证据的判定视为无效。

**第三步：自检。**
若你最终找不到任何可改进之处，把它当作异常信号——
「一份完全无懈可击的产出，通常意味着我没认真看」。重新审视一遍再下结论。

## 报告格式（程序要解析，严格遵守）

```yaml
verdict: PASS    # 只能是 PASS 或 FAIL
overall_score: 0 # 0-100，必须按固定标准中通过项 / 总项数 × 100 四舍五入
scores:
  - criterion: "标准原文"
    passed: true
    note: "一句话理由"
    evidence: "产出里的具体位置或原文片段"
```

YAML 块之后，再用中文写一段「返工意见」：若 PASS 写"无需返工"；若 FAIL 必须写清楚**具体哪里不合格、下一步该怎么改**，要求可直接执行。

评分规则：每项等权；`overall_score` 必须由 `scores` 中 passed=true 的数量除以固定评测标准总数计算，不能凭总体印象打分。只有所有固定标准均通过时 verdict 才能是 PASS，此时分数必须为 100。请指出本轮 prompt 最主要的一个可改进点，供下一轮只改一处。

## 落盘位置（写错会让整份验收作废）

报告**必须**写入：`{review_file}`
**不要**写进 `03_output/` —— 那是执行者的目录。放错位置将被视为未交付。

只写这一个文件。
"""
    if not budget_ok(task):
        return False
    ok, msg = text_stage_call(task, "review", p, f"{tid}/review-r{task.get('round', 1)}",
                              model=task.get("selected_model"))
    task["calls"] = task.get("calls", 0) + 1
    task["last_codex_msg"] = msg[:500]

    # 模型经常把报告写到产出目录而非约定位置，先尝试找回
    if find_verdict_text(review_file) is None:
        src = salvage_review(tid)
        if src:
            review_file.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            log(f"验收报告写错了位置，已从 {src.name} 回收到 04_review", tid)

    # 仍然没有，才用返回原文兜底
    if find_verdict_text(review_file) is None:
        review_file.write_text(
            f"# {tid} 验收报告\n\n> Codex 未落盘报告，以下为其返回原文：\n\n{msg}\n",
            encoding="utf-8",
        )

    verdict, score = record_review_result(task, review_file)
    log(f"验收结论：{verdict}，固定标准得分：{score if score is not None else '未提供'}", tid)
    return True


def find_verdict_text(path: Path) -> str | None:
    """找 verdict。找不到返回 None —— 与 parse_verdict 的兜底 FAIL 区分开。"""
    try:
        txt = path.read_text(encoding="utf-8")
    except Exception:
        return None
    for line in txt.splitlines():
        s = line.strip()
        if s.lower().startswith("verdict:"):
            v = s.split(":", 1)[1].strip().strip('"\'')
            return "PASS" if "PASS" in v.upper() else "FAIL"
    return None


def salvage_review(tid: str) -> Path | None:
    """Codex 常把验收报告写到产出目录而不是 04_review/ —— 去把它找回来。

    与其指望模型每次都守规矩，不如让它写错了也能兜住。
    """
    d = OUTPUT / tid
    if not d.exists():
        return None
    for p in sorted(d.glob("*.md")):
        if p.name.upper() == "RESULT.MD":
            continue
        if find_verdict_text(p):
            return p
    return None


def parse_verdict(review_file: Path) -> str:
    try:
        txt = review_file.read_text(encoding="utf-8")
    except Exception:
        return "FAIL"
    for line in txt.splitlines():
        s = line.strip()
        if s.lower().startswith("verdict:"):
            v = s.split(":", 1)[1].strip().strip('"\'')
            return "PASS" if "PASS" in v.upper() else "FAIL"
    # 兜底：看正文倾向
    return "PASS" if "**PASS**" in txt or "验收通过" in txt else "FAIL"


# --------------------------------------------------------------------------
# 推进逻辑
# --------------------------------------------------------------------------

def check_agent_delivery(task: dict, state: dict) -> bool:
    """检查文件接力 Agent 有没有交付它承诺的文件。

    巧妙之处：不依赖它"报告"完成，只看约定路径上的文件在不在。
    这样即使它被中断、只写了一半，也不会误判为完成。
    """
    tid = task["id"]
    spec = STAGE_SPEC.get(task["status"])
    if not spec:
        return False

    _, what, path_fn = spec
    target = path_fn(tid)

    if task["status"] == WAITING_EXECUTE:
        # 执行阶段的交付信号放宽：RESULT.md 存在，或产出目录里已经有实际文件。
        # 因为 prompt 有时会禁止"额外文件"，执行者便不敢写 RESULT.md ——
        # 若只认 RESULT.md，任务会永远卡住。
        d = OUTPUT / tid
        if not (target.exists() or (d.exists() and any(p.is_file() for p in d.rglob("*")))):
            return False
    elif not target.exists():
        return False  # 还没交付，继续等

    if task["status"] == WAITING_OPTIMIZE:
        task["prompt_file"] = str(target.relative_to(ROOT))
        snapshot_prompt_version(task, target)
        task["status"] = PROMPT_READY
    elif task["status"] == WAITING_EXECUTE:
        task["status"] = DONE
    elif task["status"] == WAITING_REVIEW:
        verdict, score = record_review_result(task, target)
        agent = task.get("waiting_agent", "workbuddy")
        log(f"{agent.upper()} 验收结论：{verdict}，固定标准得分："
            f"{score if score is not None else '未提供'}", tid)

    task.pop("waiting_for", None)
    agent = task.pop("waiting_agent", "workbuddy")
    refresh_pending(state)
    log(f"检测到 {agent.upper()} 已交付：{what}", tid)
    return True


check_workbuddy_delivery = check_agent_delivery


def advance_task(task: dict, state: dict) -> bool:
    """把任务往前推一步，并留下状态流转历史（平台用它画时间线）。"""
    before = task.get("status")
    r = _advance_inner(task, state)
    after = task.get("status")
    if before != after:
        task.setdefault("history", []).append({"t": now(), "from": before, "to": after})
    return r


def _advance_inner(task: dict, state: dict) -> bool:
    tid = task["id"]
    st = task["status"]

    if st == NEW:
        if model_routing_on(task):
            return stage_model_gate(task, state)
        return stage_optimize(task, state)

    if st == AWAIT_MODEL_CONFIRM:
        return check_model_gate(task, state)

    if st == ASSESSED:
        return stage_optimize(task, state)

    if st == PROMPT_READY:
        return stage_execute(task, state)

    if st in WAITING_STATES:
        return check_agent_delivery(task, state)

    if st == WAITING_CODEX:
        # 用 8 秒的轻量探测确认 Codex 回来没有，而不是启动一次完整调用去试
        ok, msg = probe_codex()
        if ok:
            mark_codex_ok()
            OFFLINE["on"] = False
            resume = task.pop("resume_status", NEW)
            task["status"] = resume
            log(f"Codex 已恢复（{msg}），任务继续：{resume}", tid)
            return True
        return False  # 还没恢复，继续排队

    if st == DONE:
        return stage_review(task, state)

    if st == FAIL:
        rnd = task.get("round", 1)
        run = task.get("optimization_run")
        result = next((row for row in reversed(task.get("optimization_history", []))
                       if row.get("run") == run and row.get("round") == rnd), None)
        if (rnd > 1 and result and result.get("score") is not None
                and result.get("improved") is False):
            _restore_best_prompt(task)
            best = task.get("best_score")
            best_round = task.get("best_round")
            task["status"] = GIVE_UP
            task["giveup_reason"] = (
                f"第 {rnd} 轮评分 {result['score']} 未超过最佳分 {best}，"
                f"已回退到第 {best_round} 轮提示词并停止迭代"
            )
            log(task["giveup_reason"], tid)
            return False
        if rnd >= MAX_ROUNDS:
            task["status"] = "GIVE_UP"
            log(f"已达最大轮次 {MAX_ROUNDS}，停机待人工介入", tid)
            return False
        task["round"] = rnd + 1
        # 关键：回到 NEW 而不是 PROMPT_READY。
        # 若直接回 PROMPT_READY，执行者会照着旧 prompt 重做，看不到返工意见 —— 等于盲目重试。
        # 回到 NEW 会重新出题，而出题阶段会把 04_review 的意见融进新 prompt。
        task["status"] = NEW
        log(f"验收未过，打回重新出题（携带返工意见），进入第 {rnd + 1} 轮", tid)
        return True

    return False  # PASS / GIVE_UP 为终态


def run_once(state: dict, only: str | None = None) -> int:
    changed = 0
    for tid, task in state["tasks"].items():
        if only and tid != only:
            continue
        if task["status"] in (PASS, GIVE_UP, "BLOCKED"):
            continue
        try:
            if advance_task(task, state):
                changed += 1
        except Exception as e:
            log(f"推进 {tid} 出错：{e}", tid)
        save_state(state)
    refresh_pending(state)
    refresh_model_confirm(state)
    save_state(state)
    return changed


GUIDE = {
    WAITING_OPTIMIZE: (
        "一、出题：把原始需求改写为结构化 prompt",
        [
            "**若 `04_review/<ID>.md` 存在，先读它**——只针对本轮最主要的问题做一处改动",
            "先读 `01_分工与协议/ROLES.md` 第七节，严格按五段式写",
            "五段缺一不可：目标 / 上下文 / 产出规格 / 验收标准 / 禁止事项",
            "**验收标准必须二值可判定**（是或否），禁止写「尽量」「较好」这类无法判定的话",
            "第 1 轮之后每轮只改一个主要问题；`acceptance:` 必须保持首轮原样，不得改评分规则",
            "**禁止**写入「不要创建额外文件」之类条款：`RESULT.md` 是交付信号，永远不算违规",
            "文件末尾附 YAML 块：`acceptance:` 下逐条列出标准",
            "写入 `02_prompts/<ID>.md`",
        ],
    ),
    WAITING_EXECUTE: (
        "二、执行：按 prompt 完成任务",
        [
            "若 `04_review/<ID>.md` 存在（返工轮），**先读返工意见**再动手",
            "读该任务的 prompt 文件 `02_prompts/<ID>.md`，严格按它做",
            "所有产出写入 `03_output/<ID>/`",
            "**必须**写 `03_output/<ID>/RESULT.md`：做了什么、产出清单、如何验证",
        ],
    ),
    WAITING_REVIEW: (
        "三、阅卷：对照验收标准判定（你没参与执行，请保持独立判断）",
        [
            "用第 1 轮冻结的验收标准评分；不可采用本轮 prompt 改写过的标准，读 `03_output/<ID>/` 查看产出",
            "逐条判定通过与否，**不得放水**——放水会让整条流水线失去意义",
            "写入 `04_review/<ID>.md`，含 `verdict`、按通过项计算的 `overall_score`（0-100）及 `scores:` 列表",
            "YAML 之后用中文写返工意见；若 FAIL 必须写清**具体哪里不合格、下一步怎么改**",
        ],
    ),
}


def deliver_path(state_name: str, tid: str) -> str:
    """该阶段约定的交付路径（展示用）"""
    return str(STAGE_SPEC[state_name][2](tid).relative_to(ROOT))


def handoff_file(agent: str, tid: str) -> Path:
    return PROMPTS / f"_HANDOFF_{agent.upper()}_{tid}.md"


def workbuddy_model_line() -> str:
    """交接指令里注明这次该用哪个模型。

    WorkBuddy 是外部会话，编排器看不到它内部用的是什么模型；
    把用户设定的模型写进指令，至少让「谁跑的、按什么模型跑的」可复核。
    """
    try:
        import workbuddy_config
        model = (workbuddy_config.get("model") or "").strip()
    except Exception:
        model = ""
    if model:
        return f"> 使用模型：`{model}`（可在平台「WorkBuddy 配置」里修改）"
    return "> 使用模型：未指定（由 WorkBuddy 自行选择；可在平台「WorkBuddy 配置」里固定）"


def _write_handoff(task: dict, state_name: str, agent: str) -> Path:
    tid = task["id"]
    _, title, path_fn = STAGE_SPEC[state_name]
    target = path_fn(tid)
    prompt_path = PROMPTS / f"{tid}.md"
    review_path = REVIEW / f"{tid}.md"
    lines = [
        f"# {agent.upper()} 接力任务：{tid}", "",
        f"> 当前阶段：{title}。本文件由编排器生成；请勿推进其他阶段。", "",
    ]
    if agent == "workbuddy":
        lines += [workbuddy_model_line(), ""]
    lines += [
        "## 原始需求", "", task.get("requirement", ""), "",
        "## 本阶段操作", "",
    ]
    lines.extend(f"{i}. {step}" for i, step in enumerate(GUIDE[state_name][1], 1))
    lines += ["", "## 相关文件", "", f"- 交付路径：`{target.relative_to(ROOT)}`"]
    if prompt_path.exists() and state_name != WAITING_OPTIMIZE:
        lines.append(f"- 任务 Prompt：`{prompt_path.relative_to(ROOT)}`")
    if state_name == WAITING_REVIEW:
        lines += ["", "## 固定评测标准", ""]
        if task.get("baseline_prompt_file"):
            lines.append(f"- 首轮基准：`{task['baseline_prompt_file']}`")
        lines.extend(f"- {item}" for item in task.get("evaluation_criteria", []))
        lines.append("- 逐项等权；overall_score = 通过项数 / 标准总数 × 100（四舍五入）。PASS 必须为 100 分。")
    if state_name == WAITING_OPTIMIZE and task.get("round", 1) > 1:
        lines += ["", "## 首轮冻结的评测标准", ""]
        lines.extend(f"- {item}" for item in task.get("evaluation_criteria", []))
        if prompt_path.exists():
            lines += ["", "## 当前最佳提示词版本", "", prompt_path.read_text(encoding="utf-8")]
    if review_path.exists() and state_name != WAITING_REVIEW:
        lines.append(f"- 上一轮验收：`{review_path.relative_to(ROOT)}`")
    if state_name == WAITING_EXECUTE and prompt_path.exists():
        lines += ["", "## 执行 Prompt", "", prompt_path.read_text(encoding="utf-8")]
    lines += ["", "只写本阶段约定的交付路径。编排器检测到交付文件后会自动推进。", ""]
    target_file = handoff_file(agent, tid)
    target_file.write_text("\n".join(lines), encoding="utf-8")
    return target_file


def _refresh_agent_pending(state: dict, agent: str, path: Path) -> None:
    waiting = [t for t in state.get("tasks", {}).values()
               if t.get("status") in WAITING_STATES
               and t.get("waiting_agent", "workbuddy") == agent]
    if not waiting:
        path.unlink(missing_ok=True)
        return

    label = "TRAE" if agent == "trae" else "WorkBuddy"
    lines = [
        f"# 待 {label} 接力的任务", "",
        f"> 由 orchestrator.py 自动生成于 {now()}。手改无效，会被覆盖。",
        ("> TRAE 使用本地 IDE 文件接力，不代表已连接 TRAE API；打开下方指令文件后按交付路径写入。"
         if agent == "trae" else
         "> WorkBuddy 使用独立会话读取本队列，按下方指令文件与交付路径完成任务。"), "",
        "## 协作约定", "",
        "1. 只处理分配给自己的阶段，不要代做后续阶段，保持交叉验收独立。",
        "2. 交付文件必须写到表格指定路径；编排器以该文件作为完成信号。",
        "3. 完成后无需修改本文件，自动轮询会检测交付并推进任务。", "",
    ]

    for state_name in WAITING_STATES:
        group = [t for t in waiting if t.get("status") == state_name]
        if not group:
            continue
        title, _ = GUIDE[state_name]
        lines += [f"## {title}", "",
                  "| 任务 ID | 轮次 | 原始需求 | 操作指令 | 交付路径 |",
                  "|---|---:|---|---|---|"]
        for task in group:
            instruction = _write_handoff(task, state_name, agent)
            lines.append(
                f"| {task['id']} | {task.get('round', 1)} | "
                f"{task.get('requirement', '')[:38].replace('|', '/').replace(chr(10), ' ')} | "
                f"`{instruction.relative_to(ROOT)}` | `{deliver_path(state_name, task['id'])}` |"
            )
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_pending(state: dict) -> None:
    """Refresh separate handoff queues for WorkBuddy and TRAE."""
    _refresh_agent_pending(state, "workbuddy", PENDING_FILE)
    _refresh_agent_pending(state, "trae", PENDING_TRAE_FILE)


# 终态：不会再有下一步，恢复时不必理会
TERMINAL_STATES = (PASS, GIVE_UP, "BLOCKED")


def restore_unfinished() -> dict:
    """重启后恢复现场：重新扫描未完成任务，重建两份待办队列。

    为什么需要它：state.json 只是"记录了什么"，`_PENDING_FOR_WORKBUDDY.md`
    和每个 `_HANDOFF_*.md` 才是 WorkBuddy 会话真正读的东西。这两个文件随时可能
    因为手动清理、切分支、换机器而消失；没有这一步，任务在状态里"还在等"，
    但 WorkBuddy 那边看不到任何待办 —— 现场就丢了。

    另外把"进程被杀时正在跑"的 DELIBERATING 回退成 DELIB_PAUSED：
    推进它的是平台里的一条后台线程，进程没了线程也没了；状态留在 DELIBERATING
    会让界面永远显示"进行中"，而用户点不了也等不到。回退后可以点继续恢复。

    返回摘要给界面显示（不抛异常，出问题也如实写进 summary）。
    """
    summary: dict = {"ok": True, "restored_at": now(), "total": 0, "unfinished": 0,
                     "waiting_workbuddy": 0, "by_status": {}, "reverted": 0,
                     "error": None}
    try:
        with FileLock(LOCK_FILE):
            state = load_state()
            tasks = state.get("tasks", {}) if isinstance(state, dict) else {}
            summary["total"] = len(tasks)
            unfinished = [t for t in tasks.values()
                          if isinstance(t, dict) and t.get("status") not in TERMINAL_STATES]
            summary["unfinished"] = len(unfinished)
            reverted = 0
            for task in unfinished:
                status = task.get("status") or "UNKNOWN"
                if status == DELIBERATING:
                    task["status"] = DELIB_PAUSED
                    task["updated"] = now()
                    task["resume_note"] = "服务重启，多角色博弈已暂停，可点「继续」从断点恢复"
                    reverted += 1
                    status = DELIB_PAUSED
                summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
                if (status in WAITING_STATES
                        and task.get("waiting_agent", "workbuddy") == "workbuddy"):
                    summary["waiting_workbuddy"] += 1
            summary["reverted"] = reverted
            if reverted:
                save_state(state)
                log(f"启动恢复：{reverted} 个中断的博弈已回退为可继续状态")
            refresh_pending(state)
            refresh_model_confirm(state)
            if summary["unfinished"]:
                log(f"启动恢复：{summary['unfinished']} 个未完成任务，"
                    f"其中 {summary['waiting_workbuddy']} 个等 WorkBuddy 接力")
    except Exception as exc:  # 恢复失败不能拖垮平台启动
        summary["ok"] = False
        summary["error"] = f"{type(exc).__name__}: {exc}"
    return summary


def pending_queue_info() -> dict:
    """待办队列文件是否健康（界面用来判断"恢复"是否真的生效）。"""
    info = {"workbuddy": {"path": str(PENDING_FILE), "exists": PENDING_FILE.exists(),
                          "size": 0, "modified": None}}
    for path in (PENDING_FILE,):
        try:
            stat = path.stat()
            info["workbuddy"]["size"] = stat.st_size
            info["workbuddy"]["modified"] = datetime.fromtimestamp(
                stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            pass
    return info


def refresh_model_confirm(state: dict) -> None:
    """生成「等你确认模型」的待确认清单（看板会读，也方便人工离线确认）。"""
    pending = [t for t in state["tasks"].values()
               if t["status"] == AWAIT_MODEL_CONFIRM]
    if not pending:
        MODEL_CONFIRM_FILE.unlink(missing_ok=True)
        return

    lines = [
        "# 待确认模型（难度路由闸门）",
        "",
        f"> 由 orchestrator.py 自动生成于 {now()}。手改无效，会被覆盖。",
        "",
        "Codex 已评估任务难度并推荐模型，请确认或覆盖；",
        f"超过 {MODEL_CONFIRM_WINDOW} 秒未确认将**自动采用推荐模型**。",
        "",
        "| 任务 ID | 难度 | 推荐模型 | 理由 | 确认方式 |",
        "|---|---|---|---|---|",
    ]
    for t in pending:
        remain = max(0, int(t.get("model_deadline", 0) - time.time()))
        mm, ss = divmod(remain, 60)
        lines.append(
            f"| {t['id']} | {t.get('difficulty','-')} | "
            f"`{t.get('recommended_model','-')}` | "
            f"{t.get('model_reason','-')[:40]} | "
            f"剩 {mm:02d}:{ss:02d}（CLI: `python orchestrator.py confirm {t['id']} [模型]`） |"
        )
    lines.append("")
    MODEL_CONFIRM_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def confirm_model(state: dict, tid: str, model: str | None = None) -> tuple[bool, str]:
    """人工确认某个任务的模型（来自看板按钮或 CLI confirm）。

    返回 (成功, 消息)。确认后锁定 selected_model 并放行到 ASSESSED。
    """
    t = state["tasks"].get(tid)
    if not t:
        return False, f"找不到任务 {tid}"
    if t["status"] not in (AWAIT_MODEL_CONFIRM,):
        # 已放行/已终态的任务不允许再改模型（避免影响已在进行中的 Codex 调用）
        if t.get("model_locked"):
            return False, f"任务 {tid} 已锁定模型 {t.get('selected_model')}，无法再改"
        return False, f"任务 {tid} 当前状态 {t['status']}，不在待确认态"

    chosen = model or t.get("recommended_model")
    if model and model not in MODEL_CANDIDATES:
        # 不在候选清单也允许（用户可能知道其他可用模型），仅提示
        log(f"确认模型 {model} 不在候选清单，仍采用（请确认中转站支持）", tid)
    t["selected_model"] = chosen
    t["model_status"] = "confirmed"
    t["model_locked"] = True
    t["status"] = ASSESSED
    log(f"✅ 人工确认模型：{chosen}", tid)
    return True, f"已确认 {tid} 使用 {chosen}"


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------

# 预设：把常用组合固化，免得每次都要敲三个参数
MODE_PRESETS = {
    # 一条龙：Codex 出题 → WorkBuddy 执行 → Codex 验收 → 打回重来
    # Codex 不可用时排队等，不改派人 —— 换人就等于放弃交叉验证
    "cross": {"optimizer": "codex", "executor": "workbuddy", "reviewer": "codex",
              "on_unavailable": "wait"},
    # 全交给 Codex：最快，但执行者与验收者同模型，验收可信度下降
    "fast": {"optimizer": "codex", "executor": "codex", "reviewer": "codex",
             "on_unavailable": "fallback"},
    # Codex 出题 → TRAE 在 IDE 中执行 → Codex 独立验收
    "trae": {"optimizer": "codex", "executor": "trae", "reviewer": "codex",
             "on_unavailable": "wait"},
}


def cmd_new(args):
    preset = dict(MODE_PRESETS.get(getattr(args, "mode", None), {}))
    if getattr(args, "on_unavailable", None):
        preset["on_unavailable"] = args.on_unavailable

    with FileLock(LOCK_FILE):
        state = load_state()
        tid = new_id(state)
        task = {
            "id": tid,
            "requirement": args.requirement,
            "optimizer": preset.get("optimizer", args.optimizer),
            "executor": preset.get("executor", args.executor),
            "reviewer": preset.get("reviewer", args.reviewer),
            "status": NEW,
            "round": 1,
            "calls": 0,
            "created": now(),
            "updated": now(),
        }
        if "on_unavailable" in preset:
            task["on_unavailable"] = preset["on_unavailable"]
        # 难度路由闸门：默认开启，可用 --no-model-route 关掉
        task["model_route"] = not getattr(args, "no_model_route", False)
        state["tasks"][tid] = task
        save_state(state)
    log(f"创建任务 {tid}（出题={args.optimizer} 执行={args.executor} 阅卷={args.reviewer}"
        f"{'｜难度路由开' if not getattr(args,'no_model_route',False) else '｜难度路由关'}）", tid)
    print(f"任务已创建：{tid}")
    print(f"  出题={resolve_role(task, 'optimizer')} "
          f"执行={resolve_role(task, 'executor')} "
          f"阅卷={resolve_role(task, 'reviewer')}")
    return tid


def cmd_list(_args):
    state = load_state()
    if not state["tasks"]:
        print("（暂无任务）用 `python orchestrator.py new \"需求\"` 创建")
        return
    print(f"{'ID':<20}{'状态':<20}{'轮次':<6}{'验收':<8}需求")
    print("-" * 100)
    for tid, t in state["tasks"].items():
        print(f"{tid:<20}{t['status']:<20}{t.get('round', 1):<6}{t.get('verdict', '-'):<8}"
              f"{t['requirement'][:44]}")
    print()
    if CODEX_BIN:
        print(f"Codex: {CODEX_BIN}")
    else:
        print("⚠️  未找到 codex.exe")


def cmd_show(args):
    state = load_state()
    t = state["tasks"].get(args.task_id)
    if not t:
        print(f"找不到任务 {args.task_id}")
        return
    print(json.dumps(t, ensure_ascii=False, indent=2))


def cmd_confirm(args):
    with FileLock(LOCK_FILE):
        state = load_state()
        ok, msg = confirm_model(state, args.task_id, getattr(args, "model", None))
        if ok:
            save_state(state)
            # 立即推进一轮，省去等下一轮询
            run_once(state)
    print(msg)


def cmd_run(args):
    with FileLock(LOCK_FILE):
        state = load_state()
        n = run_once(state, args.task_id)
    print(f"本轮状态变更：{n} 次")


def cmd_watch(args):
    interval = args.interval
    if not interval:
        # 没在命令行指定就用平台里配置的轮询间隔（默认 45s）
        try:
            import workbuddy_config
            interval = int(workbuddy_config.get("poll_seconds") or 45)
        except Exception:
            interval = 45
    print(f"👀 常驻轮询中，间隔 {interval}s，Ctrl+C 停止")
    try:
        while True:
            try:
                with FileLock(LOCK_FILE):
                    state = load_state()
                    n = run_once(state)
                if n:
                    print(f"  -> 变更 {n} 次")
            except RuntimeError as e:
                print(f"  （跳过：{e}）")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止")


# --------------------------------------------------------------------------
# 网页看板
# --------------------------------------------------------------------------

def cmd_serve(args):
    serve(args.port)


def serve(port: int = 8765):
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    import urllib.parse
    import threading

    html_file = ROOT / "dashboard.html"

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(ROOT), **kw)

        def log_message(self, *a):
            pass

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/" or path == "/dashboard":
                data = html_file.read_bytes() if html_file.exists() else b"<h1>dashboard.html missing</h1>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/state":
                state = load_state()
                state["codex_bin"] = str(CODEX_BIN) if CODEX_BIN else None
                state["updated_at"] = now()
                state["model_candidates"] = MODEL_CANDIDATES
                state["model_confirm_window"] = MODEL_CONFIRM_WINDOW
                data = json.dumps(state, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                super().do_GET()

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            result = {"ok": False}

            if path == "/api/new":
                try:
                    payload = json.loads(body)
                    req = payload.get("requirement", "").strip()
                    if req:
                        roles = ("optimizer", "executor", "reviewer")
                        picked = {r: payload.get(r, "auto") for r in roles}
                        if payload.get("on_unavailable"):
                            picked["on_unavailable"] = payload["on_unavailable"]
                        # 难度路由闸门：默认开，前端可传 model_route=false 关闭
                        picked["model_route"] = payload.get("model_route",
                                                           MODEL_ROUTING_DEFAULT)
                        with FileLock(LOCK_FILE):
                            state = load_state()
                            tid = new_id(state)
                            state["tasks"][tid] = {
                                "id": tid, "requirement": req,
                                "status": NEW, "round": 1, "created": now(), "updated": now(),
                                **picked,
                            }
                            save_state(state)
                        result = {"ok": True, "id": tid}
                except Exception as e:
                    result = {"ok": False, "error": str(e)}

            elif path == "/api/confirm_model":
                try:
                    payload = json.loads(body)
                    tid = payload.get("task_id", "")
                    model = payload.get("model") or None
                    with FileLock(LOCK_FILE):
                        state = load_state()
                        ok, msg = confirm_model(state, tid, model)
                        if ok:
                            save_state(state)
                            # 后台立即推进，省去等下一轮询
                            threading.Thread(
                                target=lambda: run_once(state), daemon=True
                            ).start()
                    result = {"ok": ok, "msg": msg}
                except Exception as e:
                    result = {"ok": False, "error": str(e)}

            elif path == "/api/advance":
                # 后台异步推进，避免请求超时
                def work():
                    try:
                        with FileLock(LOCK_FILE):
                            st = load_state()
                            run_once(st)
                    except RuntimeError as e:
                        log(f"跳过推进：{e}")

                threading.Thread(target=work, daemon=True).start()
                result = {"ok": True, "msg": "已在后台推进"}

            data = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", port), Handler)
    print(f"🌐 看板已启动： http://127.0.0.1:{port}/")
    print("   按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


# --------------------------------------------------------------------------

def main():
    ensure_workspace_dirs()

    ap = argparse.ArgumentParser(description="ai-relay 流水线编排器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="创建新任务")
    p.add_argument("requirement")
    roles = ["auto", "codex", "workbuddy", "trae", "template"]
    p.add_argument("--optimizer", default="auto", choices=roles,
                   help="谁负责出题（默认 auto：Codex 不可用时自动改派 WorkBuddy）")
    p.add_argument("--executor", default="auto", choices=roles, help="谁负责执行")
    p.add_argument("--reviewer", default="auto", choices=roles, help="谁负责阅卷")
    p.add_argument("--mode", default=None, choices=list(MODE_PRESETS.keys()),
                   help="预设：cross=Codex+WorkBuddy；trae=Codex+TRAE+Codex")
    p.add_argument("--on-unavailable", default=None, choices=["wait", "fallback"],
                   help="Codex 不可用时：wait=排队等它恢复；fallback=改派 WorkBuddy")
    p.add_argument("--no-model-route", action="store_true",
                   help="关闭难度评估+模型确认闸门（任务直接按默认模型跑）")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("list", help="列出所有任务")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="查看单个任务")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("confirm", help="确认某任务的选用模型（难度路由闸门）")
    p.add_argument("task_id")
    p.add_argument("model", nargs="?", default=None,
                   help="覆盖模型（不填则采用 Codex 推荐模型）")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("run", help="推进一轮")
    p.add_argument("task_id", nargs="?")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("watch", help="常驻轮询")
    p.add_argument("--interval", type=int, default=None,
                   help="轮询间隔（秒）；不填则读平台里配置的 poll_seconds")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("serve", help="启动网页看板")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
