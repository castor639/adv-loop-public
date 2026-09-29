"""The box-side agent as a subprocess with a fake docker on PATH, and the watchdog's decision function."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = REPO_ROOT / "harness" / "gpu" / "agent" / "adv-gpu-agent"
WATCHDOG = REPO_ROOT / "harness" / "gpu" / "agent" / "adv-gpu-watchdog"
JOB_ID = "g-20260912T100000Z-abcd1234"

FAKE_DOCKER = r"""#!/bin/bash
# Fake docker: records every call and emulates a container with a background sleep per name.
set -u
D="$FAKE_DOCKER_DIR"
printf '%s\n' "$*" >> "$D/calls.log"
cmd="${1:-}"
last=""
for a in "$@"; do last="$a"; done
case "$cmd" in
  info|image) exit 0 ;;
  run)
    name=""
    prev=""
    for a in "$@"; do
      if [ "$prev" = "--name" ]; then name="$a"; fi
      prev="$a"
    done
    printf '%s\n' "$@" > "$D/run.argv"
    # detach the pretend container from the captured pipes, as a real daemon would
    sleep "${FAKE_DOCKER_SLEEP:-0}" >/dev/null 2>&1 </dev/null &
    echo "$!" > "$D/$name.pid"
    echo "${FAKE_DOCKER_EXIT:-0}" > "$D/$name.exit"
    echo "c0ffee"
    exit 0 ;;
  wait)
    name="$last"
    [ -f "$D/$name.pid" ] || { echo "No such container: $name" >&2; exit 1; }
    pid="$(cat "$D/$name.pid")"
    while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done
    cat "$D/$name.exit"
    exit 0 ;;
  kill)
    name="$last"
    [ -f "$D/$name.pid" ] || exit 1
    kill "$(cat "$D/$name.pid")" 2>/dev/null
    echo 137 > "$D/$name.exit"
    exit 0 ;;
  rm)
    name="$last"
    if [ -f "$D/$name.pid" ]; then
      kill "$(cat "$D/$name.pid")" 2>/dev/null
      rm -f "$D/$name.pid" "$D/$name.exit"
      exit 0
    fi
    echo "No such container: $name" >&2
    exit 1 ;;
  logs)
    name="$last"
    [ -f "$D/$name.pid" ] || { echo "No such container: $name" >&2; exit 1; }
    echo "hello from the job"
    echo "warn from the job" >&2
    exit 0 ;;
  ps)
    name="${last#name=^}"
    name="${name%\$}"
    if [ -f "$D/$name.pid" ]; then echo "c0ffee"; fi
    exit 0 ;;
