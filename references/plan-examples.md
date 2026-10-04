# Orchestration Plan Examples

Contents: plan structure / step types / container restart / deployment and execution / execution semantics

## Plan structure

```json
{
  "sessions": ["n1", "n2", "n3"],
  "steps": [
    {"name": "step-name", "command": "remote-command", "expect": "marker"}
  ]
}
```

`--sessions` overrides the session list in the plan. Each step runs in parallel across all sessions. **Every session must finish the current step before the next step begins.** This barrier allows batch operations to advance safely.

## Step types

| `expect` | Purpose | Required fields | Success condition |
|---|---|---|---|
| `marker` | Ordinary command | `command` | `rc` is in `allow_rc` (default `[0]`) |
| `disconnect` | Expected shell disappearance | Optional `command` | Shell identity changes or the pane disappears |
| `ready` | Wait for availability | `ready_command` | Repeated probes return 0 |
| `json` | Structured check | `python` | The Python function body returns normally |

Optional fields: `timeout_seconds`, `allow_rc`, `settle_seconds` (delay after sending a `ready` step's command), and `poll_seconds` (probe interval).

## Container restart

A typical use of this skill is to exit, restart, and reenter containers across multiple machines.

```json
{
  "sessions": ["n1", "n2", "n3", "n4"],
  "steps": [
    {
      "name": "exit-container",
      "command": "exit",
      "expect": "disconnect",
      "timeout_seconds": 30
    },
    {
      "name": "restart-container",
      "command": "sudo docker restart myctr",
      "expect": "marker",
      "timeout_seconds": 300
    },
    {
      "name": "reenter-container",
      "command": "sudo docker exec -it myctr /bin/bash",
      "expect": "ready",
      "ready_command": "test -d /workspace",
      "timeout_seconds": 180,
      "settle_seconds": 3
    },
    {
      "name": "verify-env",
      "python": "import os\nreturn {'cwd': os.getcwd(), 'has_workspace': os.path.isdir('/workspace')}",
      "expect": "json"
    }
  ]
}
```

Three key points:

- Use `disconnect` to exit the container. The shell is expected to disappear, so waiting for a `marker` receipt would time out.
- Run the restart command on the host. The pane has returned to the host shell, so use `marker`.
- Use `ready` for reentry. Container services may take time to become available; repeated probes are more reliable than a fixed `sleep`.

`disconnect` asks the shell to report its own PID and checks for a change. tmux tracks only the outermost pane process; exiting a nested or container shell does not change `pane_pid`, so the shell itself must be queried.

Identity probes must receive valid receipts. A timeout is not treated as a successful disconnect. `ready_command` is retried only after a nonzero exit code is received. A receipt timeout returns `TIMEOUT` and blocks further writes until the pane is inspected and the session is explicitly recovered.

## Deployment and execution

```json
{
  "sessions": ["n1", "n2"],
  "steps": [
    {"name": "clean", "command": "rm -rf /remote/project/build", "expect": "marker"},
    {"name": "unpack", "command": "cd /remote/project && unzip -o bundle.zip", "expect": "marker"},
    {"name": "deps", "command": "cd /remote/project && python3 -m pip install -q -r requirements.txt", "expect": "marker", "timeout_seconds": 600},
    {"name": "smoke", "command": "cd /remote/project && python3 -c 'import app'", "expect": "marker"},
    {
      "name": "record",
      "python": "import hashlib,os\nroot='/remote/project'\nfiles=sorted(f for f in os.listdir(root) if f.endswith('.py'))\nreturn {'file_count': len(files)}",
      "expect": "json"
    }
  ]
}
```

Upload the archive to each machine with `transfer.py put` beforehand. The plan handles only unpacking and verification.

## Execution semantics

**Dry-run first.** `--dry-run` prints the planned steps without touching any session. Run it after changing a plan.

**Stop on failure.** By default, a failed step in any session stops the entire plan to avoid advancing with inconsistent state. `--continue-on-failure` removes failed sessions and lets the remaining sessions continue; use it when some machines may be left behind.

**Result structure.** Output is grouped by step. Each group includes its status, failed session names, and per-session details, making it possible to identify the failing machine and step directly.

**Destructive commands.** Commands in a plan execute for real. For `rm -rf`, restarts, or service stops, confirm paths and targets and review a `--dry-run` first.
