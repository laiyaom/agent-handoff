"""Task state machine and step execution.

State model (task.json):
  status: ready -> running -> ready ... -> awaiting_approval -> done
                                      \\-> blocked (human must act)
  phase:  the NEXT step to execute: develop -> review -> (fix -> review)* -> ...

A step is committed by a single atomic save of task.json at its end. A crash
mid-step leaves status=running; the next run re-executes only that step.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import gitutil, prompts
from .adapters import AgentRequest, build_adapter
from .store import HandoffError, Store, now

HUMAN = "human"


def create_task(store: Store, spec: dict) -> dict:
    cfg = store.config()
    if not spec.get("title") or not spec.get("goal"):
        raise HandoffError("task needs at least a title and a goal")
    roles = {**cfg["roles"], **spec.get("roles", {})}
    for agent in roles.values():
        if agent not in cfg["agents"]:
            raise HandoffError(f"role agent {agent!r} is not defined in config.agents")
    task = {
        "task_id": spec.get("task_id") or store.new_task_id(spec["title"]),
        "title": spec["title"],
        "goal": spec["goal"],
        "acceptance_criteria": list(spec.get("acceptance_criteria", [])),
        "files": list(spec.get("files", [])),
        "test_command": spec.get("test_command", ""),
        "roles": roles,
        "max_fix_rounds": int(spec.get("max_fix_rounds", cfg["max_fix_rounds"])),
        "status": "ready",
        "phase": "develop",
        "current_agent": None,
        "next_agent": roles["coder"],
        "base_commit": gitutil.head(store.repo),
        "retry_count": 0,       # automatic NEED_FIX -> fix loops used
        "step_attempts": 0,     # failed attempts of the current step
        "last_verdict": None,
        "open_issues": [],
        "changed_files": [],
        "test_results": None,
        "results": [],          # per-step agent summaries
        "rounds": [],
        "blocked_reason": None,
        "created_at": now(),
    }
    if store.task_dir(task["task_id"]).exists():
        raise HandoffError(f"task {task['task_id']!r} already exists")
    store.save_task(task)
    store.event(task["task_id"], "created", status="ready", phase="develop")
    return task


def _transition(store: Store, task: dict, status: str, **fields) -> None:
    old = (task["status"], task["phase"])
    task["status"] = status
    task.update(fields)
    store.save_task(task)
    store.event(task["task_id"], "status", frm=list(old), to=[task["status"], task["phase"]],
                **{k: v for k, v in fields.items() if k in ("blocked_reason", "next_agent")})


def _run_tests(repo: Path, command: str, timeout: int, log: Path) -> dict:
    if not command:
        return {"status": "skipped", "command": "", "exit_code": None, "output_tail": ""}
    started = now()
    try:
        proc = subprocess.run(command, shell=True, cwd=repo, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        code, out = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        code, out = 124, f"{exc.stdout or ''}{exc.stderr or ''}\n[timed out after {timeout}s]"
    log.write_text(f"$ {command}\n# started {started} exit={code}\n\n{out}", encoding="utf-8")
    return {"status": "passed" if code == 0 else "failed", "command": command,
            "exit_code": code, "output_tail": out[-4000:], "finished_at": now()}


def run_task(store: Store, task_id: str, max_steps: int | None = None) -> dict:
    """Advance a task until it needs a human, is paused, or is done."""
    cfg = store.config()
    steps = 0
    with store.lock():
        while True:
            task = store.load_task(task_id)  # re-read: humans may edit between steps
            if task["status"] == "running":
                store.event(task_id, "recovered", phase=task["phase"],
                            note="previous run stopped mid-step; re-running that step only")
                task["interrupted"] = True
                _transition(store, task, "ready")
            if store.pause_file(task_id).exists():
                store.event(task_id, "paused_at_boundary", phase=task["phase"])
                return task
            if task["status"] != "ready" or (max_steps is not None and steps >= max_steps):
                return task
            _step(store, cfg, task)
            steps += 1


def _step(store: Store, cfg: dict, task: dict) -> None:
    repo, task_id, phase = store.repo, task["task_id"], task["phase"]
    reviewing = phase == "review"
    agent = task["roles"]["reviewer" if reviewing else "coder"]
    adapter = build_adapter(agent, cfg)

    round_no = len(task["rounds"]) + 1
    rdir = store.task_dir(task_id) / "rounds" / f"{round_no:02d}-{phase}-{agent}"
    if rdir.exists():  # leftovers of an interrupted attempt: keep them for the audit trail
        rdir.rename(rdir.with_name(f"{rdir.name}-interrupted-{len(list(rdir.parent.glob(rdir.name + '-interrupted-*'))) + 1}"))
    rdir.mkdir(parents=True)

    diff_before = gitutil.diff(repo, task["base_commit"])
    fp_before = gitutil.fingerprint(repo)
    handoff_path = store.task_dir(task_id) / "handoff.md"
    prompt = prompts.build_prompt(
        task, phase, diff_before,
        handoff_path.read_text(encoding="utf-8") if handoff_path.exists() else "",
        store.notes(task_id), cfg["max_diff_chars"])
    (rdir / "prompt.md").write_text(prompt, encoding="utf-8")
    (rdir / "diff_before.patch").write_text(diff_before, encoding="utf-8")

    started = now()
    _transition(store, task, "running", current_agent=agent)
    store.event(task_id, "step_started", round=round_no, phase=phase, agent=agent)

    spec = cfg["agents"][agent]
    result = adapter.run(AgentRequest(
        task_id=task_id, phase=phase, mode="read" if reviewing else "write", prompt=prompt,
        workdir=repo, round_dir=rdir, timeout=int(spec.get("timeout", 1800))))

    (rdir / "output.md").write_text(result.output, encoding="utf-8")
    (rdir / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (rdir / "stderr.log").write_text(result.stderr, encoding="utf-8")
    diff_after = gitutil.diff(repo, task["base_commit"])
    (rdir / "diff.patch").write_text(diff_after, encoding="utf-8")

    record = {"round": round_no, "phase": phase, "agent": agent, "started_at": started,
              "finished_at": now(), "duration_s": result.duration_s,
              "exit_code": result.exit_code, "timed_out": result.timed_out,
              "dir": str(rdir.relative_to(store.root)), "verdict": None}
    task.pop("interrupted", None)

    def finish(status: str, **fields) -> None:
        (rdir / "meta.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        task["rounds"].append(record)
        task["changed_files"] = gitutil.changed_files(repo, task["base_commit"])
        _transition(store, task, status, **fields)
        _write_handoff(store, task, record, result.output)
        store.event(task_id, "step_finished", round=round_no, phase=phase, agent=agent,
                    exit_code=result.exit_code, verdict=record["verdict"], status=status)

    # 1. Agent failure: bounded retry of the same step, then a human decides.
    if result.exit_code != 0:
        task["step_attempts"] += 1
        if task["step_attempts"] > cfg["max_step_retries"]:
            return finish("blocked", next_agent=HUMAN, blocked_reason=(
                f"{agent} failed {task['step_attempts']}x in {phase} (exit {result.exit_code}); "
                f"see {record['dir']}/stderr.log"))
        return finish("ready", next_agent=agent)
    task["step_attempts"] = 0
    task["results"].append({"round": round_no, "agent": agent, "phase": phase,
                            "summary": result.output.strip()[-2000:]})

    # 2. Write step: orchestrator runs the tests itself, then hands off to review.
    if not reviewing:
        tests = _run_tests(repo, task.get("test_command", ""), int(cfg["test_timeout"]),
                           rdir / "test.log")
        task["test_results"] = record["tests"] = tests
        return finish("ready", phase="review", next_agent=task["roles"]["reviewer"])

    # 3. Review step: must not have touched the workspace.
    if gitutil.fingerprint(repo) != fp_before:
        return finish("blocked", next_agent=HUMAN, blocked_reason=(
            f"read-only reviewer {agent} modified the workspace; inspect `git status` "
            f"and {record['dir']}/diff.patch, then fix and `handoff resume`"))

    verdict = prompts.parse_verdict(result.output)
    if verdict is None:
        return finish("blocked", next_agent=HUMAN, blocked_reason=(
            f"could not parse a PASS/NEED_FIX JSON verdict from {record['dir']}/output.md"))
    tests = task.get("test_results") or {}
    if verdict["verdict"] == "PASS" and tests.get("status") == "failed":
        verdict["verdict"] = "NEED_FIX"
        verdict["issues"].append({"severity": "high", "file": None, "source": "orchestrator",
                                  "description": f"test command failed: {tests.get('command')}"})
    record["verdict"] = task["last_verdict"] = verdict["verdict"]
    record["review"] = verdict

    if verdict["verdict"] == "PASS":
        task["open_issues"] = []
        if cfg["require_human_approval"]:
            return finish("awaiting_approval", next_agent=HUMAN)
        return finish("done", next_agent=None)

    task["open_issues"] = verdict["issues"] or [
        {"severity": "medium", "file": None, "description": verdict["summary"] or "see review"}]
    if task["retry_count"] >= task["max_fix_rounds"]:
        return finish("blocked", phase="fix", next_agent=HUMAN, blocked_reason=(
            f"still NEED_FIX after {task['retry_count']} automatic fix rounds; "
            "approve --force, reject with guidance, or edit the task and resume"))
    task["retry_count"] += 1
    return finish("ready", phase="fix", next_agent=task["roles"]["coder"])


def _write_handoff(store: Store, task: dict, record: dict, output: str) -> None:
    tests = task.get("test_results") or {}
    issues = "\n".join(f"- [{i.get('severity', '?')}] {i.get('file') or '-'}: {i.get('description', '')}"
                       for i in task.get("open_issues", [])) or "- (none)"
    text = f"""# Handoff after round {record['round']:02d}

