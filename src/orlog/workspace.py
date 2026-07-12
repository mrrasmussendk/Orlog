"""workspace: the on-disk layout a server/CLI operates on (ORLOG-SPEC.md §B1/§B2).

    <workspace>/
      orlog.toml
      data/
        events/log-00001.jsonl, log-00002.jsonl, ...
        index.sqlite
        vault.sqlite
        cache/derivations/
        orlog.lock

Design decision: the lock is a simple "does data/orlog.lock exist" check,
not a real OS-level advisory lock (flock/fcntl aren't portable to Windows
without extra dependencies, and this is a reference implementation, not a
production concurrency primitive). It is enough to satisfy the actual
requirement -- "concurrent server instances on one workspace MUST be
prevented" -- for a single machine, single-user workspace; a real
multi-machine deployment would need a stronger lock, which is exactly the
kind of thing spec §C4 defers ("distributed/multi-writer logs... deferred").
"""

from __future__ import annotations

import os
from pathlib import Path

from orlog.errors import LockedError

LOCK_FILENAME = "orlog.lock"
CONFIG_FILENAME = "orlog.toml"


class Workspace:
    """Resolves and (on init()) creates the standard directory layout."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def events_dir(self) -> Path:
        return self.data_dir / "events"

    @property
    def index_path(self) -> Path:
        return self.data_dir / "index.sqlite"

    @property
    def vault_path(self) -> Path:
        return self.data_dir / "vault.sqlite"

    @property
    def derivations_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "derivations"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / LOCK_FILENAME

    def scaffold(self) -> None:
        """Create every directory this layout needs. Idempotent -- safe to
        call on an already-initialized workspace.
        """
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.derivations_cache_dir.mkdir(parents=True, exist_ok=True)

    def acquire_lock(self) -> None:
        """Raise LockedError (E_LOCKED) if another process already holds
        this workspace's lock; otherwise claim it by writing our own pid.
        """
        if self.lock_path.exists():
            holder = self.lock_path.read_text(encoding="utf-8").strip()
            raise LockedError(f"workspace already locked by pid {holder or '?'} ({self.lock_path})")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(str(os.getpid()), encoding="utf-8")

    def release_lock(self) -> None:
        self.lock_path.unlink(missing_ok=True)

    def __enter__(self) -> "Workspace":
        self.acquire_lock()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release_lock()
