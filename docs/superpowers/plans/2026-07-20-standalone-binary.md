# Standalone orlog Binary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a GitHub Actions pipeline that builds a standalone `orlog` executable (Windows/macOS/Linux, no Python required) with the full CLI, and publishes it to a GitHub Release on version tags.

**Architecture:** PyInstaller (`--onefile`) bundles `src/orlog/` behind a thin entrypoint script, driven by a checked-in `orlog.spec`. A tag-triggered GitHub Actions matrix builds natively on each OS, smoke-tests the binary by actually running it, zips it, and attaches it to a GitHub Release.

**Tech Stack:** Python 3.10, PyInstaller + pyinstaller-hooks-contrib, GitHub Actions (`actions/checkout`, `actions/setup-python`, `actions/upload-artifact`/`download-artifact`, `softprops/action-gh-release`).

## Global Constraints

- Full CLI surface ships in the binary: `init` (incl. `--claude-code`), `serve`, `replay`, `inspect`, `forget`, `conformance` — not just the MCP server.
- Both `anthropic` and `openai` extras are bundled into every build.
- The fastembed model weights are NOT bundled; the default embedder keeps downloading/caching on first use, same as today.
- No code signing in this pass — unsigned-binary OS warnings are an accepted, documented limitation.
- No changes to existing runtime behavior or the existing pip/pipx install path, except the `cmd_conformance` fix required for frozen-binary compatibility (Task 1).
- Builds are native per OS (no cross-compilation): `windows-latest`, `macos-latest`, `ubuntu-latest`.
- Build trigger: push of a tag matching `v*` only.
- Build Python version: 3.10 (the project's `requires-python` floor).

Reference: `docs/superpowers/specs/2026-07-20-standalone-binary-design.md`.

---

### Task 1: Make `orlog conformance` work inside a frozen binary

**Files:**
- Modify: `src/orlog/cli.py:1-37` (imports), `src/orlog/cli.py:159-170` (`cmd_conformance`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `orlog.cli._conformance_dir() -> Path` — resolves `tests/conformance/`, source-tree-relative normally, `sys._MEIPASS`-relative when `sys.frozen` is set. Consumed by `cmd_conformance` and by Task 2's PyInstaller `datas` bundling (which must mirror this exact layout: `<bundle_root>/tests/conformance/*.py` plus `<bundle_root>/tests/conftest.py`).

Two real problems exist in today's `cmd_conformance`, not just path resolution:
1. `Path(__file__).resolve().parents[2] / "tests" / "conformance"` assumes a source/editable install layout that doesn't exist inside a PyInstaller bundle.
2. It shells out via `subprocess.run([sys.executable, "-m", "pytest", ...])` — inside a frozen binary, `sys.executable` **is the frozen `orlog` binary itself**, not a Python interpreter, so `-m pytest` cannot work at all. This needs to become an in-process call to `pytest.main(...)` instead of a subprocess.

- [ ] **Step 1: Write the failing tests**

Add `import sys` near the top of `tests/test_cli.py` (alongside the existing `import json`), and add `cmd_conformance` to the existing `from orlog.cli import ...` line so it reads:

```python
from orlog.cli import build_parser, cmd_conformance, cmd_forget, cmd_init, cmd_inspect, cmd_replay, cmd_serve, write_claude_code_mcp_config
```

Then append these tests to `tests/test_cli.py`:

```python
def test_conformance_dir_resolves_inside_meipass_when_frozen(tmp_path, monkeypatch):
    from orlog.cli import _conformance_dir

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert _conformance_dir() == tmp_path / "tests" / "conformance"


def test_conformance_dir_resolves_the_source_tree_when_not_frozen(monkeypatch):
    from orlog.cli import _conformance_dir

    monkeypatch.delattr(sys, "frozen", raising=False)

    result = _conformance_dir()

    assert result.name == "conformance" and result.parent.name == "tests"


def test_conformance_runs_pytest_in_process_and_writes_a_report(tmp_path, monkeypatch):
    from orlog import cli as cli_module

    conformance_dir = tmp_path / "conformance"
    conformance_dir.mkdir()
    monkeypatch.setattr(cli_module, "_conformance_dir", lambda: conformance_dir)
    monkeypatch.chdir(tmp_path)

    captured = {}

    def fake_main(pytest_args):
        captured["args"] = pytest_args
        return 0

    monkeypatch.setattr(pytest, "main", fake_main)

    rc = cli_module.cmd_conformance(build_parser().parse_args(["conformance"]))

    assert rc == 0
    assert captured["args"] == [str(conformance_dir), "-v"]
    report = json.loads((tmp_path / "conformance-report.json").read_text())
    assert report == {"target": "self", "passed": True}


def test_conformance_reports_failure_when_pytest_main_returns_nonzero(tmp_path, monkeypatch):
    from orlog import cli as cli_module

    conformance_dir = tmp_path / "conformance"
    conformance_dir.mkdir()
    monkeypatch.setattr(cli_module, "_conformance_dir", lambda: conformance_dir)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pytest, "main", lambda pytest_args: 1)

    rc = cli_module.cmd_conformance(build_parser().parse_args(["conformance"]))

    assert rc == 1
    report = json.loads((tmp_path / "conformance-report.json").read_text())
    assert report["passed"] is False


def test_conformance_fails_clearly_when_the_suite_directory_is_missing(tmp_path, monkeypatch, capsys):
    from orlog import cli as cli_module

    missing_dir = tmp_path / "nope"
    monkeypatch.setattr(cli_module, "_conformance_dir", lambda: missing_dir)

    rc = cli_module.cmd_conformance(build_parser().parse_args(["conformance"]))

    assert rc == 2
    assert "conformance suite not found" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_cli.py -k conformance -v`
Expected: FAIL — `_conformance_dir` doesn't exist yet (`AttributeError`/`ImportError`), and the in-process/report-format tests fail against the current subprocess-based implementation.

- [ ] **Step 3: Implement the fix**

In `src/orlog/cli.py`, remove the now-unused `import subprocess` line (it appears once, in the import block, and after this change nothing in the file uses it):

```python
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
```

Replace the existing `cmd_conformance` function (currently `src/orlog/cli.py:159-170`) with:

```python
def _conformance_dir() -> Path:
    """Resolve tests/conformance/ both from a source checkout and from
    inside a frozen PyInstaller binary (bundled as `datas` in orlog.spec,
    unpacked under sys._MEIPASS at runtime)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "tests" / "conformance"
    return Path(__file__).resolve().parents[2] / "tests" / "conformance"


def cmd_conformance(args: argparse.Namespace) -> int:
    import pytest

    conformance_dir = _conformance_dir()
    if not conformance_dir.is_dir():
        print(f"conformance suite not found at {conformance_dir}", file=sys.stderr)
        return 2

    # In-process, not subprocess: inside a frozen binary sys.executable IS
    # the orlog binary itself, not a real Python interpreter, so
    # `sys.executable -m pytest` (the old approach) cannot work there.
    returncode = int(pytest.main([str(conformance_dir), "-v"]))
    Path("conformance-report.json").write_text(
        json.dumps({"target": args.target, "passed": returncode == 0}, indent=2)
    )
    print("Conformance report written to conformance-report.json")
    return returncode
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_cli.py -v`
Expected: PASS, all tests in the file including the 5 new ones.

- [ ] **Step 5: Run the full suite to confirm nothing else broke**

Run: `py -m pytest`
Expected: PASS (same pass count as before this task, plus 5).

- [ ] **Step 6: Commit**

```bash
git add src/orlog/cli.py tests/test_cli.py
git commit -m "fix: make orlog conformance work inside a frozen PyInstaller binary"
```

---

### Task 2: PyInstaller packaging — spec file, entrypoint, local build + smoke test

**Files:**
- Modify: `pyproject.toml` (new `build` optional-dependency group), `.gitignore`
- Create: `packaging/entrypoint.py`
- Create: `packaging/smoke_test.py`
- Create: `orlog.spec`

**Interfaces:**
- Consumes: `orlog.cli._conformance_dir()`'s expected layout from Task 1 (`tests/conformance/*.py` + `tests/conftest.py` bundled at those exact relative paths under the frozen root).
- Produces: `orlog.spec` (consumed by Task 3's CI job — `pyinstaller orlog.spec`), `packaging/smoke_test.py` (consumed by Task 3's CI smoke-test step — invoked as `python packaging/smoke_test.py <path-to-binary>`, exits 0 on success/nonzero on failure).

This task is validated by actually building on this machine (Windows) — the highest-risk part of the whole plan is whether PyInstaller can bundle `onnxruntime`/`fastembed`/`cryptography`/`mcp` correctly, and that's only provable by running the real build, not by reading the spec file.

- [ ] **Step 1: Add build tooling as an optional-dependency group**

In `pyproject.toml`, add a new group alongside the existing `anthropic`/`openai`/`dev`/`benchmark` groups under `[project.optional-dependencies]`:

```toml
build = [
    "pyinstaller>=6.0",
    "pyinstaller-hooks-contrib>=2024.0",
]
```

- [ ] **Step 2: Ignore PyInstaller build output**

Append to `.gitignore`:

```
build/
dist/
*.spec.bak
```

(Note: `build/` here is PyInstaller's own scratch directory, unrelated to any `src/orlog/` module of the same name — there is none today.)

- [ ] **Step 3: Create the entrypoint script**

Create `packaging/entrypoint.py`:

```python
"""Entry point for the PyInstaller build (see orlog.spec) -- Analysis
needs a real script file to start from, not the pip-generated console-
script shim, so this thin wrapper calls the same orlog.cli.main() the
`orlog` console script calls.
"""

import sys

from orlog.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Create the smoke test script**

Create `packaging/smoke_test.py`:

```python
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

    with tempfile.TemporaryDirectory() as tmp:
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
```

- [ ] **Step 5: Create the PyInstaller spec file**

Create `orlog.spec` at the repo root:

```python
# PyInstaller spec for the standalone orlog CLI/MCP-server binary.
# Build with: pyinstaller orlog.spec
# See docs/superpowers/specs/2026-07-20-standalone-binary-design.md.

from pathlib import Path

block_cipher = None

repo_root = Path(SPECPATH)

# Bundle the conformance suite so `orlog conformance` works standalone
# (see orlog/cli.py's _conformance_dir(), which expects exactly this
# layout under sys._MEIPASS: tests/conformance/*.py + tests/conftest.py).
conformance_src = repo_root / "tests" / "conformance"
datas = [(str(repo_root / "tests" / "conftest.py"), "tests")]
datas += [(str(f), "tests/conformance") for f in conformance_src.glob("*.py")]

a = Analysis(
    [str(repo_root / "packaging" / "entrypoint.py")],
    pathex=[str(repo_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "onnxruntime",
        "mcp.server.fastmcp",
        "fastembed",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="orlog",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
```

- [ ] **Step 6: Install build dependencies and run the local build**

Run: `py -m pip install -e ".[anthropic,openai,dev,build]"`
Then run: `py -m PyInstaller orlog.spec`
Expected: completes and produces `dist/orlog.exe` (this machine is Windows). Note any `WARNING: Hidden import ... not found` lines in the output — not necessarily fatal by themselves, but worth cross-checking against Step 7's failures.

- [ ] **Step 7: Run the smoke test against the built binary, iterating until it passes**

Run: `py packaging/smoke_test.py dist/orlog.exe`

If it fails with `ModuleNotFoundError: No module named 'X'` (typically surfaced in the `serve` or `conformance` stderr the script prints), add `"X"` to the `hiddenimports` list in `orlog.spec`, re-run Step 6's `PyInstaller orlog.spec` and this step again. Repeat until `Smoke test passed.` prints and the script exits 0. This loop is expected — PyInstaller's static import analysis routinely misses dynamically-imported modules in packages like `onnxruntime`/`fastembed`/`mcp`, and the exact set can't be known without actually running the build on this machine.

Expected end state: `Smoke test passed.`, exit code 0.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore packaging/entrypoint.py packaging/smoke_test.py orlog.spec
git commit -m "build: add PyInstaller spec, entrypoint, and smoke test for a standalone orlog binary"
```

(Do not add `dist/`/`build/` — they're gitignored per Step 2, and are build output, not source.)

---

### Task 3: GitHub Actions release pipeline

**Files:**
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: `orlog.spec` and `packaging/smoke_test.py` from Task 2, run exactly as validated locally there.

- [ ] **Step 1: Create the release workflow**

Create `.github/workflows/release.yml`:

```yaml
name: Release

on:
  push:
    tags:
      - "v*"

permissions:
  contents: write

jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        include:
          - os: windows-latest
            asset_os: windows
            asset_arch: x86_64
            bin_name: orlog.exe
          - os: macos-latest
            asset_os: macos
            asset_arch: arm64  # macos-latest runners are Apple Silicon
            bin_name: orlog
          - os: ubuntu-latest
            asset_os: linux
            asset_arch: x86_64
            bin_name: orlog
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"

      - name: Install orlog and build tooling
        run: python -m pip install -e ".[anthropic,openai,dev,build]"

      - name: Build standalone binary
        run: pyinstaller orlog.spec

      - name: Smoke test the built binary
        run: python packaging/smoke_test.py dist/${{ matrix.bin_name }}

      - name: Package artifact
        shell: bash
        run: |
          set -e
          version="${GITHUB_REF_NAME#v}"
          mkdir -p out
          cd dist
          zip -r "../out/orlog-${version}-${{ matrix.asset_os }}-${{ matrix.asset_arch }}.zip" "${{ matrix.bin_name }}"

      - uses: actions/upload-artifact@v4
        with:
          name: orlog-${{ matrix.asset_os }}
          path: out/*.zip

  release:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with:
          path: out
          merge-multiple: true

      - uses: softprops/action-gh-release@v2
        with:
          files: out/*.zip
```

- [ ] **Step 2: Validate the YAML parses**

Run: `py -m pip install pyyaml` (one-off, not a project dependency)
Then run: `py -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml', encoding='utf-8')); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: add tag-triggered release pipeline building standalone orlog binaries"
```

**Not part of this task's steps, deliberately:** actually pushing a `vX.Y.Z` tag to trigger the first real run. That creates a public GitHub Release and consumes CI minutes on 3 runners — a real, externally-visible action. Once this plan's tasks are merged, push a tag yourself when you're ready to cut the first release; don't automate that push as part of "finishing" this plan.

---

### Task 4: Document the standalone binary in README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a new section**

Insert a new `## Standalone binary (no Python required)` section into `README.md`, directly after the existing `## Using the CLI` section (i.e. after its closing code block, before `## Known deviations from ORLOG-SPEC.md v1.0`):

```markdown
## Standalone binary (no Python required)

Every tagged release (`vX.Y.Z`) publishes standalone `orlog` executables
for Windows, macOS, and Linux to
[GitHub Releases](https://github.com/mrrasmussendk/Orlog/releases) — no
Python install required. Download the zip for your platform, unzip it
somewhere on `PATH`, and the same CLI/MCP-server workflow above applies
unchanged:

```
orlog init myworkspace --claude-code
```

The binary bundles both the `anthropic` and `openai` extras. The default
`fastembed` embedder still downloads its model over the network on first
use (see `orlog.toml`'s `[retrieval] embedder`); everything else works
fully offline.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document the standalone binary install path"
```
