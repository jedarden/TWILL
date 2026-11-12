"""Trusted JSON-function assertions against generated Python in disposable Docker.

The trusted supervisor and expected values are never readable by generated code.
Only the supervisor retains SETUID/SETGID; candidate interpreters permanently drop
privileges before loading files. No candidate code is imported by the controller.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

TYPE = "python-functions/v1"
MAX_FILES = 16
MAX_BYTES = 64 * 1024
IMAGE = "python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c"
_IMAGE = re.compile(r"^(?:docker.io/library/)?python:3\.[0-9]+\.[0-9]+-slim@sha256:[0-9a-f]{64}$")
_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}\.py$")
_FORBIDDEN = {".git", ".codex", ".claude", "AGENTS.md", "CLAUDE.md", "sitecustomize.py", "usercustomize.py"}

# This program runs ONLY inside the pinned image. Assertions and this supervisor
# are root-only. A fresh child per assertion prevents cross-test state leakage.
RUNNER = r'''import json, os, resource, signal, subprocess, sys, tempfile
from pathlib import Path
assert os.getuid() == 0
spec = json.loads(Path('/trusted/assertions.json').read_text())
CHILD = """import importlib.util,json,sys
request=json.loads(sys.stdin.read())
sys.path.insert(0,'/workspace')
spec=importlib.util.spec_from_file_location('candidate','/workspace/'+request['file'])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
value=getattr(module,request['function'])(*request['args'],**request['kwargs'])
print(json.dumps(value,allow_nan=False,separators=(',',':')))
"""
def drop():
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_CPU,(2,2))
    resource.setrlimit(resource.RLIMIT_AS,(128*1024*1024,128*1024*1024))
    resource.setrlimit(resource.RLIMIT_FSIZE,(32768,32768))
    resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
    resource.setrlimit(resource.RLIMIT_NPROC,(32,32))
rows=[]
for assertion in spec['assertions']:
    request={key: assertion.get(key,default) for key,default in [('args',[]),('kwargs',{})]}
    request.update(file=spec['target_file'],function=spec['function'])
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc=subprocess.Popen([sys.executable,'-I','-S','-c',CHILD], stdin=subprocess.PIPE,
            stdout=stdout,stderr=stderr,env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8'},
            cwd='/workspace',preexec_fn=drop,start_new_session=True)
        error=None
        try:
            proc.communicate(json.dumps(request).encode(),timeout=3)
        except subprocess.TimeoutExpired:
            error='candidate_timeout'
        finally:
            # CAP_KILL remains dropped. Match the child UID only while
            # signaling; the supervisor retains real/saved UID 0.
            try:
                os.seteuid(65534)
                try: os.killpg(proc.pid,signal.SIGKILL)
                finally: os.seteuid(0)
            except ProcessLookupError: pass
            proc.wait()
        stdout.seek(0)
        output=stdout.read(32769)
        value=None
        if not error and proc.returncode:
            error='candidate_exit'
        if not error:
            try: value=json.loads(output,parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            except (ValueError,UnicodeError): error='candidate_invalid_json'
        # Canonical JSON preserves JSON type distinctions (True != 1).
        passed=error is None and json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)==json.dumps(assertion['equals'],sort_keys=True,separators=(',',':'),allow_nan=False)
        rows.append({'id':assertion['id'],'passed':passed,'returncode':proc.returncode,'error':error})
print(json.dumps({'assertions':rows,'executed':True},separators=(',',':')))
sys.exit(0 if all(row['passed'] for row in rows) else 1)
'''


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def safe_path(name: object) -> bool:
    if not isinstance(name, str) or not _PATH.fullmatch(name):
        return False
    return all(part and part not in {".", ".."} | _FORBIDDEN and not part.startswith(".")
               for part in name.split("/"))


def validate_oracle(oracle: object) -> None:
    if (not isinstance(oracle, dict) or set(oracle) != {"type", "image", "output_files", "target_file", "function", "assertions"}
            or oracle.get("type") != TYPE):
        raise ValueError("execution oracle requires python-functions/v1 and its exact schema")
    if not isinstance(oracle["image"], str) or not _IMAGE.fullmatch(oracle["image"]):
        raise ValueError("execution image must be an official Python patch-version slim image pinned by digest")
    files = oracle["output_files"]
    if (not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES
            or any(not safe_path(name) for name in files) or len(set(files)) != len(files)):
        raise ValueError("output_files must contain unique safe relative Python paths")
    if oracle["target_file"] not in files or not isinstance(oracle["function"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", oracle["function"]):
        raise ValueError("oracle target must select a declared file and public function")
    if any(other.startswith(name + "/") for name in files for other in files if name != other):
        raise ValueError("output_files cannot overlap file and directory paths")
    assertions = oracle["assertions"]
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= 12:
        raise ValueError("oracle needs 1 to 12 independently authored assertions")
    ids = set()
    for row in assertions:
        if (not isinstance(row, dict) or set(row) - {"id", "args", "kwargs", "equals"}
                or "equals" not in row or not isinstance(row.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row["id"]) or row["id"] in ids
                or not isinstance(row.get("args", []), list) or not isinstance(row.get("kwargs", {}), dict)
                or any(not isinstance(key, str) for key in row.get("kwargs", {}))):
            raise ValueError("invalid execution assertion")
        ids.add(row["id"])
    try:
        if len(_canonical(oracle)) > MAX_BYTES:
            raise ValueError("execution oracle exceeds 64 KiB")
    except (TypeError, OverflowError) as exc:
        raise ValueError("execution oracle must contain finite JSON data") from exc


def suite_binding() -> dict:
    return {"type": TYPE, "runner_sha256": hashlib.sha256(RUNNER.encode()).hexdigest(),
            "isolation_version": 1, "assertion_timeout_seconds": 3}


def _docker(args: list[str], *, root: Path, timeout: float) -> dict:
    # Keep the single production subprocess boundary visible to the audit gate.
    from twill_experiments import _run_process
    env = {"PATH": os.environ.get("PATH", "")}
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        env["DOCKER_HOST"] = host
    return _run_process(["docker", *args], cwd=root, env=env,
                        prompt="", timeout=timeout, control=root)


def _command(image: str, root: Path, name: str) -> list[str]:
    return ["run", "--rm", "--pull=never", "--name", name, "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID",
            "--security-opt", "no-new-privileges", "--pids-limit", "64", "--cpus", "0.5",
            "--memory", "256m", "--memory-swap", "256m", "--ulimit", "nofile=64:64",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--mount", f"type=bind,src={root / 'trusted'},dst=/trusted,readonly",
            "--mount", f"type=bind,src={root / 'workspace'},dst=/workspace,readonly",
            "--workdir", "/workspace", "--entrypoint", "/usr/local/bin/python", image,
            "-I", "-S", "/trusted/runner.py"]


def _error(code: str, *, executed: bool = False) -> dict:
    return {"status": "error", "error": code, "executed": executed,
            "assertions": [], "returncode": None}


def execute_answer(oracle: dict, answer: str, *, work_root: Path, timeout_seconds: float = 45) -> dict:
    """Execute a strict JSON file mapping; setup errors remain unknown, never fail/pass."""
    validate_oracle(oracle)
    try:
        files = json.loads(answer)
        if (not isinstance(files, dict) or set(files) != {"files"} or not isinstance(files["files"], dict)
                or set(files["files"]) != set(oracle["output_files"])
                or any(not safe_path(name) or not isinstance(content, str) for name, content in files["files"].items())
                or len(_canonical(files)) > MAX_BYTES):
            raise ValueError("invalid generated files")
    except (ValueError, TypeError):
        return {"status": "fail", "error": "candidate_file_protocol", "executed": False,
                "assertions": [], "returncode": None}
    with tempfile.TemporaryDirectory(prefix="twill-execution-", dir=work_root) as directory:
        root = Path(directory)
        trusted = root / "trusted"
        trusted.mkdir(mode=0o700)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o755)
        (trusted / "runner.py").write_text(RUNNER)
        (trusted / "assertions.json").write_bytes(_canonical(oracle))
        for name, content in files["files"].items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            for parent in path.parents:
                if parent == root:
                    break
                parent.chmod(0o755)
            path.write_text(content)
            path.chmod(0o644)
        name = "twill-oracle-" + os.urandom(12).hex()
        try:
            invocation = _docker(_command(oracle["image"], root, name), root=root, timeout=max(0.01, min(45, timeout_seconds)))
        finally:
            # Killing docker's client does not kill its daemon-owned container.
            _docker(["rm", "--force", name], root=root, timeout=5)
        if invocation["error"]:
            return _error("sandbox_" + invocation["error"])
        if invocation["returncode"] not in (0, 1):
            return _error("sandbox_setup_failure")
        try:
            report = json.loads(invocation["stdout"])
            rows = report["assertions"]
            if (report.get("executed") is not True or not isinstance(rows, list)
                    or len(rows) != len(oracle["assertions"])
                    or [row["id"] for row in rows] != [row["id"] for row in oracle["assertions"]]
                    or any(type(row["passed"]) is not bool for row in rows)
                    or (invocation["returncode"] == 0) != all(row["passed"] for row in rows)):
                raise ValueError("unverified supervisor output")
        except (ValueError, TypeError, KeyError):
            return _error("sandbox_unverified_report")
        return {"status": "pass" if invocation["returncode"] == 0 else "fail", "executed": True,
                "assertions": rows, "returncode": invocation["returncode"],
                "image": oracle["image"], "runner_sha256": suite_binding()["runner_sha256"]}


def preflight_oracles(cases: list[dict], *, work_root: Path) -> dict:
    """Verify pinned runtime/isolation before any model call; no implicit pull."""
    images = sorted({case["oracle"]["image"] for case in cases if "oracle" in case})
    reports = {}
    for image in images:
        probe = {"type": TYPE, "image": image, "output_files": ["probe.py"],
                 "target_file": "probe.py", "function": "probe", "assertions": [
                     {"id": "isolation", "equals": [65534, True, True, True, True, True, True]}]}
        code = """def probe():
 import os,socket
 from pathlib import Path
 hidden=False
 try: Path('/trusted/assertions.json').read_text()
 except PermissionError: hidden=True
 readonly=False
 try: Path('/workspace/escape.py').write_text('escape')
 except OSError: readonly=True
 no_root_write=False
 try: Path('/etc/twill-probe').write_text('escape')
 except OSError: no_root_write=True
 no_network=False
 try:
  connection=socket.create_connection(('192.0.2.1',443),timeout=.2)
  connection.close()
 except OSError: no_network=True
 status=Path("/proc/self/status").read_text()
 no_caps="CapEff:\t0000000000000000" in status and "NoNewPrivs:\t1" in status
 clean_environment=set(os.environ).issubset({"PATH","LANG","LC_CTYPE"})
 return [os.getuid(),hidden,readonly,no_root_write,no_network,no_caps,clean_environment]
"""
        outcome = execute_answer(probe, json.dumps({"files": {"probe.py": code}}), work_root=work_root)
        if outcome["status"] != "pass":
            raise RuntimeError("execution runtime unavailable or isolation probe failed: " + outcome.get("error", outcome["status"]))
        reports[image] = {"verified": True, "runner_sha256": suite_binding()["runner_sha256"]}
    return reports
