#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 配置：本地持久化 + 校验 + 原子写入
================================================

为什么单独一个文件，而不是塞进 `_state/agents.json`：
`agents.json` 会被「多 Agent 分工」面板整体重写（load → 改 → save），
`agents.load_config()` 只保留 backends / roles 两段，任何额外字段都会在
下一次保存时被丢掉。WorkBuddy 的配置有自己的生命周期（模型、轮询间隔、
超时、自动恢复），独立成文件才不会互相踩。

可靠性约定（每条都对应一种真实故障）：
1. 文件不存在  —— 读的时候用默认值（不产生副作用），`ensure_file()` 会把默认值
                 落盘，让用户有一个明确可改的文件；
2. 内容损坏    —— 备份成 `workbuddy.corrupt-<时间戳>.json`，降级用默认值，
                 并把告警带回界面（绝不静默吞掉用户配置）；
3. 字段非法    —— 逐字段校验，非法值不落盘：写入返回 400 + 字段级错误，
                 读取时非法值回退默认值并记告警；
4. 写一半断电  —— 写临时文件 + fsync + os.replace 原子替换，再留一份 .bak；
5. 多进程并发  —— 文件锁（Windows 用 msvcrt，其他平台用 flock）；
6. 无权限/被占用 —— 读降级（默认值 + 告警），写明确报错，不让服务崩。
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "_state" / "workbuddy.json"
LOCK_FILE = ROOT / "_state" / ".workbuddy.lock"
BACKUP_FILE = ROOT / "_state" / "workbuddy.bak.json"

# ---------------------------------------------------------------- 字段定义
# 每一项：默认值、给人看的中文名、类型、允许范围/正则、说明
FIELD_SPECS: dict[str, dict] = {
    "model": {
        "default": "",
        "label": "WorkBuddy 使用的模型",
        "type": "model",
        "hint": "留空 = 由 WorkBuddy 自己决定。填写后会写进交接指令，便于复核是谁跑的。",
    },
    "poll_seconds": {
        "default": 45,
        "label": "轮询间隔（秒）",
        "type": "int",
        "min": 5,
        "max": 3600,
        "hint": "常驻轮询多久检查一次交付文件。太小会空转，太大会让接力变慢。",
    },
    "handoff_timeout_minutes": {
        "default": 120,
        "label": "单次接力超时（分钟）",
        "type": "int",
        "min": 5,
        "max": 10080,
        "hint": "超过这个时间还没交付，看板会把任务标成「疑似卡住」，不会自动判失败。",
    },
    "auto_resume": {
        "default": True,
        "label": "启动时自动恢复未完成任务",
        "type": "bool",
        "hint": "开启后，平台每次启动都会扫描未完成任务并重建 WorkBuddy 待办队列。",
    },
}

DEFAULTS: dict = {k: v["default"] for k, v in FIELD_SPECS.items()}

_MODEL_RE = re.compile(r"[\w .:/@+\-]{1,80}", re.UNICODE)
_ILLEGAL_CHARS = re.compile(r"[\x00-\x1f\x7f<>\"'`\\]")


class ConfigError(ValueError):
    """写入被拒绝（校验不通过或磁盘写失败）。errors 为字段级明细。"""

    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.message, "errors": self.errors}


