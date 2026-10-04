"""Shared tmux control primitives for the tmux-ssh-workflow skill.

Design constraints baked into this module:

* The control plane is a local tmux socket plus one or more tmux session names.
  Nothing else is configured -- no host, user, port or pane map.  The active
  pane of a session is rediscovered on every operation because a container
  restart or an SSH reconnect invalidates a previously captured pane id.
* Every remote command carries a unique marker.  A timeout never means the
  command failed; it means the result is not visible yet, so callers must
  inspect instead of blindly resending.
* Concurrency is per session: several sessions run in parallel, operations
  inside one session are serialised through a file lock keyed by socket and
  session.  Two drivers writing to the same PTY interleave their receipts.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import errno
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
import uuid
import zlib
from pathlib import Path

if sys.platform == "win32":
    # tmux, its socket and fcntl only exist inside WSL; native Windows Python
    # cannot reach a WSL socket. Fail before `import fcntl` raises instead.
    _script = os.path.abspath(sys.argv[0] or "scripts/X.py").replace("\\", "/")
    if _script[1:3] == ":/":
        _script = f"/mnt/{_script[0].lower()}{_script[2:]}"
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps({
        "status": "FAIL",
        "error": "native Windows is not supported; run inside WSL",
        "hint": f'wsl -d <distribution> -e python3 "{_script}" ...',
    }, indent=2))
    sys.exit(2)

import fcntl  # noqa: E402 - POSIX only, guarded above

MARKER_PREFIX = "TSW"
# A canonical PTY input line tops out near 4 KiB; stay well below it.
MAX_INPUT_LINE = 3500
CHUNK_CHARS = 1800
CAPTURE_LINES = 2000
# Files that docker and podman create inside every container.
CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")
DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
IN_WSL = "microsoft" in platform.uname().release.lower() or "WSL_DISTRO_NAME" in os.environ


class TmuxError(RuntimeError):
    """A tmux level failure: missing session, dead pane, bad socket."""


class OutputPipeBusy(TmuxError):
    """Download could not acquire a pipe; no remote payload was triggered."""


class RemoteCommandError(RuntimeError):
    """The remote command ran and reported a failure."""


class RemoteTimeout(TimeoutError):
    """The marker did not appear in time. The command may still be running."""


def new_marker(kind: str) -> str:
    return f"{MARKER_PREFIX}_{kind}_{uuid.uuid4().hex}"


def new_nonce() -> str:
    return f"{os.getpid()}-{uuid.uuid4().hex[:12]}"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Tmux:
    """A thin wrapper around one tmux socket."""

    def __init__(self, socket: str, tmux_binary: str = "tmux") -> None:
        if not socket:
            raise ValueError("socket is required")
        self.socket = socket
        self.base = [tmux_binary, "-S", socket]

    def run(self, *args: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.base + list(args),
            check=check,
            text=text,
            capture_output=True,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
        )

    # -- discovery ---------------------------------------------------------
    def has_session(self, session: str) -> bool:
        return self.run("has-session", "-t", session, check=False).returncode == 0

    def list_sessions(self) -> list[str]:
        result = self.run("list-sessions", "-F", "#{session_name}", check=False)
        if result.returncode != 0:
            return []
        return [row for row in result.stdout.splitlines() if row]

    def active_pane(self, session: str) -> str:
        """Resolve the session's active pane id.

        Returning the concrete `%id` keeps a single logical operation pinned to
        one pane, while re-resolving per operation survives restarts.
        """
        if not self.has_session(session):
            raise TmuxError(f"session not found on socket: {session}")
        result = self.run(
            "list-panes",
            "-t",
            session,
            "-F",
            "#{pane_id}\t#{window_active}\t#{pane_active}",
            check=False,
        )
        if result.returncode != 0:
            raise TmuxError(f"cannot list panes for session {session}: {result.stderr.strip()}")
        panes = []
        for row in result.stdout.splitlines():
            parts = row.split("\t")
            if len(parts) == 3:
                panes.append(parts)
        for pane_id, window_active, pane_active in panes:
            if window_active == "1" and pane_active == "1":
                return pane_id
        if panes:
            return panes[0][0]
        raise TmuxError(f"session has no panes: {session}")

    def pane_info(self, pane: str) -> dict:
        result = self.run(
            "display-message",
            "-p",
            "-t",
            pane,
            "-F",
            "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_dead}\t#{session_name}",
            check=False,
        )
        if result.returncode != 0:
            raise TmuxError(f"pane not available: {pane}: {result.stderr.strip()}")
        parts = result.stdout.strip().split("\t")
        while len(parts) < 5:
            parts.append("")
        return {
            "pane_id": parts[0],
            "pane_pid": parts[1],
            "current_command": parts[2],
            "dead": parts[3] == "1",
            "session": parts[4],
        }

    def capture(self, pane: str, lines: int = CAPTURE_LINES) -> str:
        result = self.run("capture-pane", "-p", "-J", "-t", pane, "-S", f"-{lines}", check=False)
        if result.returncode != 0:
            raise TmuxError(f"cannot capture pane {pane}: {result.stderr.strip()}")
        return result.stdout

    # -- input -------------------------------------------------------------
    def send_line(self, pane: str, line: str) -> None:
        """Send one literal line followed by Enter."""
        self.run("send-keys", "-t", pane, "-l", line)
        self.run("send-keys", "-t", pane, "Enter")

    def send_enter(self, pane: str) -> None:
        self.run("send-keys", "-t", pane, "Enter")

    def send_keys(self, pane: str, *keys: str) -> None:
        self.run("send-keys", "-t", pane, *keys)

    def load_buffer(self, buffer_name: str, path: str | Path) -> None:
        self.run("load-buffer", "-b", buffer_name, str(path))

    def paste_buffer(self, buffer_name: str, pane: str) -> None:
        self.run("paste-buffer", "-b", buffer_name, "-t", pane)

    def delete_buffer(self, buffer_name: str) -> None:
        self.run("delete-buffer", "-b", buffer_name, check=False)

    def require_no_pipe(self, pane: str) -> None:
        if self.run("display-message", "-p", "-t", pane, "#{pane_pipe}").stdout.strip() == "1":
            raise OutputPipeBusy("pane already has an output pipe; preserve it and use another pane")

    def pipe_pane_start(self, pane: str, command: str) -> None:
        self.require_no_pipe(pane)
        # Race: -o toggles, so a pipe installed between the check and this
        # call is closed and ours is not opened. Callers detect that through a
        # handshake and refuse; the closed pipe cannot be restored.
        self.run("pipe-pane", "-o", "-t", pane, command)

    def pipe_pane_stop(self, pane: str) -> None:
        self.run("pipe-pane", "-t", pane, check=False)


def lock_path(socket: str, session: str) -> Path:
    socket = str(Path(socket).expanduser().resolve())
    key = hashlib.sha256(f"{socket}\0{session}".encode()).hexdigest()[:16]
    root = Path("/tmp") / f"tmux-ssh-workflow-locks-{os.getuid()}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / f"{key}.lock"


@contextlib.contextmanager
def session_lock(socket: str, session: str, timeout: float = 900.0, recover: bool = False):
    """Serialise operations that share one PTY.

    Different sessions take different locks, so batch drivers stay parallel.
    """
    path = lock_path(socket, session)
    handle = open(path, "a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise TmuxError(
                        f"another operation holds the lock for session {session}; "
                        "inspect it instead of forcing a parallel run"
                    ) from error
                time.sleep(0.2)
        handle.seek(0)
        previous = handle.read()
        try:
            previous_state = json.loads(previous).get("state") if previous else None
        except (ValueError, AttributeError):
            previous_state = "UNKNOWN"
        if previous_state in {"BUSY", "UNKNOWN"} and not recover:
            raise TmuxError(
                f"session {session} has an unfinished or timed-out operation; "
                "inspect the pane, restore an idle shell, then use tmux_exec.py --recover-session"
            )

        def record(state: str, error: str = "") -> None:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "session": session,
                                     "at": time.time(), "state": state, "error": error}))
            handle.flush()

        # If the driver is killed, BUSY survives the released OS lock.
        record("BUSY")
        try:
            yield
        except BaseException as error:
            uncertain = (
                isinstance(error, (RemoteTimeout, TmuxError,
                                   subprocess.CalledProcessError, KeyboardInterrupt))
                and not isinstance(error, OutputPipeBusy)
            )
            record("UNKNOWN" if uncertain else "IDLE", str(error))
            raise
        else:
            record("IDLE")
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _marker_rows(text: str, marker: str) -> list[str]:
    """Collect payloads that follow a marker at the start of an output line.

    The pane echoes the command containing the marker literally, so a naive
    substring match reads back the unexpanded `rc=%s` template. Two rules
    separate real output from that echo: the remote always emits a newline
    before the marker, so genuine receipts start at column zero, and a
    genuine payload is either valid JSON or fully expanded `key=value` pairs
    with no shell metacharacters left in them.
    """
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(marker):
            continue
        payload = line[len(marker):].strip()
        if payload.startswith("{") or payload.startswith("["):
            rows.append(payload)
            continue
        if "%s" in payload or "$" in payload or "\\n" in payload or '"' in payload:
            continue
        rows.append(payload)
    return rows


def wait_for_marker(
    tmux: Tmux,
    pane: str,
    marker: str,
    timeout: float,
    poll_interval: float = 0.5,
    capture_lines: int = CAPTURE_LINES,
) -> str:
    deadline = time.monotonic() + timeout
    while True:
        rows = _marker_rows(tmux.capture(pane, capture_lines), marker)
        if rows:
            return rows[-1]
        if time.monotonic() >= deadline:
            raise RemoteTimeout(
                f"marker {marker} not visible within {timeout:.0f}s on pane {pane}; "
                "the remote command may still be running -- inspect before retrying"
            )
        time.sleep(poll_interval)


def run_shell(
    tmux: Tmux,
    pane: str,
    command: str,
    timeout: float = 60.0,
    capture_output: bool = False,
) -> dict:
    """Run a shell command in the pane and read back its exit status.

    The remote side prints `<marker> rc=<code>` so the result never depends on
    parsing a human prompt. The command travels base64 encoded and runs via
    `eval` in the current shell: `cd` and exports persist, while a trailing
    `&`, a `#` comment, newlines or quotes cannot break the receipt wrapper.
    """
    marker = new_marker("RC")
    out_path = f"/tmp/tsw-out-{uuid.uuid4().hex}"

    def wrap(decode: str) -> str:
        if capture_output:
            return (
                f"eval \"$({decode})\" > {out_path} 2>&1; __tsw_rc=$?; "
                f"printf '\\n{marker} rc=%s out=%s\\n' \"$__tsw_rc\" {out_path}"
            )
        return f"eval \"$({decode})\"; __tsw_rc=$?; printf '\\n{marker} rc=%s\\n' \"$__tsw_rc\""

    # The base64 alphabet needs no shell quoting.
    encoded = base64.b64encode(command.encode()).decode()
    wrapped = wrap(f"printf %s {encoded} | base64 -d")
    if len(wrapped) > MAX_INPUT_LINE:
        # Stage long commands in bounded lines, like run_python does.
        scratch = f"/tmp/tsw-cmd-{uuid.uuid4().hex}"
        tmux.send_line(pane, f": > {scratch}")
        for offset in range(0, len(encoded), CHUNK_CHARS):
            tmux.send_line(pane, f"printf %s {encoded[offset:offset + CHUNK_CHARS]} >> {scratch}")
        wrapped = wrap(f"base64 -d < {scratch}; rm -f {scratch}")
    tmux.send_line(pane, wrapped)
    row = wait_for_marker(tmux, pane, marker, timeout)
    fields = dict(
        part.split("=", 1) for part in row.split() if "=" in part
    )
    rc_text = fields.get("rc", "")
    try:
        rc = int(rc_text)
    except ValueError as error:
        raise RemoteCommandError(f"cannot parse exit code from receipt: {row!r}") from error
    result = {"rc": rc, "marker": marker, "receipt": row}
    if capture_output:
        result["remote_output_path"] = fields.get("out")
    return result


def run_python(
    tmux: Tmux,
    pane: str,
    body: str,
    timeout: float = 120.0,
    python: str = "python3",
) -> object:
    """Execute a Python body remotely and return its JSON result.

    `body` is an indented-free function body that ends with `return <value>`.
    Long payloads are staged into a remote scratch file in bounded lines
    because a single PTY line cannot carry them.
    """
    marker = new_marker("PY")
    indented = "\n".join("    " + line for line in body.splitlines())
    source = (
        "import json,traceback\n"
        "def __tsw_action():\n"
        f"{indented}\n"
        "try:\n"
        "    __tsw_result={'ok':True,'result':__tsw_action()}\n"
        "    __tsw_payload=json.dumps(__tsw_result)\n"
        "except BaseException:\n"
        "    __tsw_payload=json.dumps({'ok':False,'error':traceback.format_exc()})\n"
        f"print('\\n'+{marker!r}+__tsw_payload,flush=True)\n"
    )
    # Validate the actual generated body, including embedded shell quoting,
    # before writing anything to the remote pane.
    compile(source, "<tmux-ssh remote action>", "exec")
    encoded = base64.b64encode(zlib.compress(source.encode())).decode()
    bootstrap = f"import base64,zlib;exec(zlib.decompress(base64.b64decode({encoded!r})))"
    command = f"{shlex.quote(python)} -c {shlex.quote(bootstrap)}"
    if len(command.encode()) > MAX_INPUT_LINE:
        scratch = f"/tmp/tsw-rpc-{uuid.uuid4().hex}"
        tmux.send_line(pane, f": > {shlex.quote(scratch)}")
        for offset in range(0, len(encoded), CHUNK_CHARS):
            piece = encoded[offset:offset + CHUNK_CHARS]
            tmux.send_line(pane, f"printf %s {shlex.quote(piece)} >> {shlex.quote(scratch)}")
        bootstrap = (
            "import base64,zlib;from pathlib import Path;"
            f"p=Path({scratch!r});b=p.read_bytes();p.unlink();"
            "exec(zlib.decompress(base64.b64decode(b)))"
        )
        command = f"{shlex.quote(python)} -c {shlex.quote(bootstrap)}"
    tmux.send_line(pane, command)
    row = wait_for_marker(tmux, pane, marker, timeout)
    try:
        payload = json.loads(row)
    except json.JSONDecodeError as error:
        raise RemoteCommandError(f"malformed remote receipt: {row[:400]!r}") from error
    if not payload.get("ok"):
        raise RemoteCommandError(payload.get("error", "unknown remote failure"))
    return payload.get("result")


def probe_session(tmux: Tmux, session: str, timeout: float = 30.0,
                  container_markers: tuple[str, ...] = CONTAINER_MARKERS) -> dict:
    """Report where a session's active pane currently sits."""
    pane = tmux.active_pane(session)
    info = tmux.pane_info(pane)
    facts = run_python(
        tmux,
        pane,
        "import os,socket,getpass\n"
        "try:\n"
        "    user=getpass.getuser()\n"
        "except Exception:\n"
        "    user='unknown'\n"
        # PID 1's cgroup names the runtime under cgroup v1; v2 hides it,
        # which is why the marker files come first.
        f"hints=[p for p in {list(container_markers)!r} if os.path.exists(p)]\n"
        "try:\n"
        "    cgroup=open('/proc/1/cgroup').read()\n"
        "except OSError:\n"
        "    cgroup=''\n"
        "hints+=['cgroup:'+w for w in ('docker','kubepods','containerd','libpod','lxc') if w in cgroup]\n"
        "return {'host':socket.gethostname(),'user':user,\n"
        "    'cwd':os.getcwd(),'shell_pid':os.getppid(),'container_hints':hints}",
        timeout=timeout,
    )
    return {
        "session": session,
        "pane": pane,
        "pane_pid": info["pane_pid"],
        "current_command": info["current_command"],
        "host": facts.get("host"),
        "user": facts.get("user"),
        "cwd": facts.get("cwd"),
        "shell_pid": facts.get("shell_pid"),
        "in_container": bool(facts.get("container_hints")),
        "container_hints": facts.get("container_hints", []),
    }


