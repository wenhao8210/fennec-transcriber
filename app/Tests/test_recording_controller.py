#!/usr/bin/env python3
"""Isolated native recorder regression tests. Run on macOS with Xcode CLI tools.

Uses synthetic PCM, an ephemeral local HTTP server, a private defaults suite,
and temporary files. Does not start AVAudioEngine, request permission, launch
the application, or write app/.build. Production and the private test extension
are compiled as one temporary Swift source so release builds expose no test API.
"""
from __future__ import annotations

import datetime
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading


def main() -> None:
    project = Path(__file__).resolve().parents[2]
    requests: list[dict] = []
    failures: list[str] = []

    class ImportHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                assert self.path == "/api/recordings/import"
                assert Path(body["path"]).is_file()
                assert body["started_at"].endswith("Z") and body["ended_at"].endswith("Z")
                assert datetime.datetime.fromisoformat(body["ended_at"].replace("Z", "+00:00")) > datetime.datetime.fromisoformat(body["started_at"].replace("Z", "+00:00"))
                assert isinstance(body["auto_transcribe"], bool)
                if "toggle-auto" in body["path"]:
                    assert body["auto_transcribe"] is False
                if body["capture_id"].startswith("reject_"):
                    status, response = 422, {"detail": "permanent fixture rejection"}
                elif len(requests) == 1:
                    status, response = 503, {"detail": "temporary fixture outage"}
                else:
                    status, response = 200, {"id": "test-" + body["capture_id"], "name": Path(body["path"]).name}
            except Exception as error:
                failures.append(repr(error))
                status, response = 500, {"detail": repr(error)}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), ImportHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="fennec-recorder-test-") as temporary:
            temporary_path = Path(temporary)
            source = temporary_path / "main.swift"
            executable = temporary_path / "test-recorder"
            source.write_text(
                (project / "app/Sources/NativeSpeechDetector.swift").read_text()
                + "\n"
                + (project / "app/Sources/RecordingController.swift").read_text()
                + "\n"
                + (project / "app/Tests/RecordingControllerHarness.swift").read_text()
            )
            subprocess.run([
                "xcrun", "swiftc", "-D", "RECORDER_TESTING", str(source), "-o", str(executable),
                "-framework", "AppKit", "-framework", "AVFoundation", "-framework", "CoreAudio", "-framework", "SoundAnalysis",
            ], check=True)
            environment = dict(os.environ)
            environment["FENNEC_RECORDER_TEST_DIR"] = temporary
            environment["FENNEC_RECORDER_TEST_ENDPOINT"] = f"http://127.0.0.1:{server.server_port}/"
            subprocess.run([str(executable)], env=environment, check=True, timeout=90)
            assert not failures, failures
            assert len(requests) >= 10, f"Too few import requests: {len(requests)}"
            assert requests[0]["capture_id"] == requests[1]["capture_id"], "Offline retry changed capture identity"
            assert sum(item["capture_id"].startswith("reject_") for item in requests) == 1
            print(f"PASS HTTP contract: {len(requests)} requests, strict subsecond dates, idempotent retry identity")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
