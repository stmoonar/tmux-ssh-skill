#!/usr/bin/env python3
"""Start, inspect, stop and collect long-running jobs on the remote side.

State lives in a remote job directory, not in tmux scrollback, so a local
process can exit and later reattach through the same session:

  <job-root>/<job_id>/
      meta.json     command, cwd, pid, pgid, container, start time
      stdout.log    merged stdout and stderr
      rc            exit code, written when the job finishes

Subcommands:
  launch   start a detached process group and record its handles
  status   report running/exited plus a log tail
  stop     signal the remote process group (TERM then KILL) or stop a container
  collect  tar the job directory remotely and report the archive path

`stop` never touches tmux itself: kill-session, kill-window and kill-pane are
deliberately absent so the working session survives for inspection.
"""
from __future__ import annotations

import argparse
import re
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


# Shared remote code uses ps fields available on both macOS and Linux. Zombies
# have already exited and must not prevent a successful stop indefinitely.
GROUP_HELPERS = (
    "def group_members(pgid):\n"
    "    if not pgid or int(pgid)<=1:\n"
    "        return []\n"
    "    done=subprocess.run(['ps','-axo','pid=,pgid=,stat=,lstart='],\n"
    "        capture_output=True,text=True,check=True,env={**os.environ,'LC_ALL':'C'})\n"
    "    members=[]\n"
    "    for line in done.stdout.splitlines():\n"
    "        fields=line.split(None,3)\n"
    "        if len(fields)==4 and int(fields[1])==int(pgid) and not fields[2].startswith('Z'):\n"
    "            members.append({'pid':int(fields[0]),'started':fields[3]})\n"
    "    return members\n"
    "def verify_group(meta,members):\n"
    "    pgid=int(meta.get('pgid') or 0)\n"
    "    if pgid<=1 or pgid==os.getpgrp():\n"
    "        raise SystemError('invalid or controlling-shell process group')\n"
    "    leader=next((p for p in members if p['pid']==int(meta.get('pid') or 0)),None)\n"
    "    if leader and meta.get('pid_started') and leader['started']!=meta['pid_started']:\n"
    "        raise SystemError('job PID was reused; refusing to signal an unrelated process group')\n"
)
RC_SUFFIX = ' 2>&1; __tsw_rc=$?; printf "%s\\n" "$__tsw_rc" > '


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
                "import json,os,shlex,subprocess,time\n"
                "from pathlib import Path\n" + GROUP_HELPERS +
                f"job=Path({job_dir!r}).resolve()\n"
                f"if job.exists() and not {args.reuse!r}:\n"
                "    raise SystemError('job directory already exists: '+str(job))\n"
                "if (job/'meta.json').exists():\n"
                "    old=json.loads((job/'meta.json').read_text())\n"
                "    if group_members(old.get('pgid')):\n"
                "        raise SystemError('existing job process group is still running; use a new job ID')\n"
                "log=job/'stdout.log'\n"
                f"container={args.container!r}\n"
                f"runtime={args.container_runtime!r}\n"
                "container_id=''\n"
                f"cwd={args.cwd!r}\n"
                "if container:\n"
                "    if Path('/.dockerenv').exists() or Path('/run/.containerenv').exists():\n"
                "        raise SystemError('container jobs require a host shell; leave the container first')\n"
                "    inspected=subprocess.run([runtime,'inspect','--format',\n"
                "        '{{.State.Running}} {{.Id}}',container],capture_output=True,text=True,check=True)\n"
                "    running,container_id=inspected.stdout.strip().split()\n"
                "    if running!='true':\n"
                "        raise SystemError('container is not running')\n"
                "    argv=[runtime,'exec']+(['-w',cwd] if cwd else [])+[container_id,'bash','-lc',"
                f"{args.command!r}]\n"
                "    controller_cwd=str(job)\n"
                "else:\n"
                "    cwd=cwd or str(job)\n"
                f"    argv=['bash','-lc',{args.command!r}]\n"
                "    controller_cwd=cwd\n"
                f"job.mkdir(parents=True, exist_ok={args.reuse!r})\n"
                "rc=job/'rc'\n"
                "rc.unlink(missing_ok=True)\n"
                f"inner={args.command!r}\n"
                "wrapper=(shlex.join(argv)+' >> '+shlex.quote(str(log))+"
                f"{RC_SUFFIX!r}+shlex.quote(str(rc)))\n"
                # start_new_session gives the child its own process group without
                # depending on setsid, which is absent on macOS.
                "proc=subprocess.Popen(['bash','-lc',wrapper],\n"
                "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
                "    stderr=subprocess.DEVNULL, cwd=controller_cwd, start_new_session=True)\n"
                "stamp=subprocess.run(['ps','-p',str(proc.pid),'-o','lstart='],\n"
                "    text=True,capture_output=True,env={**os.environ,'LC_ALL':'C'}).stdout.strip()\n"
                "try:\n"
                "    pgid=os.getpgid(proc.pid)\n"
                "except Exception:\n"
                "    pgid=proc.pid\n"
                "meta={'job_id':"
                f"{args.job_id!r},'command':inner,'cwd':cwd,'pid':proc.pid,"
                "'pgid':pgid,'pid_started':stamp,'log':str(log),'rc_file':str(rc),"
                "'container':container,'container_id':container_id,'container_runtime':runtime,"
                "'controller':'host' if container else 'shell','started_at':time.time()}\n"
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
                "import json,os,subprocess\n"
                "from pathlib import Path\n" + GROUP_HELPERS +
                f"job=Path({job_dir!r})\n"
                "if not job.exists():\n"
                "    return {'state':'MISSING','job_dir':str(job)}\n"
                "meta=json.loads((job/'meta.json').read_text()) if (job/'meta.json').exists() else {}\n"
                "rc_file=job/'rc'\n"
                "rc=rc_file.read_text().strip() if rc_file.exists() else None\n"
                "members=group_members(meta.get('pgid'))\n"
                "if members:\n"
                "    verify_group(meta,members)\n"
                "alive=bool(members)\n"
                "log=job/'stdout.log'\n"
                "tail=[]\n"
                "size=0\n"
                "if log.exists():\n"
                "    size=log.stat().st_size\n"
                f"    tail=log.read_text(errors='replace').splitlines()[-{args.log_lines}:]\n"
                "if alive:\n"
                "    state='RUNNING'\n"
                "elif rc is not None:\n"
                "    state='SUCCEEDED' if rc=='0' else 'FAILED'\n"
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
                "from pathlib import Path\n" + GROUP_HELPERS +
                f"job=Path({job_dir!r})\n"
                "if not job.exists():\n"
                "    raise SystemError('job directory missing: '+str(job))\n"
                "meta=json.loads((job/'meta.json').read_text())\n"
                "notes=[]\n"
                "container=meta.get('container')\n"
                "runtime=meta.get('container_runtime','docker')\n"
                "container_stopped=not container\n"
                "if container:\n"
                "    if meta.get('controller')!='host' or not meta.get('container_id'):\n"
                "        raise SystemError('legacy container job has no host controller; stop it from the host shell')\n"
                "    try:\n"
                "        target=meta['container_id']\n"
                f"        done=subprocess.run([runtime,'stop','-t',str(int({args.grace_seconds!r})),target],\n"
                f"            text=True,capture_output=True,timeout={args.grace_seconds!r}+30)\n"
                "        notes.append('container stop rc=%s %s'%(done.returncode,\n"
                "            (done.stderr or '').strip()[:200]))\n"
                "        inspected=subprocess.run([runtime,'inspect','--format','{{.State.Running}}',target],\n"
                "            text=True,capture_output=True,timeout=15)\n"
                "        container_stopped=done.returncode==0 and inspected.returncode==0 and inspected.stdout.strip()=='false'\n"
                "    except (OSError,subprocess.TimeoutExpired) as err:\n"
                "        notes.append('container stop failed: %s'%err)\n"
                "pgid=meta.get('pgid')\n"
                "pid=meta.get('pid')\n"
                "def alive():\n"
                "    members=group_members(pgid)\n"
                "    verify_group(meta,members)\n"
                "    return bool(members)\n"
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
                "still_alive=alive()\n"
                "return {'signals':signalled,'still_alive':still_alive,'rc':rc,\n"
                "    'container_stopped':container_stopped,'stopped':not still_alive and container_stopped,\n"
                "    'notes':notes,'pid':pid,'pgid':pgid,'container':container}",
                timeout=max(args.timeout, 2 * args.grace_seconds + 90),
            )
            row["result"] = result
            row["status"] = "PASS" if result.get("stopped") else "FAIL"
            if not result.get("stopped"):
                row["error"] = "remote process group or container is still running, or its stop could not be verified"
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
    parser.add_argument("--container", default="", help="run in this container from a host shell; stop it with the job")
    parser.add_argument("--container-runtime", default="docker", help="runtime used at launch and saved for later stop")
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.job_id):
        parser.error("--job-id must be a simple name using letters, digits, dots, underscores or hyphens")
    if args.cwd and not args.cwd.startswith("/"):
        parser.error("--cwd must be an absolute POSIX path")
    if args.grace_seconds < 0 or args.timeout <= 0:
        parser.error("--grace-seconds must be nonnegative and --timeout must be positive")

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