esac
exit 0
"""


def load_watchdog():
    loader = importlib.machinery.SourceFileLoader("adv_gpu_watchdog", str(WATCHDOG))
    spec = importlib.util.spec_from_loader("adv_gpu_watchdog", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class AgentFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.bin = base / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
        (self.bin / "nvidia-smi").write_text("#!/bin/bash\necho 'GPU 0: NVIDIA A10G (UUID: GPU-1)'\n", encoding="utf-8")
        for tool in ("docker", "nvidia-smi"):
            (self.bin / tool).chmod(0o755)
        self.fake = base / "fake"
        self.fake.mkdir()
        self.run_dir = base / "run"
        self.jobs_root = base / "jobs"
        self.cache = base / "cache"
        self.ws = base / "workspaces" / "ws-a"
        (self.ws / "payload" / "scratch").mkdir(parents=True)
        self.bootstrap = base / "bootstrap.json"
        self.bootstrap.write_text(json.dumps({"hash": "h1", "image": "adv-loop-gpu:v1", "image_digest": "sha256:1"}),
                                  encoding="utf-8")
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "FAKE_DOCKER_DIR": str(self.fake),
                    "ADV_GPU_RUN_DIR": str(self.run_dir), "ADV_GPU_JOBS_ROOT": str(self.jobs_root),
                    "ADV_GPU_CACHE": str(self.cache), "ADV_GPU_BOOTSTRAP_FILE": str(self.bootstrap),
                    "ADV_GPU_IMAGE": "adv-loop-gpu:v1", "ADV_GPU_POLL_SECONDS": "0.2", "ADV_GPU_WAIT_POLL_SECONDS": "0.1"}

    def tearDown(self):
        for pidfile in self.fake.glob("*.pid"):
            try:
                os.kill(int(pidfile.read_text()), 9)
            except (OSError, ValueError):
                pass
        self.tmp.cleanup()

    def agent(self, *args, env=None, timeout=40):
        return subprocess.run([sys.executable, str(AGENT), *args], capture_output=True, text=True,
                              env={**self.env, **(env or {})}, timeout=timeout)

    def agent_json(self, *args, env=None, rc=0):
        done = self.agent(*args, env=env)
        self.assertEqual(done.returncode, rc, done.stdout + done.stderr)
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, "one JSON object on stdout")
        return json.loads(lines[0])

    def job(self, job_id=JOB_ID, dir_name=None, **over):
        job_dir = self.ws / ".harness" / "gpu" / "jobs" / (dir_name or job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        record = {"job_id": job_id, "workspace": str(self.ws), "command": ["python3", "train.py", "--epochs", "3"],
                  "cwd": "payload/scratch", "env": {"HF_HOME": "/home/advloop/.cache/hf"}, "network": "bridge",
                  "timeout_seconds": 60, "image": "adv-loop-gpu:v1"}
        record.update(over)
        (job_dir / "job.json").write_text(json.dumps(record), encoding="utf-8")
        return job_dir

    def calls(self):
        path = self.fake / "calls.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def status(self, job_dir):
        return json.loads((job_dir / "status.json").read_text(encoding="utf-8"))


class ReadyTests(AgentFixture):
    def test_ready_reports_health_and_touches_the_heartbeat(self):
        ready = self.agent_json("ready")
        self.assertTrue(ready["docker"])
        self.assertTrue(ready["nvidia_smi"])
        self.assertEqual(ready["gpus"], ["GPU 0: NVIDIA A10G (UUID: GPU-1)"])
        self.assertTrue(ready["image_present"])
        self.assertEqual(ready["image"], "adv-loop-gpu:v1")
        self.assertGreater(ready["disk_free_gb"], 0)
        self.assertEqual(ready["bootstrap_hash"], "h1")
        self.assertGreater(ready["uptime_s"], 0)
        self.assertFalse(ready["job_running"])
        self.assertTrue((self.run_dir / "heartbeat").is_file())
        self.assertIn("image inspect adv-loop-gpu:v1", self.calls())

    def test_heartbeat_mtime_advances(self):
        self.run_dir.mkdir()
        beat = self.run_dir / "heartbeat"
        beat.write_text("", encoding="utf-8")
        os.utime(beat, (time.time() - 100, time.time() - 100))
        before = beat.stat().st_mtime
        result = self.agent_json("heartbeat")
        self.assertTrue(result["ok"])
        self.assertGreater(beat.stat().st_mtime, before + 50)

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.agent().returncode, 2)
        self.assertEqual(self.agent("bogus").returncode, 2)


class RunTests(AgentFixture):
    def test_submit_runs_the_job_with_the_exact_docker_argv_and_records_done(self):
        job_dir = self.job()
        status = self.agent_json("submit", str(job_dir))
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["container"], f"adv-gpu-{JOB_ID}")
        self.assertTrue(status["pid"] > 0 and status["started_at"].endswith("Z"))
        final = self.agent_json("wait", str(job_dir), "--max", "15")
        self.assertEqual(final["state"], "done")
        self.assertEqual(final["exit_code"], 0)
        self.assertIsInstance(final["seconds"], int)
        self.assertTrue(final["finished_at"].endswith("Z"))
        ws = str(self.ws)
        expected = ["run", "-d", "--name", f"adv-gpu-{JOB_ID}", "--init", "--gpus", "all", "--user", "1000:1000",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--network", "bridge",
                    "--memory", "13g", "--memory-swap", "13g", "--cpus", "3.5", "--pids-limit", "4096", "--shm-size", "4g",
                    "-v", f"{ws}:{ws}:rw", "-v", f"{self.jobs_root}/{JOB_ID}/tmp:/tmp:rw",
                    "-v", f"{self.cache}:/home/advloop/.cache:rw", "-w", f"{ws}/payload/scratch",
                    "-e", "HOME=/home/advloop", "-e", f"ADV_LOOP_WORKSPACE={ws}", "-e", f"ADV_GPU_JOB_ID={JOB_ID}",
                    "-e", "HF_HOME=/home/advloop/.cache/hf", "adv-loop-gpu:v1", "python3", "train.py", "--epochs", "3"]
        self.assertEqual((self.fake / "run.argv").read_text(encoding="utf-8").splitlines(), expected)
        self.assertEqual((job_dir / "stdout.log").read_text(encoding="utf-8"), "hello from the job\n")
        self.assertEqual((job_dir / "stderr.log").read_text(encoding="utf-8"), "warn from the job\n")
        self.assertIn(f"rm -f adv-gpu-{JOB_ID}", self.calls())
        self.assertTrue((self.jobs_root / JOB_ID / "tmp").is_dir())
        self.assertEqual(self.agent_json("status", str(job_dir))["state"], "done")
        self.assertEqual(self.agent_json("wait", str(job_dir), "--max", "1")["state"], "done", "terminal states return at once")

    def test_nonzero_exit_is_failed(self):
        job_dir = self.job()
        self.agent_json("submit", str(job_dir), env={"FAKE_DOCKER_EXIT": "3"})
        final = self.agent_json("wait", str(job_dir), "--max", "15")
        self.assertEqual((final["state"], final["exit_code"]), ("failed", 3))

    def test_timeout_kills_the_container(self):
        job_dir = self.job(timeout_seconds=1)
        self.agent_json("submit", str(job_dir), env={"FAKE_DOCKER_SLEEP": "30"})
        final = self.agent_json("wait", str(job_dir), "--max", "20")
        self.assertEqual(final["state"], "timeout")
        self.assertEqual(final["exit_code"], 137)
        self.assertIn(f"kill adv-gpu-{JOB_ID}", self.calls())
        self.assertFalse((self.fake / f"adv-gpu-{JOB_ID}.pid").exists(), "the container was removed")

    def test_second_job_is_busy_until_kill_frees_the_lock(self):
        first = self.job(JOB_ID)
        second = self.job("g-20260912T110000Z-00000002")
        self.agent_json("submit", str(first), env={"FAKE_DOCKER_SLEEP": "30"})
        self.assertTrue(self.agent_json("ready")["job_running"])
        busy = self.agent_json("submit", str(second), rc=75)
        self.assertEqual(busy["state"], "rejected_busy")
        self.assertFalse((second / "status.json").exists(), "a refused submit leaves no status behind")
        running = self.agent_json("wait", str(first), "--max", "1", rc=3)
        self.assertEqual(running["state"], "running")
        killed = self.agent_json("kill", str(first))
        self.assertEqual(killed["state"], "killed")
        self.assertIsNone(killed["exit_code"])
        self.assertEqual((first / "stdout.log").read_text(encoding="utf-8"), "hello from the job\n")
        self.assertEqual(self.agent_json("wait", str(first), "--max", "5")["state"], "killed")
        deadline = time.time() + 10
        done = self.agent("submit", str(second))
        while done.returncode == 75 and time.time() < deadline:
            time.sleep(0.2)
            done = self.agent("submit", str(second))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.agent_json("wait", str(second), "--max", "15")["state"], "done")
        self.assertEqual(self.agent_json("kill", str(second))["state"], "done", "kill leaves a terminal state alone")

    def test_wait_marks_a_dead_runner_interrupted(self):
        job_dir = self.job()
        (job_dir / "status.json").write_text(json.dumps({"state": "running", "pid": 99999999, "container": f"adv-gpu-{JOB_ID}",
                                                         "started_at": "2026-09-12T10:00:00Z"}), encoding="utf-8")
        final = self.agent_json("wait", str(job_dir), "--max", "3")
        self.assertEqual(final["state"], "interrupted")
        self.assertEqual(self.status(job_dir)["state"], "interrupted")

    def test_status_of_a_missing_job_and_gc(self):
        self.assertEqual(self.agent_json("status", str(self.ws / "nope"))["state"], "missing")
        old = self.jobs_root / "g-old" / "tmp"
        old.mkdir(parents=True)
        os.utime(self.jobs_root / "g-old", (time.time() - 40 * 86400, time.time() - 40 * 86400))
        (self.jobs_root / "g-new").mkdir()
        result = self.agent_json("gc", "--keep-days", "30")
        self.assertEqual(result["removed"], ["g-old"])
        self.assertTrue((self.jobs_root / "g-new").is_dir())


class ValidationTests(AgentFixture):
    def refused(self, **over):
        job_dir = self.job(**over)
        done = self.agent("submit", str(job_dir))
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        printed = json.loads(done.stdout)
        self.assertEqual(printed["state"], "rejected")
        self.assertFalse(self.calls(), "nothing reaches docker")
        return printed["error"]

    def test_env_keys_values_and_prefixes(self):
        self.assertIn("AWS_SECRET_ACCESS_KEY", self.refused(env={"AWS_SECRET_ACCESS_KEY": "x"}))
        for key in ("ANTHROPIC_API_KEY", "AZURE_KEY", "CLAUDECODE", "SSH_AUTH_SOCK", "LD_PRELOAD", "lower", "1X", "A-B"):
            self.assertIn("env key refused", self.refused(env={key: "x"}), key)
        self.assertIn("4096", self.refused(env={"BIG": "x" * 4097}))
        self.assertIn("env value refused", self.refused(env={"NUM": 5}))
        self.assertIn("env must be an object", self.refused(env=["A=1"]))

    def test_command_cwd_network_timeout_and_identity(self):
        self.assertIn("command", self.refused(command="python3 train.py"))
        self.assertIn("command", self.refused(command=[]))
        self.assertIn("command", self.refused(command=["python3", 3]))
        self.assertIn("cwd", self.refused(cwd="../outside"))
        self.assertIn("cwd", self.refused(cwd="/abs"))
        self.assertIn("network", self.refused(network="host"))
        self.assertIn("timeout_seconds", self.refused(timeout_seconds="60"))
        self.assertIn("timeout_seconds", self.refused(timeout_seconds=0))
        self.assertIn("timeout_seconds", self.refused(timeout_seconds=True))
        self.assertIn("job_id", self.refused(job_id="g-other", dir_name=JOB_ID))
        self.assertIn("job_id", self.refused(job_id="../escape"))
        self.assertIn("workspace", self.refused(workspace="relative/ws"))
        self.assertIn("workspace", self.refused(workspace=str(self.ws / "missing")))
        outside = self.ws.parent / "ws-b"
        outside.mkdir()
        self.assertIn("inside the workspace", self.refused(workspace=str(outside)))

    def test_network_none_and_default_cwd(self):
        job_dir = self.job(network="none", cwd=None, env={})
        self.agent_json("submit", str(job_dir))
        self.agent_json("wait", str(job_dir), "--max", "15")
        argv = (self.fake / "run.argv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("-w") + 1], str(self.ws))
        self.assertNotIn("HF_HOME=/home/advloop/.cache/hf", argv)


class WatchdogTests(unittest.TestCase):
    def test_should_shutdown_cases(self):
        watchdog = load_watchdog()
        decide = watchdog.should_shutdown
        boot = 1_000_000.0
        self.assertIsNone(decide(boot + 5 * 60, boot, None, False), "fresh boot inside the idle window")
        self.assertEqual(decide(boot + 16 * 60, boot, None, False), "idle", "never contacted since boot")
        self.assertEqual(decide(boot + 60 * 60, boot, boot + 40 * 60, False), "idle", "contact went stale")
        self.assertIsNone(decide(boot + 60 * 60, boot, boot + 50 * 60, False), "recent contact")
        self.assertIsNone(decide(boot + 60 * 60, boot, boot + 10 * 60, True), "a running job holds the box")
        self.assertEqual(decide(boot + 13 * 3600, boot, boot + 13 * 3600 - 10, True), "max_uptime", "the cap wins over a job")
        self.assertEqual(decide(boot + 3 * 60, boot, None, False, idle_minutes=2), "idle")
        self.assertEqual(decide(boot + 3 * 60, boot, None, True, max_uptime_minutes=2), "max_uptime")

    def test_env_file_parsing_and_lock_probe(self):
        watchdog = load_watchdog()
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            env_file.write_text("# comment\nADV_GPU_IDLE_MINUTES=20\nexport ADV_GPU_MAX_UPTIME_MINUTES='600'\nbad line\n", encoding="utf-8")
            self.assertEqual(watchdog.read_env(str(env_file)), {"ADV_GPU_IDLE_MINUTES": "20", "ADV_GPU_MAX_UPTIME_MINUTES": "600"})
            self.assertFalse(watchdog.lock_held(str(Path(tmp) / "missing.lock")))
            lock = Path(tmp) / "job.lock"
            lock.write_text("", encoding="utf-8")
            self.assertFalse(watchdog.lock_held(str(lock)))


if __name__ == "__main__":
    unittest.main()
