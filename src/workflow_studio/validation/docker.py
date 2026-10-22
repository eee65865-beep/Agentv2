"""Local-only Docker adapter: no shell, downloads, privileged or writable binds.

Only a reviewed, preloaded digest-pinned Linux Python image is accepted. Docker
is a host trust boundary, not a guarantee against a container/kernel escape.
"""

from dataclasses import dataclass
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

from dotenv import dotenv_values

from workflow_studio.validation.models import ValidationLimits

IMAGE_PATTERN = r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}"
BOOTSTRAP = "import runpy,sys;sys.path.insert(0,'/submission');runpy.run_path('/submission/main.py',run_name='__main__')"
INTERFACE = "import sys,json;sys.path.insert(0,'/submission');from calculator import add;d=json.load(sys.stdin);print(json.dumps({'sum':add(d['a'],d['b'])}))"
STORAGE_HOLDER = "import time;time.sleep(300)"


class BackendError(ValueError):
    pass


@dataclass(frozen=True)
class Capture:
    exit_code: int | None
    stdout: bytes
    reason: str | None = None


def capture(argv, *, env, timeout, stdin=b"", limit=16384, cancel=None):
    """Drain pipes with a hard byte ceiling, stop the CLI on timeout/cancellation.

    Caller must remove its container even when the Docker CLI is terminated.
    No raw stdout/stderr is logged or returned in public reports.
    """
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, shell=False, creationflags=flags)
    except OSError:
        raise BackendError("backend_unavailable") from None
    output = bytearray()
    overflow = Event()
    def drain(pipe, save):
        count = 0
        try:
            while data := pipe.read(1024):
                count += len(data)
                if save and len(output) < limit:
                    output.extend(data[:limit - len(output)])
                if count > limit:
                    overflow.set()
                    break
        except (OSError, ValueError):
            pass
        finally:
            pipe.close()
    def send():
        try:
            proc.stdin.write(stdin)
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
    readers = [Thread(target=drain, args=(proc.stdout, True), daemon=True), Thread(target=drain, args=(proc.stderr, False), daemon=True)]
    for thread in readers:
        thread.start()
    Thread(target=send, daemon=True).start()
    deadline = monotonic() + max(0, timeout)
    reason = None
    while proc.poll() is None:
        if overflow.is_set():
            reason = "output_limit"
        elif cancel is not None and cancel.is_set():
            reason = "cancelled"
        elif monotonic() >= deadline:
            reason = "deadline"
        if reason:
            proc.kill()
            break
        overflow.wait(0.02)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise BackendError("runtime_error") from None
    for thread in readers:
        thread.join(timeout=1)
    if any(thread.is_alive() for thread in readers):
        reason = reason or "runtime_error"
    if overflow.is_set():
        reason = reason or "output_limit"
    return Capture(proc.returncode, bytes(output), reason)


