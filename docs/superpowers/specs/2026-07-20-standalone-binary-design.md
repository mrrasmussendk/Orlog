# Standalone orlog binary: build pipeline for a Python-free install

Status: approved for planning
Date: 2026-07-20

## Motivation

orlog is installed today via `pip`/`pipx` from source or GitHub, which
requires a working Python toolchain on the machine. That's friction for a
non-Python consumer — e.g. using orlog's MCP server and CLI (`init`,
`serve`, `replay`, `inspect`, `forget`, `conformance`) alongside a .NET
project, on a machine where `pip`/`python` aren't even on `PATH` (only the
Microsoft Store stub is, as hit directly in this session). A standalone
executable — download one file, run it, no Python required at all —
removes that friction entirely, the same way most MCP servers people
install with one command are consumed as opaque binaries.

## Goals

- A GitHub Actions pipeline that builds a single standalone executable per
  platform (Windows, macOS, Linux) from `src/orlog/`, using PyInstaller in
  `--onefile` mode.
- The executable exposes orlog's full CLI surface unchanged — `init`
  (including `--claude-code` auto-registration), `serve`, `replay`,
  `inspect`, `forget`, `conformance` — not just the MCP server subset.
- Both `anthropic` and `openai` extras are bundled into every build, so the
  binary works with either LLM `Deriver` backend out of the box, selected
  at runtime the same way as today (`orlog.toml`'s `[deriver] backend`).
- `orlog conformance` works unchanged inside the frozen binary:
  `tests/conformance/` is bundled as PyInstaller data files, and the
  path resolution in `cmd_conformance` (`cli.py`) is fixed to resolve
  correctly both when run from source and when frozen
  (`sys.frozen`/`sys._MEIPASS`).
- Builds trigger on pushing a version tag (`v*`), and are published as
  artifacts attached to a GitHub Release for that tag.
- A post-build smoke test in CI actually runs the built binary (not just
  checks that the build succeeded) — `orlog init` into a scratch dir plus
  a basic invocation that exercises the fastembed/onnxruntime import path,
  since that's the highest-risk dependency to bundle correctly.

## Non-goals

- No cross-compilation. PyInstaller builds natively per OS; the GitHub
  Actions matrix (`windows-latest`, `macos-latest`, `ubuntu-latest`) covers
  this, matching the shape of the existing `ci.yml` test matrix.
- No bundling of the fastembed embedding model weights (~130MB). The
  binary keeps today's behavior: the model downloads and caches on first
  `recall()` call that needs the embedder. The binary itself stays in the
  tens-of-MB-plus-deps range rather than 150MB+ from model weights alone.
- No code signing. Unsigned binaries will trigger SmartScreen (Windows)
  and Gatekeeper (macOS) warnings on first run — documented as a known
  limitation, not solved here. Can be a follow-up if distribution grows
  beyond personal/team use.
- No changes to `src/orlog/`'s runtime behavior, dependency versions, or
  the existing pip/pipx install path — this is purely an additional
  distribution mechanism alongside what already exists in `README.md`.
- No in-binary test suite run beyond the smoke test. `ci.yml`'s existing
  matrix already validates correctness on real Python across 3.10–3.12;
  this pipeline only needs to prove packaging didn't break anything.

## Approach

**Build tool: PyInstaller**, `--onefile` mode, driven by a checked-in
`orlog.spec` file. Rejected alternatives:

- **Nuitka** — could produce a faster/smaller binary, but build
  configuration for this dependency stack (onnxruntime via fastembed,
  cryptography, two LLM SDKs) is more fragile and slower to iterate on,
  for a tool that's I/O-bound (MCP stdio, file/DB access), not CPU-bound.
  Not worth the extra build complexity.
- **Docker image** — rejected outright: the goal is a *standalone*
  executable, and a Docker image trades "install Python" for "install
  Docker Desktop," which doesn't reduce friction for a .NET-project
  consumer any more than the current pip-based install does.

`--onefile` self-extracts to a temp directory on every launch (~0.5-1s
overhead). For `orlog serve` (a long-lived MCP stdio process) this is a
one-time cost per session; for one-shot commands like `orlog inspect`
it's a small per-invocation tax, accepted as a reasonable trade for the
simplicity of "one file, one download" over `--onedir`'s multi-file output.

## Pipeline structure

- New workflow file, e.g. `.github/workflows/release.yml` (kept separate
  from `ci.yml`'s test matrix — distinct concern, distinct trigger).
- **Trigger:** `push` of a tag matching `v*` (e.g. `v0.1.0`).
- **Matrix:** `windows-latest`, `macos-latest`, `ubuntu-latest`.
- **Per-job steps:**
  1. Checkout, set up Python (matching the minimum supported version,
     3.10, for the widest compatible build).
  2. `pip install -e ".[anthropic,openai]" pyinstaller`.
  3. `pyinstaller orlog.spec` — the spec file declares the `orlog` CLI
     entry point as the target, bundles `tests/conformance/` as `datas`,
     and includes any PyInstaller hooks needed for `onnxruntime`
     (available via `pyinstaller-hooks-contrib`, added as a build-only
     dependency).
  4. Smoke test: run the built binary's `init` and `--help`-equivalent
     paths against a scratch directory; fail the job if either errors.
  5. Package the single binary into
     `orlog-<version>-<os>-<arch>.zip` (`orlog.exe` on Windows, `orlog`
     elsewhere).
  6. Upload the zip as a build artifact.
- **Publish job** (after the matrix succeeds): create a GitHub Release
  from the tag (auto-generated release notes to start) and attach all
  three platform zips.

## Code changes required

- `cli.py`'s `cmd_conformance`: replace the current
  `Path(__file__).resolve().parents[2] / "tests" / "conformance"`
  resolution (which assumes an editable/source install layout that
  doesn't exist inside a frozen PyInstaller bundle) with logic that
  checks `sys.frozen` and resolves against `sys._MEIPASS` when frozen,
  falling back to today's path otherwise.
- New `orlog.spec` file at the repo root, declaring the PyInstaller build
  (entry point, `datas` for `tests/conformance/`, any explicit hidden
  imports PyInstaller's static analysis misses for `fastembed`/
  `onnxruntime`/`mcp`).
- `pyproject.toml`: add `pyinstaller` and `pyinstaller-hooks-contrib` as a
  new optional-dependency group (e.g. `[project.optional-dependencies]
  build = [...]`) so the release workflow's install step is declarative
  rather than a bare `pip install pyinstaller` floating in YAML.

## Testing

- CI smoke test per platform, as described above — the bar is "the built
  binary starts, initializes a workspace, and doesn't crash on import,"
  not full functional coverage (that's `ci.yml`'s job on real Python).
- Manually verified once per platform before the first tagged release:
  full `orlog init --claude-code` → `orlog serve` → a real `remember`/
  `recall` round-trip through Claude Code, on at least Windows (the
  motivating platform).

## Known limitations (accepted, documented)

- Unsigned binaries trigger OS security warnings on first run.
- Binary size: expect roughly 150-300MB per platform (onnxruntime +
  cryptography + two LLM SDKs + interpreter), acceptable for a one-time
  GitHub Release download.
- `--onefile` adds modest startup latency versus a raw Python invocation
  or `--onedir`.
