"""The owner-run AWS scripts and adv-ctl against a fake aws, ssh, and rsync on PATH; no cloud, no network."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AWS_DIR = REPO_ROOT / "harness" / "aws"
BASH = shutil.which("bash")
MUTATING_PREFIXES = ("create-", "put-", "add-", "associate-", "delete-", "update-", "attach-", "authorize-", "revoke-",
                     "run-", "start-", "stop-", "terminate-")

FAKE_AWS = """#!{python}
import json, os, shutil, sys
argv = sys.argv[1:]
log = os.environ["FAKE_AWS_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps(argv) + "\\n")
for i, arg in enumerate(argv):
    if arg == "--user-data" and argv[i + 1].startswith("file://"):
        shutil.copy(argv[i + 1][7:], log + ".userdata")
script = {{}}
if os.environ.get("FAKE_AWS_SCRIPT"):
    with open(os.environ["FAKE_AWS_SCRIPT"]) as handle:
        script = json.load(handle)
key = " ".join(argv[:2])
responses = script.get(key)
if not responses:
    sys.exit(0)
counts_path = log + ".counts"
try:
    with open(counts_path) as handle:
        counts = json.load(handle)
except (OSError, ValueError):
    counts = {{}}
index = counts.get(key, 0)
counts[key] = index + 1
with open(counts_path, "w") as handle:
    json.dump(counts, handle)
response = responses[min(index, len(responses) - 1)]
sys.stdout.write(response.get("stdout", ""))
sys.stderr.write(response.get("stderr", ""))
sys.exit(response.get("rc", 0))
"""
FAKE_TOOL = "#!/bin/bash\nprintf '%s %s\\n' \"$(basename \"$0\")\" \"$*\" >> \"$FAKE_LOG\"\n"
CAPACITY = "\nAn error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation: no g5.xlarge in this az\n"
SAMPLE_SSH_CONFIG = """Host other
  HostName 1.2.3.4
  User me

Host harness-box
  HostName 9.9.9.9
  User ubuntu
  IdentityFile ~/.ssh/k.pem

Host last
  HostName 5.5.5.5
"""


@unittest.skipUnless(BASH, "bash unavailable")
class ScriptFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "aws.log"
        self.tool_log = self.base / "tools.log"
        (self.bin / "aws").write_text(FAKE_AWS.format(python=sys.executable), encoding="utf-8")
        for tool in ("ssh", "rsync", "curl"):
            (self.bin / tool).write_text(FAKE_TOOL, encoding="utf-8")
        for tool in ("aws", "ssh", "rsync", "curl"):
            (self.bin / tool).chmod(0o755)
        self.home = self.base / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, script, *args, script_json=None, env=None, timeout=120):
        script_path = self.base / "script.json"
        script_path.write_text(json.dumps(script_json or {}), encoding="utf-8")
        merged = {"PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "HOME": str(self.home), "FAKE_AWS_LOG": str(self.log),
                  "FAKE_AWS_SCRIPT": str(script_path), "FAKE_LOG": str(self.tool_log), "ADV_CTL_CONFIG": str(self.base / "none.env"),
                  "LANG": os.environ.get("LANG", "C"), "TMPDIR": str(self.base),
                  "KEY_NAME": "adv-test-key", "SG": "sg-0test"}
        merged.update(env or {})
        for stale in (self.log, self.tool_log, Path(str(self.log) + ".counts")):
            if stale.exists():
                stale.unlink()
        return subprocess.run([BASH, str(script), *args], capture_output=True, text=True, env=merged, timeout=timeout)

    def aws_calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def tool_calls(self):
        return self.tool_log.read_text(encoding="utf-8").splitlines() if self.tool_log.exists() else []


class SyntaxTests(ScriptFixture):
    def test_every_script_parses(self):
        scripts = sorted(AWS_DIR.glob("*.sh")) + [AWS_DIR / "adv-ctl", AWS_DIR / "adv-drop", AWS_DIR / "adv-sync-inbox"]
        for script in scripts:
            done = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"{script.name}: {done.stderr}")
        for name in ("adv-ctl", "launch-gpu-box.sh", "gpu-box-bootstrap.sh", "setup-iam.sh", "setup-budgets.sh"):
            self.assertTrue(os.access(AWS_DIR / name, os.X_OK), name)
        for doc in sorted((AWS_DIR / "iam").glob("*.json")):
            json.loads(doc.read_text(encoding="utf-8"))
        self.assertEqual(len(list((AWS_DIR / "iam").glob("*.json"))), 4)
        policy = json.loads((AWS_DIR / "iam" / "adv-harness-box-policy.json").read_text(encoding="utf-8"))
        start_stop = [s for s in policy["Statement"] if "ec2:StartInstances" in s["Action"]][0]
        self.assertEqual(start_stop["Condition"], {"StringEquals": {"aws:ResourceTag/adv-loop": "gpu"}})


class LaunchTests(ScriptFixture):
    def test_run_instances_argv_tags_metadata_and_az_fallback(self):
        script = {
            "ec2 describe-instances": [{"stdout": "\n"}, {"stdout": "i-0new\tus-east-2b\t172.31.9.9\n"}],
            "ec2 describe-subnets": [{"stdout": "subnet-a\n"}, {"stdout": "subnet-b\n"}, {"stdout": "subnet-c\n"}],
            "ec2 run-instances": [{"rc": 254, "stderr": CAPACITY}, {"stdout": "i-0new\n"}],
        }
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKey/With+Chars= adv-harness-box"
        done = self.run_script(AWS_DIR / "launch-gpu-box.sh", script_json=script, env={"GPU_AUTHORIZED_KEY": key})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip().splitlines()[-1], "i-0new\tus-east-2b\t172.31.9.9")
        self.assertIn("no g5.xlarge capacity in us-east-2a", done.stderr)
        runs = [c for c in self.aws_calls() if c[:2] == ["ec2", "run-instances"]]
        self.assertEqual(len(runs), 2)
        self.assertEqual([r[r.index("--subnet-id") + 1] for r in runs], ["subnet-a", "subnet-b"])
        first = runs[0]
        tag_spec = first.index("--tag-specifications")
        tags = "Tags=[{Key=Name,Value=adv-gpu-box},{Key=adv-loop,Value=gpu}]"
        self.assertEqual(first[tag_spec + 1:tag_spec + 3], [f"ResourceType=instance,{tags}", f"ResourceType=volume,{tags}"])
        self.assertEqual(first[first.index("--metadata-options") + 1], "HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled")
        self.assertEqual(first[first.index("--instance-initiated-shutdown-behavior") + 1], "stop")
        self.assertEqual(first[first.index("--image-id") + 1], "ami-07639bdb1dc95014c")
        self.assertEqual(first[first.index("--instance-type") + 1], "g5.xlarge")
        self.assertEqual(first[first.index("--key-name") + 1], "adv-test-key")
        self.assertEqual(first[first.index("--security-group-ids") + 1], "sg-0test")
        self.assertEqual(first[first.index("--region") + 1], "us-east-2")
        self.assertEqual(json.loads(first[first.index("--block-device-mappings") + 1]),
                         [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 200, "VolumeType": "gp3", "DeleteOnTermination": True}}])
        self.assertTrue(first[first.index("--user-data") + 1].startswith("file://"))
        userdata = Path(str(self.log) + ".userdata").read_text(encoding="utf-8")
        self.assertTrue(userdata.startswith("#cloud-config"))
        self.assertIn(f"- 'restrict,from=\"172.31.0.0/16\" {key}'", userdata)
        self.assertNotIn("__GPU_AUTHORIZED_KEY__", userdata)
        self.assertNotIn("__VPC_CIDR__", userdata)
        self.assertIn("/srv/adv-loop/cache", userdata)
        waits = [c for c in self.aws_calls() if c[:3] == ["ec2", "wait", "instance-running"]]
        self.assertEqual(waits[0][waits[0].index("--instance-ids") + 1], "i-0new")
        subnet_calls = [c for c in self.aws_calls() if c[:2] == ["ec2", "describe-subnets"]]
        self.assertIn("Name=availability-zone,Values=us-east-2a", subnet_calls[0])
        self.assertIn("Name=default-for-az,Values=true", subnet_calls[0])
        self.assertEqual(len(subnet_calls), 2, "the third AZ was never needed")

    def test_existing_box_short_circuits_and_a_bad_key_is_refused(self):
        script = {"ec2 describe-instances": [{"stdout": "i-exists\n"}]}
        done = self.run_script(AWS_DIR / "launch-gpu-box.sh", script_json=script, env={"GPU_AUTHORIZED_KEY": "ssh-ed25519 AAAA x"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "i-exists")
        self.assertFalse([c for c in self.aws_calls() if c[:2] == ["ec2", "run-instances"]])
        done = self.run_script(AWS_DIR / "launch-gpu-box.sh", env={"GPU_AUTHORIZED_KEY": "ssh-rsa AAAA x"})
        self.assertEqual(done.returncode, 2)
        done = self.run_script(AWS_DIR / "launch-gpu-box.sh")
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(self.aws_calls())

    def test_every_az_exhausted_exits_3(self):
        script = {"ec2 describe-instances": [{"stdout": "\n"}], "ec2 describe-subnets": [{"stdout": "subnet-x\n"}],
                  "ec2 run-instances": [{"rc": 254, "stderr": CAPACITY}]}
        done = self.run_script(AWS_DIR / "launch-gpu-box.sh", script_json=script, env={"GPU_AUTHORIZED_KEY": "ssh-ed25519 AAAA x"})
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(len([c for c in self.aws_calls() if c[:2] == ["ec2", "run-instances"]]), 3)


class SetupTests(ScriptFixture):
    def mutating(self):
        return [c for c in self.aws_calls() if len(c) > 1 and c[1].startswith(MUTATING_PREFIXES)]

    def test_setup_iam_dry_run_mutates_nothing(self):
        script = {"iam get-role": [{"rc": 254, "stderr": "NoSuchEntity"}], "iam get-role-policy": [{"rc": 254}],
                  "iam get-instance-profile": [{"rc": 254}], "ec2 describe-iam-instance-profile-associations": [{"stdout": "None\n"}]}
        done = self.run_script(AWS_DIR / "setup-iam.sh", script_json=script, env={"ADV_LOOP_HARNESS_BOX": "i-0harness"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.mutating(), [])
        self.assertIn("dry-run: aws iam create-role --role-name adv-harness-box", done.stdout)
        self.assertIn("dry-run: aws iam put-role-policy --role-name adv-harness-box --policy-name adv-harness-box-gpu", done.stdout)
        self.assertIn("dry-run: aws iam create-instance-profile --instance-profile-name adv-harness-box", done.stdout)
        self.assertIn("dry-run: aws iam add-role-to-instance-profile", done.stdout)
        self.assertIn("dry-run: aws ec2 associate-iam-instance-profile --region us-east-2 --instance-id i-0harness "
                      "--iam-instance-profile Name=adv-harness-box", done.stdout)
        self.assertIn("adv-harness-box-policy.json", done.stdout)
        self.assertTrue(all(c[1].startswith(("get-", "describe-", "list-")) for c in self.aws_calls()))

    def test_setup_iam_apply_is_idempotent_when_everything_exists(self):
        script = {"iam get-role": [{"stdout": "{}"}], "iam get-role-policy": [{"stdout": "{}"}],
                  "iam get-instance-profile": [{"stdout": "adv-harness-box\n"}],
                  "ec2 describe-iam-instance-profile-associations": [{"stdout": "arn:aws:iam::1:instance-profile/adv-harness-box\n"}]}
        done = self.run_script(AWS_DIR / "setup-iam.sh", "--apply", script_json=script, env={"ADV_LOOP_HARNESS_BOX": "i-0harness"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([c[1] for c in self.mutating()], ["put-role-policy"], "only the inline policy is refreshed")

    def test_setup_budgets_dry_run_mutates_nothing(self):
        script = {"sts get-caller-identity": [{"stdout": "123456789012\n"}], "iam get-role": [{"rc": 254}],
                  "budgets describe-budget": [{"rc": 254}], "budgets describe-budget-actions-for-budget": [{"rc": 254}],
                  "cloudwatch describe-alarms": [{"stdout": "None\n"}]}
        done = self.run_script(AWS_DIR / "setup-budgets.sh", script_json=script,
                               env={"ADV_LOOP_OWNER_EMAIL": "owner@example.com", "ADV_LOOP_HARNESS_BOX": "i-0harness", "ADV_LOOP_GPU_BOX": "i-0gpu"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.mutating(), [])
        out = done.stdout
        self.assertIn("dry-run: aws iam create-role --role-name adv-budgets-stop-ec2", out)
        self.assertIn('"BudgetName":"adv-aws-monthly","BudgetLimit":{"Amount":"150","Unit":"USD"},"TimeUnit":"MONTHLY"', out)
        self.assertIn('"BudgetName":"adv-aws-lifetime","BudgetLimit":{"Amount":"1800","Unit":"USD"},"TimeUnit":"ANNUALLY"', out)
        self.assertIn('"TimePeriod":{"Start":"2026-09-01T00:00:00Z"}', out)
        self.assertIn("--action-type RUN_SSM_DOCUMENTS", out)
        self.assertIn('"ActionSubType":"STOP_EC2_INSTANCES","Region":"us-east-2","InstanceIds":["i-0harness","i-0gpu"]', out)
        self.assertIn("--execution-role-arn arn:aws:iam::123456789012:role/adv-budgets-stop-ec2", out)
        self.assertIn("--alarm-name adv-gpu-box-idle-stop", out)
        self.assertIn("--alarm-actions arn:aws:automate:us-east-2:ec2:stop", out)
        self.assertIn("--treat-missing-data notBreaching", out)
        monthly = [line for line in out.splitlines() if "adv-aws-monthly" in line and "create-budget " in line][0]
        lifetime = [line for line in out.splitlines() if "adv-aws-lifetime" in line and "create-budget " in line][0]
        action = [line for line in out.splitlines() if "create-budget-action" in line][0]
        self.assertEqual(monthly.count('"SubscriptionType":"EMAIL","Address":"owner@example.com"'), 3)
        self.assertEqual(lifetime.count('"SubscriptionType":"EMAIL","Address":"owner@example.com"'), 3)
        self.assertEqual(action.count("owner@example.com"), 1)
        self.assertEqual([monthly.count(f'"Threshold":{t}') for t in (50, 80, 100, 95)], [1, 1, 1, 0])
        self.assertEqual([lifetime.count(f'"Threshold":{t}') for t in (50, 80, 95, 100)], [1, 1, 1, 0])
        self.assertNotIn("STOP_EC2", monthly)
        done = self.run_script(AWS_DIR / "setup-budgets.sh", script_json=script, env={"ADV_LOOP_GPU_BOX": "i-0gpu"})
        self.assertNotEqual(done.returncode, 0, "the owner email is required")


class AdvCtlTests(ScriptFixture):
    def rewrite(self, config, ip, host="harness-box"):
        path = self.base / "ssh_config"
        path.write_text(config, encoding="utf-8")
        done = self.run_script(AWS_DIR / "adv-ctl", "_rewrite-hostname", str(path), host, ip)
        self.assertEqual(done.returncode, 0, done.stderr)
        return path.read_text(encoding="utf-8")

    def block(self, text, host):
        lines = []
        inside = False
        for line in text.splitlines():
            if line.startswith("Host "):
                inside = line == f"Host {host}"
                continue
            if inside and line.strip():
                lines.append(line.strip())
        return lines

    def test_hostname_rewrite_touches_only_the_harness_box_block(self):
        text = self.rewrite(SAMPLE_SSH_CONFIG, "3.3.3.3")
        self.assertEqual(self.block(text, "harness-box"), ["HostKeyAlias harness-box", "HostName 3.3.3.3", "User ubuntu", "IdentityFile ~/.ssh/k.pem"])
        self.assertEqual(self.block(text, "other"), ["HostName 1.2.3.4", "User me"])
        self.assertEqual(self.block(text, "last"), ["HostName 5.5.5.5"])
        again = self.rewrite(text, "4.4.4.4")
        self.assertEqual(again.count("HostKeyAlias harness-box"), 1)
        self.assertIn("HostName 4.4.4.4", self.block(again, "harness-box"))
        self.assertNotIn("9.9.9.9", again)
        self.assertNotIn("3.3.3.3", again)

    def test_missing_block_or_hostname_is_added(self):
        text = self.rewrite("Host other\n  HostName 1.2.3.4\n", "7.7.7.7")
        self.assertEqual(self.block(text, "harness-box"), ["HostName 7.7.7.7", "HostKeyAlias harness-box", "User ubuntu"])
        self.assertEqual(self.block(text, "other"), ["HostName 1.2.3.4"])
        text = self.rewrite("Host harness-box\n  User ubuntu\n\nHost other\n  HostName 1.2.3.4\n", "8.8.8.8")
        self.assertEqual(self.block(text, "harness-box"), ["HostKeyAlias harness-box", "HostName 8.8.8.8", "User ubuntu"])
        self.assertEqual(self.block(text, "other"), ["HostName 1.2.3.4"])

    def test_drop_stages_through_incoming_then_moves(self):
        drop = self.base / "goal"
        drop.mkdir()
        (drop / "goal.md").write_text("do it", encoding="utf-8")
        done = self.run_script(AWS_DIR / "adv-ctl", "drop", str(drop), "--no-start", env={"ADV_LOOP_HARNESS_BOX": "i-h"})
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.tool_calls()
        self.assertEqual([c.split()[0] for c in calls], ["ssh", "rsync", "ssh"])
        self.assertIn("install -d -o advloop -g advloop /srv/adv-loop/inbox /srv/adv-loop/inbox/.incoming", calls[0])
        self.assertIn("--rsync-path=sudo rsync", calls[1])
        self.assertIn(f"{drop}/ harness-box:/srv/adv-loop/inbox/.incoming/goal-", calls[1])
        self.assertIn("sudo chown -R advloop:advloop '/srv/adv-loop/inbox/.incoming/goal-", calls[2])
        self.assertIn("sudo mv '/srv/adv-loop/inbox/.incoming/goal-", calls[2])
        self.assertIn("' '/srv/adv-loop/inbox/goal-", calls[2])
        self.assertFalse(self.aws_calls(), "--no-start never touches aws")
        self.assertIn("dropped /srv/adv-loop/inbox/goal-", done.stdout)

    def test_config_file_and_usage(self):
        config = self.base / "adv-ctl.env"
        config.write_text("# fleet\nADV_LOOP_HARNESS_BOX=i-from-file\nexport ADV_LOOP_GPU_BOX=\"i-gpu\"\n", encoding="utf-8")
        script = {"ec2 describe-instances": [{"stdout": "stopped\n"}]}
        done = self.run_script(AWS_DIR / "adv-ctl", "gpu", "stop", script_json=script, env={"ADV_CTL_CONFIG": str(config)})
        self.assertEqual(done.returncode, 0, done.stderr)
        describe = [c for c in self.aws_calls() if c[:2] == ["ec2", "describe-instances"]][0]
        self.assertEqual(describe[describe.index("--instance-ids") + 1], "i-gpu")
        self.assertIn("adv-harness gpu stop --state /srv/adv-loop/state --reason operator", self.tool_calls()[0])
        done = self.run_script(AWS_DIR / "adv-ctl", "help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("adv-ctl drop <dir>", done.stdout)
        self.assertEqual(self.run_script(AWS_DIR / "adv-ctl", "bogus").returncode, 2)
        done = self.run_script(AWS_DIR / "adv-ctl", "status")
        self.assertEqual(done.returncode, 2, "no harness box configured")
        self.assertIn("ADV_LOOP_HARNESS_BOX", done.stderr)


if __name__ == "__main__":
    unittest.main()
