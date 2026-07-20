"""Smoke test for a PyInstaller-built orlog binary: proves it actually
starts and its heaviest dependencies (cryptography, pytest, mcp,
fastembed/onnxruntime) import and run -- not just that the build
succeeded.

Usage: python packaging/smoke_test.py <path-to-built-binary>
Exit code 0 on success, 1 on failure.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _extract_vault_key(init_stdout: str) -> str | None:
    for line in init_stdout.splitlines():
        line = line.strip()
        if line.startswith("ORLOG_VAULT_KEY="):
            return line.split("=", 1)[1]
    return None


def main() -> int:
    binary = str(Path(sys.argv[1]).resolve())

    # ignore_cleanup_errors: on Windows, the just-terminated `orlog serve`
    # process's file handle on data/index.sqlite isn't always released by
    # the instant proc.wait() returns, which otherwise makes rmtree() raise
    # PermissionError here -- after every real check above has already
    # passed. That's an OS-level cleanup race, not a smoke-test failure.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        workspace = Path(tmp) / "smoke-workspace"

        init = subprocess.run([binary, "init", str(workspace)], capture_output=True, text=True)
        if init.returncode != 0:
            print(f"FAIL: init exited {init.returncode}\nstdout:\n{init.stdout}\nstderr:\n{init.stderr}")
            return 1
        vault_key = _extract_vault_key(init.stdout)
        if not vault_key:
            print(f"FAIL: could not find ORLOG_VAULT_KEY in init output\n{init.stdout}")
            return 1
        print("OK: init")

        conformance = subprocess.run([binary, "conformance"], capture_output=True, text=True, cwd=tmp)
        if conformance.returncode != 0:
            print(f"FAIL: conformance exited {conformance.returncode}\nstdout:\n{conformance.stdout}\nstderr:\n{conformance.stderr}")
            return 1
        print("OK: conformance")

        env = {**os.environ, "ORLOG_VAULT_KEY": vault_key}
        proc = subprocess.Popen(
            [binary, "serve", str(workspace)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(3)
        started_ok = proc.poll() is None  # still running == didn't crash on startup/import
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        if not started_ok:
            out, err = proc.communicate()
            print(f"FAIL: serve exited early\nstdout:\n{out}\nstderr:\n{err}")
            return 1
        print("OK: serve started and stayed up")

    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