# ---------------------------------------------------------------- 文件锁
@contextmanager
def file_lock(path: Path = LOCK_FILE, timeout: float = 5.0):
    """跨进程互斥。抢不到锁就抛 ConfigError，而不是无限等。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
    except OSError as exc:
        raise ConfigError(f"无法创建配置锁文件 {path.name}：{exc}") from exc
    locked = False
    deadline = time.time() + timeout
    try:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.time() >= deadline:
                    raise ConfigError(
                        f"配置文件被其他进程占用（等待 {timeout:.0f}s 仍未拿到锁）：{path.name}")
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


# ---------------------------------------------------------------- 值校验
def _coerce_model(raw) -> tuple[str, str | None]:
    """返回 (值, 错误)。空串代表"不指定"，是合法值。"""
    if not isinstance(raw, str):
        return "", f"模型名必须是字符串（收到 {type(raw).__name__}）"
    value = " ".join(raw.split()).strip()
    if not value:
        return "", None
    if _ILLEGAL_CHARS.search(value):
        return "", "模型名不能包含控制字符或 <>\"'`\\ 等特殊符号"
    if not _MODEL_RE.fullmatch(value):
        return "", "模型名只能用字母、数字、汉字、空格和 . _ : / @ + -（最长 80 字符）"
    return value, None


def _coerce_int(raw, low: int, high: int) -> tuple[int, str | None]:
    if isinstance(raw, bool):
        return low, "不能是布尔值"
    if isinstance(raw, float):
        # JSON 里 1e400 是合法写法，解析出来是 inf；inf/NaN 直接 int() 会抛
        # OverflowError / ValueError，把整个请求打成 500。必须先挡掉。
        if not math.isfinite(raw):
            return low, "必须是有限整数（收到无穷大或 NaN）"
        if raw != int(raw):
            return low, "必须是整数"
        raw = int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return low, "不能为空"
        if not re.fullmatch(r"[+-]?\d+", text):
            return low, f"「{text[:20]}」不是整数"
        raw = int(text)
    if not isinstance(raw, int):
        return low, f"必须是整数（收到 {type(raw).__name__}）"
    if raw < low or raw > high:
        return low, f"必须在 {low}–{high} 之间（收到 {raw}）"
    return raw, None


def _coerce_bool(raw) -> tuple[bool, str | None]:
    if isinstance(raw, bool):
        return raw, None
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw), None
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("1", "true", "yes", "on", "是", "开"):
            return True, None
        if text in ("0", "false", "no", "off", "否", "关"):
            return False, None
        return False, f"「{text[:20]}」不是布尔值（可用 true/false、1/0、是/否）"
    return False, f"必须是布尔值（收到 {type(raw).__name__}）"


def validate(data: dict, *, partial: bool = False) -> tuple[dict, list[dict]]:
    """逐字段校验。partial=True 时只校验出现的字段（用于局部更新）。"""
    if not isinstance(data, dict):
        raise ConfigError("配置必须是一个 JSON 对象")
    out: dict = {} if partial else dict(DEFAULTS)
    errors: list[dict] = []

    for name, spec in FIELD_SPECS.items():
        if partial and name not in data:
            continue
        if name not in data:
            continue
        raw = data[name]
        kind = spec["type"]
        if kind == "model":
            value, err = _coerce_model(raw)
        elif kind == "int":
            value, err = _coerce_int(raw, spec["min"], spec["max"])
        else:
            value, err = _coerce_bool(raw)
        if err:
            errors.append({"field": name, "label": spec["label"], "error": err,
                           "received": _short(raw)})
        else:
            out[name] = value
    return out, errors


def _short(value) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:
        text = repr(value)
    return text[:60]


# ---------------------------------------------------------------- 读写
def write_json_atomic(path: Path, payload: dict, backup: Path | None = None) -> None:
    """写临时文件 + fsync + 原子替换；替换前把旧文件留一份备份。

    通用实现：agents.json 也走这里，避免"写一半被读到"或断电留下半个文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if backup is not None and path.exists():
            try:
                shutil.copy2(path, backup)
            except OSError:
                pass  # 备份失败不应阻断保存
        os.replace(tmp, path)
    except OSError as exc:
        raise ConfigError(f"写入 {path.name} 失败：{exc}") from exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _atomic_write(path: Path, payload: dict) -> None:
    write_json_atomic(path, payload, backup=BACKUP_FILE)



def ensure_file() -> tuple[dict, list[str]]:
    """首次运行/文件被删时，把默认配置落盘，让用户有个明确的文件可改。

    读取路径（load）本身不写盘，避免"只是查一下配置"就产生副作用。
    """
    cfg, warnings = load()
    if not CONFIG_FILE.exists():
        try:
            _atomic_write(CONFIG_FILE, cfg)
        except ConfigError as exc:
            warnings.append(f"{exc}（当前使用内存中的默认值，重启后会丢失）")
    return cfg, warnings


