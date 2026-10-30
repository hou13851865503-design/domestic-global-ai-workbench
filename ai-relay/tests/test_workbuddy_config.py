#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 配置持久化：缺失 / 损坏 / 非法值 / 读写失败 / 并发 的回归测试。

这些用例对应真实会发生的场景，不是凑覆盖率：
- 第一次安装：没有配置文件
- 手改坏了 JSON、或写到一半断电
- 界面上填了非法模型名 / 越界间隔
- 目录只读、磁盘满、被占用
- 平台和编排器同时保存
"""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agents
import orchestrator as orch
import workbuddy_config as wbc

# 本项目的入口文件叫 platform.py，与标准库 platform 同名 ——
# `import platform` 拿到的是标准库那个，所以按路径显式加载。
import importlib.util

_spec = importlib.util.spec_from_file_location("ai_relay_platform", ROOT / "platform.py")
plat = importlib.util.module_from_spec(_spec)
sys.modules["ai_relay_platform"] = plat
_spec.loader.exec_module(plat)


class _PluginTestBase(unittest.TestCase):
    """把配置、状态、待办全部指向临时目录，测试不碰真实 _state。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("00_inbox", "02_prompts", "03_output", "04_review",
                     "_state", "_logs"):
            (self.root / name).mkdir()
        self.patches = [
            patch.object(wbc, "ROOT", self.root),
            patch.object(wbc, "CONFIG_FILE", self.root / "_state" / "workbuddy.json"),
            patch.object(wbc, "BACKUP_FILE", self.root / "_state" / "workbuddy.bak.json"),
            patch.object(wbc, "LOCK_FILE", self.root / "_state" / ".workbuddy.lock"),
            patch.object(agents, "AGENTS_FILE", self.root / "_state" / "agents.json"),
            patch.object(orch, "ROOT", self.root),
            patch.object(orch, "PROMPTS", self.root / "02_prompts"),
            patch.object(orch, "OUTPUT", self.root / "03_output"),
            patch.object(orch, "REVIEW", self.root / "04_review"),
            patch.object(orch, "STATE", self.root / "_state"),
            patch.object(orch, "LOGS", self.root / "_logs"),
            patch.object(orch, "PROMPT_VERSIONS", self.root / "_state" / "prompt_versions"),
            patch.object(orch, "EVALUATIONS", self.root / "_state" / "evaluations"),
            patch.object(orch, "PENDING_FILE",
                         self.root / "02_prompts" / "_PENDING_FOR_WORKBUDDY.md"),
            patch.object(orch, "PENDING_TRAE_FILE",
                         self.root / "02_prompts" / "_PENDING_FOR_TRAE.md"),
            patch.object(orch, "AGENT_ACTIVITY_FILE", self.root / "_state" / "agent_activity.json"),
            patch.object(orch, "AGENT_ACTIVITY_LOCK_FILE", self.root / "_state" / ".activity.lock"),
            patch.object(orch, "STATE_FILE", self.root / "_state" / "state.json"),
            patch.object(orch, "LOCK_FILE", self.root / "_state" / ".lock"),
        ]
        for item in self.patches:
            item.start()
        self.file = wbc.CONFIG_FILE

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()


