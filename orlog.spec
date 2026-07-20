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
        # Only imported by tests/conftest.py + tests/conformance/*.py (bundled
        # as `datas` above and loaded dynamically by pytest.main() at
        # runtime), not by any module reachable from packaging/entrypoint.py's
        # static import graph -- orlog.huginn.Assertion / orlog.heimdall.
        # VerificationResult are the classes actually used at runtime; these
        # orlog.models.* ones are a separate, older model pair the test
        # fixtures still build directly.
        "orlog.models.assertion",
        "orlog.models.verification",
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