class DockerBackend:
    def __init__(self, image=None, *, executable=None, env_file=".env"):
        if image is None:
            image = os.environ.get("WORKFLOW_VALIDATION_IMAGE")
            if image is None:
                image = (dotenv_values(env_file, interpolate=False).get("WORKFLOW_VALIDATION_IMAGE") if env_file is not None else None) or ""
        self.image = image
        if self.image and (len(self.image) > 160 or re.fullmatch(IMAGE_PATTERN, self.image) is None):
            raise BackendError("image_unconfigured")
        self.executable = executable or shutil.which("docker")
        self.environment = "unavailable"

    def _argv(self, *args):
        endpoint = "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"
        return [self.executable, "--host", endpoint, *args]

    def _env(self, config):
        # No API key, Docker context/TLS setting, user docker config or proxy.
        values = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
        values["DOCKER_CONFIG"] = str(config)
        return values

    def prepare(self, config, remaining, cancel=None):
        if not self.image:
            raise BackendError("image_unconfigured")
        if not self.executable:
            raise BackendError("backend_unavailable")
        began = monotonic()
        env = self._env(config)
        def query(args):
            left = remaining - (monotonic() - began)
            if left <= 0:
                raise BackendError("deadline")
            result = capture(self._argv(*args), env=env, timeout=min(5, left), limit=65536, cancel=cancel)
            if result.reason:
                raise BackendError(result.reason if result.reason in ("deadline", "cancelled") else "backend_unavailable")
            if result.exit_code:
                raise BackendError("backend_unavailable")
            try:
                return json.loads(result.stdout)
            except (ValueError, UnicodeError):
                raise BackendError("backend_unavailable") from None
        info = query(["info", "--format", "{{json .}}"])
        if not isinstance(info, dict) or info.get("OSType") != "linux" or info.get("Warnings") or not all(info.get(key) is True for key in ("MemoryLimit", "SwapLimit", "PidsLimit", "CPUCfsQuota")) or not any(str(item).startswith("name=seccomp") for item in info.get("SecurityOptions", [])):
            raise BackendError("backend_unavailable")
        image = query(["image", "inspect", "--format", "{{json .}}", self.image])
        if not isinstance(image, dict) or image.get("Os") != "linux" or self.image not in image.get("RepoDigests", []) or re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", "")) is None or not isinstance(image.get("Config", {}), dict) or image.get("Config", {}).get("Volumes"):
            raise BackendError("backend_unavailable")
        # ENTRYPOINT and environment inherited from this image are bypassed/reset
        # where possible; the administrator must still review its Python runtime.
        self.environment = self.image + ";" + image["Id"]

    def run_case(self, suite, case, submission: Path, config: Path, limits: ValidationLimits, remaining, cancel=None):
        name = "workflow-validation-" + uuid4().hex
        env = self._env(config)
        deadline = monotonic() + min(limits.case_seconds, remaining)
        def run(args, *, stdin=b"", limit=None, cleanup=False):
            left = 5 if cleanup else deadline - monotonic()
            if left <= 0:
                return Capture(None, b"", "deadline")
            return capture(self._argv(*args), env=env, timeout=left, stdin=stdin,
                           limit=limit or limits.max_output_bytes, cancel=None if cleanup else cancel)
        command = INTERFACE if suite.id == "multi-module" and case.id == "interface" else BOOTSTRAP
        storage_case = suite.id == "todo-storage"
        args = ["create", "--interactive", "--name", name, "--pull=never", "--network=none", "--read-only", "--user=65534:65534",
                "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--memory=128m", "--memory-swap=128m",
                "--cpus=0.5", "--pids-limit=32", "--ulimit=nofile=64:64", "--log-driver=none", "--ipc=none",
                "--tmpfs", "/work:rw,noexec,nosuid,nodev,size=16m,uid=65534,gid=65534,mode=700",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m,uid=65534,gid=65534,mode=700",
                "--mount", f"type=bind,source={submission},target=/submission,readonly",
                "--workdir=/work", "--entrypoint=python", "--env=PYTHONIOENCODING=utf-8",
                self.image, "-I", "-B", "-c", STORAGE_HOLDER if storage_case else command]
        result = Capture(None, b"", "runtime_error")
        storage_ok = True
        try:
            created = run(args)
            if created.reason or created.exit_code:
                result = Capture(created.exit_code, b"", created.reason or "runtime_error")
            else:
                if storage_case:
                    # tmpfs is lost on container stop. Keep a fixed holder alive
                    # while executing the candidate and copying the evidence.
                    result = run(["start", name])
                    if not result.reason and result.exit_code == 0:
                        result = run(["exec", "--interactive", name, "python", "-I", "-B", "-c", command], stdin=case.stdin.encode("utf-8"))
                else:
                    result = run(["start", "--attach", "--interactive", name], stdin=case.stdin.encode("utf-8"))
                if not result.reason:
                    inspected = run(["container", "inspect", "--format", "{{json .State}}", name])
                    try:
                        state = json.loads(inspected.stdout)
                        expected_status = "running" if storage_case else "exited"
                        if inspected.reason or inspected.exit_code or not isinstance(state, dict) or state.get("Status") != expected_status or state.get("Running") is not storage_case or state.get("Error") or type(state.get("ExitCode")) is not int:
                            raise ValueError("invalid_container_exit_state")
                        result = Capture(result.exit_code if storage_case else state["ExitCode"], result.stdout, "resource_limit" if state.get("OOMKilled") else None)
                    except (ValueError, UnicodeError):
                        result = Capture(result.exit_code, b"", inspected.reason or "runtime_error")
                if not result.reason and not result.exit_code and suite.id == "todo-storage":
                    copied = run(["cp", name + ":/work/todos.json", "-"], limit=65536)
                    if copied.reason:
                        result = Capture(result.exit_code, b"", copied.reason)
                    elif copied.exit_code:
                        storage_ok = False
                    else:
                        storage_ok = self._check_storage(copied.stdout, case)
        finally:
            # The container may have been created even if its create CLI timed out.
            try:
                removed = run(["rm", "--force", name], cleanup=True)
                if removed.reason or removed.exit_code:
                    result = Capture(result.exit_code, b"", "cleanup_failed")
            except BackendError:
                result = Capture(result.exit_code, b"", "cleanup_failed")
        return result, storage_ok

    @staticmethod
    def _check_storage(payload, case):
        try:
            with tarfile.open(fileobj=BytesIO(payload), mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != 1 or not members[0].isfile() or members[0].size > 16384:
                    return False
                data = json.loads(archive.extractfile(members[0]).read())
            return data == json.loads(case.stdin)["items"]
        except (ValueError, UnicodeError, RecursionError, tarfile.TarError, OSError):
            return False
