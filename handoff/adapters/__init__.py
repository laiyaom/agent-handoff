"""Adapter registry. Add a backend with register_adapter("name", Class)."""

from __future__ import annotations

from .base import AgentAdapter, AgentRequest, AgentResult
from .claude_cli import ClaudeCLIAdapter
from .codex_cli import CodexCLIAdapter
from .command import CommandAdapter

ADAPTERS: dict[str, type[AgentAdapter]] = {
    "codex_cli": CodexCLIAdapter,
    "claude_cli": ClaudeCLIAdapter,
    "command": CommandAdapter,
}


def register_adapter(kind: str, cls: type[AgentAdapter]) -> None:
    ADAPTERS[kind] = cls


def build_adapter(agent_name: str, config: dict) -> AgentAdapter:
    spec = config.get("agents", {}).get(agent_name)
    if spec is None:
        raise KeyError(f"agent {agent_name!r} is not defined in config.agents")
    kind = spec.get("adapter")
    if kind not in ADAPTERS:
        raise KeyError(f"agent {agent_name!r}: unknown adapter {kind!r} (known: {sorted(ADAPTERS)})")
    return ADAPTERS[kind](agent_name, spec)


__all__ = ["AgentAdapter", "AgentRequest", "AgentResult", "ADAPTERS",
           "register_adapter", "build_adapter"]
