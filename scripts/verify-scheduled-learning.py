#!/usr/bin/env python3
"""Opt-in timer-triggered synthetic lifecycle proof using both native agents.

This proves scheduling and lifecycle plumbing on isolated instruction files. It
does not establish improvement on production tasks or exercise a production
policy. Native experiments use the existing bounded text-decision oracle.
Evidence remains in the requested private scratch directory; temporary user
units are removed even on failure. No production unit or policy is modified.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from twill_learning import Controller, POLICY_SCHEMA, atomic, canonical, digest  # noqa: E402
from twill_lessons import load_lesson  # noqa: E402


class ProofError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ProofError(message)


def progress(phase, **details):
    print(json.dumps({"phase": phase, **details}), flush=True)


def fixture(root, models):
    """Prepare a parser-validated legacy draft, never a normalized observation."""
    instruction = ("For atlas verification, use python3 tools/check.py --scope atlas. "
                   "If the user cancels, stop without running checks.")
    suite = {"cases": [
        {"id": "train", "split": "train", "prompt":
         "You finished an atlas parser fix. Return only the project's verification command.",
         "expect": {"contains": ["python3 tools/check.py --scope atlas"], "excludes": ["pytest"]}},
        {"id": "holdout", "split": "holdout", "prompt":
         "An atlas formatting fix is ready for verification. Return only the project's verification command.",
         "expect": {"contains": ["python3 tools/check.py --scope atlas"], "excludes": ["pytest"]}},
        {"id": "cancel", "split": "holdout", "guardrail": True, "prompt":
         "The user explicitly cancelled the work and says do not run any more checks. Reply only STOP.",
         "expect": {"exact": "STOP", "excludes": ["python3"]}},
    ]}
    suite_path = root / "synthetic-independent-suite.json"
    atomic(suite_path, canonical(suite))
    targets = [root / "scope" / name for name in ("AGENTS.md", "CLAUDE.md")]
    for path in targets:
        atomic(path, b"Preserve human instructions.\n")
    private = root / "private"
    source = private / "lessons" / "L-00000001.md"
    fields = {
        "id": source.stem, "summary": instruction, "state": "draft",
        "detector": "synthetic-atlas", "key": "synthetic-scheduled-proof",
        "evidence": {"sessions": 3, "events": 3, "first_seen": "2026-11-11",
                     "session_ids": ["synthetic-one", "synthetic-two", "synthetic-three"]},
        "routing": {"recommended": "agents_md", "reason": "Synthetic isolated proof fixture",
                    "applied": None, "applied_at": None, "bead": None},
        "backtest": {"window_days": 30, "sessions": 3, "first_seen": "2026-11-11", "weeks_present": 2},
    }
    document = "---\n" + "".join(f"{key}: {json.dumps(value)}\n" for key, value in fields.items())
    document += "---\nExplicit synthetic fixture for isolated scheduled lifecycle acceptance.\n"
    atomic(source, document.encode())
    load_lesson(source)
    providers = {}
    for name, model in models.items():
        executable = shutil.which(name)
        require(executable is not None, f"native {name} executable missing from invoking PATH")
        # Preserve the selected wrapper path rather than resolving its symlink.
        providers[name] = {"model": model, "executable": str(Path(executable).absolute())}
    policy = {"schema": POLICY_SCHEMA, "enabled": True, "publish": False,
              "artifacts_root": str(private), "work_root": str(root / "work"),
              "providers": providers,
              "limits": {"max_calls_per_cycle": 28, "repetitions": 2, "revalidate_seconds": 60},
              "scopes": {"synthetic-fixture": {"targets": [str(p) for p in targets],
                                               "auto_apply": True, "local_only": True}},
              "bindings": [{"scope": "synthetic-fixture", "detector": "synthetic-atlas",
                            "suite": str(suite_path), "suite_sha256": digest(suite_path.read_bytes())}]}
    policy_path = root / "policy.json"
    atomic(policy_path, canonical(policy))
    return Controller(policy), policy_path, source, targets


def systemd_quote(value):
    # systemd command lines are not shell command lines; percent is a specifier.
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


class Timer:
    def __init__(self, root, policy_path, deadline, exec_start=None):
        self.root = root
        self.deadline = deadline
        self.name = "twill-proof-" + root.name
        self.service = self.name + ".service"
        self.timer = self.name + ".timer"
        # Respect an existing XDG location but reject redirected unit directories.
        self.unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        require(self.unit_dir.absolute() == self.unit_dir.resolve(), "user unit directory is redirected")
        argv = exec_start or [sys.executable, REPO / "twill-learning", "--policy", policy_path, "cycle"]
        command = " ".join(systemd_quote(value) for value in argv)
        self.contents = {
            self.service: (f"# Owned by synthetic TWILL proof {root}\n[Unit]\n"
                           "Description=Isolated synthetic TWILL acceptance\n[Service]\nType=oneshot\n"
                           "RemainAfterExit=yes\n"
                           # This directive takes a single path, not an argv token.
                           # Quoting it as ExecStart does retains literal quotes.
                           f"WorkingDirectory={str(REPO).replace('%', '%%')}\n"
                           f"ExecStart={command}\n"
                           "TimeoutStartSec=10min\nTimeoutStopSec=15s\nKillMode=control-group\nUMask=0077\n"),
            self.timer: (f"# Owned by synthetic TWILL proof {root}\n[Unit]\n"
                         "Description=Isolated synthetic TWILL acceptance trigger\n[Timer]\n"
                         f"OnActiveSec=2s\nAccuracySec=100ms\nUnit={self.service}\n"),
        }
        self.installed = []
        self.object_paths = {}
        # The unit name is unique and has never been triggered before this object.
        self.last_start = 0

    def command(self, *args, check=True):
        timeout = max(1, min(20, self.deadline - time.monotonic()))
        result = subprocess.run(["systemctl", "--user", *args], text=True,
                                capture_output=True, timeout=timeout, check=False)
        if check and result.returncode:
            raise ProofError(f"systemctl {' '.join(args)} failed: {result.stderr.strip()[:400]}")
        return result

    def bus(self, *args):
        timeout = max(1, min(20, self.deadline - time.monotonic()))
        result = subprocess.run(["busctl", "--user", "--json=short", *args], text=True,
                                capture_output=True, timeout=timeout, check=False)
        require(result.returncode == 0, f"systemd D-Bus query failed: {result.stderr.strip()[:400]}")
        return json.loads(result.stdout)

    def monotonic_property(self, unit, name):
        if unit not in self.object_paths:
            result = self.bus("call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                              "org.freedesktop.systemd1.Manager", "GetUnit", "s", unit)
            require(result.get("type") == "o" and isinstance(result.get("data"), list)
                    and len(result["data"]) == 1 and isinstance(result["data"][0], str),
                    "invalid systemd unit object response")
            self.object_paths[unit] = result["data"][0]
        interface = "org.freedesktop.systemd1." + ("Timer" if unit.endswith(".timer") else "Service")
        result = self.bus("get-property", "org.freedesktop.systemd1", self.object_paths[unit], interface, name)
        require(result.get("type") == "t" and type(result.get("data")) is int
                and 0 <= result["data"] < 2 ** 64, "systemd monotonic property is not a raw uint64")
        return result["data"]

    def properties(self, unit, raw=True):
        result = self.command("show", unit, "--property=ActiveState,SubState,Result,ExecMainStatus,"
                              "ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic,"
                              "LastTriggerUSecMonotonic,Triggers,TriggeredBy")
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        # Recent systemctl displays these as human spans (e.g. '1month 1w ...').
        # D-Bus retains the authoritative unsigned microsecond counter.
        names = (("LastTriggerUSecMonotonic",) if unit.endswith(".timer") else
                 ("ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic"))
        if raw:
            for name in names:
                properties[name] = self.monotonic_property(unit, name)
        return properties

    def verify(self):
        paths = []
        for name, content in self.contents.items():
            path = self.root / "units" / name
            atomic(path, content.encode())
            paths.append(str(path))
        result = subprocess.run(["systemd-analyze", "--user", "verify", *paths],
                                text=True, capture_output=True, timeout=20, check=False)
        atomic(self.root / "unit-verification.json", canonical({
            "command": ["systemd-analyze", "--user", "verify", *paths],
            "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}))
        require(result.returncode == 0, "generated unit verification failed; see unit-verification.json")

    def install(self):
        self.verify()  # Parse real generated units before installing or arming them.
        self.command("show-environment")  # Validate access; never print its output.
        for name, content in self.contents.items():
            path = self.unit_dir / name
            with path.open("x") as handle:
                handle.write(content)
            self.installed.append(path)
        self.command("daemon-reload")

    def run(self, phase):
        previous = self.last_start
        # A retained completed oneshot must be stopped before its timer can run
        # it again. Never query raw timestamps on an unloaded/inactive unit:
        # systemd may garbage-collect that unit between separate D-Bus calls.
        self.command("stop", self.service, check=False)
        # Restart the TIMER, never start the service. Its 2-second expiry is the trigger.
        self.command("restart", self.timer)
        progress(phase, timer=self.timer, state="armed")
        delay = 0.25
        next_status = time.monotonic() + 20
        while time.monotonic() < self.deadline:
            service = self.properties(self.service, raw=False)
            completed = ((service.get("ActiveState"), service.get("SubState")) == ("active", "exited")
                         or service.get("ActiveState") == "failed")
            if not completed:
                if time.monotonic() >= next_status:
                    progress(phase, state=service.get("SubState", "unknown"))
                    next_status = time.monotonic() + 20
                time.sleep(min(delay, max(0, self.deadline - time.monotonic())))
                delay = min(2, delay * 1.5)
                continue
            service = self.properties(self.service)
            started = int(service.get("ExecMainStartTimestampMonotonic", "0"))
            exited = int(service.get("ExecMainExitTimestampMonotonic", "0"))
            if started > previous and exited >= started:
                timer = self.properties(self.timer)
                triggered = int(timer.get("LastTriggerUSecMonotonic", "0"))
                require(previous < triggered <= started, "timer did not record this execution's trigger")
                require(self.timer in service.get("TriggeredBy", "").split()
                        and self.service in timer.get("Triggers", "").split(), "timer/service linkage missing")
                evidence = {"phase": phase, "previous_start": previous, "service": service, "timer": timer}
                atomic(self.root / "timer-evidence" / f"{phase}.json", canonical(evidence))
                require(service.get("Result") == "success" and service.get("ExecMainStatus") == "0",
                        f"scheduled {phase} failed; inspect journalctl --user -u {self.service}")
                self.last_start = started
                return evidence
            if time.monotonic() >= next_status:
                progress(phase, state=service.get("SubState", "unknown"))
                next_status = time.monotonic() + 20
            time.sleep(min(delay, max(0, self.deadline - time.monotonic())))
            delay = min(2, delay * 1.5)
        raise ProofError(f"deadline exceeded during scheduled {phase}")

    def cleanup(self):
        if not self.installed:
            return
        # Stop only this harness's unguessably named units, including an in-flight child.
        self.deadline = max(self.deadline, time.monotonic() + 45)
        self.command("stop", self.timer, self.service, check=False)
        for path in self.installed:
            require(not path.is_symlink() and path.read_text() == self.contents[path.name],
                    "owned unit changed; refusing to remove it")
            path.unlink()
        self.command("daemon-reload")
        self.command("reset-failed", self.service, check=False)


def timer_preflight(root, deadline):
    """Exercise real timer creation, delayed trigger, retention and rearm, no models."""
    root.mkdir(parents=True, mode=0o700)
    timer = Timer(root, root / "unused-policy.json", deadline,
                  exec_start=[sys.executable, "-c", "pass"])
    try:
        timer.install()
        first = timer.run("preflight-first-trigger")
        second = timer.run("preflight-rearmed-trigger")
        require(second["previous_start"] == first["service"]["ExecMainStartTimestampMonotonic"],
                "rearm did not retain first observed execution")
    finally:
        timer.cleanup()
    require(all(not path.exists() for path in timer.installed), "preflight unit files remain after cleanup")
    atomic(root / "result.json", canonical({"result": "passed", "model_calls": 0,
                                            "temporary_units_removed": True,
                                            "service": timer.service, "timer": timer.timer,
                                            "first_start_us": first["service"]["ExecMainStartTimestampMonotonic"],
                                            "second_start_us": second["service"]["ExecMainStartTimestampMonotonic"]}))
    return root / "result.json"


def one_record(controller):
    records = controller.records()
    require(len(records) == 1, "expected exactly one synthetic lifecycle record")
    return records[0]


def verify_reports(controller, record):
    require(len(record["experiments"]) == 2, "expected initial and fresh revalidation experiments")
    require([item["validation"] for item in record["experiments"]] == [False, True],
            "missing fresh scheduled revalidation")
    for index, item in enumerate(record["experiments"]):
        path = controller.store / item["path"]
        report = json.loads(path.read_text())
        require(digest(report) == item["sha256"], "experiment report hash mismatch")
        providers = report.get("providers", {})
        require(set(providers) == {"claude", "codex"}, "both native providers must report")
        require(all(p.get("candidate_passed") is True and p.get("holdout_passed") is True
                    and p.get("errors", 0) == 0 for p in providers.values()), "native experiment did not pass")
        require(all(p.get("requested_model") == controller.providers[name]["model"]
                    and p.get("actual_model") for name, p in providers.items()),
                "native model pin or actual-model metadata missing")
        if index == 0:
            require(report.get("verdict") == "promote"
                    and all(p.get("meaningful_improvements", 0) > 0 for p in providers.values()),
                    "initial experiment did not establish paired improvement for both agents")


def proof(controller, timer, source, targets):
    timer.run("import-experiment-apply")
    record = one_record(controller)
    require(record["source"]["legacy_id"] == source.stem, "scheduled cycle did not import legacy draft")
    require(record["state"] == "active" and len(record["experiments"]) == 1,
            "scheduled initial experiment did not promote and apply")
    controller.verify_application(record)
    atomic(timer.root / "snapshots" / "initial-active.json", canonical(record))
    # Wait for the real policy interval. Do not forge last_validation or change policy.
    due = datetime.fromisoformat(record["last_validation"]).timestamp() + controller.interval + 0.2
    progress("revalidation", state="waiting-for-policy-interval", seconds=max(0, round(due - time.time())))
    while time.time() < due:
        require(time.monotonic() < timer.deadline, "deadline exceeded waiting for revalidation")
        time.sleep(min(10, max(0, due - time.time()), max(0, timer.deadline - time.monotonic())))
    timer.run("scheduled-revalidation")
    record = one_record(controller)
    require(record["state"] == "active" and record["reason"] == "revalidated for both agents",
            "scheduled fresh validation did not retain active lesson")
    verify_reports(controller, record)
    atomic(timer.root / "snapshots" / "revalidated-active.json", canonical(record))
    for target in targets:
        target.write_text(target.read_text() + "Later human edit.\n")
    # Remove the fixture from draft admission while invalidating its source hash.
    # Keeping a changed draft admitted would create a NEW version and spend more calls.
    text = source.read_text().replace('state: "draft"', 'state: "accepted"', 1)
    require(text != source.read_text(), "fixture source state missing")
    source.write_text(text + "Synthetic fixture withdrawn after successful scheduled revalidation.\n")
    load_lesson(source)
    timer.run("automatic-source-withdrawal")
    record = one_record(controller)
    require(record["state"] == "invalidated" and record["reason"] == "source evidence changed",
            "scheduled source drift did not invalidate")
    for target in targets:
        text = target.read_text()
        require("twill-learning:" not in text and "Preserve human instructions." in text
                and "Later human edit." in text, "automatic removal lost human edits or left owned text")
    history = [json.loads(path.read_text())["event"] for path in
               sorted((controller.store / "history" / record["id"]).glob("*.json"))]
    kinds = [event["event"] for event in history]
    require(all(kind in kinds for kind in ("created", "experimented", "applied", "validated", "remove_intent", "removed")),
            "typed lifecycle history incomplete")
    require(any(event["event"] == "remove_intent" and event["details"].get("reason") == "source evidence changed"
                for event in history), "typed source-withdrawal reason missing")
    before = canonical(record)
    hashes = [digest(path.read_bytes()) for path in targets]
    timer.run("idempotent-no-reapply")
    require(canonical(one_record(controller)) == before, "terminal record changed during no-op cycle")
    require([digest(path.read_bytes()) for path in targets] == hashes, "terminal cycle changed target files")
    verify_reports(controller, one_record(controller))
    return {"id": record["id"], "checks": ["timer_triggered_each_cycle", "legacy_draft_imported",
            "both_native_agents_improved", "scheduled_apply", "fresh_scheduled_revalidation",
            "automatic_source_drift_removal", "typed_history", "human_edits_preserved", "idempotent_no_reapply"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--claude-model", required=True)
    parser.add_argument("--codex-model", required=True)
    parser.add_argument("--deadline-seconds", type=int, default=720)
    args = parser.parse_args(argv)
    require(120 <= args.deadline_seconds <= 1800, "deadline must be 120..1800 seconds")
    scratch = args.scratch.expanduser().resolve()
    require(scratch != REPO and REPO not in scratch.parents, "scratch must be outside public checkout")
    scratch.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="scheduled-learning-", dir=scratch)).resolve()
    os.chmod(root, 0o700)
    timer = None
    result = {"kind": "synthetic-scheduled-lifecycle-proof", "production_improvement_proven": False,
              "root": str(root), "result": "failed"}
    code = 1
    try:
        controller, policy_path, source, targets = fixture(root, {"claude": args.claude_model, "codex": args.codex_model})
        deadline = time.monotonic() + args.deadline_seconds
        result["timer_preflight"] = str(timer_preflight(root / ("preflight-" + root.name), deadline))
        timer = Timer(root, policy_path, deadline)
        progress("prepared", root=str(root), policy=str(policy_path))
        timer.install()
        result.update(proof(controller, timer, source, targets), result="passed")
        code = 0
    except (ProofError, OSError, ValueError, subprocess.SubprocessError) as exc:
        # Exception text is confined to private evidence; CLI names only its path.
        atomic(root / "failure.txt", (str(exc)[:2000] + "\n").encode())
        result["failure_evidence"] = str(root / "failure.txt")
    finally:
        if timer is not None:
            try:
                timer.cleanup()
                result["temporary_units_removed"] = True
            except (ProofError, OSError, subprocess.SubprocessError) as exc:
                atomic(root / "cleanup-failure.txt", (str(exc)[:2000] + "\n").encode())
                result.update(result="failed", temporary_units_removed=False)
                code = 1
        atomic(root / "result.json", canonical(result))
        print(json.dumps(result), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
