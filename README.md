# 国内外 AI 协同工作台

**Domestic & Global AI Collaboration Workbench** is a local-first Windows desktop tool for coordinating AI agents. Route planning, prompt optimization, execution, and independent review to different backends, then follow real call and handoff status in one dashboard.

**国内外 AI 协同工作台**是一个本地优先的 Windows 桌面工具。它把任务拆分为评估、提示词优化、执行和独立验收，可按角色分配不同 AI 后端，并在工作台实时查看 Agent 的调用、接力等待与最近结果。

> This repository contains the app and source code. It does not include personal API keys, task history, local logs, or runtime state.

## Highlights

- **Multi-agent workflow:** separate assess, optimize, execute, and review roles; configure a backend for each role.
- **Backend choices:** Codex CLI and OpenAI-compatible APIs for text roles; Codex CLI, WorkBuddy, and TRAE for file-producing or handoff work.
- **Live participation monitor:** see which role is actually making a request, its task, model, elapsed time, recent outcome, and call count.
- **Honest handoff status:** TRAE and WorkBuddy file handoffs show as waiting; the dashboard does not claim to observe activity inside those external apps.
- **Prompt improvement loop:** freeze acceptance criteria, compare review scores across a bounded number of rounds, and retain the best prompt.
- **Independent review and traceability:** review outputs separately and inspect task transitions and public-safe process events.
- **Local-first operation:** the dashboard binds to `127.0.0.1`; API keys and workflow state stay on the local machine.
- **No web framework dependencies:** the Python service uses the standard library.

## Start on Windows

1. Install Python 3.10 or newer and make it available on `PATH`. The desktop launcher also checks `AI_RELAY_PYTHON` and common per-user installation paths.
2. Double-click `AI多Agent流水线.exe` in the repository root. It starts the local services and opens the dashboard at <http://127.0.0.1:8767>.
3. Open **多 Agent 分工** to choose role backends and configure OpenAI-compatible endpoint details. API keys are saved locally under `ai-relay/_state/agents.json`.
4. Configure and sign in to Codex CLI separately when using Codex. WorkBuddy and TRAE use task-file handoffs and may require you to complete the work in those applications.

The live monitor is available from **AI 状态**. It refreshes independently of the task list. “尚未参与” means no tracked call has occurred yet; it is not a backend connectivity test. Use the explicit backend test control for backends that support a connectivity check.

### Run from source

```powershell
cd ai-relay
python start_all.py --interval 45
```

This starts the local platform, the Codex-compatible proxy helper, and task polling. To start only the platform, run:

```powershell
python platform.py --port 8767
```

Alternatively, double-click `ai-relay/start.bat` to start all services. The dashboard and APIs are local-machine services, not a hosted web application.

## Build the Windows launcher

The checked-in executable is ready to launch the workbench. To rebuild it, install Python and PyInstaller, then run `build.ps1` in PowerShell:

```powershell
python -m pip install pyinstaller
./build.ps1
```

Set `AI_RELAY_BUILD_PYTHON` to the desired `python.exe` if Python is not on `PATH`.

## Workflow

```text
Task request
    -> Assess and select a model (optional)
    -> Optimize a structured prompt
    -> Execute with a selected agent
    -> Independently review the delivered files
    -> Keep the best-scoring prompt or stop on a non-improving round
```

Each task has a bounded call and round budget. TRAE and WorkBuddy can participate through generated instructions and delivery files. Their internal reasoning and live IDE state are not observable through this integration.

## WorkBuddy settings and restart recovery

Open **WorkBuddy 配置** in the top navigation (or `#wbPanel`) to change:

| Field | Default | Meaning |
| --- | --- | --- |
| `model` | *(empty)* | Model the WorkBuddy session should use. Empty means "let WorkBuddy decide". The value is written into every `_HANDOFF_WORKBUDDY_<id>.md`, so the model used for each handoff is auditable. |
| `poll_seconds` | `45` | How often the resident polling loop looks for delivered files (`orchestrator.py watch` uses this unless `--interval` is given). |
| `handoff_timeout_minutes` | `120` | How long a handoff may stay unanswered before the dashboard flags it as possibly stuck. It never auto-fails a task. |
| `auto_resume` | `true` | On startup, scan unfinished tasks and rebuild the WorkBuddy handoff queue. |

Values live in `ai-relay/_state/workbuddy.json` (plain JSON, safe to edit by hand while the
service is stopped). Writes go through a temporary file + `fsync` + atomic replace, a
`.bak` copy is kept, and a cross-process file lock serialises concurrent saves.

