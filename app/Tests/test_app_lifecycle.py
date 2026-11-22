#!/usr/bin/env python3
"""Run isolated, microphone-free AppKit shutdown tests against a built .app.

Example:
  python3 app/Tests/test_app_lifecycle.py --app /absolute/path/Fennec.app

Each case starts the bundle executable directly with its own temporary storage
and high loopback port. It never uses port 8778, opens the microphone, or kills
processes by a generic name. Temporary logs and a JSON report are retained.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import secrets
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def command(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else ""


def free_high_port() -> int:
    for _ in range(100):
        port = 49152 + secrets.randbelow(65536 - 49152)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            try:
                reservation.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Could not reserve an unused high loopback port")


def listener_pids(port: int) -> set[int]:
    result = command(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    return {int(value) for value in result.splitlines() if value.isdigit()}


def process_identity(pid: int, root: Path, port: int) -> str | None:
    """Require this project's executable, exact port, cwd, and process start time."""
    process_command = command(["/bin/ps", "-p", str(pid), "-o", "command="])
    if str(root / "venv/bin/uvicorn") not in process_command:
        return None
    if "main:app" not in process_command or f"--port {port} " not in process_command:
        return None
    cwd = command(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    if "n" + str(root / "backend") not in cwd.splitlines():
        return None
    return command(["/bin/ps", "-p", str(pid), "-o", "lstart="]) or None


def owned_backend(app_pid: int, root: Path, port: int) -> tuple[int, str] | None:
    owners = listener_pids(port)
    if not owners:
        return None
    if len(owners) != 1:
        raise RuntimeError(f"Unexpected listener owners on isolated port {port}: {owners}")
    pid = next(iter(owners))
    identity = process_identity(pid, root, port)
    parent = command(["/bin/ps", "-p", str(pid), "-o", "ppid="])
    if not identity or parent != str(app_pid):
        raise RuntimeError(f"Port {port} is not owned by a verified child of test PID {app_pid}; refusing to manage PID {pid}")
    return pid, identity


def stop_owned_processes(
    app: subprocess.Popen[bytes], backend: tuple[int, str] | None, root: Path, port: int
) -> list[str]:
    actions: list[str] = []
    # Popen retains ownership of this exact child; never select an app by name.
    if app.poll() is None:
        app.terminate()
        actions.append(f"SIGTERM test app PID {app.pid}")
        try:
            app.wait(timeout=3)
        except subprocess.TimeoutExpired:
            app.kill()
            app.wait(timeout=3)
            actions.append(f"SIGKILL unresponsive test app PID {app.pid}")
    if backend:
        pid, identity = backend
        # Recheck command, cwd, port argument, and start time before either signal.
        if process_identity(pid, root, port) == identity:
            try:
                os.kill(pid, signal.SIGTERM)
                actions.append(f"SIGTERM verified test backend PID {pid}")
            except ProcessLookupError:
                return actions
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if process_identity(pid, root, port) != identity:
                    break
                time.sleep(0.1)
            if process_identity(pid, root, port) == identity:
                try:
                    os.kill(pid, signal.SIGKILL)
                    actions.append(f"SIGKILL verified test backend PID {pid}")
                except ProcessLookupError:
                    pass
    return actions


def recording_files(directory: Path) -> list[str]:
    return [str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file() or path.is_symlink()]


def run_case(executable: Path, root: Path, directory: Path, double_signal: bool, startup_timeout: float) -> dict:
    directory.mkdir()
    recordings = directory / "Recordings"
    recordings.mkdir()
    legacy = directory / "empty-legacy"
    legacy.mkdir()
    port = free_high_port()
    environment = dict(os.environ)
    environment.update({
        "FENNEC_HOME": str(root),
        "FENNEC_PORT": str(port),
        "FENNEC_LOG_PATH": str(directory / "server.log"),
        "FENNEC_APP_DATA_DIR": str(directory / "app-data"),
        "FENNEC_DATA_DIR": str(legacy),
        "FENNEC_RECORDINGS_DIR": str(recordings),
    })
    result: dict = {"case": directory.name, "port": port, "directory": str(directory), "passed": False}
    backend: tuple[int, str] | None = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with (directory / "app.log").open("wb") as output:
        app = subprocess.Popen([str(executable)], cwd=root, env=environment, stdout=output, stderr=subprocess.STDOUT)
        result["app_pid"] = app.pid
        try:
            deadline = time.monotonic() + startup_timeout
            health = None
            while time.monotonic() < deadline:
                if app.poll() is not None:
                    raise RuntimeError(f"App exited before health readiness: {app.returncode}")
                if backend is None:
                    backend = owned_backend(app.pid, root, port)
                try:
                    with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as response:
                        candidate = json.load(response)
                        if candidate.get("service") == "fennec-transcriber" and candidate.get("status") == "ok":
                            health = candidate
                            break
                except (OSError, urllib.error.URLError, ValueError):
                    pass
                time.sleep(0.1)
            if health is None:
                raise RuntimeError(f"Health readiness timed out after {startup_timeout:g} seconds")
            backend = backend or owned_backend(app.pid, root, port)
            if backend is None:
                raise RuntimeError("Health responded without a verified owned backend")
            result["backend_pid"] = backend[0]
            if health.get("active_transcriptions") != 0:
                raise RuntimeError("Isolated backend unexpectedly has an active transcription")
            if recording_files(recordings):
                raise RuntimeError("Unexpected recording files before shutdown; no microphone action was requested")
            started = time.monotonic()
            app.send_signal(signal.SIGTERM)
            if double_signal:
                time.sleep(0.01)
                if app.poll() is None:
                    app.send_signal(signal.SIGTERM)
                    result["second_signal_sent"] = True
                else:
                    result["second_signal_sent"] = False
                    raise RuntimeError("App exited before the second SIGTERM; the double-signal case was not exercised")
            exit_code = app.wait(timeout=max(0.01, 10 - (time.monotonic() - started)))
            elapsed = time.monotonic() - started
            result.update({"exit_code": exit_code, "exit_seconds": round(elapsed, 3)})
            if exit_code != 0 or elapsed > 10:
                raise RuntimeError(f"Expected clean exit within 10 seconds, got code {exit_code} after {elapsed:.3f}s")
            deadline = time.monotonic() + 4
            while listener_pids(port) and time.monotonic() < deadline:
                time.sleep(0.1)
            if listener_pids(port):
                raise RuntimeError("Owned backend still listens after app shutdown")
            files = recording_files(recordings)
            result["recording_files"] = files
            if files:
                raise RuntimeError(f"Unexpected recording files after microphone-free lifecycle test: {files}")
            result["passed"] = True
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        finally:
            # A child may have started shortly before a readiness failure. Only adopt
            # it while its original app parent still exists and ownership is verified.
            if backend is None and app.poll() is None:
                try:
                    backend = owned_backend(app.pid, root, port)
                except Exception as error:
                    result["ownership_warning"] = str(error)
            result["cleanup"] = stop_owned_processes(app, backend, root, port)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="Built .app bundle to test")
    parser.add_argument("--startup-timeout", default=60.0, type=float)
    arguments = parser.parse_args()
    bundle = arguments.app.expanduser().resolve()
    root = Path(__file__).resolve().parents[2]
    with (bundle / "Contents/Info.plist").open("rb") as handle:
        executable_name = plistlib.load(handle)["CFBundleExecutable"]
    executable = bundle / "Contents/MacOS" / executable_name
    if not executable.is_file():
        parser.error(f"Missing bundle executable: {executable}")
    directory = Path(tempfile.mkdtemp(prefix="fennec-app-lifecycle-"))
    print(f"Isolated lifecycle logs: {directory}", flush=True)
    results = []
    for name, double_signal in [("single-sigterm", False), ("double-sigterm", True)]:
        result = run_case(executable, root, directory / name, double_signal, arguments.startup_timeout)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status} {name}: {result.get('exit_seconds', '?')}s, exit={result.get('exit_code', '?')}, recordings={result.get('recording_files', '?')}", flush=True)
        if result.get("error"):
            print(result["error"], flush=True)
    (directory / "results.json").write_text(json.dumps({"app": str(bundle), "results": results}, ensure_ascii=False, indent=2))
    print(f"Report: {directory / 'results.json'}", flush=True)
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