def _read_config_text(path: Path) -> tuple[str | None, str | None]:
    """按字节读，再尝试解码。

    只写 `read_text(encoding="utf-8")` 是不够的：Windows 记事本"另存为"默认是
    ANSI(GBK)，新版也可能是 "UTF-8 带 BOM"。前者让 read_text 抛 UnicodeDecodeError
    （这个异常既不是 OSError 也不是 JSONDecodeError，会直接冒到接口层变 500），
    后者让 json.loads 报 "Unexpected UTF-8 BOM"。两种都得认。
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, f"配置文件读不出来：{exc}"
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding), None
        except UnicodeDecodeError:
            continue
    return None, "配置文件不是 UTF-8 或 GBK 编码（可能是二进制或已损坏）"


def load() -> tuple[dict, list[str]]:
    """读配置。永远返回一份可用的配置 + 告警列表（不抛异常、不写盘）。"""
    warnings: list[str] = []
    if not CONFIG_FILE.exists():
        return dict(DEFAULTS), warnings

    raw, read_error = _read_config_text(CONFIG_FILE)
    if raw is None:
        warnings.append(f"{read_error}；已回退默认值")
        return dict(DEFAULTS), warnings

    if not raw.strip():
        warnings.append("配置文件是空的；已回退默认值（下次保存会覆盖它）")
        return dict(DEFAULTS), warnings

    try:
        saved = json.loads(raw)
    except json.JSONDecodeError as exc:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bad = CONFIG_FILE.with_name(f"workbuddy.corrupt-{stamp}.json")
        try:
            shutil.copy2(CONFIG_FILE, bad)
            warnings.append(f"配置文件不是合法 JSON（{exc.msg} 第 {exc.lineno} 行）；"
                            f"已备份为 {bad.name} 并回退默认值")
        except OSError:
            warnings.append(f"配置文件不是合法 JSON（{exc.msg}）；已回退默认值")
        return dict(DEFAULTS), warnings

    if not isinstance(saved, dict):
        warnings.append("配置文件顶层不是对象；已回退默认值")
        return dict(DEFAULTS), warnings

    cfg, errors = validate(saved)
    for item in errors:
        warnings.append(f"{item['label']}：{item['error']}（已用默认值 "
                        f"{_short(DEFAULTS[item['field']])}）")
    unknown = [k for k in saved if k not in FIELD_SPECS]
    if unknown:
        warnings.append(f"忽略未知字段：{', '.join(sorted(unknown))}")
    return cfg, warnings


def get(name: str):
    cfg, _ = load()
    return cfg.get(name, DEFAULTS.get(name))


def save(patch: dict) -> dict:
    """局部更新并落盘。校验不通过抛 ConfigError（带字段级明细）。"""
    clean, errors = validate(patch, partial=True)
    if errors:
        raise ConfigError("配置校验未通过，未写入任何内容", errors)
    with file_lock():
        current, _ = load()
        merged = {**dict(DEFAULTS), **current, **clean}
        # 合并后整体再校验一遍：防止 .bak/历史文件把非法值带进来
        final, errors = validate(merged)
        if errors:
            raise ConfigError("合并后的配置不合法，未写入", errors)
        _atomic_write(CONFIG_FILE, final)
    return final


def reset() -> dict:
    with file_lock():
        _atomic_write(CONFIG_FILE, dict(DEFAULTS))
    return dict(DEFAULTS)


def status() -> dict:
    """给界面用的诊断信息：文件在哪、能不能写、上次改到什么时候。"""
    exists = CONFIG_FILE.exists()
    info = {
        "path": str(CONFIG_FILE),
        "relative_path": str(CONFIG_FILE.relative_to(ROOT)) if CONFIG_FILE.is_relative_to(ROOT)
        else str(CONFIG_FILE),
        "exists": exists,
        "writable": os.access(CONFIG_FILE.parent, os.W_OK) if CONFIG_FILE.parent.exists() else False,
        "modified": None,
        "size": 0,
        "backup": BACKUP_FILE.exists(),
    }
    if exists:
        try:
            stat = CONFIG_FILE.stat()
            info["modified"] = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            info["size"] = stat.st_size
        except OSError:
            pass
    return info


def describe() -> dict:
    """把字段定义吐给前端，免得 UI 里再抄一份校验规则。"""
    return {
        name: {"label": spec["label"], "hint": spec["hint"],
               "default": spec["default"], "type": spec["type"],
               **({"min": spec["min"], "max": spec["max"]} if spec["type"] == "int" else {})}
        for name, spec in FIELD_SPECS.items()
    }