Failure handling is explicit rather than silent:

- **File missing** — defaults are used; the file is created on first read of the settings panel.
- **File corrupt** — it is copied to `workbuddy.corrupt-<timestamp>.json`, defaults are used,
  and the warning is shown in the panel.
- **Illegal value** — the save is rejected as a whole (nothing is written) and the API returns
  HTTP 400 with a per-field error; reading an illegal value falls back to the default and
  reports it.
- **Read/write failure** — reads degrade to defaults with a warning; writes fail with an
  actionable message instead of corrupting the file or crashing the service.

Restart recovery: `platform.py` calls `restore_unfinished()` on startup (unless
`auto_resume` is off). It re-scans `_state/state.json`, regenerates
`02_prompts/_PENDING_FOR_WORKBUDDY.md` plus each handoff instruction file, and reverts any
task that was mid-deliberation when the process died back to a resumable state, so it does
not sit on "in progress" forever. `POST /api/workbuddy/resume` re-runs the same routine on
demand; the panel shows the last result and the queue file's timestamp.

## Configuration and privacy

- Backend settings, API keys, and task state are created locally in `ai-relay/_state/`; this directory is excluded from Git.
- WorkBuddy settings (model, polling interval, handoff timeout, auto-resume) are stored separately in `ai-relay/_state/workbuddy.json` — see the section above.
- Input files, generated prompts, outputs, and reviews are kept in the local `ai-relay` workspace and are excluded from Git.
- The dashboard listens on loopback (`127.0.0.1`) and has no authentication layer. Do not change the bind address or expose the port to a network without adding appropriate access controls.
- The Codex CLI integration can run with elevated workspace permissions. Use a dedicated workspace, review generated code, and do not place sensitive material in the app workspace.
- The monitor records call metadata and concise public-facing events. It intentionally does not display private chain-of-thought.
- External AI providers receive prompts and files according to their own service terms. Configure only providers you trust with the data you submit.

## Tests

Run the Python test suite from the repository root:

```powershell
python -m unittest discover -s ai-relay/tests -v
```

The suite covers activity tracking, interrupted calls, handoff states, prompt improvement, event privacy, Codex recovery, TRAE file handoffs, and WorkBuddy settings persistence (missing/corrupt/illegal values, read-write failures, concurrent saves, restart recovery).

## Repository layout

| Path | Purpose |
| --- | --- |
| `AI多Agent流水线.exe` | Windows desktop launcher |
| `ai-relay/platform.html` | Local multi-agent workbench UI |
| `ai-relay/platform.py` | Local dashboard and HTTP API |
| `ai-relay/orchestrator.py` | Task state machine and agent workflow |
| `ai-relay/agents.py` | Backend configuration and adapters |
| `ai-relay/workbuddy_config.py` | WorkBuddy settings: validation, atomic persistence, recovery |
| `ai-relay/01_分工与协议/` | Agent role boundaries and collaboration protocol |
| `ai-relay/tests/` | Automated regression tests |

## Current limitations

- TRAE and WorkBuddy integrations are file handoffs, not headless control APIs.
- OpenAI-compatible backends must provide a compatible chat completions endpoint and valid model configuration.
- The workbench is designed for a single local user; it is not a multi-tenant or internet-hosted service.
- Usage, provider limits, and returned results depend on the configured AI services.

## 中文简介

这个项目把 AI 协作流程做成一个可在本机运行的工作台：先评估需求，再优化提示词，由选定的 Agent 执行，最后交给独立角色验收。Codex CLI 与兼容 OpenAI 的文本模型可参与自动调用；TRAE 和 WorkBuddy 通过文件接力参与。网页监视器只显示可验证的调用与交接状态，不会把“已配置”误报为“正在工作”。

首次使用请从 **多 Agent 分工** 设置后端。模型密钥、任务状态、输入和产物都保存在本机 `ai-relay` 目录中，并已排除在 Git 提交之外。仪表板仅绑定本机回环地址，不适合直接暴露到互联网。

WorkBuddy 的专属配置在 **WorkBuddy 配置**（顶部导航直达）：可指定它使用的模型、轮询间隔、接力超时，以及开关「启动时自动恢复未完成任务」。配置存在 `ai-relay/_state/workbuddy.json`，写入是原子替换并留备份；文件缺失、被改坏、值不合法、目录不可写都会给出明确提示而不是静默失效。平台每次启动都会重新扫描未完成任务、重建 WorkBuddy 待办队列，进程被杀时正在跑的博弈会回退为可继续状态，所以「上次没做完的任务」重启后能接着做。
