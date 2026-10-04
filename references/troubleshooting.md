# Troubleshooting

Contents: sessions and channels / execution receipts / transfers / remote jobs / disconnect and reentry / investigation order

## Sessions and channels

| Symptom | Cause | Action |
|---|---|---|
| `session not found on socket` | Wrong session name or socket | Check actual names with `tmux -S SOCK list-sessions` |
| `session has no panes` | The session remains but its panes are gone | Manually recreate a usable shell in that session |
| Lock wait timeout | Another driver is using the same session | Identify the owner; do not bypass the lock to operate on the same PTY concurrently |
| `unfinished or timed-out operation` | The previous operation timed out or its driver was terminated | Inspect the pane read-only, confirm the shell is idle, then recover with `tmux_exec.py --recover-session` |
| Command reaches the wrong environment | The pane is at an unexpected shell level | Run `session_preflight.py` with `--expect-cwd-prefix` first |

## Execution receipts

**No receipt appears.** The pane may be inside an interactive program such as a pager, editor, or password prompt. The command becomes input to that program and never produces a receipt. Inspect the pane and exit the interactive program before continuing.

**Interpreting timeouts.** `TIMEOUT` means the receipt is not visible yet, not that the command failed. The command may still be running. Inspect remote processes, files, and logs before considering a retry; resending can duplicate execution.

**Why receipts occupy their own line.** A PTY echoes sent commands, including literal markers in command text. The protocol requires real receipts to begin at the start of a line and contain valid JSON or expanded `key=value` fields. This distinguishes echoed input from actual results. Do not manually print skill markers in custom commands.

**Remote Python errors.** Exceptions return a full traceback. `--python-file` must contain a function body ending with `return`, and the return value must be JSON-serializable. Returning a `set` or file object fails.

## Transfers

**Upload stops at `remote reader not ready`.** The remote shell did not enter its read loop. It may have exited, be stuck in an interactive program, or lack `stty`.

**Download parsing finds no data.** The captured output has no valid start marker, often because continuous background output is interfering. Quiet the pane before transferring.

**An output pipe already exists.** Downloads refuse to replace an existing `pipe-pane` log. Use a pane without an output pipe; do not disable logging of unknown origin.

**Verification fails.** `wire mismatch` indicates corrupted wire data; `payload mismatch` indicates incorrect decompressed content. Both clean up staging files and leave the final path untouched, so retransmit the file.

**Poor throughput.** Base64 adds about 33% overhead, and PTY line-by-line processing is slow. Check `throughput_mib_s`, then consider `--compress` for text, packaging directories, adjusting `--chunk-bytes`, or parallel transfers across sessions. Do not expect native file-channel throughput.

**A container path is inaccessible.** If a path is visible only inside a nested environment, first move the file to a location directly accessible to the current shell, then transfer it.

## Remote jobs

| State | Meaning | Action |
|---|---|---|
| `RUNNING` | The process group still contains running processes | Continue polling |
| `SUCCEEDED` | Exit code is 0 | Collect results |
| `FAILED` | Exit code is nonzero | Inspect `tail` and full logs |
| `LOST` | The process is gone but no exit code exists | It may have been killed externally or the machine restarted; inspect logs before rerunning |
| `MISSING` | The job directory does not exist | Check `job-root` and `job-id`, or whether the directory was cleaned up |

**Duplicate job names.** `launch` rejects an existing job directory to preserve historical evidence. Add `--reuse` explicitly only when reuse is intended.

**Stopping has no effect.** `stop` sends `TERM` to the process group, then `KILL` after a grace period. A remaining `still_alive` result usually indicates an uninterruptible process or insufficient permissions and requires manual intervention.

**Container jobs.** Jobs launched with `--container` start from the host shell. Their state directory lives on the host, while commands execute inside the container through the runtime's `exec`. Invoke subsequent operations from the host too. Older jobs recording only a container name cannot be stopped automatically; verify the actual container and processes on the host. For a pane already inside a container, use ordinary shell jobs without `--container`, as described in `SKILL.md`.

**Why exit codes remain reliable.** A launch wrapper writes the actual exit code to an `rc` file. After a local disconnect or reconnect, the result remains readable without relying on a local process staying alive.

**Never kill the session.** `stop` affects only the remote process group or container. Preserve tmux sessions so the environment remains available for investigation and reentry.

## Disconnect and reentry

**Using `marker` where `disconnect` is required.** Exiting a container or restarting SSH can make the shell disappear, so waiting for a receipt times out. Declare these steps with `expect: disconnect`.

**The shell is reported as still alive.** Its identity has not changed. Confirm that the command actually exits the shell; `docker restart` on the host does not kill the host shell.

**A probe receives no answer.** The result is `TIMEOUT`, not a successful disconnect. The probe is not resent automatically. Inspect the pane first; changing the active pane does not prove the original shell exited.

**Failure immediately after reentry.** The container is running but its services are not ready. Set `ready_command` to a check that represents actual availability, such as a required directory, process, or port, and allow enough `timeout_seconds`.

## Investigation order

Follow this sequence; the first two steps often locate the problem:

1. Confirm the session exists with `tmux -S SOCK list-sessions`.
2. Use `session_preflight.py` to confirm the shell environment, user, and directory match expectations.
3. Run a minimal command such as `echo ok` with `tmux_exec.py` to check the channel.
4. Investigate the specific failing operation: transfer, job management, or orchestration.
5. Keep distinguishing a missing receipt from a failed operation; they require different responses.
