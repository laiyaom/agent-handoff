from __future__ import annotations

from .base import AgentAdapter, AgentRequest, AgentResult, run_process


class CommandAdapter(AgentAdapter):
    """Any CLI that reads the prompt on stdin and prints its final message.

    Useful for Gemini (`["gemini", "-p"]`), wrappers, and the demo's scripted agents.
    Read-only mode is enforced by the orchestrator's workspace fingerprint check;
    the command also receives HANDOFF_MODE so it can restrict itself.
    """

    def run(self, req: AgentRequest) -> AgentResult:
        argv = self.spec["command"]
        if isinstance(argv, str):
            argv = [argv]
        env = {
            "HANDOFF_TASK_ID": req.task_id,
            "HANDOFF_PHASE": req.phase,
            "HANDOFF_MODE": req.mode,
            "HANDOFF_ROUND_DIR": str(req.round_dir),
        }
        return run_process([*argv, *self.spec.get("extra_args", [])], req, env)
