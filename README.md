# agent-handoff

A small, file-based orchestrator that lets **Codex** and **Claude Code** hand work
back and forth inside one Git repository:

```
create task → Codex develops → Claude reviews ─PASS─→ human approves → done
                    ↑                 │
                    └── NEED_FIX ─────┘   (bounded by max_fix_rounds)
```

The defaults are Codex for coding, editing, testing, and fixing, and Claude for
analysis, review, risk checks, and re-review. Each task can override the roles.
The framework is built to keep a human in control, not to run unattended: every
step is a resumable checkpoint, every round is saved to disk, and anything
unexpected stops in `blocked` for a person to decide.

Requirements: Python 3.9+, `git`, and the agent CLIs you configure (`codex`, `claude`).
It uses only the standard library.

## Quick start

```bash
# 1. Deterministic demo (scripted agents, no tokens): the full
#    develop → NEED_FIX → fix → PASS → approve flow in a throwaway repo
python3 examples/demo/run_demo.py

# 2. Use it on your own repository (must have at least one commit)
cd /path/to/your/repo
python3 -m handoff init                       # or `agent-handoff init` after `pip install -e .`
python3 -m handoff start --spec task.json     # create + run until a human is needed
python3 -m handoff status
python3 -m handoff approve <task_id>
```

Run the tests with `python3 -m unittest discover -s tests -v`.

## Commands

| Command | Effect |
| --- | --- |
| `init` | Create `.agent-handoff/` and a default `config.json`, and add the directory to `.git/info/exclude` |
| `start --spec F` / `--title --goal --acceptance --file --test-command [--coder X --reviewer Y] [--no-run] [--max-steps N]` | Create a task and run it until it needs a human |
| `status [id]` | Show the status, next phase and agent, verdict, fix-round budget, open issues, and the suggested next command |
| `pause id` | Stop at the next step boundary. A running agent call is never killed. |
| `resume id` | Clear the pause, turn `blocked` back into `ready`, and continue. This is also the crash-recovery command. |
| `approve id [--force]` | `awaiting_approval → done`. `--force` also accepts a `blocked` task. |
| `reject id --reason TEXT [--run]` | Send the task back to the coder with your reason as a high-severity issue. This also resets the automatic fix budget. |
| `note id TEXT` | Add a human note. Notes appear first in every later prompt. |
| `log id` | Print the event log (state transitions, steps, human actions) |
| `unlock` | Remove a stale lock (needed on Windows only; on POSIX, dead locks are detected automatically) |

## Directory layout

```
agent-handoff/
├── handoff/
│   ├── cli.py             # commands above
│   ├── orchestrator.py    # state machine, step execution, human controls
│   ├── store.py           # .agent-handoff/ layout, task.json validation, lock, events
│   ├── prompts.py         # prompt construction + reviewer verdict parsing
│   ├── gitutil.py         # diff vs base (incl. untracked), workspace fingerprint
│   └── adapters/          # Agent Adapter interface + codex_cli / claude_cli / command
├── examples/demo/         # run_demo.py, task.json, scripted mock_codex.py / mock_claude.py
├── examples/config.example.json
└── tests/test_handoff.py

<your repo>/.agent-handoff/          (git-excluded)
├── config.json
├── lock                              # present while an orchestrator runs
└── tasks/<task_id>/
    ├── task.json                     # source of truth (human-editable while idle)
    ├── task.md                       # rendered view of task.json
    ├── handoff.md                    # latest handoff, fed to the next agent
    ├── notes.md                      # human notes
    ├── events.jsonl                  # every state change with timestamps
    ├── PAUSE                         # present = pause requested
    └── rounds/NN-<phase>-<agent>/
        ├── prompt.md                 # exact prompt sent
        ├── output.md                 # agent's final message
        ├── stdout.log / stderr.log
        ├── diff_before.patch / diff.patch   # git diff vs base, before/after the step
        ├── test.log                  # write steps: orchestrator-run test command
        └── meta.json                 # times, duration, exit code, verdict
```

## Task protocol (`task.json`)

| Field | Meaning |
| --- | --- |
| `task_id`, `title`, `goal` | Identity and objective |
| `acceptance_criteria`, `files` | Acceptance criteria and the files in scope |
| `test_command` | Run by the orchestrator after each write step, not left to the agent |
| `roles` | `{"coder": ..., "reviewer": ...}`. Defaults come from config; tasks may override them. |
| `status` | `ready`, `running`, `awaiting_approval`, `blocked`, or `done` |
| `phase` | The **next** step: `develop`, `review`, or `fix` |
| `current_agent`, `next_agent` | The agent that ran last and the agent (or `human`) up next |
| `base_commit` | Every diff is computed against this commit |
| `last_verdict`, `open_issues` | `PASS`/`NEED_FIX` and the issue list handed to the coder |
| `retry_count` / `max_fix_rounds` | Automatic fix loops used and the allowed limit |
| `step_attempts` | Failed attempts of the current step (bounded by `max_step_retries`) |
| `changed_files`, `test_results`, `results`, `rounds` | Files changed, latest test result, per-step summaries, round records |
| `blocked_reason` | Why a human is needed |

A minimal spec for `start --spec` needs only `title` and `goal`. See
`examples/demo/task.json`.

### Reviewer verdict contract

The reviewer prompt requires the reply to end with one fenced JSON block:

```json
{"verdict": "PASS" | "NEED_FIX", "summary": "...",
 "issues": [{"severity": "high|medium|low", "file": "...", "description": "...", "suggestion": "..."}]}
```

