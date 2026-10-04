#!/usr/bin/env python3
"""Start, inspect, stop and collect long-running jobs on the remote side.

State lives in a remote job directory, not in tmux scrollback, so a local
process can exit and later reattach through the same session:

  <job-root>/<job_id>/
      meta.json     command, cwd, pid, pgid, container, start time
      stdout.log    merged stdout and stderr
      rc            exit code, written when the job finishes

Subcommands:
  launch   nohup a setsid'd process group and record its handles
  status   report running/exited plus a log tail
  stop     signal the remote process group (TERM then KILL) or stop a container
  collect  tar the job directory remotely and report the archive path

`stop` never touches tmux itself: kill-session, kill-window and kill-pane are
deliberately absent so the working session survives for inspection.
"""
from __future__ import annotations

import argparse
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor

from tmuxlib import (
    RemoteCommandError,
    RemoteTimeout,
    Tmux,
    TmuxError,
    emit,
    posix_arg,
    run_python,
    session_lock,
)


def _job_dir(root: str, job_id: str) -> str:
    return f"{root.rstrip('/')}/{job_id}"


def launch(args: argparse.Namespace, session: str) -> dict:
    tmux = Tmux(args.socket)
    row: dict = {"session": session, "action": "launch", "job_id": args.job_id, "status": "FAIL"}
    job_dir = _job_dir(args.job_root, args.job_id)
    try:
        with session_lock(args.socket, session, timeout=args.lock_timeout):
            pane = tmux.active_pane(session)
            row["pane"] = pane
            result = run_python(
                tmux,
                pane,
                "import json,os,subprocess,time\n"
                "from pathlib import Path\n"
                f"job=Path({job_dir!r})\n"
                f"if job.exists() and not {args.reuse!r}:\n"
                "    raise SystemError('job directory already exists: '+str(job))\n"
                "job.mkdir(parents=True, exist_ok=True)\n"
                "log=job/'stdout.log'\n"
                f"cwd={args.cwd!r} or str(job)\n"
                "rc=job/'rc'\n"
                "rc.unlink(missing_ok=True)\n"
                f"inner={args.command!r}\n"
                "wrapper=('cd '+cwd+' && { '+inner+' ; } >> '+str(log)+' 2>&1; "
                "echo $? > '+str(rc))\n"
                # start_new_session gives the child its own process group without
                # depending on setsid, which is absent on macOS.
                "proc=subprocess.Popen(['bash','-lc',wrapper],\n"
                "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
                "    stderr=subprocess.DEVNULL, cwd=cwd, start_new_session=True)\n"
                "time.sleep(0.5)\n"
                "try:\n"
                "    pgid=os.getpgid(proc.pid)\n"
                "except Exception:\n"
                "    pgid=proc.pid\n"
                "meta={'job_id':"
                f"{args.job_id!r},'command':inner,'cwd':cwd,'pid':proc.pid,"
                "'pgid':pgid,'log':str(log),'rc_file':str(rc),"
                f"'container':{args.container!r},'started_at':time.time()}}\n"
                "(job/'meta.json').write_text(json.dumps(meta,indent=2))\n"
                "return meta",
                timeout=args.timeout,
            )
            row["meta"] = result
            row["status"] = "PASS"
    except RemoteTimeout as error:
        row["status"] = "TIMEOUT"
        row["error"] = str(error)
    except (TmuxError, RemoteCommandError) as error:
        row["error"] = str(error)
    except Exception as error:  # noqa: BLE001
        row["error"] = f"{type(error).__name__}: {error}"
    return row