class WorkbuddyConfigTests(_PluginTestBase):
    """配置模块本身的可靠性。"""

    # ---------------- 缺失 ----------------
    def test_missing_file_returns_defaults_without_side_effect(self):
        cfg, warnings = wbc.load()
        self.assertEqual(cfg, dict(wbc.DEFAULTS))
        self.assertEqual(warnings, [])
        self.assertFalse(self.file.exists(), "只读取不应该创建文件")

    def test_ensure_file_creates_editable_default(self):
        cfg, warnings = wbc.ensure_file()
        self.assertTrue(self.file.exists())
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8")), cfg)
        self.assertEqual(warnings, [])

    # ---------------- 损坏 ----------------
    def test_corrupt_json_backed_up_and_falls_back(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text('{"model": "x", 坏掉了', encoding="utf-8")
        cfg, warnings = wbc.load()
        self.assertEqual(cfg, dict(wbc.DEFAULTS))
        self.assertTrue(any("不是合法 JSON" in w for w in warnings))
        backups = list(self.file.parent.glob("workbuddy.corrupt-*.json"))
        self.assertEqual(len(backups), 1, "损坏的文件必须留档，不能直接冲掉")
        self.assertIn("坏掉了", backups[0].read_text(encoding="utf-8"))

    def test_empty_file_falls_back(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text("   \n", encoding="utf-8")
        cfg, warnings = wbc.load()
        self.assertEqual(cfg, dict(wbc.DEFAULTS))
        self.assertTrue(any("空的" in w for w in warnings))

    def test_non_object_json_falls_back(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text("[1, 2, 3]", encoding="utf-8")
        cfg, warnings = wbc.load()
        self.assertEqual(cfg, dict(wbc.DEFAULTS))
        self.assertTrue(any("顶层不是对象" in w for w in warnings))

    # ---------------- 非法值 ----------------
    def test_illegal_values_rejected_and_nothing_written(self):
        wbc.save({"model": "ok-model", "poll_seconds": 60})
        before = self.file.read_text(encoding="utf-8")
        with self.assertRaises(wbc.ConfigError) as ctx:
            wbc.save({"model": "bad<script>alert(1)</script>"})
        fields = [e["field"] for e in ctx.exception.errors]
        self.assertEqual(fields, ["model"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), before,
                         "校验失败时一个字节都不能写")

    def test_int_range_and_type(self):
        for bad in (1, 99999, "abc", "", True, 3.5):
            with self.assertRaises(wbc.ConfigError, msg=f"{bad!r} 应被拒绝"):
                wbc.save({"poll_seconds": bad})
        self.assertEqual(wbc.save({"poll_seconds": "90"})["poll_seconds"], 90)

    def test_bool_coercion(self):
        self.assertTrue(wbc.save({"auto_resume": "是"})["auto_resume"])
        self.assertFalse(wbc.save({"auto_resume": "off"})["auto_resume"])
        self.assertTrue(wbc.save({"auto_resume": 1})["auto_resume"])
        with self.assertRaises(wbc.ConfigError):
            wbc.save({"auto_resume": "大概吧"})

    def test_model_empty_is_allowed_means_unspecified(self):
        self.assertEqual(wbc.save({"model": "  "})["model"], "")
        self.assertEqual(wbc.save({"model": "deepseek-v3.2"})["model"], "deepseek-v3.2")
        self.assertEqual(wbc.save({"model": "  多  空格  "})["model"], "多 空格")

    def test_partial_update_keeps_other_fields(self):
        wbc.save({"model": "m1", "poll_seconds": 30})
        wbc.save({"handoff_timeout_minutes": 30})
        cfg = wbc.load()[0]
        self.assertEqual(cfg["model"], "m1")
        self.assertEqual(cfg["poll_seconds"], 30)
        self.assertEqual(cfg["handoff_timeout_minutes"], 30)

    # ---------------- 读写失败 ----------------
    def test_write_failure_raises_clean_error(self):
        with patch.object(wbc.os, "replace", side_effect=OSError("磁盘已满")):
            with self.assertRaises(wbc.ConfigError) as ctx:
                wbc.save({"model": "m"})
        self.assertIn("磁盘已满", str(ctx.exception))
        # 失败的写入不能留下半个文件或临时垃圾
        self.assertFalse(self.file.exists())
        self.assertEqual(list(self.file.parent.glob(".workbuddy-*.tmp")), [])

    def test_read_permission_error_falls_back(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text("{}", encoding="utf-8")
        # 注意：load() 现在走 read_bytes() 再自己解码（为了兼容 GBK/BOM），
        # 所以要拦 read_bytes 而不是 read_text，否则补丁打空、测试假通过。
        with patch.object(Path, "read_bytes", side_effect=OSError("拒绝访问")):
            cfg, warnings = wbc.load()
        self.assertEqual(cfg, dict(wbc.DEFAULTS))
        self.assertTrue(any("读不出来" in w for w in warnings))

    # ---------------- 备份与并发 ----------------
    def test_backup_written_on_second_save(self):
        wbc.save({"model": "first"})
        wbc.save({"model": "second"})
        self.assertTrue(wbc.BACKUP_FILE.exists())
        self.assertEqual(json.loads(wbc.BACKUP_FILE.read_text(encoding="utf-8"))["model"],
                         "first")

    def test_concurrent_saves_keep_file_valid(self):
        wbc.save({"model": "base", "poll_seconds": 45})
        errors = []

        def worker(field, value):
            try:
                wbc.save({field: value})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=("poll_seconds", 120)),
                   threading.Thread(target=worker,
                                    args=("handoff_timeout_minutes", 240)),
                   threading.Thread(target=worker, args=("model", "concurrent-model"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        cfg = json.loads(self.file.read_text(encoding="utf-8"))  # 必须是完整 JSON
        self.assertEqual(cfg["model"], "concurrent-model")
        self.assertEqual(cfg["poll_seconds"], 120)
        self.assertEqual(cfg["handoff_timeout_minutes"], 240)

    def test_unknown_fields_are_reported_not_crashing(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"model": "m", "旧字段": 1}), encoding="utf-8")
        cfg, warnings = wbc.load()
        self.assertEqual(cfg["model"], "m")
        self.assertTrue(any("忽略未知字段" in w for w in warnings))

    # ---------------- 编码（用户用记事本改过配置的两种典型情况）----------------
    def test_gbk_encoded_file_is_read_not_crashing(self):
        """记事本"另存为 ANSI"= GBK。原来 read_text(utf-8) 抛 UnicodeDecodeError，
        既不是 OSError 也不是 JSONDecodeError，会一路冒到接口层变 500。
        现在应当**正常读出**用户配置（而不是静默丢掉回退默认值）。"""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_bytes('{"model": "深度求索"}'.encode("gbk"))
        cfg, warnings = wbc.load()          # 不允许抛异常
        self.assertEqual(cfg["model"], "深度求索", "GBK 文件里的配置应被正确读出")
        self.assertEqual(warnings, [], "能读出来就不该报告警")

    def test_utf8_bom_file_parses_normally(self):
        """记事本"UTF-8 带 BOM"：BOM 必须被吞掉，而不是当成坏 JSON 丢掉用户配置。"""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"model": "bom-model"}, ensure_ascii=False)
        self.file.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        cfg, warnings = wbc.load()
        self.assertEqual(cfg["model"], "bom-model")
        self.assertEqual(warnings, [])

    def test_binary_garbage_falls_back(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_bytes(bytes(range(256)))
        cfg, warnings = wbc.load()
        self.assertEqual(cfg, dict(wbc.DEFAULTS))
        self.assertTrue(any("编码" in w or "不是合法 JSON" in w for w in warnings))

    # ---------------- 数值边界 ----------------
    def test_infinite_and_nan_rejected_not_crashing(self):
        """JSON 里 1e400 合法，解析成 inf；int(inf) 会抛 OverflowError 打成 500。"""
        for raw in (float("inf"), float("-inf"), float("nan"), 1e400):
            with self.assertRaises(wbc.ConfigError, msg=f"{raw!r} 应被拒绝"):
                wbc.save({"poll_seconds": raw})
        # 直接走校验函数也不能抛非 ConfigError 的异常
        _, errors = wbc.validate({"handoff_timeout_minutes": float("inf")}, partial=True)
        self.assertEqual(errors[0]["field"], "handoff_timeout_minutes")

    # ---------------- 其它 ----------------
    def test_log_creates_missing_logs_dir(self):
        """新克隆的目录里 _logs 不存在：既不能抛异常，也要能自己把目录补上。

        早先的实现直接 open(LOGS / "日期.log")，FileNotFoundError 会从
        log() 里逃出去，把一个本该成功的 HTTP 请求打成 500。现在 log()
        会先 mkdir(parents=True)，所以这里断言的是"不抛 + 目录被建出来"。
        """
        missing = self.root / "不存在的日志目录" / "更深的子目录"
        with patch.object(orch, "LOGS", missing):
            orch.log("测试")                # 不允许抛异常
        self.assertTrue(missing.is_dir(), "log() 应自愈创建日志目录")
        written = list(missing.glob("*.log"))
        self.assertEqual(len(written), 1, "应写入当天的一个日志文件")

    def test_log_never_raises_when_logs_dir_unwritable(self):
        """日志目录没法创建（路径被同名文件占住）时，同样不能把业务打死。"""
        blocked = self.root / "被文件占住的日志目录"
        blocked.write_text("我是个文件，不是目录", encoding="utf-8")
        with patch.object(orch, "LOGS", blocked / "子目录"):
            orch.log("测试")                # 不允许抛异常，静默放弃写盘
        self.assertTrue(blocked.is_file(), "不能把同名文件删掉换成目录")


class WorkbuddyIntegrationTests(_PluginTestBase):
    """配置接入到流水线后是否真的生效。"""

    def test_agents_deep_merge_keeps_backend_type(self):
        """只存了 model 的后端条目不能把 type/label 冲掉。"""
        agents.AGENTS_FILE.write_text(json.dumps(
            {"backends": {"deepseek": {"model": "deepseek-reasoner"}}}), encoding="utf-8")
        cfg = agents.load_config()
        self.assertEqual(cfg["backends"]["deepseek"]["model"], "deepseek-reasoner")
        self.assertEqual(cfg["backends"]["deepseek"]["type"], "openai")
        self.assertEqual(cfg["backends"]["deepseek"]["base_url"],
                         agents.DEFAULT_CONFIG["backends"]["deepseek"]["base_url"])

    def test_agents_config_exposes_workbuddy_model(self):
        wbc.save({"model": "gpt-5.1-codex"})
        cfg = agents.load_config()
        self.assertEqual(cfg["backends"]["workbuddy"]["model"], "gpt-5.1-codex")

    def test_handoff_contains_configured_model(self):
        wbc.save({"model": "deepseek-v3.2"})
        task = {"id": "T-1", "requirement": "做个东西", "round": 1}
        path = orch._write_handoff(task, orch.WAITING_EXECUTE, "workbuddy")
        text = path.read_text(encoding="utf-8")
        self.assertIn("deepseek-v3.2", text)

    def test_handoff_without_model_says_unspecified(self):
        wbc.save({"model": ""})
        task = {"id": "T-2", "requirement": "做个东西", "round": 1}
        text = orch._write_handoff(task, orch.WAITING_EXECUTE, "workbuddy").read_text(
            encoding="utf-8")
        self.assertIn("未指定", text)

    def test_restore_unfinished_rebuilds_queue(self):
        state = {"seq": 2, "tasks": {
            "T-A": {"id": "T-A", "status": orch.WAITING_EXECUTE, "requirement": "A",
                    "waiting_agent": "workbuddy", "round": 1},
            "T-B": {"id": "T-B", "status": orch.PASS, "requirement": "B"},
            "T-C": {"id": "T-C", "status": orch.NEW, "requirement": "C"},
        }}
        orch.save_state(state)
        summary = orch.restore_unfinished()
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["unfinished"], 2)
        self.assertEqual(summary["waiting_workbuddy"], 1)
        self.assertIn(orch.WAITING_EXECUTE, summary["by_status"])
        self.assertTrue(orch.PENDING_FILE.exists())
        self.assertIn("T-A", orch.PENDING_FILE.read_text(encoding="utf-8"))

    def test_restore_unfinished_survives_broken_state(self):
        orch.STATE_FILE.write_text("{不是 json", encoding="utf-8")
        summary = orch.restore_unfinished()
        self.assertTrue(summary["ok"])          # 坏状态文件不能让恢复崩掉
        self.assertEqual(summary["unfinished"], 0)

    def test_restore_reverts_interrupted_deliberation(self):
        """进程被杀时正在跑的博弈线程没了，状态不能永远停在 DELIBERATING。"""
        state = {"seq": 1, "tasks": {
            "T-D": {"id": "T-D", "status": orch.DELIBERATING, "requirement": "D"}}}
        orch.save_state(state)
        summary = orch.restore_unfinished()
        self.assertEqual(summary["reverted"], 1)
        self.assertEqual(summary["by_status"], {orch.DELIB_PAUSED: 1})
        saved = orch.load_state()["tasks"]["T-D"]
        self.assertEqual(saved["status"], orch.DELIB_PAUSED)
        self.assertIn("重启", saved["resume_note"])

    def test_platform_save_endpoint_contract(self):
        status, body = plat.workbuddy_save({"model": "api-model", "poll_seconds": 30})
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["model"], "api-model")
        status, body = plat.workbuddy_save({"poll_seconds": -1})
        self.assertEqual(status, 400)
        self.assertEqual(body["errors"][0]["field"], "poll_seconds")
        self.assertEqual(wbc.load()[0]["model"], "api-model", "失败请求不能改动已存配置")

    def test_platform_view_reports_file_and_warnings(self):
        wbc.save({"model": "m"})
        view = plat.workbuddy_view()
        self.assertTrue(view["ok"])
        self.assertEqual(view["config"]["model"], "m")
        self.assertTrue(view["file"]["exists"])
        self.assertIn("workbuddy.json", view["file"]["path"])
        for name in wbc.FIELD_SPECS:
            self.assertIn(name, view["fields"])


if __name__ == "__main__":
    unittest.main()
