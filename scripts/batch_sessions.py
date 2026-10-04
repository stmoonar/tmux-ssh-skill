#!/usr/bin/env python3
"""Drive an ordered plan across many tmux sessions with stage barriers.

Every step runs on all sessions in parallel; the next step starts only after
each session finished the current one. That barrier is what makes fleet-wide
"exit all containers, restart them, re-enter, then probe" safe.

Each step declares what it expects:

  marker              a normal command that must report rc=0
  disconnect          the shell is expected to die (container restart, exit)
  ready               wait until a probe command succeeds again, retrying
  json                run a Python body and keep its parsed result

Plan file (JSON):
{
  "sessions": ["remote69", "remote112"],
  "steps": [
    {"name": "exit-container",    "command": "exit",              "expect": "disconnect"},
    {"name": "restart-container", "command": "docker restart foo", "expect": "marker",
     "timeout_seconds": 300},
    {"name": "reenter",           "command": "docker exec -it foo bash",
     "expect": "ready", "ready_command": "test -d /workspace",
     "timeout_seconds": 180},
    {"name": "probe",             "command": "cat /etc/hostname",  "expect": "marker"}
  ]
}

Usage:
  batch_sessions.py --socket PATH --plan plan.json [--sessions a,b] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from tmuxlib import (
    RemoteTimeout,
    Tmux,
    TmuxError,
    emit,
    local_path,
    new_marker,
    posix_arg,
    run_python,
    run_shell,
    session_lock,
    wait_for_marker,
)

VALID_EXPECT = {"marker", "disconnect", "ready", "json"}


def _shell_identity(tmux: Tmux, session: str, timeout: float = 10.0) -> dict:
    """Fingerprint the shell; an unanswered probe remains an unknown result.

    tmux only tracks the outermost pane process, so a nested shell exiting or a
    container shell dying leaves `pane_pid` unchanged. Asking the live shell for
    its own pid detects those transitions. A timeout is never proof of exit.
    """
    pane = tmux.active_pane(session)
    info = tmux.pane_info(pane)
    marker = new_marker("ID")
    tmux.send_line(pane, f"printf '\\n%s pid=%s\\n' {marker} $$")
    row = wait_for_marker(tmux, pane, marker, timeout=timeout, poll_interval=0.3,
                          capture_lines=40)
    fields = dict(part.split("=", 1) for part in row.split() if "=" in part)
    if not (fields.get("pid") or "").isdigit():
        raise TmuxError("shell identity probe returned no valid PID")
    return {"pane": pane, "pane_pid": info["pane_pid"],
            "shell_pid": fields["pid"], "current_command": info["current_command"]}


def _await_disconnect(
    tmux: Tmux,
    session: str,
    pane: str,
    timeout: float,
    before: dict,
) -> dict:
    """Wait until the shell that served the pane is gone.

    A restart that kills the SSH or container shell is a success here, not a
    failure, so nothing is ever resent into a dead shell.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1.0)
        # A missing server/socket is not evidence of remote shell exit.
        current_pane = tmux.active_pane(session)
        info = tmux.pane_info(current_pane)
        if current_pane != pane:
            raise TmuxError("active pane changed; cannot infer whether the original shell exited")
        if info["dead"]:
            return {"disconnected": True, "reason": "pane reported dead"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        after = _shell_identity(tmux, session, timeout=min(10.0, remaining))
        if after["shell_pid"] and after["shell_pid"] != before["shell_pid"]:
            return {
                "disconnected": True,
                "reason": "shell pid changed",
                "from_shell_pid": before["shell_pid"],
                "to_shell_pid": after["shell_pid"],
            }
        if after["pane_pid"] != before["pane_pid"]:
            return {"disconnected": True, "reason": "pane pid changed"}
    raise RemoteTimeout(f"shell exit was not confirmed within {timeout:.0f}s; inspect before retrying")


def _await_ready(
    tmux: Tmux,
    session: str,
    ready_command: str,
    timeout: float,
    interval: float,
) -> dict:
    deadline = time.monotonic() + timeout
    attempts = 0
    last = ""
    while time.monotonic() < deadline:
        attempts += 1
        try:
            pane = tmux.active_pane(session)
        except TmuxError as error:
            last = str(error)
        else:
            # Retry only a completed nonzero probe. An unknown receipt propagates
            # as TIMEOUT and must never enqueue another copy of the command.
            remaining = deadline - time.monotonic()
            outcome = run_shell(tmux, pane, ready_command, timeout=max(0.01, min(30.0, remaining)))
            if outcome["rc"] == 0:
                return {"ready": True, "attempts": attempts, "pane": pane}
            last = f"rc={outcome['rc']}"
        time.sleep(max(0.0, min(interval, deadline - time.monotonic())))
    return {"ready": False, "attempts": attempts, "last": last}


def run_step(args: argparse.Namespace, step: dict, session: str) -> dict:
    tmux = Tmux(args.socket)
    expect = step.get("expect", "marker")
    timeout = float(step.get("timeout_seconds", args.default_timeout))
    row: dict = {"session": session, "step": step.get("name", "unnamed"), "expect": expect,
                 "status": "FAIL"}
    started = time.monotonic()
    try:
        with session_lock(args.socket, session, timeout=args.lock_timeout):
            pane = tmux.active_pane(session)
            row["pane"] = pane
            command = step.get("command", "")

            if expect == "disconnect":
                # Fingerprint the shell before it is asked to leave, otherwise
                # there is no baseline left to compare against.
                baseline = _shell_identity(tmux, session)
                if command:
                    # Send without waiting for a receipt: the shell is going away.
                    tmux.send_line(pane, command)
                outcome = _await_disconnect(tmux, session, pane, timeout, baseline)
                row.update(outcome)
                row["status"] = "PASS" if outcome["disconnected"] else "FAIL"

            elif expect == "ready":
                ready_command = step.get("ready_command")
                if not ready_command:
                    raise ValueError("a 'ready' step needs ready_command")
                if command:
                    tmux.send_line(pane, command)
                    time.sleep(float(step.get("settle_seconds", 2.0)))
                outcome = _await_ready(
                    tmux, session, ready_command, timeout,
                    float(step.get("poll_seconds", 3.0)),
                )
                row.update(outcome)
                row["status"] = "PASS" if outcome["ready"] else "FAIL"

            elif expect == "json":
                body = step.get("python")
                if not body:
                    raise ValueError("a 'json' step needs a python body")
                row["result"] = run_python(tmux, pane, body, timeout=timeout)
                row["status"] = "PASS"

            else:
                if not command:
                    raise ValueError("a 'marker' step needs command")
                outcome = run_shell(tmux, pane, command, timeout=timeout)
                row["rc"] = outcome["rc"]
                allowed = step.get("allow_rc", [0])
                row["status"] = "PASS" if outcome["rc"] in allowed else "FAIL"
    except RemoteTimeout as error:
        row["status"] = "TIMEOUT"
        row["error"] = str(error)
    except Exception as error:  # noqa: BLE001
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        row["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=posix_arg)
    parser.add_argument("--plan", type=local_path, required=True)
    parser.add_argument("--sessions", help="override the plan's session list")
    parser.add_argument("--default-timeout", type=float, default=120.0)
    parser.add_argument("--lock-timeout", type=float, default=1800.0)
    parser.add_argument("--max-parallel", type=int, default=8)
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    sessions = (
        [name.strip() for name in args.sessions.split(",") if name.strip()]
        if args.sessions
        else list(plan.get("sessions", []))
    )
    steps = list(plan.get("steps", []))
    if not sessions:
        parser.error("no sessions given in the plan or on the command line")
    if not steps:
        parser.error("the plan has no steps")
    for step in steps:
        expect = step.get("expect", "marker")
        if expect not in VALID_EXPECT:
            parser.error(f"step {step.get('name')!r} has unknown expect {expect!r}")

    if args.dry_run:
        emit({
            "action": "batch",
            "dry_run": True,
            "sessions": sessions,
            "steps": [
                {"name": s.get("name"), "expect": s.get("expect", "marker"),
                 "command": s.get("command", "")}
                for s in steps
            ],
        })
        return 0

    workers = max(1, min(args.max_parallel, len(sessions)))
    stages: list[dict] = []
    active = list(sessions)
    overall = "PASS"

    for step in steps:
        if not active:
            break
        with ThreadPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(lambda name: run_step(args, step, name), active))
        failed = [row["session"] for row in rows if row["status"] != "PASS"]
        stages.append({
            "step": step.get("name", "unnamed"),
            "expect": step.get("expect", "marker"),
            "status": "PASS" if not failed else "FAIL",
            "failed_sessions": failed,
            "results": rows,
        })
        if failed:
            overall = "FAIL"
            if not args.continue_on_failure:
                break
            # Barrier semantics: drop broken sessions, keep the fleet moving.
            active = [name for name in active if name not in failed]

    emit({
        "action": "batch",
        "socket": args.socket,
        "sessions": sessions,
        "status": overall,
        "stages": stages,
    })
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
