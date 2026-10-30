#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多 Agent 后端适配层
=================

目标：流水线的每个「角色」可以绑定不同的 AI 后端，实现多模型分工与交叉检验。

能力边界（如实说明，不吹牛）：
- 文本型角色（难度评估 / 出题 / 验收）：只需"文本进、文本出"，
  任何 OpenAI 兼容的 HTTP API 都能干 —— DeepSeek、豆包(火山 Ark)、tokken 中转等。
- 执行型角色：要真的把文件写进 03_output/，支持三种方式：
    * codex_cli   —— 本机 Codex CLI（经 8787 代理）
    * workbuddy   —— WorkBuddy（文件总线协议，定时任务唤起）
    * trae        —— TRAE（IDE 工作区 + 文件接力，需人工在 IDE 确认执行）
- TRAE 不依赖私有 API 或全自动控制；编排器提供阶段指令和文件交付检测。

配置文件：_state/agents.json（api_key 只存本机；平台接口读取时会打码）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENTS_FILE = ROOT / "_state" / "agents.json"

# 默认配置：首次运行时落盘。角色默认绑定与旧版行为一致。
DEFAULT_CONFIG: dict = {
    "backends": {
        "codex": {"type": "codex_cli", "label": "Codex CLI (tokken 中转)"},
        "workbuddy": {"type": "workbuddy", "label": "WorkBuddy (文件总线)"},
        "trae": {"type": "trae", "label": "TRAE (IDE 文件接力)"},
        "tokken": {
            "type": "openai", "label": "tokken 中转 (GPT 系)",
            "base_url": "http://127.0.0.1:8787/v1",
            "model": "gpt-6-sol",
            "api_key": "",  # 留空则自动读 ~/.codex/config.toml 的 token
        },
        "deepseek": {
            "type": "openai", "label": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": "",
        },
        "doubao": {
            "type": "openai", "label": "豆包 (火山 Ark)",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "model": "",  # 需填 inference endpoint id，如 ep-xxxx
            "api_key": "",
        },
    },
    "roles": {
        "assess": "codex",      # 难度评估
        "optimize": "codex",    # 出题
        "execute": "workbuddy", # 执行（只能 codex / workbuddy）
        "review": "codex",      # 验收
    },
}

# 执行角色只能用能写文件的后端
EXECUTE_ALLOWED = ("codex", "workbuddy", "trae")


def find_trae_executable() -> Path | None:
    """Locate the TRAE desktop app without assuming a fixed install directory."""
    candidates = [os.environ.get("TRAE_BIN", "")]
    if os.name == "nt":
        local_app_data = Path(os.environ.get(
            "LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        candidates.extend((
            str(local_app_data / "Programs" / "TRAE SOLO CN" / "TRAE SOLO CN.exe"),
            str(local_app_data / "Programs" / "TRAE" / "TRAE.exe"),
        ))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    for command in ("trae", "trae.exe"):
        found = shutil.which(command)
        if found:
            return Path(found)
    return None


def open_trae(workspace: Path) -> tuple[bool, str]:
    """Open a workspace in TRAE; the agent still completes work via files."""
    executable = find_trae_executable()
    if not executable:
        return False, "未找到 TRAE。可设置 TRAE_BIN 指向 TRAE 可执行文件。"
    try:
        subprocess.Popen([str(executable), str(workspace)], cwd=str(workspace))
        return True, f"已在 TRAE 中打开工作区：{workspace}"
    except Exception as exc:
        return False, f"无法启动 TRAE：{exc}"


