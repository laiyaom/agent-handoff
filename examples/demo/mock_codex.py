"""Scripted stand-in for Codex. Develop round ships a subtle bug; fix round repairs it.

Reads the prompt on stdin (like `codex exec -`) and edits files in the cwd.
"""
import os
import sys
from pathlib import Path

prompt = sys.stdin.read()
phase = os.environ.get("HANDOFF_PHASE")

BUGGY = '''def slugify(text: str) -> str:
    """Turn a title into a URL slug."""
    return text.strip().lower().replace(" ", "-")
'''

FIXED = '''import re


def slugify(text: str) -> str:
    """Turn a title into a URL slug: lowercase ASCII words joined by single hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''

EXTRA_TEST = '''

class PunctuationTest(unittest.TestCase):
    def test_punctuation_and_repeated_spaces(self):
        self.assertEqual(slugify("  Hello,  World! "), "hello-world")
'''

if phase == "develop":
    Path("textutil.py").write_text(BUGGY)
    print("Implemented slugify() in textutil.py using lower() + space replacement. "
          "Existing tests pass.")
elif phase == "fix":
    if "punctuation" not in prompt.lower():
        print("No actionable issue found in the prompt", file=sys.stderr)
        sys.exit(2)
    Path("textutil.py").write_text(FIXED)
    test = Path("test_textutil.py")
    test.write_text(test.read_text() + EXTRA_TEST)
    print("Fixed the reviewer's issue: slugify() now collapses punctuation/whitespace runs "
          "into one hyphen and trims them. Added PunctuationTest.")
else:
    sys.exit(f"unexpected phase {phase}")
