"""Behavioural tests against real temp git repos and scripted command agents.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from handoff import orchestrator  # noqa: E402
from handoff.prompts import parse_verdict  # noqa: E402
from handoff.store import HandoffError, Store  # noqa: E402

DEMO = ROOT / "examples" / "demo"

CODER = """
import os, pathlib, sys
prompt = sys.stdin.read()
p = pathlib.Path("work.txt")
p.write_text((p.read_text() if p.exists() else "") + os.environ["HANDOFF_PHASE"] + "\\n")
print("coder done")
"""

def reviewer_script(verdicts: list[str], counter: Path, extra: str = "") -> str:
    return textwrap.dedent(f"""
        import json, pathlib, sys
        sys.stdin.read()
        c = pathlib.Path({str(counter)!r})
        n = int(c.read_text()) if c.exists() else 0
        c.write_text(str(n + 1))
        verdicts = {verdicts!r}
        v = verdicts[min(n, len(verdicts) - 1)]
        {extra}
        issues = [] if v == "PASS" else [{{"severity": "high", "file": "work.txt", "description": "issue %d" % n}}]
        print("```json\\n" + json.dumps({{"verdict": v, "summary": "s", "issues": issues}}) + "\\n```")
    """)


class Repo:
    def __init__(self, tmp: Path, reviewer: str, coder: str = CODER, **cfg_overrides):
        self.path = tmp / "repo"
        self.path.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=self.path, check=True)
        (self.path / "README").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=self.path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.path, check=True)
        (tmp / "coder.py").write_text(coder)
        (tmp / "reviewer.py").write_text(reviewer)
        self.store = Store(self.path)
        self.store.init()
        cfg = json.loads((self.store.root / "config.json").read_text())
        cfg["agents"]["c"] = {"adapter": "command", "command": [sys.executable, str(tmp / "coder.py")], "timeout": 30}
        cfg["agents"]["r"] = {"adapter": "command", "command": [sys.executable, str(tmp / "reviewer.py")], "timeout": 30}
        cfg["roles"] = {"coder": "c", "reviewer": "r"}
        cfg.update(cfg_overrides)
        (self.store.root / "config.json").write_text(json.dumps(cfg))

    def create(self, **spec) -> str:
        spec = {"title": "t", "goal": "g", **spec}
        return orchestrator.create_task(self.store, spec)["task_id"]

    def phases(self, task_id: str) -> list[str]:
        return [r["phase"] for r in self.store.load_task(task_id)["rounds"]]


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.counter = self.tmp / "review_count"

    def tearDown(self):
        self._tmp.cleanup()

    def test_demo_full_flow(self):
        out = subprocess.run([sys.executable, str(DEMO / "run_demo.py"), "--workdir", str(self.tmp)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        store = Store(self.tmp / "repo")
        (task,) = store.list_tasks()
        self.assertEqual(task["status"], "done")
        self.assertEqual([(r["phase"], r["verdict"]) for r in task["rounds"]],
                         [("develop", None), ("review", "NEED_FIX"), ("fix", None), ("review", "PASS")])
        self.assertEqual(task["test_results"]["status"], "passed")
        self.assertIn("textutil.py", task["changed_files"])

    def test_need_fix_loop_stops_at_max_rounds(self):
        repo = Repo(self.tmp, reviewer_script(["NEED_FIX"], self.counter))
        tid = repo.create(max_fix_rounds=2)
        task = orchestrator.run_task(repo.store, tid)
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(repo.phases(tid), ["develop", "review", "fix", "review", "fix", "review"])
        self.assertIn("2 automatic fix rounds", task["blocked_reason"])
        # Human gives one more round: resume -> fix -> review (PASS now).
        self.counter.write_text("99")
        (self.tmp / "reviewer.py").write_text(reviewer_script(["PASS"], self.counter))
        orchestrator.resume(repo.store, tid)
        task = orchestrator.run_task(repo.store, tid)
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertEqual(repo.phases(tid)[-2:], ["fix", "review"])

    def test_reviewer_write_is_detected_and_blocks(self):
        sneaky = reviewer_script(["PASS"], self.counter, extra='pathlib.Path("work.txt").write_text("tampered")')
        repo = Repo(self.tmp, sneaky)
        task = orchestrator.run_task(repo.store, repo.create())
        self.assertEqual(task["status"], "blocked")
        self.assertIn("modified the workspace", task["blocked_reason"])

    def test_unparseable_verdict_blocks_instead_of_passing(self):
        repo = Repo(self.tmp, "import sys; sys.stdin.read(); print('looks good to me')")
        task = orchestrator.run_task(repo.store, repo.create())
        self.assertEqual(task["status"], "blocked")
        self.assertIn("could not parse", task["blocked_reason"])

    def test_pass_with_failing_tests_is_downgraded(self):
        repo = Repo(self.tmp, reviewer_script(["PASS"], self.counter))
        tid = repo.create(test_command=f'"{sys.executable}" -c "raise SystemExit(1)"', max_fix_rounds=0)
        task = orchestrator.run_task(repo.store, tid)
        self.assertEqual(task["last_verdict"], "NEED_FIX")
        self.assertEqual(task["open_issues"][-1]["source"], "orchestrator")

    def test_agent_failure_retries_then_blocks(self):
        repo = Repo(self.tmp, reviewer_script(["PASS"], self.counter),
                    coder="import sys; sys.stdin.read(); sys.exit(3)")
        task = orchestrator.run_task(repo.store, repo.create())
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(repo.phases(task["task_id"]), ["develop", "develop"])  # 1 retry

    def test_pause_stops_at_boundary_and_resume_continues(self):
        repo = Repo(self.tmp, reviewer_script(["PASS"], self.counter))
        tid = repo.create()
        orchestrator.pause(repo.store, tid)
        task = orchestrator.run_task(repo.store, tid)
        self.assertEqual((task["status"], task["rounds"]), ("ready", []))
        # Human edits the task while paused; the next prompt must use the edit.
        path = repo.store.task_dir(tid) / "task.json"
        data = json.loads(path.read_text())
        data["goal"] = "EDITED GOAL"
        path.write_text(json.dumps(data))
        repo.store.add_note(tid, "HUMAN NOTE")
        orchestrator.resume(repo.store, tid)
        task = orchestrator.run_task(repo.store, tid)
        self.assertEqual(task["status"], "awaiting_approval")
        prompt = (repo.store.root / task["rounds"][0]["dir"] / "prompt.md").read_text()
        self.assertIn("EDITED GOAL", prompt)
        self.assertIn("HUMAN NOTE", prompt)

    def test_reject_sends_back_to_coder_with_reason(self):
        repo = Repo(self.tmp, reviewer_script(["PASS"], self.counter))
        tid = repo.create()
        orchestrator.run_task(repo.store, tid)
        with self.assertRaises(HandoffError):
            orchestrator.reject(repo.store, "nope", "x")
        orchestrator.reject(repo.store, tid, "rename the file")
        task = orchestrator.run_task(repo.store, tid)
        self.assertEqual(repo.phases(tid), ["develop", "review", "fix", "review"])
        fix_prompt = (repo.store.root / task["rounds"][2]["dir"] / "prompt.md").read_text()
        self.assertIn("rename the file", fix_prompt)
        orchestrator.approve(repo.store, tid)
        self.assertEqual(repo.store.load_task(tid)["status"], "done")

    @unittest.skipIf(os.name == "nt", "uses SIGKILL")
    def test_crash_mid_review_resumes_without_repeating_completed_steps(self):
        # First review invocation kills the orchestrator process (a real crash).
        crash = reviewer_script(["PASS"], self.counter, extra=(
            "import os, signal\n        if n == 0: os.kill(os.getppid(), signal.SIGKILL)"))
        repo = Repo(self.tmp, crash)
        tid = repo.create()
        proc = subprocess.run([sys.executable, "-m", "handoff", "--repo", str(repo.path), "resume", tid],
                              cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        task = repo.store.load_task(tid)
        self.assertEqual((task["status"], task["phase"]), ("running", "review"))
        self.assertTrue((repo.store.root / "lock").exists())  # stale lock left behind

        task = orchestrator.run_task(repo.store, tid)  # stale lock reclaimed, step re-run
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertEqual(repo.phases(tid), ["develop", "review"])
        self.assertEqual((repo.path / "work.txt").read_text(), "develop\n")  # coder ran once
        events = [e["event"] for e in repo.store.events(tid)]
        self.assertIn("recovered", events)
        self.assertTrue(list((repo.store.task_dir(tid) / "rounds").glob("02-review-r-interrupted-*")))

    def test_role_override_must_name_a_configured_agent(self):
        repo = Repo(self.tmp, reviewer_script(["PASS"], self.counter))
        with self.assertRaises(HandoffError):
            repo.create(roles={"reviewer": "gemini"})


class VerdictParsingTests(unittest.TestCase):
    def test_last_json_block_wins(self):
        text = '```json\n{"verdict": "PASS"}\n```\nthen\n```json\n{"verdict": "need-fix", "issues": ["x"]}\n```'
        v = parse_verdict(text)
        self.assertEqual(v["verdict"], "NEED_FIX")
        self.assertEqual(v["issues"], [{"description": "x"}])

    def test_no_verdict(self):
        self.assertIsNone(parse_verdict('```json\n{"ok": true}\n```'))


if __name__ == "__main__":
    unittest.main()
