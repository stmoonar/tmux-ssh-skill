#!/usr/bin/env python3
"""Run one command in one or more tmux sessions and read back a receipt.

Usage:
  tmux_exec.py --socket PATH --sessions a,b --command 'ls -l' [--timeout 60]
  tmux_exec.py --socket PATH --sessions a --python-file body.py
  tmux_exec.py --socket PATH --sessions a --command 'tail -n 5 log' --show-output

Sessions run in parallel; operations inside one session hold that session's
lock. A timeout is reported as TIMEOUT, never as a failure, and the command is
never resent automatically.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

from tmuxlib import (
    RemoteCommandError,
    RemoteTimeout,
    Tmux,
    TmuxError,
    emit,
    local_path,
    posix_arg,
    run_python,
    run_shell,
    session_lock,
)


def exec_one(args: argparse.Namespace, session: str, body: str | None) -> dict:
    tmux = Tmux(args.socket)
    row: dict = {"session": session, "status": "FAIL"}
    try:
        with session_lock(args.socket, session, timeout=args.lock_timeout):
            pane = tmux.active_pane(session)
            row["pane"] = pane
            if body is not None:
                row["result"] = run_python(
                    tmux, pane, body, timeout=args.timeout, python=args.python
                )
                row["status"] = "PASS"
            else:
                outcome = run_shell(
                    tmux,
                    pane,
                    args.command,
                    timeout=args.timeout,
                    capture_output=args.show_output,
                )
                row.update(outcome)
                row["status"] = "PASS" if outcome["rc"] == 0 else "FAIL"
                if args.show_output and outcome.get("remote_output_path"):
                    tail = run_python(
                        tmux,
                        pane,
                        "from pathlib import Path\n"
                        f"p=Path({outcome['remote_output_path']!r})\n"
                        "text=p.read_text(errors='replace') if p.exists() else ''\n"
                        "p.unlink(missing_ok=True)\n"
                        f"return text.splitlines()[-{args.output_lines}:]",
                        timeout=args.timeout,
                        python=args.python,
                    )
                    row["output"] = tail
    except RemoteTimeout as error:
        row["status"] = "TIMEOUT"
        row["error"] = str(error)
    except (TmuxError, RemoteCommandError) as error:
        row["error"] = str(error)
    except Exception as error:  # noqa: BLE001
        row["error"] = f"{type(error).__name__}: {error}"
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=posix_arg)
    parser.add_argument("--sessions", required=True)
    parser.add_argument("--command")
    parser.add_argument("--python-file", type=local_path, help="file holding a Python body ending in return")
    parser.add_argument("--python", default="python3", help="remote interpreter for --python-file")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--lock-timeout", type=float, default=900.0)
    parser.add_argument("--show-output", action="store_true", help="capture stdout/stderr tail")
    parser.add_argument("--output-lines", type=int, default=40)
    parser.add_argument("--max-parallel", type=int, default=8)
    args = parser.parse_args()

    if bool(args.command) == bool(args.python_file):
        parser.error("provide exactly one of --command or --python-file")

    body = args.python_file.read_text(encoding="utf-8-sig") if args.python_file else None
    sessions = [name.strip() for name in args.sessions.split(",") if name.strip()]
    if not sessions:
        parser.error("--sessions must name at least one session")

    workers = max(1, min(args.max_parallel, len(sessions)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda name: exec_one(args, name, body), sessions))

    bad = [row["session"] for row in rows if row["status"] != "PASS"]
    emit({
        "action": "exec",
        "socket": args.socket,
        "status": "PASS" if not bad else "FAIL",
        "problem_sessions": bad,
        "results": rows,
    })
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