The orchestrator parses the **last** such block. If it can't find a verdict, the
task becomes `blocked`; a missing verdict is never treated as a pass.

## State machine and recovery

- Each step reads `task.json`, marks it `running`, calls the agent, records the
  artifacts, and ends with **one atomic save**. Completed steps are never re-run.
- If the process dies mid-step, the task stays `running`. The next `resume` logs
  a `recovered` event and renames the partial round directory to
  `…-interrupted-N`. It then re-runs **only that step**, and the prompt warns
  the agent that the working tree may hold partial changes.
- A repository-wide lock file serialises orchestrators, so agent writes are always
  serial. On POSIX, a lock whose PID is dead is reclaimed automatically.
- Write step: agent → `git diff` → test command → `phase=review`.
- Review step: if the agent touched the workspace, the task is `blocked`. If no
  verdict can be parsed, it is `blocked`. A `PASS` with failing tests is
  downgraded to `NEED_FIX`. Otherwise `PASS` goes to `awaiting_approval` (or
  `done` if `require_human_approval` is false), and `NEED_FIX` goes to `fix`
  until `max_fix_rounds` is used up, after which the task is `blocked`.
- Agent failure (non-zero exit or timeout): the step is retried up to
  `max_step_retries` times, then the task is `blocked`.

### Human intervention

1. `handoff pause <id>`, then wait until `status` no longer shows `running`.
2. Edit `.agent-handoff/tasks/<id>/task.json` (goal, criteria, roles, issues,
   phase, and so on) and/or run `handoff note <id> "..."`. The file is validated
   on the next load.
3. Run `handoff resume <id>`. The orchestrator re-reads the task before every step.

## Configuration

Copy `examples/config.example.json` over `.agent-handoff/config.json`, keeping
the keys you need. Missing keys fall back to the built-in defaults.

| Key | Default | |
| --- | --- | --- |
| `roles` | `codex` / `claude` | Default coder and reviewer |
| `max_fix_rounds` | 3 | Automatic NEED_FIX → fix loops before `blocked` |
| `max_step_retries` | 1 | Retries of a failed agent call |
| `require_human_approval` | true | Stop at `awaiting_approval` after `PASS` |
| `max_diff_chars` | 60000 | Diff size inlined into prompts. Agents are told to run `git diff <base>` for the rest. |
| `test_timeout` | 900 | Seconds |
| `agents.<name>` | | `adapter` plus adapter-specific keys and `timeout` |

Built-in adapters:

| `adapter` | Invocation |
| --- | --- |
| `codex_cli` | `codex exec -C <repo> -s workspace-write\|read-only --color never -o <round>/codex_last_message.md -` (prompt on stdin) |
| `claude_cli` | `claude -p --output-format text`. Review mode uses `--allowedTools Read,Grep,Glob,Bash(git diff/log/status/show:*) --disallowedTools Edit,Write,NotebookEdit`. Write mode uses `--permission-mode acceptEdits` with `write_allowed_tools`. |
| `command` | Any argv. The prompt goes to stdin, stdout is the result, and `HANDOFF_TASK_ID/PHASE/MODE/ROUND_DIR` are set in the environment. Use it for Gemini and other CLIs, or for wrappers. |

### Adding an agent backend

```python
from handoff.adapters import AgentAdapter, AgentRequest, AgentResult, register_adapter

class ClaudeSDKAdapter(AgentAdapter):
    def run(self, req: AgentRequest) -> AgentResult:
        # call the Claude Agent SDK / Codex App Server here;
        # honour req.mode == "read" by granting only read tools
        ...

register_adapter("claude_sdk", ClaudeSDKAdapter)
```

The adapter only turns a prompt into a final message. State, diffs, tests,
verdict parsing, the read-only check, and logging all stay in the
orchestrator, so moving from the CLI to an SDK does not change the protocol.

## Safety constraints

- **Nothing is committed or pushed.** Agents are told not to commit, and the
  result stays in the working tree for a human to review and commit.
- **Reviews are read-only, and this is checked.** The review sandbox is set per
  adapter (`codex -s read-only`; claude with only read tools). The orchestrator
  also fingerprints `HEAD`, `git status`, and the full diff, including untracked
  files, before and after each review. Any change blocks the task.
- **Writes are serial.** One orchestrator per repository, enforced by the lock.
- **Loops are bounded.** `max_fix_rounds` and `max_step_retries` cap automatic
  iteration, and `blocked` always waits for a human.
- **Tests aren't trusted to agents.** The orchestrator runs `test_command` itself,
  and a failing run vetoes `PASS`.
- **There is a full audit trail.** Every prompt, output, diff, test log, time,
  and state change is on disk.
- **Use a clean branch or worktree per task.** Diffs are measured against
  `base_commit`, so uncommitted work present at creation time is attributed to
  the agents. `codex -s workspace-write` and claude `Bash` can run commands in
  the repository. Run them on trusted code, and keep the write tool allowlist as
  narrow as your workflow permits.
- `.agent-handoff/` is git-excluded locally. Never put secrets in tasks, notes,
  or config; prompts are sent to the agent providers.

## Limitations (MVP)

- `pause` takes effect at step boundaries; it does not interrupt a running agent.
- `codex_cli` and `claude_cli` are thin wrappers. Their flags follow the CLI
  versions current at the time of writing (`codex-cli 0.162`), so adjust
  `extra_args` if your version differs.
- There is one task at a time per repository. Use separate worktrees for parallel tasks.
