#!/usr/bin/env python3
"""Move files through an already-authenticated tmux pane, no credentials needed.

Upload protocol (per chunk):
  1. local file is optionally gzipped, then split into fixed-size byte chunks
  2. each chunk is base64 encoded, folded to 76 columns, ended by a nonce marker
  3. the remote shell disables echo, prints READY, then reads lines until the
     marker; the local side only pastes after seeing READY so a slow PTY cannot
     race the paste
  4. the remote decodes into `<target>.part-<nonce>` and appends into the
     staging file, then re-enables echo and prints a receipt
  5. after the last chunk the remote verifies sha256 plus byte size and only
     then renames atomically into place

Download protocol:
  `pipe-pane` taps the pane before the remote is triggered, so no byte is
  missed, and the scrollback window is irrelevant. The payload is fetched in
  byte ranges, each verified, then joined and verified as a whole.

Usage:
  transfer.py put --socket P --session S --source FILE --remote-path /abs/FILE
  transfer.py get --socket P --session S --remote-path /abs/FILE --dest FILE
  transfer.py put --socket P --sessions a,b --source F --remote-path /abs/F
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import shutil
import string
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tmuxlib import (
    RemoteTimeout,
    Tmux,
    TmuxError,
    emit,
    new_marker,
    new_nonce,
    run_python,
    session_lock,
    sha256_file,
    wait_for_marker,
)

DEFAULT_CHUNK_BYTES = 3 * 1024 * 1024
SAFE_PATH_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-"
)
BASE64_CHARS = set(string.ascii_letters + string.digits + "+/=")


def _is_base64_line(line: str) -> bool:
    """Accept only well-formed base64 rows so prompts never enter the payload."""
    if not line or len(line) > 76:
        return False
    if set(line) - BASE64_CHARS:
        return False
    return len(line) % 4 == 0


def check_remote_path(path: str) -> None:
    if not path.startswith("/"):
        raise ValueError(f"remote path must be absolute: {path}")
    bad = set(path) - SAFE_PATH_CHARS
    if bad:
        raise ValueError(f"remote path has unsafe characters {sorted(bad)}: {path}")


def _fold76(data: bytes) -> str:
    encoded = base64.b64encode(data).decode()
    return "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76))


def _upload_chunk(
    tmux: Tmux,
    pane: str,
    payload: bytes,
    staging: str,
    timeout: float,
) -> None:
    """Append one chunk to the remote staging file."""
    nonce = new_nonce()
    end_marker = f"TSW_B64_END_{nonce}"
    ready_marker = f"TSW_B64_READY_{nonce}"
    done_marker = f"TSW_B64_DONE_{nonce}"
    buffer_name = f"tsw-up-{nonce}"

    with tempfile.NamedTemporaryFile("w", suffix=".b64", delete=False) as handle:
        handle.write(_fold76(payload))
        handle.write(f"\n{end_marker}\n")
        local_payload = handle.name

    encoded_remote = f"{staging}.b64-{nonce}"
    remote_command = (
        f"stty -echo; : > {encoded_remote}; printf '\\n{ready_marker}\\n'; "
        f"while IFS= read -r line; do "
        f"if test \"$line\" = {end_marker}; then break; fi; "
        f"printf '%s\\n' \"$line\" >> {encoded_remote}; done; "
        f"base64 -d < {encoded_remote} >> {staging}; __tsw_rc=$?; "
        f"rm -f {encoded_remote}; stty echo; "
        f"printf '\\n{done_marker} rc=%s\\n' \"$__tsw_rc\""
    )
    try:
        tmux.load_buffer(buffer_name, local_payload)
        tmux.send_line(pane, remote_command)
        # Only paste once the reader loop is live.
        wait_for_marker(tmux, pane, ready_marker, timeout=60.0, poll_interval=0.2, capture_lines=40)
        tmux.paste_buffer(buffer_name, pane)
        row = wait_for_marker(tmux, pane, done_marker, timeout=timeout, capture_lines=60)
        if "rc=0" not in row:
            # Free the reader and restore echo before surfacing the failure.
            tmux.send_line(pane, end_marker)
            tmux.send_line(pane, "stty echo")
            raise RuntimeError(f"remote chunk decode failed: {row!r}")
    finally:
        tmux.delete_buffer(buffer_name)
        Path(local_payload).unlink(missing_ok=True)


def put_one(args: argparse.Namespace, session: str) -> dict:
    tmux = Tmux(args.socket)
    source = Path(args.source).expanduser().resolve()
    row: dict = {"session": session, "action": "put", "status": "FAIL"}
    started = time.monotonic()
    staged_local: Path | None = None
    try:
        check_remote_path(args.remote_path)
        if not source.is_file():
            raise FileNotFoundError(f"source file is missing: {source}")

        payload_path = source
        compressed = False
        if args.compress:
            staged_local = Path(tempfile.mkdtemp(prefix="tsw-gz-")) / (source.name + ".gz")
            with open(source, "rb") as src, gzip.open(staged_local, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            payload_path = staged_local
            compressed = True

        wire_sha = sha256_file(payload_path)
        wire_size = payload_path.stat().st_size
        final_sha = sha256_file(source)
        final_size = source.stat().st_size

        with session_lock(args.socket, session, timeout=args.lock_timeout):
            pane = tmux.active_pane(session)
            row["pane"] = pane
            nonce = new_nonce()
            staging = f"{args.remote_path}.part-{nonce}"
            wire_target = f"{staging}.gz" if compressed else staging

            prepared = run_python(
                tmux,
                pane,
                "from pathlib import Path\n"
                f"target=Path({args.remote_path!r})\n"
                f"if not {args.overwrite!r} and target.exists():\n"
                "    raise SystemError('remote target already exists: '+str(target))\n"
                "target.parent.mkdir(parents=True, exist_ok=True)\n"
                f"stage=Path({wire_target!r})\n"
                "stage.unlink(missing_ok=True)\n"
                "stage.touch()\n"
                "return {'parent': str(target.parent)}",
                timeout=args.timeout,
            )
            row["remote_parent"] = prepared.get("parent")

            chunks = 0
            with open(payload_path, "rb") as handle:
                while True:
                    block = handle.read(args.chunk_bytes)
                    if not block:
                        break
                    _upload_chunk(tmux, pane, block, wire_target, args.timeout)
                    chunks += 1
            row["chunks"] = chunks

            verified = run_python(
                tmux,
                pane,
                "import gzip,hashlib,shutil\n"
                "from pathlib import Path\n"
                f"wire=Path({wire_target!r})\n"
                f"stage=Path({staging!r})\n"
                f"target=Path({args.remote_path!r})\n"
                "def digest(path):\n"
                "    h=hashlib.sha256()\n"
                "    with path.open('rb') as fh:\n"
                "        for blk in iter(lambda: fh.read(1048576), b''):\n"
                "            h.update(blk)\n"
                "    return h.hexdigest()\n"
                "wire_sha=digest(wire)\n"
                "wire_size=wire.stat().st_size\n"
                f"if wire_sha!={wire_sha!r} or wire_size!={wire_size!r}:\n"
                "    wire.unlink(missing_ok=True)\n"
                "    raise SystemError('wire mismatch sha=%s size=%s'%(wire_sha,wire_size))\n"
                f"if {compressed!r}:\n"
                "    with gzip.open(wire,'rb') as src, stage.open('wb') as dst:\n"
                "        shutil.copyfileobj(src,dst,length=1048576)\n"
                "    wire.unlink(missing_ok=True)\n"
                "final_sha=digest(stage)\n"
                "final_size=stage.stat().st_size\n"
                f"if final_sha!={final_sha!r} or final_size!={final_size!r}:\n"
                "    stage.unlink(missing_ok=True)\n"
                "    raise SystemError('payload mismatch sha=%s size=%s'%(final_sha,final_size))\n"
                "stage.replace(target)\n"
                "return {'sha256':final_sha,'size':final_size,'path':str(target)}",
                timeout=max(args.timeout, 300.0),
            )
            row.update(verified)
            row["compressed"] = compressed
            row["status"] = "PASS"
    except RemoteTimeout as error:
        row["status"] = "TIMEOUT"
        row["error"] = str(error)
    except Exception as error:  # noqa: BLE001
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        if staged_local is not None:
            shutil.rmtree(staged_local.parent, ignore_errors=True)
        elapsed = time.monotonic() - started
        row["elapsed_seconds"] = round(elapsed, 2)
        if row.get("size") and elapsed > 0:
            row["throughput_mib_s"] = round(row["size"] / elapsed / 1048576, 2)
    return row


def _download_range(
    tmux: Tmux,
    pane: str,
    remote_path: str,
    offset: int,
    length: int,
    dest: Path,
    timeout: float,
) -> None:
    """Fetch one byte range and append it to the local staging file.

    The tap is attached before the remote is triggered so no byte is lost, and
    the payload never depends on the scrollback window size. Slicing, hashing
    and encoding all happen in remote Python because `dd`, `stat` and
    `sha256sum` differ across BSD and GNU userlands.
    """
    nonce = new_nonce()
    begin = f"TSW_B64_DL_BEGIN_{nonce}"
    end = f"TSW_B64_DL_END_{nonce}"
    tap = Path(tempfile.mkstemp(prefix="tsw-tap-")[1])
    helper = f"/tmp/tsw-dl-{nonce}.py"
    script = (
        "import base64,hashlib,sys\n"
        f"fh=open({remote_path!r},'rb')\n"
        f"fh.seek({offset})\n"
        f"data=fh.read({length})\n"
        "fh.close()\n"
        "digest=hashlib.sha256(data).hexdigest()\n"
        f"sys.stdout.write('\\n{begin} sha='+digest+' size='+str(len(data))+'\\n')\n"
        "encoded=base64.b64encode(data).decode()\n"
        "for i in range(0,len(encoded),76):\n"
        "    sys.stdout.write(encoded[i:i+76]+'\\n')\n"
        f"sys.stdout.write('{end}\\n')\n"
        "sys.stdout.flush()\n"
    )
    try:
        # Stage the helper first so the tapped output holds only the payload.
        run_python(
            tmux,
            pane,
            "from pathlib import Path\n"
            f"Path({helper!r}).write_text({script!r})\n"
            "return True",
            timeout=60.0,
        )
        tmux.pipe_pane_start(pane, f"cat >> '{tap}'")
        tmux.send_line(pane, f"python3 {helper}; rm -f {helper}")
        deadline = time.monotonic() + timeout
        while True:
            text = tap.read_text(errors="replace") if tap.exists() else ""
            if any(line.strip() == end for line in text.splitlines()):
                break
            if time.monotonic() >= deadline:
                raise RemoteTimeout(
                    f"download range {offset}+{length} did not finish within {timeout:.0f}s"
                )
            time.sleep(0.3)
    finally:
        tmux.pipe_pane_stop(pane)

    text = tap.read_text(errors="replace").replace("\r", "")
    tap.unlink(missing_ok=True)
    header = None
    payload_lines: list[str] = []
    collecting = False
    for line in text.splitlines():
        stripped = line.strip()
        if header is None and stripped.startswith(begin + " sha="):
            header = stripped
            collecting = True
            continue
        if stripped == end:
            collecting = False
            continue
        if collecting and stripped and _is_base64_line(stripped):
            payload_lines.append(stripped)
    if header is None:
        raise RuntimeError("download range produced no header marker")
    fields = dict(part.split("=", 1) for part in header.split() if "=" in part)
    data = base64.b64decode("".join(payload_lines))
    local_sha = hashlib.sha256(data).hexdigest()
    if local_sha != fields.get("sha") or str(len(data)) != fields.get("size"):
        raise RuntimeError(
            f"range verification failed: local sha={local_sha} size={len(data)} "
            f"remote sha={fields.get('sha')} size={fields.get('size')}"
        )
    with open(dest, "ab") as handle:
        handle.write(data)


def get_one(args: argparse.Namespace, session: str) -> dict:
    tmux = Tmux(args.socket)
    row: dict = {"session": session, "action": "get", "status": "FAIL"}
    started = time.monotonic()
    dest = Path(args.dest).expanduser()
    # Only disambiguate when several sessions really write into one destination.
    if len(args.session_list) > 1:
        dest = dest.parent / f"{session}-{dest.name}"
    part = dest.parent / f"{dest.name}.part-{uuid.uuid4().hex[:8]}"
    try:
        check_remote_path(args.remote_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not args.overwrite:
            raise FileExistsError(f"local destination exists: {dest}")
        part.unlink(missing_ok=True)

        with session_lock(args.socket, session, timeout=args.lock_timeout):
            pane = tmux.active_pane(session)
            row["pane"] = pane
            meta = run_python(
                tmux,
                pane,
                "import hashlib\n"
                "from pathlib import Path\n"
                f"p=Path({args.remote_path!r})\n"
                "if not p.is_file():\n"
                "    raise SystemError('remote file missing: '+str(p))\n"
                "h=hashlib.sha256()\n"
                "with p.open('rb') as fh:\n"
                "    for blk in iter(lambda: fh.read(1048576), b''):\n"
                "        h.update(blk)\n"
                "return {'sha256':h.hexdigest(),'size':p.stat().st_size}",
                timeout=args.timeout,
            )
            total = int(meta["size"])
            row["remote_sha256"] = meta["sha256"]
            row["size"] = total
            chunks = 0
            offset = 0
            while offset < total:
                length = min(args.chunk_bytes, total - offset)
                _download_range(
                    tmux, pane, args.remote_path, offset, length, part, args.timeout
                )
                offset += length
                chunks += 1
            row["chunks"] = chunks

        local_sha = sha256_file(part)
        if local_sha != meta["sha256"] or part.stat().st_size != total:
            raise RuntimeError(
                f"whole-file verification failed: local sha={local_sha} "
                f"size={part.stat().st_size} remote sha={meta['sha256']} size={total}"
            )
        part.replace(dest)
        row["dest"] = str(dest)
        row["sha256"] = local_sha
        row["status"] = "PASS"
    except RemoteTimeout as error:
        row["status"] = "TIMEOUT"
        row["error"] = str(error)
    except Exception as error:  # noqa: BLE001
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        if row["status"] != "PASS":
            part.unlink(missing_ok=True)
        elapsed = time.monotonic() - started
        row["elapsed_seconds"] = round(elapsed, 2)
        if row.get("size") and elapsed > 0:
            row["throughput_mib_s"] = round(row["size"] / elapsed / 1048576, 2)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["put", "get"])
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session")
    parser.add_argument("--sessions")
    parser.add_argument("--source")
    parser.add_argument("--remote-path", required=True)
    parser.add_argument("--dest")
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--lock-timeout", type=float, default=1800.0)
    parser.add_argument("--compress", action="store_true", help="gzip before upload")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-parallel", type=int, default=4)
    args = parser.parse_args()

    names = args.sessions or args.session
    if not names:
        parser.error("provide --session or --sessions")
    sessions = [name.strip() for name in names.split(",") if name.strip()]
    args.session_list = sessions

    if args.mode == "put" and not args.source:
        parser.error("put requires --source")
    if args.mode == "get" and not args.dest:
        parser.error("get requires --dest")

    worker = put_one if args.mode == "put" else get_one
    workers = max(1, min(args.max_parallel, len(sessions)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda name: worker(args, name), sessions))

    bad = [row["session"] for row in rows if row["status"] != "PASS"]
    emit({
        "action": args.mode,
        "socket": args.socket,
        "remote_path": args.remote_path,
        "status": "PASS" if not bad else "FAIL",
        "problem_sessions": bad,
        "results": rows,
    })
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
