"""End-to-end regression tests against throwaway local tmux servers.

Every test owns a tmux server on a socket inside its own temp directory. The
session runs `bash --norc --noprofile` as a fake remote, so "remote" paths are
local paths and results can be checked directly. Only stdlib unittest is used:

  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
TMUX = shutil.which("tmux")
SHELL = "bash --norc --noprofile"


def group_alive(pgid: int) -> bool:
    done = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True,
                          env={**os.environ, "LC_ALL": "C"}, check=True)
    for line in done.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and int(fields[1]) == pgid and not fields[2].startswith("Z"):
            return True
    return False


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@unittest.skipUnless(os.name == "posix" and TMUX, "tmux is required (Linux, macOS or WSL)")
class TmuxCase(unittest.TestCase):
    """Fixture: one private tmux server plus a fake remote session `remote`."""

    def setUp(self) -> None:
        # /tmp keeps the socket path short enough for macOS (104 bytes).
        self.tmp = Path(tempfile.mkdtemp(prefix="tsw-test-", dir="/tmp"))
        self.socket = str(self.tmp / "tmux.sock")
        self.sessions: set[str] = set()
        utf8 = "C.UTF-8" if sys.platform.startswith("linux") else "en_US.UTF-8"
        self.env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        self.env.update(LANG=utf8, LC_ALL=utf8)
        self.addCleanup(self._cleanup)
        self.start_session("remote")

    def _cleanup(self) -> None:
        self.tmux("kill-server", check=False)
        sys.path.insert(0, str(SCRIPTS))
        try:
            from tmuxlib import lock_path  # POSIX only; imported lazily
            for name in self.sessions:
                lock_path(self.socket, name).unlink(missing_ok=True)
        finally:
            sys.path.remove(str(SCRIPTS))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run([TMUX, "-S", self.socket, "-f", "/dev/null", *args],
                              capture_output=True, text=True, env=self.env, check=check)

    def start_session(self, name: str, command: str = SHELL) -> None:
        self.sessions.add(name)
        self.tmux("new-session", "-d", "-s", name, "-x", "200", "-y", "50", command)

    def run_script(self, script: str, *args: str, timeout: float = 300, **popen) -> tuple[int, dict]:
        done = subprocess.run([sys.executable, str(SCRIPTS / script), "--socket", self.socket, *args],
                              capture_output=True, text=True, encoding="utf-8", env=self.env,
                              timeout=timeout, **popen)
        try:
            return done.returncode, json.loads(done.stdout)
        except ValueError:
            self.fail(f"{script} printed no JSON (rc={done.returncode}):\n{done.stdout}\n{done.stderr}")

    def assert_pass(self, rc: int, data: dict) -> None:
        self.assertEqual((rc, data.get("status")), (0, "PASS"), json.dumps(data, ensure_ascii=False, indent=2))

    def shim_session(self, name: str, **shims: str) -> None:
        """Start a session whose PATH puts fake commands (sh scripts) first."""
        shimdir = self.tmp / f"shim-{name}"
        shimdir.mkdir()
        for tool, body in shims.items():
            (shimdir / tool).write_text("#!/bin/sh\n" + body, encoding="utf-8")
            (shimdir / tool).chmod(0o755)
        self.start_session(name, f"env PATH={shlex.quote(str(shimdir))}:\"$PATH\" {SHELL}")

    def run_plan(self, steps: list[dict], sessions: str = "remote") -> tuple[int, dict]:
        plan = self.tmp / "plan.json"
        plan.write_text(json.dumps({"sessions": sessions.split(","), "steps": steps}), encoding="utf-8")
        return self.run_script("batch_sessions.py", "--plan", str(plan))


class ExecTests(TmuxCase):
    def test_preflight_pass(self) -> None:
        rc, data = self.run_script("session_preflight.py", "--sessions", "remote",
                                   "--require-command", "python3")
        self.assert_pass(rc, data)
        self.assertEqual(data["results"][0]["problems"], [])

    def test_exec_show_output_chinese(self) -> None:
        rc, data = self.run_script("tmux_exec.py", "--sessions", "remote",
                                   "--command", "printf '你好，世界\\n第二行\\n'", "--show-output")
        self.assert_pass(rc, data)
        self.assertEqual(data["results"][0]["output"], ["你好，世界", "第二行"])

    def test_preflight_expect_host(self) -> None:
        if any(os.path.exists(p) for p in ("/.dockerenv", "/run/.containerenv")):
            self.skipTest("the test machine itself is a container")
        rc, data = self.run_script("session_preflight.py", "--sessions", "remote", "--expect-host")
        self.assert_pass(rc, data)
        self.assertFalse(data["results"][0]["in_container"])
        rc, data = self.run_script("session_preflight.py", "--sessions", "remote", "--expect-in-container")
        self.assertEqual((rc, data["status"]), (1, "FAIL"))
        self.assertIn("not inside a container", data["results"][0]["problems"][0])

    def test_preflight_expect_in_container(self) -> None:
        # A test-controlled marker stands in for /.dockerenv.
        marker = self.tmp / "dockerenv"
        marker.touch()
        args = ("--sessions", "remote", "--container-marker", str(marker))
        rc, data = self.run_script("session_preflight.py", *args, "--expect-in-container")
        self.assert_pass(rc, data)
        self.assertTrue(data["results"][0]["in_container"])
        self.assertIn(str(marker), data["results"][0]["container_hints"])
        rc, data = self.run_script("session_preflight.py", *args, "--expect-host")
        self.assertEqual((rc, data["status"]), (1, "FAIL"))
        self.assertIn("inside a container", data["results"][0]["problems"][0])


class ShellTests(TmuxCase):
    """run_shell must survive any command text, in the current shell."""

    def exec(self, command: str, *extra: str) -> tuple[int, dict]:
        return self.run_script("tmux_exec.py", "--sessions", "remote", "--command", command,
                               "--timeout", "15", *extra)

    def output(self, command: str) -> list[str]:
        rc, data = self.exec(command, "--show-output")
        self.assert_pass(rc, data)
        return data["results"][0]["output"]

    def test_trailing_ampersand(self) -> None:
        flag = self.tmp / "bg done"
        rc, data = self.exec(f"sleep 0.2 && touch {shlex.quote(str(flag))} &")
        self.assert_pass(rc, data)
        self.assertTrue(wait_until(flag.exists))

    def test_inline_comment(self) -> None:
        self.assertEqual(self.output("echo hi # trailing comment ; }"), ["hi"])

    def test_multi_line(self) -> None:
        command = "for x in a b; do\n  echo \"item $x\"\ndone\ncat <<'EOF'\nheredoc $x\nEOF"
        self.assertEqual(self.output(command), ["item a", "item b", "heredoc $x"])

    def test_quote_heavy(self) -> None:
        command = """printf '%s|' "it's" 'say "hi"' '$HOME' "tab\\there" '`x`' "{ ; }"; echo"""
        self.assertEqual(self.output(command), ["it's|say \"hi\"|$HOME|tab\\there|`x`|{ ; }|"])

    def test_cd_and_export_persist(self) -> None:
        where = self.tmp / "cd dir"
        where.mkdir()
        rc, data = self.exec(f"cd {shlex.quote(str(where))} && export TSW_PERSIST=kept")
        self.assert_pass(rc, data)
        self.assertEqual(self.output('pwd; echo "$TSW_PERSIST"'), [str(where), "kept"])

    def test_over_long_command(self) -> None:
        # Far beyond one canonical PTY line (~4 KiB); must be staged, not cut.
        # Without readline the tty line discipline truncates, as for sh/dash.
        self.start_session("canon", f"{SHELL} --noediting")
        text = "x" * 12000
        command = f"printf %s '{text}' | wc -c | tr -d ' '"
        rc, data = self.run_script("tmux_exec.py", "--sessions", "canon", "--command", command,
                                   "--timeout", "15", "--show-output")
        self.assert_pass(rc, data)
        self.assertEqual(data["results"][0]["output"], ["12000"])


class TransferTests(TmuxCase):
    def test_round_trip_remote_path_with_space(self) -> None:
        payload = os.urandom(90_000) + "中文内容\n".encode() * 2000
        source = self.tmp / "local src" / "data.bin"
        source.parent.mkdir()
        source.write_bytes(payload)
        for compress in (False, True):
            with self.subTest(compress=compress):
                remote = self.tmp / "remote dir" / f"data file {int(compress)}.bin"
                extra = ["--compress"] if compress else []
                rc, data = self.run_script("transfer.py", "put", "--session", "remote",
                                           "--source", str(source), "--remote-path", str(remote),
                                           "--chunk-bytes", "40000", *extra)
                self.assert_pass(rc, data)
                self.assertEqual(remote.read_bytes(), payload)
                dest = self.tmp / "local dst" / f"back {int(compress)}.bin"
                rc, data = self.run_script("transfer.py", "get", "--session", "remote",
                                           "--remote-path", str(remote), "--dest", str(dest),
                                           "--chunk-bytes", "40000")
                self.assert_pass(rc, data)
                self.assertEqual(dest.read_bytes(), payload)

    def test_download_low_fd_limit_many_chunks(self) -> None:
        # A descriptor leaked per range would exhaust this limit long before
        # the last of the 40 chunks.
        remote = self.tmp / "remote dir" / "small.bin"
        remote.parent.mkdir()
        payload = os.urandom(40 * 64)
        remote.write_bytes(payload)
        dest = self.tmp / "small.bin"

        import resource  # POSIX only; never import inside preexec_fn

        def limit_fds() -> None:
            resource.setrlimit(resource.RLIMIT_NOFILE, (24, 24))

        rc, data = self.run_script("transfer.py", "get", "--session", "remote",
                                   "--remote-path", str(remote), "--dest", str(dest),
                                   "--chunk-bytes", "64", preexec_fn=limit_fds)
        self.assert_pass(rc, data)
        self.assertEqual(data["results"][0]["chunks"], 40)
        self.assertEqual(dest.read_bytes(), payload)

    def test_existing_pipe_pane_is_preserved(self) -> None:
        remote = self.tmp / "file.txt"
        remote.write_text("payload\n")
        log = self.tmp / "pane.log"
        self.tmux("pipe-pane", "-t", "remote", f"cat >> {shlex.quote(str(log))}")
        helpers = set(glob.glob("/tmp/tsw-dl-*.py"))
        rc, data = self.run_script("transfer.py", "get", "--session", "remote",
                                   "--remote-path", str(remote), "--dest", str(self.tmp / "out.txt"))
        self.assertEqual((rc, data["status"]), (1, "FAIL"))
        self.assertIn("output pipe", data["results"][0]["error"])
        pipe = self.tmux("display-message", "-p", "-t", "remote", "#{pane_pipe}").stdout.strip()
        self.assertEqual(pipe, "1")
        # A refused download must not leave its remote helper behind.
        self.assertEqual(set(glob.glob("/tmp/tsw-dl-*.py")) - helpers, set())
        # The user's log keeps receiving output, and the session stays usable.
        rc, data = self.run_script("tmux_exec.py", "--sessions", "remote", "--command", "echo still-logging")
        self.assert_pass(rc, data)
        self.assertTrue(wait_until(lambda: "still-logging" in log.read_text(errors="replace")))


class JobTests(TmuxCase):
    def job(self, mode: str, job_id: str, root: Path, *extra: str,
            session: str = "remote") -> tuple[int, dict]:
        return self.run_script("remote_job.py", mode, "--sessions", session, "--job-id", job_id,
                               "--job-root", str(root), *extra)

    def test_launch_status_stop_with_spaces(self) -> None:
        root = self.tmp / "job root"
        cwd = self.tmp / "work dir"
        cwd.mkdir()
        rc, data = self.job("launch", "spaced", root, "--cwd", str(cwd),
                            "--command", "pwd; echo hello-job; sleep 600")
        self.assert_pass(rc, data)
        meta = data["results"][0]["meta"]
        self.assertEqual(meta["cwd"], str(cwd))
        self.assertTrue((root / "spaced" / "meta.json").is_file())

        def tail_ready() -> bool:
            return "hello-job" in (root / "spaced" / "stdout.log").read_text(errors="replace")
        self.assertTrue(wait_until(tail_ready))
        rc, data = self.job("status", "spaced", root)
        self.assert_pass(rc, data)
        self.assertEqual(data["results"][0]["state"], "RUNNING")
        self.assertIn(str(cwd), data["results"][0]["job"]["tail"])

        rc, data = self.job("stop", "spaced", root, "--grace-seconds", "3")
        self.assert_pass(rc, data)
        self.assertFalse(group_alive(meta["pgid"]))
        rc, data = self.job("status", "spaced", root)
        self.assert_pass(rc, data)
        self.assertNotEqual(data["results"][0]["state"], "RUNNING")

    def test_stop_kills_child_that_ignores_term(self) -> None:
        root = self.tmp / "jobs"
        rc, data = self.job("launch", "stubborn", root,
                            "--command", "bash -c 'trap \"\" TERM; exec sleep 600' & wait")
        self.assert_pass(rc, data)
        pgid = data["results"][0]["meta"]["pgid"]
        time.sleep(1.0)
        rc, data = self.job("stop", "stubborn", root, "--grace-seconds", "2")
        self.assert_pass(rc, data)
        self.assertIn("KILL", data["results"][0]["result"]["signals"])
        self.assertFalse(group_alive(pgid))

    def test_stop_group_after_main_pid_exited(self) -> None:
        root = self.tmp / "jobs"
        rc, data = self.job("launch", "orphan", root, "--command", "sleep 600 & exit 0")
        self.assert_pass(rc, data)
        meta = data["results"][0]["meta"]
        self.assertTrue(wait_until(lambda: (root / "orphan" / "rc").exists()))
        self.assertTrue(wait_until(lambda: subprocess.run(
            ["kill", "-0", str(meta["pid"])], capture_output=True).returncode != 0))
        rc, data = self.job("status", "orphan", root)
        self.assertEqual(data["results"][0]["state"], "RUNNING")
        rc, data = self.job("stop", "orphan", root, "--grace-seconds", "3")
        self.assert_pass(rc, data)
        self.assertFalse(group_alive(meta["pgid"]))

    def test_launch_without_ps_starts_nothing(self) -> None:
        # A slim image without `ps`: launch must fail before Popen, otherwise
        # the job keeps running with no meta.json and cannot be managed.
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for tool in ("bash", "python3", "sleep"):
            (bindir / tool).symlink_to(shutil.which(tool))
        self.start_session("slim", f"env PATH={shlex.quote(str(bindir))} {SHELL}")
        root = self.tmp / "jobs"
        started = self.tmp / "started"
        rc, data = self.run_script("remote_job.py", "launch", "--sessions", "slim", "--job-id", "slim",
                                   "--job-root", str(root),
                                   "--command", f"echo yes > {shlex.quote(str(started))}; sleep 30")
        self.assertEqual((rc, data["status"]), (1, "FAIL"))
        self.assertIn("ps", data["results"][0]["error"])
        time.sleep(1.5)
        self.assertFalse(started.exists(), "the job ran although launch reported FAIL")
        self.assertFalse((root / "slim").exists())

    def test_launch_with_busybox_ps_starts_nothing(self) -> None:
        # BusyBox ps exists but rejects the BSD selectors that status/stop need.
        self.shim_session("bb", ps="echo \"ps: invalid option -- 'x'\" >&2\nexit 1\n")
        root = self.tmp / "jobs"
        started = self.tmp / "started"
        rc, data = self.job("launch", "bb", root, "--command",
                            f"echo yes > {shlex.quote(str(started))}; sleep 30", session="bb")
        self.assertEqual((rc, data["status"]), (1, "FAIL"))
        self.assertIn("ps", data["results"][0]["error"])
        time.sleep(1.5)
        self.assertFalse(started.exists(), "the job ran although launch reported FAIL")
        self.assertFalse((root / "bb").exists())

    def test_stop_removed_rm_container(self) -> None:
        # Fake docker: `stop` removes the container as `run --rm` would, after
        # which inspect fails with "No such object".
        gone = shlex.quote(str(self.tmp / "container removed"))
        self.shim_session("host", docker=(
            "for target; do :; done\n"
            "case \"$1\" in\n"
            f"inspect) if [ -e {gone} ]; then echo \"Error: No such object: $target\" >&2; exit 1; fi\n"
            "  case \"$3\" in *Id*) echo 'true fake0123';; *) echo true;; esac;;\n"
            "exec) shift; if [ \"$1\" = -w ]; then cd \"$2\" || exit 1; shift 2; fi; shift; exec \"$@\";;\n"
            f"stop) : > {gone};;\n"
            "*) echo \"fake docker: $1 unsupported\" >&2; exit 2;;\n"
            "esac\n"))
        root = self.tmp / "jobs"
        rc, data = self.job("launch", "ctr", root, "--container", "trainer", "--command", "sleep 600",
                            session="host")
        self.assert_pass(rc, data)
        pgid = data["results"][0]["meta"]["pgid"]
        rc, data = self.job("stop", "ctr", root, "--grace-seconds", "2", session="host")
        self.assert_pass(rc, data)
        self.assertTrue(data["results"][0]["result"]["container_stopped"])
        self.assertFalse(group_alive(pgid))

    def test_instant_exit_job_records_start_stamp(self) -> None:
        # The unreaped child stays visible as a zombie, so ps still dates it.
        root = self.tmp / "jobs"
        rc, data = self.job("launch", "instant", root, "--command", "true")
        self.assert_pass(rc, data)
        self.assertRegex(data["results"][0]["meta"]["pid_started"], r"\d\d:\d\d:\d\d \d{4}$")

    def test_missing_stamp_still_stops_own_job(self) -> None:
        root = self.tmp / "jobs"
        rc, data = self.job("launch", "legacy", root, "--command", "sleep 600")
        self.assert_pass(rc, data)
        meta_file = root / "legacy" / "meta.json"
        meta = json.loads(meta_file.read_text())
        meta["pid_started"] = ""
        meta_file.write_text(json.dumps(meta))
        rc, data = self.job("stop", "legacy", root, "--grace-seconds", "2")
        self.assert_pass(rc, data)
        self.assertFalse(group_alive(meta["pgid"]))

    def test_missing_stamp_refuses_reused_pid(self) -> None:
        # The recorded PID now leads an unrelated group started long after the
        # job; without a stamp, stop must refuse rather than kill it.
        stranger = subprocess.Popen(["sleep", "600"], start_new_session=True)
        self.addCleanup(stranger.wait)
        self.addCleanup(stranger.kill)
        job = self.tmp / "jobs" / "reused"
        job.mkdir(parents=True)
        (job / "meta.json").write_text(json.dumps({
            "job_id": "reused", "pid": stranger.pid, "pgid": stranger.pid, "pid_started": "",
            "container": "", "started_at": time.time() - 3600}))
        rc, data = self.job("stop", "reused", job.parent, "--grace-seconds", "1")
        self.assertEqual((rc, data["status"]), (1, "FAIL"))
        self.assertIn("PID", data["results"][0]["error"])
        self.assertIsNone(stranger.poll(), "an unrelated process group was signalled")


class BatchTests(TmuxCase):
    def test_disconnect_on_busy_shell_is_timeout(self) -> None:
        rc, data = self.run_plan([{"name": "busy", "command": "sleep 30", "expect": "disconnect",
                                   "timeout_seconds": 4}])
        self.assertEqual(rc, 1)
        row = data["stages"][0]["results"][0]
        self.assertEqual(row["status"], "TIMEOUT", row)
        self.assertNotIn("disconnected", row)

    def test_disconnect_when_session_closes(self) -> None:
        # The pane command is the shell itself (like ssh or docker exec), so
        # `exit` removes the pane and session; with no other session tmux
        # exits too. Either way the step passes and leaves no stale lock.
        for keeper, reason in ((True, "pane closed"), (False, "tmux server exited")):
            with self.subTest(keeper=keeper):
                self.tmux("kill-server", check=False)
                # kill-server returns before the old server stops listening.
                self.assertTrue(wait_until(lambda: "no server running" in self.tmux(
                    "has-session", check=False).stderr))
                self.start_session("remote")
                if keeper:
                    self.start_session("keeper")
                rc, data = self.run_plan([{"name": "leave", "command": "exit", "expect": "disconnect",
                                           "timeout_seconds": 15}])
                self.assert_pass(rc, data)
                row = data["stages"][0]["results"][0]
                self.assertEqual((row["disconnected"], row["reason"]), (True, reason))
                self.start_session("remote")
                rc, data = self.run_script("tmux_exec.py", "--sessions", "remote", "--command", "true")
                self.assert_pass(rc, data)


if __name__ == "__main__":
    unittest.main()