def load_config() -> dict:
    """读配置：默认值打底，磁盘上的值覆盖。

    backends 必须**逐个后端深合并**：早先的实现是
    `cfg["backends"].update(saved["backends"])`，只要磁盘上某后端只存了一个字段
    （比如只存了 model），整个后端条目就会被替换掉，type/label 一起丢失 ——
    之后 test_backend 就会报"后端类型不支持"，而配置看起来"只是少了个模型名"。
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if AGENTS_FILE.exists():
        try:
            saved = json.loads(AGENTS_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            saved = {}
        if isinstance(saved, dict):
            backends = saved.get("backends")
            if isinstance(backends, dict):
                for name, fields in backends.items():
                    if not isinstance(fields, dict):
                        continue
                    base = cfg["backends"].get(name)
                    if isinstance(base, dict):
                        base.update({k: v for k, v in fields.items() if v is not None})
                    else:
                        cfg["backends"][name] = dict(fields)
            roles = saved.get("roles")
            if isinstance(roles, dict):
                cfg["roles"].update(roles)

    # WorkBuddy 的模型等参数存在独立文件里，读出来并进后端条目，
    # 这样交接指令、状态面板、连通性自检拿到的都是同一份值。
    try:
        import workbuddy_config
        wb = workbuddy_config.load()[0]
        if "workbuddy" in cfg["backends"]:
            cfg["backends"]["workbuddy"]["model"] = wb.get("model", "")
            cfg["backends"]["workbuddy"]["poll_seconds"] = wb.get("poll_seconds")
            cfg["backends"]["workbuddy"]["handoff_timeout_minutes"] = wb.get(
                "handoff_timeout_minutes")
    except Exception:
        pass
    return cfg


# 这些字段的真实来源是 _state/workbuddy.json，load_config 只是把它们并进来方便读取；
# 写回 agents.json 时必须摘掉，否则同一个模型会有两份副本，改一边另一边是陈的。
WORKBUDDY_INJECTED = ("model", "poll_seconds", "handoff_timeout_minutes")


def save_config(cfg: dict) -> None:
    """原子写入（临时文件 + fsync + 替换 + .bak），避免写一半被读到。"""
    import workbuddy_config
    payload = json.loads(json.dumps(cfg))  # 深拷贝，别改调用方传进来的对象
    backend = payload.get("backends", {}).get("workbuddy")
    if isinstance(backend, dict):
        for key in WORKBUDDY_INJECTED:
            backend.pop(key, None)
    workbuddy_config.write_json_atomic(
        AGENTS_FILE, payload, backup=AGENTS_FILE.with_name("agents.bak.json"))


def backend_for_role(role: str, cfg: dict | None = None) -> dict | None:
    """返回角色绑定的后端配置（带 name 字段）；找不到返回 None。"""
    cfg = cfg or load_config()
    name = cfg.get("roles", {}).get(role, "codex")
    b = cfg.get("backends", {}).get(name)
    if isinstance(b, dict):
        return {**b, "name": name}
    return None


def is_openai(backend: dict | None) -> bool:
    return bool(backend) and backend.get("type") == "openai"


def tokken_token() -> str:
    """从 ~/.codex/config.toml 里抓 bearer token，给 tokken 后端免配置用。"""
    try:
        cfg = (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
        m = re.search(r'experimental_bearer_token\s*=\s*"([^"]+)"', cfg)
        return m.group(1) if m else ""
    except Exception:
        return ""


def call_openai(backend: dict, prompt: str, timeout: int = 600,
                on_event=None) -> tuple[bool, str]:
    """调任意 OpenAI 兼容 /chat/completions。纯标准库，无第三方依赖。

    注意：WorkBuddy 宿主环境会注入 HTTP_PROXY（只代理自家域名），
    国内 API（DeepSeek/Ark）直连反而更快 —— 所以默认绕过环境代理直连；
    个别确需代理的后端可在配置里加 "proxy": "http://127.0.0.1:7890"。
    """
    name = backend.get("name", "?")
    base = (backend.get("base_url") or "").rstrip("/")
    model = backend.get("model") or ""
    key = backend.get("api_key") or ""
    if "tokken" in name and not key:
        key = tokken_token()
    if not base or not model:
        return False, f"后端 {name} 未配置 base_url / model"

    if on_event:
        on_event({"type": "system", "message": f"[{name}] 调用 {model} …"})

    url = f"{base}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}" if key else "",
    })
    proxy = backend.get("proxy") or None
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({} if not proxy else
                                    {"http": proxy, "https": proxy}))

    t0 = time.time()
    try:
        resp = opener.open(req, timeout=timeout)
        data = json.loads(resp.read().decode("utf-8"))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if on_event:
            on_event({"type": "assistant", "message": text[:600]})
        return True, text
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        msg = f"[{name}] HTTP {e.code}: {detail or e.reason}"
        if on_event:
            on_event({"type": "error", "message": msg})
        return False, msg
    except Exception as e:
        msg = f"[{name}] 调用失败：{e}"
        if on_event:
            on_event({"type": "error", "message": msg})
        return False, msg


def call_backend(backend: dict, prompt: str, tag: str, timeout: int = 600,
                 model: str | None = None, on_event=None) -> tuple[bool, str]:
    """统一入口：按后端类型分发。codex_cli 走 orchestrator.call_codex。"""
    btype = (backend or {}).get("type")
    if btype == "codex_cli":
        import orchestrator as orch  # 延迟导入，避免循环
        return orch.call_codex(prompt, orch.ROOT, tag, timeout=timeout,
                               on_event=on_event, model=model)
    if btype == "openai":
        return call_openai(backend, prompt, timeout=timeout, on_event=on_event)
    return False, f"后端类型 {btype} 不支持文本直调"


def test_backend(name: str, cfg: dict | None = None) -> tuple[bool, str]:
    """连通性自检：发一句"回复OK"。"""
    cfg = cfg or load_config()
    b = cfg.get("backends", {}).get(name)
    if not b:
        return False, f"后端 {name} 不存在"
    if b.get("type") == "trae":
        executable = find_trae_executable()
        return (bool(executable),
                f"已找到 {executable}；通过本地文件接力，不会直接控制 IDE" if executable
                else "未找到 TRAE。可设置 TRAE_BIN 指向 TRAE 可执行文件。")
    if b.get("type") in ("codex_cli", "workbuddy"):
        import orchestrator as orch
        ok = orch.codex_available() if b["type"] == "codex_cli" else True
        return (ok, "可用" if ok else "Codex 冷却中/不可用")
    ok, msg = call_openai({**b, "name": name},
                          "只回复两个字：OK", timeout=30)
    return ok, (msg.strip()[:80] if ok else msg)
