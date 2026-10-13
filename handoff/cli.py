"""Command line: init, start, status, pause, resume, approve, reject, note, log, unlock."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import gitutil, orchestrator
from .store import HandoffError, Store


def _store(args) -> Store:
    return Store(gitutil.toplevel(Path(args.repo).resolve()))


def _print_task(store: Store, task: dict) -> None:
    paused = " (pause requested)" if store.pause_file(task["task_id"]).exists() else ""
    phase = f" next_phase={task['phase']}" if task["status"] in ("ready", "running", "blocked") else ""
    print(f"{task['task_id']}: status={task['status']}{paused}{phase} "
          f"next_agent={task.get('next_agent') or '-'} verdict={task.get('last_verdict') or '-'} "
          f"fix_rounds={task.get('retry_count', 0)}/{task.get('max_fix_rounds')}")
    if task.get("blocked_reason"):
        print(f"  blocked: {task['blocked_reason']}")
    for issue in task.get("open_issues", []):
        print(f"  issue [{issue.get('severity', '?')}] {issue.get('file') or '-'}: {issue.get('description')}")
    hints = {
        "awaiting_approval": "approve with `handoff approve {id}` or `handoff reject {id} --reason ...`",
        "blocked": "edit the task or add `handoff note`, then `handoff resume {id}` "
                   "(or `approve --force` / `reject`)",
        "ready": "continue with `handoff resume {id}`",
    }
    if task["status"] in hints:
        print("  next: " + hints[task["status"]].format(id=task["task_id"]))


def cmd_init(args) -> None:
    store = _store(args)
    store.init()
    print(f"initialised {store.root} (config: {store.root / 'config.json'})")


def cmd_start(args) -> None:
    store = _store(args)
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8")) if args.spec else {}
    for key in ("title", "goal", "test_command"):
        if getattr(args, key):
            spec[key] = getattr(args, key)
    if args.acceptance:
        spec["acceptance_criteria"] = args.acceptance
    if args.file:
        spec["files"] = args.file
    for flag, role in ((args.coder, "coder"), (args.reviewer, "reviewer")):
        if flag:
            spec.setdefault("roles", {})[role] = flag
    task = orchestrator.create_task(store, spec)
    print(f"created {task['task_id']} in {store.task_dir(task['task_id'])}")
    if not args.no_run:
        task = orchestrator.run_task(store, task["task_id"], args.max_steps)
    _print_task(store, task)


def cmd_status(args) -> None:
    store = _store(args)
    store.require_init()
    tasks = [store.load_task(args.task_id)] if args.task_id else store.list_tasks()
    if not tasks:
        print("no tasks")
    for task in tasks:
        _print_task(store, task)


def cmd_resume(args) -> None:
    store = _store(args)
    orchestrator.resume(store, args.task_id)
    _print_task(store, orchestrator.run_task(store, args.task_id, args.max_steps))


def cmd_pause(args) -> None:
    store = _store(args)
    orchestrator.pause(store, args.task_id)
    print("pause requested; the orchestrator stops at the next step boundary")


def cmd_approve(args) -> None:
    store = _store(args)
    _print_task(store, orchestrator.approve(store, args.task_id, args.force))


def cmd_reject(args) -> None:
    store = _store(args)
    task = orchestrator.reject(store, args.task_id, args.reason)
    if args.run:
        task = orchestrator.run_task(store, args.task_id)
    _print_task(store, task)


def cmd_note(args) -> None:
    store = _store(args)
    store.load_task(args.task_id)
    store.add_note(args.task_id, args.text)
    store.event(args.task_id, "human_note", text=args.text)
    print("note added; it is included in every later prompt")


def cmd_log(args) -> None:
    store = _store(args)
    for ev in store.events(args.task_id):
        rest = {k: v for k, v in ev.items() if k not in ("time", "event")}
        print(f"{ev['time']}  {ev['event']:<18} {json.dumps(rest, ensure_ascii=False)}")


def cmd_unlock(args) -> None:
    store = _store(args)
    print("lock removed" if store.unlock() else "no lock present")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="handoff", description=__doc__)
    p.add_argument("--repo", default=".", help="path inside the target git repository")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create .agent-handoff/ and default config").set_defaults(fn=cmd_init)

    s = sub.add_parser("start", help="create a task (from --spec JSON and/or flags) and run it")
    s.add_argument("--spec", help="task JSON file")
    s.add_argument("--title")
    s.add_argument("--goal")
    s.add_argument("--acceptance", action="append", help="acceptance criterion (repeatable)")
    s.add_argument("--file", action="append", help="file in scope (repeatable)")
    s.add_argument("--test-command", dest="test_command")
    s.add_argument("--coder", help="override coder agent")
    s.add_argument("--reviewer", help="override reviewer agent")
    s.add_argument("--no-run", action="store_true", help="only create the task")
    s.add_argument("--max-steps", type=int, help="stop after N steps")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("status", help="show one or all tasks")
    s.add_argument("task_id", nargs="?")
    s.set_defaults(fn=cmd_status)

    for name, fn, helptext in (("resume", cmd_resume, "clear pause/blocked and continue (also crash recovery)"),
                               ("pause", cmd_pause, "stop at the next step boundary")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("task_id")
        if name == "resume":
            s.add_argument("--max-steps", type=int)
        s.set_defaults(fn=fn)

    s = sub.add_parser("approve", help="accept a reviewed task")
    s.add_argument("task_id")
    s.add_argument("--force", action="store_true", help="also accept a blocked task")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("reject", help="send the task back to the coder with a reason")
    s.add_argument("task_id")
    s.add_argument("--reason", required=True)
    s.add_argument("--run", action="store_true", help="continue immediately")
    s.set_defaults(fn=cmd_reject)

    s = sub.add_parser("note", help="add a human note to all later prompts")
    s.add_argument("task_id")
    s.add_argument("text")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("log", help="print the event log")
    s.add_argument("task_id")
    s.set_defaults(fn=cmd_log)

    sub.add_parser("unlock", help="remove a stale orchestrator lock").set_defaults(fn=cmd_unlock)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (HandoffError, gitutil.GitError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