- From: `{record['agent']}` ({record['phase']}, exit {record['exit_code']})
- To: `{task.get('next_agent') or '-'}` (next phase: `{task['phase']}`, status: `{task['status']}`)
- Verdict: `{record.get('verdict') or '-'}`
- Tests: `{tests.get('status', 'not run')}`
- Changed files vs base: {', '.join(f'`{f}`' for f in task['changed_files']) or '(none)'}
- Artifacts: `.agent-handoff/{record['dir']}/`

## Open issues

{issues}

## Message from {record['agent']}

{output.strip()[-6000:] or '(empty)'}
"""
    (store.task_dir(task["task_id"]) / "handoff.md").write_text(text, encoding="utf-8")


# -- human controls ---------------------------------------------------------

def _assert_idle(task: dict) -> None:
    if task["status"] == "running":
        raise HandoffError("task is running; `handoff pause` it and wait for the step to finish")


def approve(store: Store, task_id: str, force: bool = False) -> dict:
    task = store.load_task(task_id)
    _assert_idle(task)
    if task["status"] != "awaiting_approval" and not (force and task["status"] == "blocked"):
        raise HandoffError(f"cannot approve from status {task['status']!r}"
                           + (" (use --force to accept a blocked task)" if task["status"] == "blocked" else ""))
    store.event(task_id, "human_approve", forced=force)
    _transition(store, task, "done", next_agent=None, blocked_reason=None)
    return task


def reject(store: Store, task_id: str, reason: str) -> dict:
    task = store.load_task(task_id)
    _assert_idle(task)
    if task["status"] not in ("awaiting_approval", "blocked", "done"):
        raise HandoffError(f"cannot reject from status {task['status']!r}")
    task["open_issues"] = [*task.get("open_issues", []),
                           {"severity": "high", "file": None, "source": "human", "description": reason}]
    task["retry_count"] = 0  # a human rejection grants a fresh automatic fix budget
    store.event(task_id, "human_reject", reason=reason)
    _transition(store, task, "ready", phase="fix", next_agent=task["roles"]["coder"],
                blocked_reason=None, last_verdict="NEED_FIX")
    return task


def resume(store: Store, task_id: str) -> dict:
    task = store.load_task(task_id)
    store.pause_file(task_id).unlink(missing_ok=True)
    store.event(task_id, "resumed", status=task["status"])
    if task["status"] == "blocked":
        task["step_attempts"] = 0
        _transition(store, task, "ready", next_agent=task["roles"][
            "reviewer" if task["phase"] == "review" else "coder"], blocked_reason=None)
    return task


def pause(store: Store, task_id: str) -> dict:
    task = store.load_task(task_id)
    store.pause_file(task_id).write_text(now() + "\n")
    store.event(task_id, "pause_requested", status=task["status"])
    return task
