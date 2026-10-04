#!/usr/bin/env python3
"""Check that every target tmux session is usable before doing real work.

Usage:
  session_preflight.py --socket PATH --sessions a,b,c [--timeout 30]
                       [--expect-host-contains TEXT]
                       [--expect-cwd-prefix PATH]
                       [--require-command CMD]
                       [--expect-in-container | --expect-host]

Reports one row per session with the active pane, remote host, user and cwd.
Exit code 0 only when every session passes.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

from tmuxlib import (
    CONTAINER_MARKERS,
    Tmux,
    TmuxError,
    emit,
    posix_arg,
    probe_session,
    run_shell,
    session_lock,
)


def check_one(args: argparse.Namespace, session: str) -> dict:
    tmux = Tmux(args.socket)
    row: dict = {"session": session, "status": "FAIL"}
    try:
        with session_lock(args.socket, session, timeout=args.lock_timeout):
            info = probe_session(tmux, session, timeout=args.timeout,
                                 container_markers=CONTAINER_MARKERS + tuple(args.container_marker))
            row.update(info)
            problems = []
            if args.expect_host_contains and args.expect_host_contains not in (info["host"] or ""):
                problems.append(
                    f"host {info['host']!r} does not contain {args.expect_host_contains!r}"
                )
            if args.expect_cwd_prefix and not (info["cwd"] or "").startswith(args.expect_cwd_prefix):
                problems.append(
                    f"cwd {info['cwd']!r} is not under {args.expect_cwd_prefix!r}"
                )
            # A restarted container drops a `docker exec` pane back to the
            # host shell, where later commands would silently land.
            if args.expect_in_container and not info["in_container"]:
                problems.append(f"shell is not inside a container (host {info['host']!r})")
            if args.expect_host and info["in_container"]:
                problems.append(f"shell is inside a container: {', '.join(info['container_hints'])}")
            for command in args.require_command:
                probe = run_shell(
                    tmux,
                    info["pane"],
                    f"command -v {command} >/dev/null 2>&1",
                    timeout=args.timeout,
                )
                if probe["rc"] != 0:
                    problems.append(f"missing remote command: {command}")
            row["problems"] = problems
            row["status"] = "PASS" if not problems else "FAIL"
    except TmuxError as error:
        row["problems"] = [str(error)]
    except Exception as error:  # noqa: BLE001 - surfaced per session, never fatal
        row["problems"] = [f"{type(error).__name__}: {error}"]
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=posix_arg)
    parser.add_argument("--sessions", required=True, help="comma separated tmux session names")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--lock-timeout", type=float, default=300.0)
    parser.add_argument("--expect-host-contains")
    parser.add_argument("--expect-cwd-prefix", type=posix_arg)
    parser.add_argument("--require-command", action="append", default=[])
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--expect-in-container", action="store_true",
                       help="fail unless the shell runs inside a container")
    where.add_argument("--expect-host", action="store_true",
                       help="fail if the shell runs inside a container")
    parser.add_argument("--container-marker", action="append", default=[], type=posix_arg,
                        help="extra remote file whose presence marks a container")
    parser.add_argument("--max-parallel", type=int, default=8)
    args = parser.parse_args()

    sessions = [name.strip() for name in args.sessions.split(",") if name.strip()]
    if not sessions:
        parser.error("--sessions must name at least one session")

    tmux = Tmux(args.socket)
    known = tmux.list_sessions()
    workers = max(1, min(args.max_parallel, len(sessions)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda name: check_one(args, name), sessions))

    failed = [row["session"] for row in rows if row["status"] != "PASS"]
    emit({
        "action": "preflight",
        "socket": args.socket,
        "sessions_on_socket": known,
        "status": "PASS" if not failed else "FAIL",
        "failed_sessions": failed,
        "results": rows,
    })
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