def status(args: argparse.Namespace, session: str) -> dict:
    tmux = Tmux(args.socket)
    row: dict = {"session": session, "action": "status", "job_id": args.job_id, "status": "FAIL"}
    job_dir = _job_dir(args.job_root, args.job_id)
    try:
        with session_lock(args.socket, session, timeout=args.lock_timeout):
            pane = tmux.active_pane(session)
            row["pane"] = pane
            result = run_python(
                tmux,
                pane,
                "import json,os,signal\n"
                "from pathlib import Path\n"
                f"job=Path({job_dir!r})\n"
                "if not job.exists():\n"
                "    return {'state':'MISSING','job_dir':str(job)}\n"
                "meta=json.loads((job/'meta.json').read_text()) if (job/'meta.json').exists() else {}\n"
                "rc_file=job/'rc'\n"
                "rc=rc_file.read_text().strip() if rc_file.exists() else None\n"
                "alive=False\n"
                "pid=meta.get('pid')\n"
                "if pid:\n"
                "    try:\n"
                "        os.kill(int(pid),0)\n"
                "        alive=True\n"
                "    except Exception:\n"
                "        alive=False\n"
                "log=job/'stdout.log'\n"
                "tail=[]\n"
                "size=0\n"
                "if log.exists():\n"
                "    size=log.stat().st_size\n"
                f"    tail=log.read_text(errors='replace').splitlines()[-{args.log_lines}:]\n"
                "if rc is not None:\n"
                "    state='SUCCEEDED' if rc=='0' else 'FAILED'\n"
                "elif alive:\n"
                "    state='RUNNING'\n"
                "else:\n"
                "    state='LOST'\n"
                "return {'state':state,'rc':rc,'alive':alive,'meta':meta,\n"
                "    'log_bytes':size,'tail':tail,'job_dir':str(job)}",
                timeout=args.timeout,
            )
            row["job"] = result
            row["state"] = result.get("state")
            row["status"] = "PASS"
    except RemoteTimeout as error:
        row["status"] = "TIMEOUT"
        row["error"] = str(error)
    except (TmuxError, RemoteCommandError) as error:
        row["error"] = str(error)
    except Exception as error:  # noqa: BLE001
        row["error"] = f"{type(error).__name__}: {error}"
    return row


def stop(args: argparse.Namespace, session: str) -> dict:
    tmux = Tmux(args.socket)
    row: dict = {"session": session, "action": "stop", "job_id": args.job_id, "status": "FAIL"}
    job_dir = _job_dir(args.job_root, args.job_id)
    try:
        with session_lock(args.socket, session, timeout=args.lock_timeout):
            pane = tmux.active_pane(session)
            row["pane"] = pane
            result = run_python(
                tmux,
                pane,
                "import json,os,signal,subprocess,time\n"
                "from pathlib import Path\n"
                f"job=Path({job_dir!r})\n"
                "if not job.exists():\n"
                "    raise SystemError('job directory missing: '+str(job))\n"
                "meta=json.loads((job/'meta.json').read_text())\n"
                "notes=[]\n"
                "container=meta.get('container')\n"
                f"runtime={args.container_runtime!r}\n"
                "if container:\n"
                "    done=subprocess.run([runtime,'stop',container],\n"
                "        text=True,capture_output=True)\n"
                "    notes.append('container stop rc=%s %s'%(done.returncode,\n"
                "        (done.stderr or '').strip()[:200]))\n"
                "pgid=meta.get('pgid')\n"
                "pid=meta.get('pid')\n"
                "def alive():\n"
                "    if not pid:\n"
                "        return False\n"
                "    try:\n"
                "        os.kill(int(pid),0)\n"
                "        return True\n"
                "    except Exception:\n"
                "        return False\n"
                "signalled=[]\n"
                "if pgid and alive():\n"
                "    try:\n"
                "        os.killpg(int(pgid),signal.SIGTERM)\n"
                "        signalled.append('TERM')\n"
                "    except Exception as err:\n"
                "        notes.append('TERM failed: %s'%err)\n"
                f"    deadline=time.time()+{args.grace_seconds!r}\n"
                "    while time.time()<deadline and alive():\n"
                "        time.sleep(1)\n"
                "    if alive():\n"
                "        try:\n"
                "            os.killpg(int(pgid),signal.SIGKILL)\n"
                "            signalled.append('KILL')\n"
                "        except Exception as err:\n"
                "            notes.append('KILL failed: %s'%err)\n"
                "        time.sleep(1)\n"
                "rc_file=job/'rc'\n"
                "rc=rc_file.read_text().strip() if rc_file.exists() else None\n"
                "return {'signals':signalled,'still_alive':alive(),'rc':rc,\n"
                "    'notes':notes,'pid':pid,'pgid':pgid,'container':container}",
                timeout=max(args.timeout, args.grace_seconds + 60),
            )
            row["result"] = result
            row["status"] = "PASS" if not result.get("still_alive") else "FAIL"
            if result.get("still_alive"):
                row["error"] = "remote job still alive after TERM and KILL"
    except RemoteTimeout as error:
        row["status"] = "TIMEOUT"
        row["error"] = str(error)
    except (TmuxError, RemoteCommandError) as error:
        row["error"] = str(error)
    except Exception as error:  # noqa: BLE001
        row["error"] = f"{type(error).__name__}: {error}"
    return row