def local_path(value: str) -> Path:
    """argparse type for LOCAL file args; never use it on remote paths.

    Under WSL a caller on the Windows side may hand in `C:\\x\\f` or `C:/x/f`;
    map it onto `/mnt/c/x/f` so the file is found from inside WSL.
    """
    if IN_WSL and DRIVE_PATH.match(value):
        with contextlib.suppress(OSError):
            done = subprocess.run(["wslpath", "-u", value], capture_output=True, text=True)
            if done.returncode == 0 and done.stdout.strip():
                return Path(done.stdout.strip())
        return Path(f"/mnt/{value[0].lower()}/{value[3:]}".replace("\\", "/"))
    return Path(value).expanduser()


def posix_arg(value: str) -> str:
    """argparse type for args that must stay POSIX: the socket and remote paths.

    Git Bash (MSYS) rewrites `/tmp/x` into `C:/Users/.../Temp/x` or
    `C:/Program Files/Git/tmp/x` before WSL ever sees it.
    """
    if DRIVE_PATH.match(value) or ":/Program Files/Git/" in value:
        raise argparse.ArgumentTypeError(
            f"{value!r} looks like a POSIX path rewritten by Git Bash; "
            "prefix the command with MSYS_NO_PATHCONV=1 or call it from PowerShell"
        )
    return value


def emit(payload: object) -> None:
    # A remote command that already ran must not lose its receipt to a
    # UnicodeEncodeError under a C/POSIX or other non-UTF-8 locale.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
