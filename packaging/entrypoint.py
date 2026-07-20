"""Entry point for the PyInstaller build (see orlog.spec) -- Analysis
needs a real script file to start from, not the pip-generated console-
script shim, so this thin wrapper calls the same orlog.cli.main() the
`orlog` console script calls.
"""

import sys

from orlog.cli import main

if __name__ == "__main__":
    sys.exit(main())
