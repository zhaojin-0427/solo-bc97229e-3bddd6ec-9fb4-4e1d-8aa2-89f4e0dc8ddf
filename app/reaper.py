"""In-process registry that keeps a version's tables alive for in-flight reads.

The catalog switch that activates a freshly built version is instantaneous, but
the superseded version's tables must not be dropped while a search request is
still reading them.  A search can take arbitrarily long (large documents, slow
disks), and without coordination this interleaving raises
``OperationalError: no such table``:

    1. search S reads the active pointer and selects version V1;
    2. a correction finishes, activates V2 and drops V1's tables;
    3. S starts reading the version it selected (V1) -> the tables are gone.

This module closes the window with explicit *pins*:

* a search calls :meth:`VersionReaper.pin` for the version it selected **before
  it opens its read snapshot**;
* publishing a superseded version calls :meth:`VersionReaper.retire`, which
  either drops its tables immediately (no reader) or records them as pending
  and waits;
* the search calls :meth:`VersionReaper.unpin` in a ``finally``; releasing the
  last pin of a retired version performs the deferred drop straight away.

A version that has already been retired refuses new pins (``False``), so the
request layer re-reads the catalog pointer and serves the new active version
instead.  Nothing ever keeps an obsolete version forever: every retired version
is dropped as soon as its last in-flight reader finishes, and a process restart
garbage-collects any tables that outlive the registry in :func:`db` recovery.

Memory is bounded: the registry only holds version ids mapped to small integer
counters/sets; no document text, positions or index rows are ever copied here.
The lock is only held for O(1) bookkeeping, never across database I/O, so
publishing is not blocked by a slow search beyond the immediate drop decision.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger("archive")

# A drop callback receives the version id and removes that version's tables.
# Callers supply it so the reaper stays independent of the connection strategy.
DropFunc = Callable[[int], None]


class VersionReaper:
    """Tracks reader pins and deferred table reclamation per version."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # version id -> number of in-flight readers
        self._pins: dict[int, int] = {}
        # versions whose tables must be dropped once their pins reach zero
        self._retired: set[int] = set()
        # versions already reaped (or retired with no readers); they must never
        # accept a late pin, so a late request re-selects the active version
        self._gone: set[int] = set()
        # drop callbacks remembered while a drop is deferred
        self._drop_funcs: dict[int, DropFunc] = {}

    def pin(self, vid: int) -> bool:
        """Register an in-flight reader for version ``vid``.

        Returns ``True`` when the version may still be read.  Returns
        ``False`` when the version has already been retired (its tables are
        gone or about to go); the caller must then re-select the active
        version rather than reading stale tables.
        """
        with self._lock:
            if vid in self._retired or vid in self._gone:
                return False
            self._pins[vid] = self._pins.get(vid, 0) + 1
            return True

    def unpin(self, vid: int) -> None:
        """Release a reader pin, dropping a retired version if it was the last."""
        drop = None
        with self._lock:
            count = self._pins.get(vid, 0)
            if count <= 1:
                self._pins.pop(vid, None)
                if vid in self._retired:
                    self._retired.discard(vid)
                    self._gone.add(vid)
                    drop = self._drop_funcs.pop(vid, None)
            else:
                self._pins[vid] = count - 1
        # Database I/O happens outside the lock so other pin/unpin calls and
        # publications never wait on a slow DROP TABLE.
        if drop is not None:
            self._run_drop(vid, drop)

    def retire(self, vid: int, drop: DropFunc) -> None:
        """Schedule the removal of a superseded version's tables.

        Drops immediately when no reader holds a pin; otherwise the drop is
        deferred and run by the final :meth:`unpin`. In both cases the version
        afterwards refuses new pins.
        """
        do_drop = False
        with self._lock:
            if self._pins.get(vid, 0) == 0:
                do_drop = True
                self._gone.add(vid)
            else:
                self._retired.add(vid)
                self._drop_funcs[vid] = drop
        if do_drop:
            self._run_drop(vid, drop)

    def is_retired(self, vid: int) -> bool:
        with self._lock:
            return vid in self._retired or vid in self._gone

    def active_pins(self) -> dict[int, int]:
        """Snapshot of outstanding pins (mainly for tests/diagnostics)."""
        with self._lock:
            return dict(self._pins)

    @staticmethod
    def _run_drop(vid: int, drop: DropFunc) -> None:
        try:
            drop(vid)
        except Exception:  # pragma: no cover - defensive; recovery reaps later
            logger.exception(
                "deferred drop of version %s failed; startup recovery will "
                "remove its tables",
                vid,
            )
