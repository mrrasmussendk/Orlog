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

Because the lock is just a pid file, a process that dies without running
its __exit__ (killed, crashed, machine rebooted) leaves the lock file
behind forever. acquire_lock() guards against that by checking whether the
recorded pid is still alive before honoring the lock -- if it isn't (or the
file is unreadable/corrupt), the lock is stale and gets reclaimed instead
of blocking every future launch.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from orlog.errors import LockedError

LOCK_FILENAME = "orlog.lock"
CONFIG_FILENAME = "orlog.toml"


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check; stdlib-only so it works without psutil."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


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

        A lock file whose recorded pid is no longer running (or isn't a
        parseable pid at all) is stale -- e.g. the previous holder crashed,
        was killed, or the machine rebooted -- and gets reclaimed rather
        than blocking forever.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # O_CREAT|O_EXCL is the whole lock: the check and the claim are one
        # atomic syscall. The previous exists()-then-write_text() left a
        # window between the two in which both of two processes starting
        # together saw no lock, both wrote, and the second silently
        # overwrote the first's pid -- so both believed they held it, and
        # each one's release_lock() then unlinked the other's.
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if not self._reclaim_if_stale():
                holder = self._holder_text()
                raise LockedError(f"workspace already locked by pid {holder} ({self.lock_path})") from None
            # The stale lock is gone; claim it, still atomically. Losing the
            # race here means someone else got there first, which is a
            # legitimate E_LOCKED rather than something to retry around.
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                holder = self._holder_text()
                raise LockedError(f"workspace already locked by pid {holder} ({self.lock_path})") from None
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))

    def _holder_text(self) -> str:
        try:
            return self.lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            return "<unreadable>"

    def _reclaim_if_stale(self) -> bool:
        """Remove the lock if its recorded pid is not running. True if it
        was reclaimed (the previous holder crashed, was killed, or the
        machine rebooted), False if a live process still holds it.
        """
        holder = self._holder_text()
        try:
            holder_pid = int(holder)
        except ValueError:
            holder_pid = None  # not a parseable pid -- treat as stale
        if holder_pid is not None and _pid_alive(holder_pid):
            return False
        self.lock_path.unlink(missing_ok=True)
        return True

    def release_lock(self) -> None:
        self.lock_path.unlink(missing_ok=True)

    def __enter__(self) -> "Workspace":
        self.acquire_lock()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release_lock()
