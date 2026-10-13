from __future__ import annotations

from .base import AgentAdapter, AgentRequest, AgentResult, run_process

READ_TOOLS = ["Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)",
              "Bash(git status:*)", "Bash(git show:*)"]
WRITE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]


class ClaudeCLIAdapter(AgentAdapter):
    """`claude -p` (print mode). Review mode only allows read tools."""

    def run(self, req: AgentRequest) -> AgentResult:
        argv = [self.spec.get("command", "claude"), "-p", "--output-format", "text"]
        if req.mode == "read":
            argv += ["--permission-mode", "default",
                     "--allowedTools", ",".join(self.spec.get("read_allowed_tools", READ_TOOLS)),
                     "--disallowedTools", "Edit,Write,NotebookEdit"]
        else:
            argv += ["--permission-mode", "acceptEdits",
                     "--allowedTools", ",".join(self.spec.get("write_allowed_tools", WRITE_TOOLS))]
        argv += self.spec.get("extra_args", [])
        return run_process(argv, req)
