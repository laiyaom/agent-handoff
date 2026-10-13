"""Prompt construction and reviewer-verdict parsing."""

from __future__ import annotations

import json
import re

CODER_RULES = """\
- You are the WRITE agent for this step. Edit files in the working tree to meet the goal.
- Do NOT commit, push, rebase, or reset. Do NOT modify `.agent-handoff/`.
- Stay within the files in scope unless the goal clearly requires more; say so if you do.
- Run the project's tests if you can. The orchestrator also runs the test command after you.
- End with a short handoff for the reviewer: what you changed, why, and anything unresolved."""

REVIEWER_RULES = """\
- You are the READ-ONLY reviewer. Do NOT edit, create, or delete any file, and do not run
  commands that change the working tree. Any workspace change blocks the task.
- Review the diff against the goal and every acceptance criterion. Check correctness,
  regressions, risks, and test coverage. Use `git diff {base}` for the full diff.
- Reply with your analysis, then END with exactly one fenced JSON block:

```json
{{"verdict": "PASS" or "NEED_FIX",
  "summary": "one paragraph",
  "issues": [{{"severity": "high|medium|low", "file": "path or null",
              "description": "what is wrong", "suggestion": "how to fix"}}]}}
```
- Use NEED_FIX only for problems that must change before acceptance; `issues` must be
  empty for PASS (put optional remarks in `summary`)."""


def _bullets(items) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"


def _issues(issues: list[dict]) -> str:
    if not issues:
        return "- (none)"
    return "\n".join(
        f"{n}. [{i.get('severity', '?')}] {i.get('file') or '-'}: {i.get('description', '')}"
        + (f"\n   Suggestion: {i['suggestion']}" if i.get("suggestion") else "")
        + (f"\n   (source: {i['source']})" if i.get("source") else "")
        for n, i in enumerate(issues, 1)
    )


def _truncate(text: str, limit: int, hint: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars; {hint}]\n"


def build_prompt(task: dict, phase: str, diff: str, handoff: str, notes: str,
                 max_diff_chars: int) -> str:
    base = task["base_commit"]
    rules = REVIEWER_RULES.format(base=base) if phase == "review" else CODER_RULES
    tests = task.get("test_results") or {}
    sections = [
        f"# Agent handoff — task `{task['task_id']}` — phase: {phase}",
        f"## Your role\n\n{rules}",
        f"## Goal\n\n{task['goal']}",
        f"## Acceptance criteria\n\n{_bullets(task.get('acceptance_criteria'))}",
        f"## Files in scope\n\n{_bullets(task.get('files'))}",
        f"## Test command\n\n`{task.get('test_command') or '(none configured)'}`",
    ]
    if task.get("interrupted"):
        sections.append("## Recovery notice\n\nThe previous attempt of this step was interrupted. "
                        "The working tree may contain partial changes from it; inspect before acting.")
    if notes.strip():
        sections.append(f"## Human notes (highest priority)\n\n{notes.strip()}")
    if phase == "fix":
        sections.append(f"## Issues you must fix\n\n{_issues(task.get('open_issues', []))}")
    if handoff.strip():
        sections.append(f"## Previous agent's handoff\n\n{handoff.strip()}")
    if phase == "review" or tests:
        sections.append(
            f"## Latest test result (run by orchestrator)\n\nStatus: `{tests.get('status', 'not run')}`"
            f"\n\n```\n{_truncate(tests.get('output_tail', ''), 4000, 'see test.log')}\n```")
    sections.append(
        f"## Current diff vs base `{base[:12]}`\n\n```diff\n"
        f"{_truncate(diff, max_diff_chars, f'run `git diff {base}` for the rest') or '(no changes)'}\n```")
    return "\n\n".join(sections) + "\n"


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_verdict(output: str) -> dict | None:
    """Return {"verdict", "summary", "issues"} from the LAST JSON block, or None."""
    for block in reversed(_JSON_BLOCK.findall(output)):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        verdict = str(data.get("verdict", "")).upper().replace("-", "_")
        if verdict in ("PASS", "NEED_FIX"):
            issues = data.get("issues") or []
            if not isinstance(issues, list):
                return None
            issues = [i if isinstance(i, dict) else {"description": str(i)} for i in issues]
            return {"verdict": verdict, "summary": str(data.get("summary", "")), "issues": issues}
    return None
