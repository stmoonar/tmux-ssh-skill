# tmux SSH Workflow Skill

English | [简体中文](./README.zh-CN.md)

![GitHub stars](https://img.shields.io/github/stars/stmoonar/tmux-ssh-skill?style=flat-square)
![Skill](https://img.shields.io/badge/Skill-Agent-111111?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square)
![tmux](https://img.shields.io/badge/tmux-supported-1f425f?style=flat-square)

A tmux remote workflow skill for Claude Code, Codex, and other local agents.

It reuses authenticated SSH or container shells already established in local tmux sessions to execute remote commands, transfer files, launch and monitor long-running jobs, and resume work after container restarts or disconnections. It does not create new SSH connections.

```text
Local tmux socket → tmux session → authenticated remote shell → commands / files / jobs / containers
```

## Quick start

### Installation

Install with `skills`:

```bash
npx skills add https://github.com/stmoonar/tmux-ssh-skill --skill tmux-ssh-workflow
```

Or clone manually into Claude Code's skill directory:

```bash
git clone https://github.com/stmoonar/tmux-ssh-skill.git \
  ~/.claude/skills/tmux-ssh-workflow
```

If Claude Code runs on the Windows side, run this in PowerShell. If it runs inside WSL, use the command above in WSL:

```powershell
git clone https://github.com/stmoonar/tmux-ssh-skill.git "$env:USERPROFILE\.claude\skills\tmux-ssh-workflow"
```

The scripts themselves must still run inside WSL; see [Windows (WSL)](#windows-wsl).

After installation, confirm the directory contains:

```text
SKILL.md
README.md
README.zh-CN.md
references/
scripts/
```

### Invoking the skill

When a local tmux socket and sessions already exist, ask the agent to operate on their remote shells:

```text
Use session remote-a on tmux socket /tmp/team.sock to check the remote environment and run a training job.
```

```text
Upload ./bundle.zip to /workspace/bundle.zip through tmux session node-01 and verify its hash.
```

```text
Run the same container restart, reentry, and environment checks across remote-a, remote-b, and remote-c. Start with a dry-run.
```

## When to use it

### Suitable uses

- Reusing SSH or container shells in existing local tmux sessions.
- Executing commands on one or more remote machines and retrieving structured results.
- Uploading or downloading through an existing PTY (pseudoterminal, the terminal channel used by tmux) without handling SSH credentials again.
- Launching training, build, deployment, or data-processing jobs that may outlast a local command timeout.
- Rediscovering the active pane (tmux terminal area) and resuming remote work after container restarts or SSH reconnects.
- Running the same sequence of dependent operations across multiple machines.

### Outside its scope

- Creating SSH connections or managing host, user, port, or jump-host configuration.
- Remote operations without an available local tmux channel.
- Transfers that require native `scp`, `rsync`, or high throughput.
- Managing local tmux sessions alone.

## Capabilities

| Capability | Script | Description |
|------------|--------|-------------|
| Session preflight | `scripts/session_preflight.py` | Check sessions, active panes, remote host, user, working directory, and required commands |
| Remote execution | `scripts/tmux_exec.py` | Run shell commands or structured Python checks across one or more sessions |
| File transfer | `scripts/transfer.py` | Upload and download through tmux PTYs with compression, chunking, SHA256 verification, and atomic renames |
| Long-running jobs | `scripts/remote_job.py` | Launch, inspect, stop, and collect remote jobs |
| Batch orchestration | `scripts/batch_sessions.py` | Coordinate sessions with stage barriers, dry-runs, reconnect waits, and structured checks |

All scripts output JSON, return exit code `0` on success, and return a nonzero code on failure. Operations run in parallel across sessions. Within a session, locks serialize operations to keep receipts from interleaving on the same PTY.

## Design principles

1. Rediscover the active pane on every operation. A previously discovered pane may be invalid after a container restart or SSH reconnect.
2. A timeout does not mean failure. It means no receipt has appeared yet; the operation may still be running. Never resend automatically.
3. Long-running job state lives in remote job directories, independent of local processes and tmux scrollback, so work can be resumed after a disconnect.
4. Stop only remote jobs or containers. Never execute `kill-session`, `kill-window`, or `kill-pane`.
5. Batch operations use stage barriers: all sessions finish the current step before the next begins.
6. Final file paths are updated only after complete verification. A failed transfer never publishes a partial or corrupted file.

## Standard workflow

Follow this sequence:

1. Use `session_preflight.py` to confirm the session is available, the remote shell is in the expected environment, and the working directory and commands meet requirements.
2. Upload code, configuration, or input data with `transfer.py put`.
3. Use `tmux_exec.py` to unpack files, check dependencies, and prepare the environment.
4. Launch a long-running job with `remote_job.py launch` and save its `pid` and `pgid` (process and process-group IDs).
5. Poll with `remote_job.py status` until the job reaches `SUCCEEDED` or `FAILED`.
6. Package results remotely with `remote_job.py collect`, then download and verify them with `transfer.py get`.
7. Stop a job with `remote_job.py stop` when needed; preserve the tmux session.

## Requirements

### Local machine

- Python `3.10+`.
- tmux installed and access to the target socket.
- At least one established, authenticated SSH or container shell.
- No third-party Python dependencies; scripts use the standard library.

### Remote environment

- A Unix-like shell accessible through the current pane. Commands run through `bash` by default.
- Python `3.8+` available as `python3` for structured checks, job state, and file ranges. Long-running job management also requires `ps`.
- Basic commands such as `base64` and `stty` for the PTY transfer protocol.
- The appropriate container runtime commands and permissions for container workflows.

### Windows (WSL)

Run only inside WSL (Windows Subsystem for Linux). The tmux socket and SSH sessions must also live in WSL. Native Windows Python exits immediately with a suggested `wsl` command.

- For an agent running on Windows, invoke from PowerShell with `wsl -d <distribution> -e python3 "/mnt/c/<skill-path>/scripts/X.py" ...`. Use the distribution and user that created tmux; add `-u <user>` if needed. With Git Bash, prefix commands with `MSYS_NO_PATHCONV=1`; otherwise POSIX arguments may be rewritten as `C:/...` and rejected.
- Claude Code running inside WSL uses the same commands as Linux.
- Local file arguments accept `C:\...` and automatically convert it to `/mnt/c/...`. `--socket` and remote paths must use POSIX syntax.
- PowerShell does not expand Bash syntax such as `scripts/*.py` or `$((...))`. Run examples using that syntax through `wsl -e bash -lc '...'`.
- Allow enough time for large transfers and inspect the session after interrupted calls. See the Windows section in [`SKILL.md`](./SKILL.md).

## Script usage

The examples below assume:

```bash
SOCKET=/tmp/team.sock
SESSIONS=remote-a,remote-b
```

### 1. Session preflight

```bash
python3 scripts/session_preflight.py \
  --socket "$SOCKET" \
  --sessions "$SESSIONS" \
  --expect-cwd-prefix /workspace \
  --require-command python3
```

Add `--expect-host-contains` to check the remote hostname. If the session should already be inside a container, such as a persistent `docker exec -it <ctr> bash`, add `--expect-in-container`. After a container restart, a pane may fall back to the host shell; this check rejects that state before later commands reach the host. Use `--expect-host` to require a shell outside a container instead.

Detection uses `/.dockerenv`, `/run/.containerenv`, or PID 1's cgroup. Add `--container-marker PATH` for other runtime marker files. Results include `in_container` and `container_hints` to explain the decision. Preflight succeeds only when every session passes.

### 2. Execute remote commands

Run an ordinary command across sessions in parallel:

```bash
python3 scripts/tmux_exec.py \
  --socket "$SOCKET" \
  --sessions "$SESSIONS" \
  --command 'nvidia-smi -L' \
  --show-output
```

For structured results, write a Python function body into a file. It must end with `return`, and its return value must be JSON-serializable:

```python
import os

return {
    "cwd": os.getcwd(),
    "has_workspace": os.path.isdir("/workspace"),
}
```

```bash
python3 scripts/tmux_exec.py \
  --socket "$SOCKET" \
  --sessions remote-a \
  --python-file check_env.py
```

Choose exactly one of `--command` or `--python-file`. With `--show-output`, the script returns the tail of remote output rather than putting entire logs into the receipt.

### 3. Upload and download files

Upload a single file:

```bash
python3 scripts/transfer.py put \
  --socket "$SOCKET" \
  --sessions "$SESSIONS" \
  --source ./bundle.zip \
  --remote-path /workspace/bundle.zip
```

Source code, logs, JSON, and other text content usually benefit from compression:

```bash
python3 scripts/transfer.py put \
  --socket "$SOCKET" \
  --session remote-a \
  --source ./source.tar \
  --remote-path /workspace/source.tar \
  --compress
```

Download results:

```bash
python3 scripts/transfer.py get \
  --socket "$SOCKET" \
  --session remote-a \
  --remote-path /workspace/result.tar.gz \
  --dest ./returns/result.tar.gz
```

When multiple sessions download to the same destination, local filenames are prefixed with session names to avoid collisions. Overwriting an existing file requires explicit `--overwrite`.

Package directories locally into a single archive before transferring:

```bash
tar -czf bundle.tar.gz project/
python3 scripts/transfer.py put \
  --socket "$SOCKET" \
  --session remote-a \
  --source ./bundle.tar.gz \
  --remote-path /workspace/bundle.tar.gz
```

### 4. Launch and manage long-running jobs

Launch a job:

```bash
python3 scripts/remote_job.py launch \
  --socket "$SOCKET" \
  --sessions remote-a \
  --job-id train-001 \
  --job-root /workspace/job-state \
  --cwd /workspace/project \
  --command './train.sh'
```

Inspect status and log tails:

```bash
python3 scripts/remote_job.py status \
  --socket "$SOCKET" \
  --sessions remote-a \
  --job-id train-001 \
  --job-root /workspace/job-state
```

Stop the remote job:

```bash
python3 scripts/remote_job.py stop \
  --socket "$SOCKET" \
  --sessions remote-a \
  --job-id train-001 \
  --job-root /workspace/job-state
```

Collect the job directory:

```bash
python3 scripts/remote_job.py collect \
  --socket "$SOCKET" \
  --sessions remote-a \
  --job-id train-001 \
  --job-root /workspace/job-state
```

`collect` creates a remote job archive and returns its path, byte count, and SHA256. Download that path with `transfer.py get`.

Job states:

| State | Meaning |
|-------|---------|
| `RUNNING` | The process group still contains running processes, even if the main process has written an exit code |
| `SUCCEEDED` | The job exit code is `0` |
| `FAILED` | The job exit code is nonzero |
| `LOST` | The process is gone but no reliable exit code exists; inspect logs first |
| `MISSING` | The job directory does not exist, usually because `job-root` or `job-id` is incorrect |

By default, `launch` refuses to overwrite an existing job directory. Add `--reuse` only when reuse is explicitly intended.

If the pane is already inside a container, such as a persistent `docker exec -it <ctr> bash`, call `remote_job.py` directly **without** `--container` and keep the shell inside the container. The job runs as an ordinary shell job there; `status`, `stop`, and `collect` work as usual. This mode requires bash, Python 3.8+, and procps `ps` inside the container; BusyBox `ps` is unsupported. If a dependency is missing, `launch` fails without starting the job. When the container may be deleted and recreated, place `--job-root` on a persistent mounted directory.

Use `--container` only when the pane is on the host and jobs need to start or stop inside a container. Launch from the host shell; the script executes the command through the container runtime's `exec`. `--cwd` is a container directory, while `--job-root` is a host state directory:

```bash
python3 scripts/remote_job.py launch \
  --socket "$SOCKET" \
  --sessions remote-a \
  --job-id train-in-container \
  --job-root /var/tmp/tmux-job-state \
  --container trainer \
  --cwd /workspace/project \
  --command './train.sh'
```

Invoke `status`, `stop`, and `collect` from the host with the same host state directory. To stop the job, the script first stops the container using the runtime and container ID saved at launch, then confirms that no child processes remain running in the process group. The default runtime is `docker`; change it at launch with `--container-runtime`. Inspect older jobs that record only a container name from the host before taking action.

## Multi-session batch orchestration

Use `batch_sessions.py` for sequences of dependent actions such as container restarts, batch deployments, and environment checks across machines.

Each step declares its expected behavior:

| `expect` | Purpose | Key fields |
|----------|---------|------------|
| `marker` | Ordinary command; wait for a receipt and check its exit code | `command`, optional `allow_rc` |
| `disconnect` | Expected shell disappearance, such as exiting a container | Optional `command` |
| `ready` | Probe repeatedly until the environment is available | `command`, `ready_command` |
| `json` | Execute a Python function body and retain structured results | `python` |

Example container restart plan:

```json
{
  "sessions": ["remote-a", "remote-b", "remote-c"],
  "steps": [
    {
      "name": "exit-container",
      "command": "exit",
      "expect": "disconnect",
      "timeout_seconds": 30
    },
    {
      "name": "restart-container",
      "command": "docker restart trainer",
      "expect": "marker",
      "timeout_seconds": 300
    },
    {
      "name": "reenter-container",
      "command": "docker exec -it trainer /bin/bash",
      "expect": "ready",
      "ready_command": "test -d /workspace",
      "timeout_seconds": 180,
      "settle_seconds": 3
    },
    {
      "name": "verify-environment",
      "expect": "json",
      "python": "import os\nreturn {\"cwd\": os.getcwd(), \"has_workspace\": os.path.isdir(\"/workspace\")}"
    }
  ]
}
```

Start with a dry-run to check the steps and target sessions:

```bash
python3 scripts/batch_sessions.py \
  --socket "$SOCKET" \
  --plan restart-plan.json \
  --dry-run
```

Then execute:

```bash
python3 scripts/batch_sessions.py \
  --socket "$SOCKET" \
  --plan restart-plan.json
```

By default, the plan stops on failure to avoid advancing with inconsistent machine state. Use `--continue-on-failure` explicitly when healthy sessions should continue; failed sessions are excluded from later stages.

## File transfer protocol

Transfers use only the authenticated shell in the tmux session. They do not rely on `scp`, `rsync`, or another login.

Uploads:

1. Split the local file into fixed-size chunks, Base64-encode them, and wrap lines at 76 columns.
2. Disable remote echo and wait for `READY` before pasting data into the remote read loop.
3. Write each chunk to a remote staging file and return a receipt after decoding.
4. Verify SHA256 and byte counts remotely after all data arrives.
5. Rename the staging file atomically to the final path only after verification passes.

Downloads:

1. Confirm there is no existing `pipe-pane` logging pipe, acquire an output pipe, then trigger the remote read. If a pipe already exists, refuse the download and preserve the log.
2. Slice the remote file by byte range, compute each range's SHA256, and encode it for output.
3. Accept only valid Base64 lines locally and verify every range.
4. Verify the whole-file SHA256 and byte count after merging all ranges.
5. Rename to the destination path only after verification passes.

The default chunk size is `3 MiB`. Adjust it for the link:

```bash
python3 scripts/transfer.py put \
  --socket "$SOCKET" \
  --session remote-a \
  --source ./large.json \
  --remote-path /workspace/large.json \
  --compress \
  --chunk-bytes $((1024 * 1024))
```

Base64 adds about 33% overhead, and PTY line-by-line processing limits throughput. Accordingly:

- Package directories and many small files into one archive.
- Use `--compress` for source code, logs, and JSON.
- Do not recompress compressed archives, images, or model weights.
- Tune using the reported `throughput_mib_s` and `elapsed_seconds`.
- Do not drive multiple operations concurrently within the same session.

See [`references/transfer-protocol.md`](./references/transfer-protocol.md) for the full sequence, parameters, and failure semantics.

## Timeouts and failures

### `TIMEOUT` does not mean the command failed

A timeout means no receipt appeared within the time limit. The command may still be running remotely:

1. Inspect the pane read-only with `tmux -S SOCK capture-pane -p -t SESSION` and confirm the shell is idle. Exit the specific transfer read loop or interactive program if necessary.
2. After a timeout or terminated driver, an unfinished-operation marker blocks further writes. Once the pane has been inspected, explicitly recover with `tmux_exec.py --socket SOCK --sessions SESSION --recover-session --command 'stty echo; pwd' --show-output`.
3. After recovery, inspect processes, files, or logs with `tmux_exec.py`, or check job state with `remote_job.py status`, before deciding whether to retry.

Never resend without checking remote state, especially for training, deployment, or database changes.

### Common issues

| Symptom | Likely cause | First action |
|---------|--------------|--------------|
| `session not found on socket` | Wrong socket or session name | Check names with `tmux -S SOCK list-sessions` |
| `session has no panes` | The session remains but its panes are gone | Manually recreate a shell in the target session |
| `remote reader not ready` | The shell exited or is stuck in an interactive program | Exit the pager, editor, or password prompt before retrying |
| No valid download marker | Continuous background output interferes with the pane | Quiet the pane before retrying |
| `wire mismatch` | Corrupted transfer bytes or link | Staging is cleaned up; check the link and retransmit |
| `payload mismatch` | Decompressed content failed verification | The final path is untouched; check the source and retransmit |
| `LOST` | The process was killed externally or the machine restarted | Inspect full logs before rerunning |
| Lock wait timeout | Another driver is using the same session | Identify the owner; do not bypass the lock |

See [`references/troubleshooting.md`](./references/troubleshooting.md) for the investigation sequence and more examples.

## Repository layout

```text
tmux-ssh-skill/
├── SKILL.md                         # Skill entrypoint: triggers, rules, and workflow
├── README.md                        # English project overview and usage guide
├── README.zh-CN.md                  # Chinese project overview and usage guide
├── references/
│   ├── plan-examples.md             # Batch plans, container restarts, and deployment
│   ├── transfer-protocol.md         # Base64 chunked protocol and performance
│   └── troubleshooting.md           # Session, execution, transfer, and job failures
├── scripts/
│   ├── batch_sessions.py            # Multi-session stage orchestrator
│   ├── remote_job.py                # Long-running remote job manager
│   ├── session_preflight.py         # Session preflight checks
│   ├── tmux_exec.py                 # Remote commands and structured Python execution
│   ├── tmuxlib.py                   # Shared tmux, receipt, lock, and execution helpers
│   └── transfer.py                  # File transfer over tmux PTYs
└── tests/
    └── test_workflow.py             # End-to-end regression checks with disposable tmux servers
```

## Common workflows

### Running remote code

```text
1. Check the working directory and python3 with preflight
2. Upload an archive with transfer put
3. Unpack and check dependencies with tmux_exec
4. Launch the job with remote_job launch
5. Poll with remote_job status
6. Collect results with remote_job collect and transfer get
```

### Resuming after a container restart

```text
1. Exit the container with a batch_sessions disconnect step
2. Restart the container on the host
3. Use ready to probe availability after reentry
4. Rediscover the pane and check the environment
```

### Checking consistency across machines

```text
1. Select targets with sessions
2. Run the same check with tmux_exec
3. Retrieve each machine's output tail with --show-output
4. Identify failing sessions from problem_sessions in the JSON result
```

## Reference documentation

- [`SKILL.md`](./SKILL.md): invocation scope, core rules, and standard workflow.
- [`references/plan-examples.md`](./references/plan-examples.md): batch plan structure, container restarts, and deployment examples.
- [`references/transfer-protocol.md`](./references/transfer-protocol.md): transfer sequence, performance parameters, and failure semantics.
- [`references/troubleshooting.md`](./references/troubleshooting.md): common failures and investigation order.

The skill entrypoint and references are maintained in English. Project documentation and usage examples are available in English and Chinese.

## Development and validation

Check Python syntax after changing scripts or documentation:

```bash
python3 -m py_compile scripts/*.py
```

### Tests

Existing regression tests use only the standard-library `unittest` module. Run from the repository root on Linux, macOS, or WSL:

```bash
python3 -m unittest discover -s tests -v
```

Each case starts a disposable tmux server on a temporary socket, uses `bash --norc --noprofile` to simulate the remote shell, invokes the real scripts, and checks JSON output. It closes only that server and cleans up temporary files afterward. The suite skips automatically when tmux is unavailable; a full run takes about 1–2 minutes.

After changing a batch plan, review it with `--dry-run` before sending any steps that restart containers, exit shells, or delete files.

Commands execute in the actual remote environment. Check the socket, session names, remote working directory, container names, and paths before use. Review commands involving `rm`, restarts, service stops, or overwrites carefully.

## Contributing

Issues and pull requests are welcome for documentation, transfer reliability, job state handling, and batch orchestration.

When submitting changes, check that:

- Behavior described in `SKILL.md` matches the scripts.
- Both README versions stay in sync and their command arguments remain valid.
- New scripts use only the Python standard library, or any additional dependencies are documented.
- Protocol or failure-semantics changes are reflected in `references/transfer-protocol.md` and `references/troubleshooting.md`.
- `python3 -m py_compile scripts/*.py` passes, and destructive remote plans are reviewed with `--dry-run` first.
