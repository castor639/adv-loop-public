#!/usr/bin/env python3
"""C5 staging checker: the mechanical half of a self-referential criterion.

Some acceptance criteria name the loop's own post-criteria phases (an
independent verification pass, the final report). Those conjuncts cannot be
produced by a research attempt: the verifier is scheduled only once every
criterion is already `satisfied` (policy.criteria_evidence_ready), and the
report is an engine projection written by the synthesizer. This hook checks
everything about such a criterion that a research attempt CAN establish, so
that marking it `satisfied` is a machine-checked staging claim rather than a
prose promise, and the engine's own verifier and report gates still decide the
conjuncts they own.

Fixed procedure; nothing is taken from `input`, so the caller cannot choose
what gets checked:

  A  clean copy   -- artifacts/ + payload/ are copied to a fresh temp tree and
                     artifacts/run_all_checks.sh is re-run there; exit 0 required
  B  fingerprints -- sha256 of every file listed in artifacts/c5-stage-manifest.json
                     is recomputed IN THE CLEAN COPY and must equal the declared value
  C  evidence map -- artifacts/criterion-evidence-map.md must name every criterion in
                     state.json, each with >=1 evidence id that exists in evidence.jsonl,
                     and carry a non-empty "## Limitations" section
  D  disclosure   -- the map must carry DISCLOSURE verbatim

Exit 0 (accepted: true) iff A-D all hold. artifact_hash is the sha256 of the
evidence map; log_hash covers this script's own transcript. Stdlib only; lives
outside src/ like every checker.
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHECKER_ID = "c5-stage-check"
CHECKER_VERSION = "1.0"

DISCLOSURE = (
    "C5's verification and report conjuncts are delegated to the engine's own gates: "
    "they are discharged by the verifier attempt and the synthesizer report, "
    "not claimed as already performed."
)

MANIFEST = "artifacts/c5-stage-manifest.json"
MAP = "artifacts/criterion-evidence-map.md"
SUITE = "artifacts/run_all_checks.sh"
EVIDENCE_ID = re.compile(r"\bE\d{6}\b")
SKIP_DIRS = {"__pycache__", "verdicts", ".git"}


class Log:
    def __init__(self):
        self.lines = []
        self.ok = True

    def check(self, passed, label, detail=""):
        self.ok = self.ok and bool(passed)
        self.lines.append(f"  [{'PASS' if passed else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
        return bool(passed)

    def note(self, text):
        self.lines.append(f"  .... {text}")

    def text(self):
        return "\n".join(self.lines) + "\n"


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def copy_tree(src, dst):
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc"),
        symlinks=False,
    )


def main():
    envelope = json.load(sys.stdin)
    workspace = Path(envelope["workspace"]).resolve()
    log = Log()
    log.note(f"workspace {workspace}")

    manifest_path = workspace / MANIFEST
    map_path = workspace / MAP
    have_manifest = log.check(manifest_path.is_file(), f"{MANIFEST} exists")
    have_map = log.check(map_path.is_file(), f"{MAP} exists")
    log.check((workspace / SUITE).is_file(), f"{SUITE} exists")

    # ---- A. clean-copy re-run -------------------------------------------------
    clean_root = None
    if log.ok:
        tmp = tempfile.mkdtemp(prefix="c5-stage-")
        clean_root = Path(tmp) / "clean"
        clean_root.mkdir(parents=True)
        for name in ("artifacts", "payload"):
            source = workspace / name
            if source.is_dir():
                copy_tree(source, clean_root / name)
        log.note(f"clean copy at {clean_root}")
        suite = clean_root / SUITE
        suite.chmod(0o755)
        run = subprocess.run(
            ["/bin/sh", str(suite)],
            cwd=str(clean_root / "artifacts"),
            capture_output=True, text=True, timeout=3000, check=False,
        )
        log.check(run.returncode == 0, "[A] run_all_checks.sh exits 0 in the clean copy",
                  f"returncode {run.returncode}")
        tail = (run.stdout or "")[-400:].replace("\n", " | ")
        log.note(f"suite stdout tail: {tail}")
        if run.returncode != 0:
            log.note(f"suite stderr tail: {(run.stderr or '')[-400:]}")

    # ---- B. fresh fingerprints in the clean copy ------------------------------
    if log.ok and have_manifest and clean_root is not None:
        try:
            declared = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            declared = None
            log.check(False, "[B] manifest parses as JSON", str(exc))
        files = (declared or {}).get("files")
        if log.check(isinstance(files, dict) and bool(files),
                     "[B] manifest declares a non-empty files map"):
            for relative, expected in sorted(files.items()):
                target = (clean_root / relative).resolve()
                if clean_root not in target.parents:
                    log.check(False, f"[B] {relative} escapes the clean copy")
                    continue
                if not target.is_file():
                    log.check(False, f"[B] {relative} present in the clean copy")
                    continue
                actual = sha256_file(target)
                log.check(actual == str(expected).lower(),
                          f"[B] fresh sha256 of {relative} matches the manifest",
                          f"{actual[:16]} vs {str(expected)[:16]}")

    # ---- C. criterion -> evidence map -----------------------------------------
    map_text = map_path.read_text(encoding="utf-8") if have_map else ""
    if have_map:
        try:
            state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
            criteria = [c["id"] for c in state["criteria"]]
        except (OSError, ValueError, KeyError) as exc:
            criteria = []
            log.check(False, "[C] state.json criteria readable", str(exc))
        known = set()
        try:
            for line in (workspace / "evidence.jsonl").read_text(encoding="utf-8").splitlines():
                if line.strip():
                    known.add(json.loads(line)["id"])
        except (OSError, ValueError, KeyError) as exc:
            log.check(False, "[C] evidence.jsonl readable", str(exc))
        sections = {}
        current = None
        for line in map_text.splitlines():
            head = re.match(r"^#{1,3}\s*(C\d+)\b", line.strip())
            if head:
                current = head.group(1)
                sections[current] = []
            elif current:
                sections[current].append(line)
        for criterion in criteria:
            body = "\n".join(sections.get(criterion, []))
            ids = [i for i in EVIDENCE_ID.findall(body) if i in known]
            log.check(criterion in sections, f"[C] map has a section for {criterion}")
            log.check(bool(ids), f"[C] {criterion} cites >=1 recorded evidence id",
                      ", ".join(sorted(set(ids))[:6]))
        limitations = re.search(r"^##\s*Limitations\s*$(.*?)(?=^##\s|\Z)",
                                map_text, re.MULTILINE | re.DOTALL)
        bullets = [ln for ln in (limitations.group(1).splitlines() if limitations else [])
                   if ln.strip().startswith(("-", "*"))]
        log.check(bool(limitations), "[C] map has a '## Limitations' section")
        log.check(len(bullets) >= 1, "[C] Limitations lists >=1 bullet", f"{len(bullets)} bullets")

    # ---- D. delegation disclosure ---------------------------------------------
    normalized = " ".join(map_text.split())
    log.check(DISCLOSURE in normalized, "[D] map carries the delegation disclosure verbatim")

    log.lines.append("")
    log.lines.append("C5 STAGING CHECKS " + ("PASSED" if log.ok else "FAILED"))
    transcript = log.text()
    sys.stderr.write(transcript)
    artifact_hash = sha256_file(map_path) if have_map else hashlib.sha256(b"").hexdigest()
    print(json.dumps({
        "accepted": bool(log.ok),
        "checker_id": CHECKER_ID,
        "checker_version": CHECKER_VERSION,
        "artifact_hash": artifact_hash,
        "log_hash": hashlib.sha256(transcript.encode("utf-8", "replace")).hexdigest(),
        "details": {"map": MAP, "manifest": MANIFEST, "transcript_tail": transcript[-1200:]},
    }))
    raise SystemExit(0 if log.ok else 1)


if __name__ == "__main__":
    main()
