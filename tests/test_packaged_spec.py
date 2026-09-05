"""Contract: the spec served as the orlog://spec MCP resource is the same
document as docs/ORLOG-SPEC.md.

server.py serves the spec from package data (src/orlog/data/) so that the
resource works from an installed wheel and from the PyInstaller binary, not
only from a source checkout. That means the file exists in two places, and
docs/ORLOG-SPEC.md is the source of truth ("source of truth for src/orlog/",
per the README's Layout section). This test is what stops the copy drifting
away from it silently.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_SPEC = REPO_ROOT / "docs" / "ORLOG-SPEC.md"
PACKAGED_SPEC = REPO_ROOT / "src" / "orlog" / "spec_data" / "ORLOG-SPEC.md"


def test_the_packaged_spec_is_not_gitignored():
    # .gitignore carries a broad `data/` rule (for workspace data dirs),
    # which also matched src/orlog/data/ -- so an earlier location for this
    # file could never be committed, and the packaged resource would have
    # been empty in every install while passing every test locally.
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", str(PACKAGED_SPEC)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode != 0, f"{PACKAGED_SPEC} is gitignored and would never ship"


def test_the_packaged_spec_exists():
    assert PACKAGED_SPEC.exists(), (
        f"{PACKAGED_SPEC} is missing -- the orlog://spec resource would fall back to "
        "'spec not found' for every non-checkout install."
    )


def test_the_packaged_spec_matches_the_docs_copy():
    assert PACKAGED_SPEC.read_text(encoding="utf-8") == DOCS_SPEC.read_text(encoding="utf-8"), (
        "src/orlog/data/ORLOG-SPEC.md has drifted from docs/ORLOG-SPEC.md. "
        "docs/ is the source of truth -- re-copy it: "
        "cp docs/ORLOG-SPEC.md src/orlog/data/ORLOG-SPEC.md"
    )


def test_the_server_can_read_the_packaged_spec():
    from orlog.server import _spec_text

    text = _spec_text()
    assert text is not None and "ORLOG" in text
