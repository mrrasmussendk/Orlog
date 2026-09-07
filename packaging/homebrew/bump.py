"""Regenerate packaging/homebrew/orlog.rb for a released version.

Reads the checksums straight from the GitHub release assets, so the
formula can never claim a sha256 that doesn't match what was published --
the failure mode of hand-editing, and one Homebrew reports to users as a
checksum mismatch rather than as a maintainer error.

Usage:
    python packaging/homebrew/bump.py 0.0.4
    python packaging/homebrew/bump.py 0.0.3 --check   # verify, don't write

Then copy the result into the tap repo (mrrasmussendk/homebrew-tap) at
Formula/orlog.rb.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
from pathlib import Path

FORMULA = Path(__file__).with_name("orlog.rb")
REPO = "mrrasmussendk/Orlog"
ASSETS = {
    "macos-arm64": "orlog-{version}-macos-arm64.zip",
    "linux-x86_64": "orlog-{version}-linux-x86_64.zip",
}


def _asset_url(version: str, filename: str) -> str:
    return f"https://github.com/{REPO}/releases/download/v{version}/{filename}"


def _sha256_of(url: str) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url) as response:  # noqa: S310 - a fixed github.com URL
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render(version: str) -> str:
    text = FORMULA.read_text(encoding="utf-8")
    text = re.sub(r'^  version "[^"]+"$', f'  version "{version}"', text, count=1, flags=re.M)

    for name, pattern in ASSETS.items():
        filename = pattern.format(version=version)
        url = _asset_url(version, filename)
        print(f"  fetching {name}: {filename}", file=sys.stderr)
        checksum = _sha256_of(url)
        # Rewrite the url/sha256 pair for this platform together, so a
        # partially-updated formula (new url, stale checksum) is impossible.
        text = re.sub(
            r'(url "https://github\.com/[^"]*' + re.escape(name) + r'\.zip"\n\s*sha256 ")[0-9a-f]{64}(")',
            lambda m, c=checksum: m.group(1) + c + m.group(2),
            text,
            count=1,
        )
        text = re.sub(
            r'url "https://github\.com/[^"]*' + re.escape(name) + r'\.zip"',
            f'url "{url}"',
            text,
            count=1,
        )
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="release version WITHOUT the leading v, e.g. 0.0.4")
    parser.add_argument("--check", action="store_true", help="verify the formula is already correct")
    args = parser.parse_args()

    updated = render(args.version)
    current = FORMULA.read_text(encoding="utf-8")

    if args.check:
        if updated == current:
            print(f"formula is up to date for {args.version}")
            return 0
        print(f"formula does NOT match the published {args.version} assets", file=sys.stderr)
        return 1

    FORMULA.write_text(updated, encoding="utf-8")
    print(f"wrote {FORMULA} for {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
