"""On-disk layout of .agent-handoff/: config, tasks, events, lock."""

from __future__ import annotations

import copy
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import gitutil

DIR_NAME = ".agent-handoff"

STATUSES = ("ready", "running", "awaiting_approval", "blocked", "done")
PHASES = ("develop", "review", "fix")

DEFAULT_CONFIG: dict = {
    "roles": {"coder": "codex", "reviewer": "claude"},
    "max_fix_rounds": 3,
    "max_step_retries": 1,
    "require_human_approval": True,
    "max_diff_chars": 60000,
    "test_timeout": 900,
    "agents": {
        "codex": {"adapter": "codex_cli", "command": "codex", "extra_args": [], "timeout": 1800},
        "claude": {"adapter": "claude_cli", "command": "claude", "extra_args": [], "timeout": 1800},
    },
}


class HandoffError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Store:
    def __init__(self, repo: Path):
        self.repo = repo
        self.root = repo / DIR_NAME
        self.tasks_dir = self.root / "tasks"

    # -- setup -------------------------------------------------------------
    def init(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        cfg = self.root / "config.json"
        if not cfg.exists():
            _write_atomic(cfg, json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        # Keep orchestrator state out of agent diffs without touching tracked files.
        exclude = gitutil.exclude_path(self.repo)
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        if f"/{DIR_NAME}/" not in lines:
            with exclude.open("a") as fh:
                fh.write(f"\n/{DIR_NAME}/\n")

    def require_init(self) -> None:
        if not (self.root / "config.json").exists():
            raise HandoffError(f"{self.root} not initialised; run `handoff init` first")

    def config(self) -> dict:
        self.require_init()
        user = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        return _merge(DEFAULT_CONFIG, user)

    # -- tasks -------------------------------------------------------------
    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id

    def new_task_id(self, title: str) -> str:
        base = f"{datetime.now():%Y%m%d}-{_slug(title)}"
        task_id, n = base, 2
        while self.task_dir(task_id).exists():
            task_id, n = f"{base}-{n}", n + 1
        return task_id

    def list_tasks(self) -> list[dict]:
        if not self.tasks_dir.exists():
            return []
        return [self.load_task(p.name) for p in sorted(self.tasks_dir.iterdir())
                if (p / "task.json").exists()]

    def load_task(self, task_id: str) -> dict:
        path = self.task_dir(task_id) / "task.json"
        if not path.exists():
            raise HandoffError(f"unknown task {task_id!r}")
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HandoffError(f"{path} is not valid JSON: {exc}") from exc
        validate_task(task)
        return task

    def save_task(self, task: dict) -> None:
        validate_task(task)
        task["updated_at"] = now()
        tdir = self.task_dir(task["task_id"])
        tdir.mkdir(parents=True, exist_ok=True)
        _write_atomic(tdir / "task.json", json.dumps(task, indent=2, ensure_ascii=False) + "\n")
        _write_atomic(tdir / "task.md", render_task_md(task))

    def event(self, task_id: str, kind: str, **data) -> None:
        line = json.dumps({"time": now(), "event": kind, **data}, ensure_ascii=False)
        with (self.task_dir(task_id) / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def events(self, task_id: str) -> list[dict]:
        path = self.task_dir(task_id) / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]

    # -- human control files (separate from task.json to avoid write races) --
    def pause_file(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "PAUSE"

    def notes(self, task_id: str) -> str:
        path = self.task_dir(task_id) / "notes.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def add_note(self, task_id: str, text: str) -> None:
        with (self.task_dir(task_id) / "notes.md").open("a", encoding="utf-8") as fh:
            fh.write(f"- [{now()}] {text}\n")

    # -- single-writer lock ------------------------------------------------
    @contextmanager
    def lock(self):
        """One orchestrator at a time per repository: agent writes stay serial."""
        path = self.root / "lock"
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if not self._lock_is_stale(path):
                    raise HandoffError(
                        f"another orchestrator holds {path}; if it is dead run `handoff unlock`")
                path.unlink(missing_ok=True)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "started": now()}))
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _lock_is_stale(path: Path) -> bool:
        try:
            pid = json.loads(path.read_text())["pid"]
        except (OSError, ValueError, KeyError):
            # Half-written lock: treat as live briefly, then stale.
            return time.time() - path.stat().st_mtime > 5
        if os.name == "nt":
            return False  # os.kill(pid, 0) terminates processes on Windows; unlock manually.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def unlock(self) -> bool:
        path = self.root / "lock"
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed


def validate_task(task: dict) -> None:
    required = ("task_id", "title", "goal", "status", "phase", "roles", "base_commit")
    missing = [k for k in required if k not in task]
    if missing:
        raise HandoffError(f"task missing fields: {', '.join(missing)}")
    if task["status"] not in STATUSES:
        raise HandoffError(f"invalid status {task['status']!r}; expected one of {STATUSES}")
    if task["phase"] not in PHASES:
        raise HandoffError(f"invalid phase {task['phase']!r}; expected one of {PHASES}")
    for role in ("coder", "reviewer"):
        if not task["roles"].get(role):
            raise HandoffError(f"task roles.{role} is not set")


def render_task_md(task: dict) -> str:
    def bullets(items) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "- (none)"

    issues = [f"[{i.get('severity', '?')}] {i.get('file') or '-'}: {i.get('description', '')}"
              for i in task.get("open_issues", [])]
    rounds = [f"{r['round']:02d} {r['phase']:<7} {r['agent']:<10} exit={r['exit_code']} "
              f"tests={r.get('tests', {}).get('status', '-')} verdict={r.get('verdict') or '-'}"
              for r in task.get("rounds", [])]
    tests = task.get("test_results") or {}
    return f"""# {task['title']}

| Field | Value |
| --- | --- |
| Task ID | `{task['task_id']}` |
| Status | `{task['status']}` |
| Next phase | `{task['phase']}` |
| Current agent | `{task.get('current_agent') or '-'}` |
| Next agent | `{task.get('next_agent') or '-'}` |
| Last verdict | `{task.get('last_verdict') or '-'}` |
| Fix rounds used | {task.get('retry_count', 0)} / {task.get('max_fix_rounds')} |
| Base commit | `{task['base_commit'][:12]}` |
| Blocked reason | {task.get('blocked_reason') or '-'} |

## Goal

{task['goal']}

## Acceptance criteria

{bullets(task.get('acceptance_criteria'))}

## Files in scope

{bullets(task.get('files'))}

## Files changed

{bullets(task.get('changed_files'))}

## Latest test result

`{tests.get('status', 'not run')}` — `{tests.get('command') or '-'}`

## Open issues

{bullets(issues)}

## Rounds

```
{chr(10).join(rounds) or '(none)'}
```
"""
