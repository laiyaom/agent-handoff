"""End-to-end demo: create task -> Codex develop -> Claude review (NEED_FIX)
-> Codex fix -> Claude re-review (PASS) -> human approve -> done.

Default uses the scripted agents in this folder so the flow is deterministic and free.
`--real` uses the real `codex` and `claude` CLIs instead (costs tokens; the
reviewer may PASS on the first round, which is a legitimate shorter path).

    python3 examples/demo/run_demo.py [--workdir DIR] [--real]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from handoff.cli import main as handoff  # noqa: E402

TEST_FILE = '''import unittest

from textutil import slugify


class BasicTest(unittest.TestCase):
    def test_simple_title(self):
        self.assertEqual(slugify("Hello World"), "hello-world")


if __name__ == "__main__":
    unittest.main()
'''


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "demo@example.invalid")
    git(repo, "config", "user.name", "handoff demo")
    (repo / "textutil.py").write_text('def slugify(text: str) -> str:\n    raise NotImplementedError\n')
    (repo / "test_textutil.py").write_text(TEST_FILE)
    (repo / ".gitignore").write_text("__pycache__/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "demo: skeleton")


def configure(repo: Path, real: bool) -> None:
    cfg_path = repo / ".agent-handoff" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    if not real:
        py = sys.executable
        cfg["agents"]["mock-codex"] = {"adapter": "command", "command": [py, str(HERE / "mock_codex.py")], "timeout": 60}
        cfg["agents"]["mock-claude"] = {"adapter": "command", "command": [py, str(HERE / "mock_claude.py")], "timeout": 60}
        cfg["roles"] = {"coder": "mock-codex", "reviewer": "mock-claude"}
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")


def run(repo: Path, *argv: str) -> None:
    print(f"\n$ handoff {' '.join(argv)}")
    code = handoff(["--repo", str(repo), *argv])
    if code:
        sys.exit(code)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", help="where to create the demo repo (default: temp dir)")
    ap.add_argument("--real", action="store_true", help="use real codex/claude CLIs")
    args = ap.parse_args()

    repo = Path(args.workdir or tempfile.mkdtemp(prefix="handoff-demo-")).resolve() / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    make_repo(repo)

    spec = json.loads((HERE / "task.json").read_text())
    spec["test_command"] = f'"{sys.executable}" -m unittest -q'
    spec_path = repo.parent / "task.json"
    spec_path.write_text(json.dumps(spec, indent=2))

    run(repo, "init")
    configure(repo, args.real)
    run(repo, "start", "--spec", str(spec_path))   # develop -> review -> fix -> review
    task_dir = next((repo / ".agent-handoff" / "tasks").iterdir())
    run(repo, "approve", task_dir.name)
    run(repo, "log", task_dir.name)

    print("\nRound artifacts:")
    for d in sorted((task_dir / "rounds").iterdir()):
        print(f"  {d.relative_to(repo)}/  {' '.join(sorted(p.name for p in d.iterdir()))}")
    print(f"\nTask record: {task_dir / 'task.md'}")
    print(f"Working tree diff left for you to review/commit:\n")
    print(subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True).stdout)


if __name__ == "__main__":
    main()
