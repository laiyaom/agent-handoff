"""Scripted stand-in for the Claude reviewer. Read-only: it never writes files.

It actually evaluates the code under review (without writing bytecode) and
emits the verdict JSON block the orchestrator expects.
"""
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
prompt = sys.stdin.read()
assert "READ-ONLY reviewer" in prompt, "orchestrator must send the review rules"

namespace = {}
exec(Path("textutil.py").read_text(), namespace)
got = namespace["slugify"]("  Hello,  World! ")

if got == "hello-world":
    verdict = {"verdict": "PASS", "issues": [],
               "summary": "slugify handles punctuation and whitespace runs; tests cover it."}
else:
    verdict = {"verdict": "NEED_FIX", "summary": "Slug is not URL-safe.",
               "issues": [{"severity": "high", "file": "textutil.py",
                           "description": f"punctuation and repeated spaces leak into the slug: "
                                          f"slugify('  Hello,  World! ') == {got!r}",
                           "suggestion": "collapse every non [a-z0-9] run into one '-' and strip "
                                         "leading/trailing '-'; add a test"}]}

print("Reviewed the diff against the acceptance criteria.\n")
print("```json\n" + json.dumps(verdict, indent=2) + "\n```")
