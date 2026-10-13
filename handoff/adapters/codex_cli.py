from __future__ import annotations

from .base import AgentAdapter, AgentRequest, AgentResult, run_process


class CodexCLIAdapter(AgentAdapter):
    """`codex exec` in non-interactive mode; review runs in the read-only sandbox."""

    def run(self, req: AgentRequest) -> AgentResult:
        last = req.round_dir / "codex_last_message.md"
        argv = [
            self.spec.get("command", "codex"), "exec",
            "-C", str(req.workdir),
            "-s", "read-only" if req.mode == "read" else "workspace-write",
            "--color", "never",
            "-o", str(last),
            *self.spec.get("extra_args", []),
            "-",  # prompt from stdin
        ]
        result = run_process(argv, req)
        if last.exists():
            result.output = last.read_text(encoding="utf-8")
        return result