def collect(args: argparse.Namespace, session: str) -> dict:
    tmux = Tmux(args.socket)
    row: dict = {"session": session, "action": "collect", "job_id": args.job_id, "status": "FAIL"}
    job_dir = _job_dir(args.job_root, args.job_id)
    try:
        with session_lock(args.socket, session, timeout=args.lock_timeout):
            pane = tmux.active_pane(session)
            row["pane"] = pane
            result = run_python(
                tmux,
                pane,
                "import hashlib,tarfile\n"
                "from pathlib import Path\n"
                f"job=Path({job_dir!r})\n"
                "if not job.exists():\n"
                "    raise SystemError('job directory missing: '+str(job))\n"
                f"archive=Path({args.archive_path!r}) if {args.archive_path!r} else job.parent/(job.name+'.tar.gz')\n"
                "archive.parent.mkdir(parents=True, exist_ok=True)\n"
                "archive.unlink(missing_ok=True)\n"
                "with tarfile.open(archive,'w:gz') as tar:\n"
                "    for item in sorted(job.rglob('*')):\n"
                "        if item.is_file():\n"
                "            tar.add(item, arcname=str(item.relative_to(job)), recursive=False)\n"
                "h=hashlib.sha256()\n"
                "with archive.open('rb') as fh:\n"
                "    for blk in iter(lambda: fh.read(1048576), b''):\n"
                "        h.update(blk)\n"
                "return {'archive':str(archive),'size':archive.stat().st_size,\n"
                "    'sha256':h.hexdigest()}",
                timeout=max(args.timeout, 600.0),
            )
            row["archive"] = result
            row["status"] = "PASS"
    except RemoteTimeout as error:
        row["status"] = "TIMEOUT"
        row["error"] = str(error)
    except (TmuxError, RemoteCommandError) as error:
        row["error"] = str(error)
    except Exception as error:  # noqa: BLE001
        row["error"] = f"{type(error).__name__}: {error}"
    return row


HANDLERS = {"launch": launch, "status": status, "stop": stop, "collect": collect}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=sorted(HANDLERS))
    parser.add_argument("--socket", required=True, type=posix_arg)
    parser.add_argument("--sessions", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--job-root", required=True, type=posix_arg, help="absolute remote directory for job state")
    parser.add_argument("--command", help="remote command for launch")
    parser.add_argument("--cwd", default="", type=posix_arg, help="remote working directory")
    parser.add_argument("--container", default="", help="container name or id to stop with the job")
    parser.add_argument("--container-runtime", default="docker")
    parser.add_argument("--grace-seconds", type=float, default=30.0)
    parser.add_argument("--archive-path", default="", type=posix_arg)
    parser.add_argument("--log-lines", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--lock-timeout", type=float, default=900.0)
    parser.add_argument("--reuse", action="store_true", help="allow an existing job directory")
    parser.add_argument("--max-parallel", type=int, default=8)
    args = parser.parse_args()

    if args.mode == "launch" and not args.command:
        parser.error("launch requires --command")
    if not args.job_root.startswith("/"):
        parser.error("--job-root must be absolute")

    sessions = [name.strip() for name in args.sessions.split(",") if name.strip()]
    if not sessions:
        parser.error("--sessions must name at least one session")

    handler = HANDLERS[args.mode]
    workers = max(1, min(args.max_parallel, len(sessions)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda name: handler(args, name), sessions))

    bad = [row["session"] for row in rows if row["status"] != "PASS"]
    emit({
        "action": args.mode,
        "socket": args.socket,
        "job_id": args.job_id,
        "status": "PASS" if not bad else "FAIL",
        "problem_sessions": bad,
        "results": rows,
    })
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
