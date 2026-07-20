"""Smoke test for a PyInstaller-built orlog binary: proves it actually
starts and its heaviest dependencies (cryptography, pytest, mcp,
fastembed/onnxruntime, anthropic, openai) import and run -- not just that
the build succeeded.

The anthropic/openai check works because orlog.runtime.Runtime.__init__
builds the configured Deriver EAGERLY (via _build_deriver()), at `orlog
serve` startup, based on orlog.toml's [deriver] backend -- not lazily on
the first recall()/remember() call. Pointing a scratch workspace's config
at backend="anthropic"/"openai" and starting `orlog serve` against it
therefore forces exactly the SDK import PyInstaller's static analysis
could have missed as a hidden import, with NO network/API call required:
AnthropicCompletion/OpenAICompletion's __init__ only imports the SDK and
constructs a client object (a dummy, syntactically-plausible API key value
satisfies the "is it set" check without ever needing to be valid -- no
request is made, and SDK client construction itself doesn't validate the
key). A missing hidden import surfaces as an unhandled
ModuleNotFoundError/ImportError that crashes the process near-instantly,
well inside the same startup window the fastembed serve check below
already uses.

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


def _check_llm_backend_bundled(binary: str, backend: str, api_key_env: str) -> bool:
    """Starts `orlog serve` against a scratch workspace configured for the
    given LLM `backend` and confirms the process is still alive after the
    startup window -- i.e. Runtime's eager deriver construction imported the
    `backend` SDK successfully instead of crashing with
    ModuleNotFoundError/ImportError. See the module docstring for why this
    needs no network call and no real API key.

    The workspace's embedder is also switched to "hashing" (dependency-free,
    instant) so this check is isolated to the deriver/SDK import path and
    isn't slowed down or muddied by the default fastembed model download --
    that path is already covered by the fastembed check in main().
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        workspace = Path(tmp) / f"smoke-{backend}-workspace"

        init = subprocess.run([binary, "init", str(workspace)], capture_output=True, text=True)
        if init.returncode != 0:
            print(f"FAIL: init ({backend} check) exited {init.returncode}\nstdout:\n{init.stdout}\nstderr:\n{init.stderr}")
            return False
        vault_key = _extract_vault_key(init.stdout)
        if not vault_key:
            print(f"FAIL: could not find ORLOG_VAULT_KEY in init output ({backend} check)\n{init.stdout}")
            return False

        config_path = workspace / "orlog.toml"
        config_text = config_path.read_text(encoding="utf-8")
        config_text = config_text.replace('backend = "scripted"', f'backend = "{backend}"', 1)
        config_text = config_text.replace('embedder = "BAAI/bge-small-en-v1.5"', 'embedder = "hashing"', 1)
        config_text = config_text.replace('api_key_env = "ANTHROPIC_API_KEY"', f'api_key_env = "{api_key_env}"', 1)
        config_path.write_text(config_text, encoding="utf-8")

        env = {**os.environ, "ORLOG_VAULT_KEY": vault_key, api_key_env: "sk-smoke-test-dummy-key-not-real"}
        proc = subprocess.Popen(
            [binary, "serve", str(workspace)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(3)
        started_ok = proc.poll() is None  # still running == the SDK import didn't crash it
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        if not started_ok:
            out, err = proc.communicate()
            print(
                f"FAIL: serve with deriver backend={backend!r} exited early -- the {backend} SDK "
                f"probably didn't get bundled (missing hidden import)\nstdout:\n{out}\nstderr:\n{err}"
            )
            return False
        print(f"OK: serve started and stayed up with deriver backend={backend!r} ({backend} SDK bundled)")
        return True


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

    if not _check_llm_backend_bundled(binary, "anthropic", "ANTHROPIC_API_KEY"):
        return 1
    if not _check_llm_backend_bundled(binary, "openai", "OPENAI_API_KEY"):
        return 1

    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
