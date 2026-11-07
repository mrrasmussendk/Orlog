# Releasing orlog

Three things ship on a `vX.Y.Z` tag, from two workflows:

| What | Workflow | Where it lands |
|---|---|---|
| sdist + wheel | [`publish-pypi.yml`](../.github/workflows/publish-pypi.yml) | PyPI (`pip`/`pipx`/`uvx orlog`) |
| standalone binaries (5 platforms) | [`release.yml`](../.github/workflows/release.yml) | GitHub Releases |
| the Homebrew formula | `packaging/homebrew/bump.py`, by hand | the tap repo (`mrrasmussendk/homebrew-tap`) |

## The trap that has already bitten once

**A tag only runs the workflows that exist in the commit it points at.**

`v0.0.3` points at the commit *before* `publish-pypi.yml` was added, so that
workflow has never run for any tag and nothing has ever been uploaded to
PyPI — while the repo looked fully set up for it. Nothing reports this: the
Actions tab simply has no run, and `pip install orlog` fails for everyone.

So: **add the workflow first, tag second.** If a workflow was added after the
tag you want to publish, either re-tag or use `publish-pypi.yml`'s
`workflow_dispatch` input, which publishes whatever version the chosen ref
declares.

## Checklist

1. **Bump `pyproject.toml`'s `version`.** Both workflows refuse to publish a
   tag that disagrees with it — `v0.0.2` once shipped against a
   `pyproject.toml` that said `0.1.0`, which is why that gate exists.
2. **Confirm PyPI Trusted Publishing is configured** for project `orlog`:
   this repo, workflow filename `publish-pypi.yml`, environment `pypi`.
   There is no API token to check — if the publisher is missing, the upload
   step is the first thing that will say so.
3. **Commit, then tag and push:**
   ```bash
   git tag v0.0.4 && git push origin v0.0.4
   ```
   Both workflows trigger on the tag. `release.yml` builds and smoke-tests a
   binary per platform before the GitHub Release is created, so a broken
   build fails the release rather than publishing one.
4. **Bump the Homebrew formula** once the Release has its assets:
   ```bash
   python packaging/homebrew/bump.py 0.0.4          # writes packaging/homebrew/orlog.rb
   python packaging/homebrew/bump.py 0.0.4 --check  # verifies it against the live assets
   ```
   It reads every checksum from the published assets, so the formula cannot
   claim a sha256 that doesn't match what users will download. A platform the
   release didn't build for gets an `odie` pointing at PyPI instead of a
   dead url. Then copy `orlog.rb` into the tap repo at `Formula/orlog.rb`.
5. **Verify the install paths a user actually takes**, on a machine with no
   checkout:
   ```bash
   uvx orlog --help
   pipx install orlog && orlog doctor
   brew install mrrasmussendk/tap/orlog && orlog doctor
   ```

## Platform coverage

`release.yml` builds macOS arm64 + x86_64, Linux x86_64 + arm64, and Windows
x86_64. PyInstaller cannot cross-compile, so each one is its own runner.

Windows arm64 is deliberately absent: `windows-11-arm` runners exist, but
`setup-python`'s arm64 coverage is newer than this project's 3.11 floor, and
a failing row blocks the `release` job (which needs *all* of `build`) from
publishing anything at all. Those users install from PyPI.

macOS binaries are ad-hoc signed, not notarized. Homebrew fetches over curl,
which doesn't set `com.apple.quarantine`, so `brew install` runs clean; a
zip downloaded in a browser hits a Gatekeeper prompt. Notarizing would need
an Apple Developer ID in CI.
