"""Regression tests for the search-vs-reclamation race.

Scenario under test
--------------------
A researcher's slow search selects a still-searchable old version. Before that
request reads any text, an OCR correction finishes uploading, publishes the new
version and reaps the old version's tables. Historically the search then failed
with ``OperationalError: no such table`` (or could silently read the wrong
version / miss hits).

The interleavings here are driven explicitly with :class:`threading.Event`
barriers instead of sleeps, so they are fully repeatable. Two layers are
exercised:

* :class:`VersionReaper` pin/retire semantics directly;
* the full HTTP path (via the WSGI shim), including a coordination hook that
  blocks a search *after* it pinned its version but *before* it opens its read
  snapshot — the exact window in which the old implementation broke.

The pre-fix matching guarantees (bounded memory, cross-block/cross-page hits,
whitespace-insensitive matching, highlight coordinates on the original text) are
re-asserted on the pinned old version while a newer version is already live.
"""
from __future__ import annotations

import contextvars
import threading
import unittest

from app import db, ingest
from app.db import get_conn
from app.reaper import VersionReaper

from tests.helpers import block, make_app, ndjson, reset_db, wsgi_request

DOC = "doc-race"

# Marks the single search request that the current test wants to park. The
# context propagates into the endpoint's threadpool worker in both the offline
# shim and real FastAPI/Starlette.
_parked_search: contextvars.ContextVar = contextvars.ContextVar(
    "parked_search", default=None
)


def _table_names(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


class VersionReaperUnitTest(unittest.TestCase):
    """Pin registry semantics independent of the HTTP layer."""

    def setUp(self) -> None:
        reset_db()
        self.conn = get_conn()
        db.init_schema(self.conn)
        self.drops: list[int] = []
        self.reaper = VersionReaper()

        def record_drop(vid: int) -> None:
            self.drops.append(vid)

        self.drop = record_drop

    def tearDown(self) -> None:
        self.conn.close()

    def test_retire_without_pins_drops_immediately(self):
        self.reaper.retire(7, self.drop)
        self.assertEqual(self.drops, [7])
        # A retired version can never be pinned by a late reader.
        self.assertFalse(self.reaper.pin(7))
        self.assertEqual(self.reaper.active_pins(), {})

    def test_retire_with_pin_defers_until_last_unpin(self):
        self.assertTrue(self.reaper.pin(3))
        self.assertTrue(self.reaper.pin(3))  # two concurrent slow readers
        self.reaper.retire(3, self.drop)
        self.assertEqual(self.drops, [])  # held by both readers
        # New readers cannot attach to a version already being reclaimed.
        self.assertFalse(self.reaper.pin(3))

        self.reaper.unpin(3)
        self.assertEqual(self.drops, [])  # one reader still active
        self.reaper.unpin(3)
        self.assertEqual(self.drops, [3])  # last reader releases -> reaped
        self.assertEqual(self.reaper.active_pins(), {})

    def test_pin_then_retire_then_pin_refused(self):
        self.assertTrue(self.reaper.pin(9))
        self.reaper.retire(9, self.drop)
        self.assertFalse(self.reaper.pin(9))
        self.reaper.unpin(9)
        self.assertEqual(self.drops, [9])

    def test_unpin_of_unknown_version_is_safe(self):
        # Never pinned / already released: must not raise.
        self.reaper.unpin(123)
        self.reaper.pin(5)
        self.reaper.unpin(5)
        self.reaper.unpin(5)
        self.assertEqual(self.drops, [])

    def test_state_is_only_bounded_counters(self):
        # The registry must never hold document data, just integer bookkeeping.
        for vid in range(50):
            self.reaper.pin(vid)
        for vid in range(50):
            self.reaper.retire(vid, self.drop)
        self.assertEqual(len(self.reaper.active_pins()), 50)
        for vid in range(50):
            self.reaper.unpin(vid)
        self.assertEqual(sorted(self.drops), list(range(50)))
        self.assertEqual(self.reaper.active_pins(), {})


class SearchReclamationInterleaveTest(unittest.TestCase):
    """End-to-end interleavings through the real request handlers."""

    def setUp(self) -> None:
        reset_db()
        self.reaper = VersionReaper()
        self.app = make_app(self.reaper)
        # Barriers are installed per parked search by _start_slow_search; no
        # hook by default so ordinary requests never block.
        self.entered = threading.Event()
        self.release = threading.Event()
        self.app.state.on_search_pinned = None

        # Baseline version V1 with a cross-page sentence.
        status, body = wsgi_request(
            self.app,
            "PUT",
            f"/documents/{DOC}",
            ndjson(
                [
                    block("old-b1", 1, "舊版第一句文字跨"),
                    block("old-b2", 2, " 頁 的第二句"),
                ]
            ),
        )
        self.assertEqual(status, 202, body)
        status, body = wsgi_request(
            self.app,
            "GET",
            f"/documents/{DOC}/search",
            query={"q": "舊版第一句文字"},
        )
        self.assertEqual(status, 200)
        self.old_vid = body["version_id"]

    def _start_slow_search(self, query: str) -> threading.Thread:
        # Fresh barriers per invocation: threading.Event is sticky and cannot be
        # re-armed cleanly, so each parked search gets its own pair. The shim
        # (and real FastAPI) run the sync endpoint inside a threadpool; a
        # contextvar is the robust marker because it propagates across both
        # asyncio.to_thread and run_in_executor, so the hook parks only THIS
        # search while concurrent ordinary requests keep running.
        self.entered = threading.Event()
        self.release = threading.Event()
        token = object()
        result: dict = {}

        def worker() -> None:
            _parked_search.set(token)
            result["response"] = wsgi_request(
                self.app,
                "GET",
                f"/documents/{DOC}/search",
                query={"q": query},
            )

        thread = threading.Thread(target=worker, daemon=True)

        def park_only_worker(doc_id: str, vid: int) -> None:
            if _parked_search.get() is token:
                self.entered.set()
                self.release.wait(timeout=10)

        self.app.state.on_search_pinned = park_only_worker
        thread.start()
        self.assertTrue(self.entered.wait(timeout=10), "search never pinned a version")
        return thread, result

    def _publish_correction(self) -> int:
        status, body = wsgi_request(
            self.app,
            "PUT",
            f"/documents/{DOC}",
            ndjson([block("new-b1", 1, "更正後的全新版本內容")]),
        )
        self.assertEqual(status, 202, body)
        return body["version_id"]

    def test_old_request_survives_publication_and_then_is_reaped(self):
        """The headline bug: publish+reap lands while an old search is pinned."""
        thread, result = self._start_slow_search("跨頁的第二句")

        # While the old search holds its pin, the correction publishes. Its old
        # tables must be retained (deferred), and new requests must see V2.
        new_vid = self._publish_correction()
        self.assertNotEqual(new_vid, self.old_vid)

        conn = get_conn()
        try:
            names = _table_names(conn)
            self.assertIn(f"blocks_{self.old_vid}", names)
            self.assertEqual(db.get_active_version(conn, DOC), new_vid)
        finally:
            conn.close()

        # A brand-new request already serves the new version while the slow one
        # is still parked on the old one.
        status, body = wsgi_request(
            self.app,
            "GET",
            f"/documents/{DOC}/search",
            query={"q": "更正後的全新版本內容"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["version_id"], new_vid)
        self.assertEqual(body["result_count"], 1)
        # The old phrase must not leak through a new-version search.
        status, body = wsgi_request(
            self.app,
            "GET",
            f"/documents/{DOC}/search",
            query={"q": "舊版第一句文字"},
        )
        self.assertEqual(body["version_id"], new_vid)
        self.assertEqual(body["result_count"], 0)

        # Release the parked old request: it must complete on the version it
        # originally selected, with correct cross-page hit and coordinates.
        self.release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        status, body = result["response"]
        self.assertEqual(status, 200, body)
        self.assertEqual(body["version_id"], self.old_vid)
        self.assertEqual(body["result_count"], 1)
        hit = body["hits"][0]
        self.assertTrue(hit["crosses_pages"])
        self.assertTrue(hit["crosses_blocks"])
        self.assertEqual(
            [s["block_id"] for s in hit["spans"]], ["old-b1", "old-b2"]
        )
        # Highlight ranges must point into the ORIGINAL whitespace-bearing text.
        span2 = next(s for s in hit["spans"] if s["block_id"] == "old-b2")
        self.assertEqual(
            " 頁 的第二句"[span2["char_start"]:span2["char_end"]],
            "頁 的第二句",
        )

        # Once the old request finishes, the obsolete tables are reclaimed; the
        # registry does not pin anything anymore (no permanent retention).
        self.assertEqual(self.reaper.active_pins(), {})
        conn = get_conn()
        try:
            names = _table_names(conn)
        finally:
            conn.close()
        self.assertNotIn(f"blocks_{self.old_vid}", names)
        self.assertIn(f"blocks_{new_vid}", names)

    def test_whitespace_and_overlap_rules_hold_on_pinned_old_version(self):
        """Matching invariants must be unchanged for the pinned old version."""
        # First, while only V1 exists, confirm ordered/overlapping hits and the
        # whitespace-bearing highlight mapping on the version that will later be
        # pinned across a publication.
        status, body = wsgi_request(
            self.app,
            "GET",
            f"/documents/{DOC}/search",
            query={"q": "句"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["version_id"], self.old_vid)
        starts = [h["compact_start"] for h in body["hits"]]
        self.assertEqual(starts, sorted(starts))
        self.assertGreaterEqual(len(starts), 1)

        # Now park a search on V1 and publish the correction underneath it.
        # Whitespace inside the query is ignored even in the pinned snapshot.
        thread, result = self._start_slow_search("跨 頁 的 第 二 句")
        self._publish_correction()
        self.release.set()
        thread.join(timeout=10)
        status, body = result["response"]
        self.assertEqual(status, 200, body)
        self.assertEqual(body["version_id"], self.old_vid)
        self.assertEqual(body["result_count"], 1)
        hit = body["hits"][0]
        # Whitespace is ignored for matching; compact length excludes spaces.
        self.assertEqual(hit["compact_end"] - hit["compact_start"], 6)
        # matched_text preserves the ORIGINAL characters (spaces kept), and
        # stripping it yields exactly the compact query.
        self.assertEqual(
            "".join(hit["matched_text"].split()), "跨頁的第二句"
        )

    def test_request_reselects_when_version_retired_before_pin(self):
        """If the pointer flips in the read-pointer -> pin gap, serve the new one.

        The reaper refuses to pin a retired version; the endpoint must re-read
        the catalog pointer and complete against the replacement instead of
        erroring.
        """
        # Pre-retire the currently active version directly, simulating a
        # publication that lands in the gap between the endpoint's pointer read
        # and its pin call.
        conn = get_conn()
        try:
            new_vid = db.create_version(conn, DOC)
            count = ingest.ingest_version(
                conn,
                new_vid,
                iter([ndjson([block("gap-b1", 1, "隙縫後的新版本文字")])]),
            )
            old = db.activate_version(conn, DOC, new_vid, count)
            self.assertEqual(old, self.old_vid)
        finally:
            conn.close()

        def drop(vid: int) -> None:
            c = get_conn()
            try:
                with db.transaction(c):
                    db.drop_version_tables(c, vid)
            finally:
                c.close()

        self.reaper.retire(self.old_vid, drop)
        self.assertTrue(self.reaper.is_retired(self.old_vid))

        # Observe the version actually pinned by the endpoint. It must not pin
        # the retired old version; it re-reads the pointer and pins the new one.
        seen = threading.Event()

        def hook(doc_id: str, vid: int) -> None:
            if vid == new_vid:
                seen.set()

        self.app.state.on_search_pinned = hook
        status, body = wsgi_request(
            self.app,
            "GET",
            f"/documents/{DOC}/search",
            query={"q": "隙縫後的新版本文字"},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(seen.wait(timeout=5))
        self.assertEqual(body["version_id"], new_vid)
        self.assertEqual(body["result_count"], 1)

    def test_publishing_never_blocked_by_slow_search(self):
        """A parked slow search must not delay the catalog switch itself."""
        thread, _ = self._start_slow_search("舊版第一句文字")
        # The PUT (including its background activation) completes fully while
        # the reader is parked; only the physical old-table drop is deferred.
        new_vid = self._publish_correction()

        conn = get_conn()
        try:
            self.assertEqual(db.get_active_version(conn, DOC), new_vid)
            state = conn.execute(
                "SELECT state FROM versions WHERE version_id = ?", (new_vid,)
            ).fetchone()["state"]
        finally:
            conn.close()
        self.assertEqual(state, db.STATE_READY)
        self.release.set()
        thread.join(timeout=10)

    def test_restart_reclaims_tables_orphaned_by_interrupted_deferred_drop(self):
        """A process killed while a pin defers a drop is cleaned on recovery.

        On a real restart the in-process pin registry is empty (no readers) but
        the superseded version's tables may still be on disk because their
        deferred drop never ran. Startup recovery must reclaim them while
        keeping the active version fully searchable.
        """
        # Publish the correction through the API so V2 is the active version.
        new_vid = self._publish_correction()

        # Recreate the crash residue: tables for the non-active old version are
        # present on disk even though no in-process reader pins them anymore.
        conn = get_conn()
        try:
            db.create_version_tables(conn, self.old_vid)
            conn.commit()
            self.assertIn(f"blocks_{self.old_vid}", _table_names(conn))
        finally:
            conn.close()

        # A brand-new app (fresh empty registry) runs startup recovery.
        app2 = make_app()
        status, report = wsgi_request(app2, "GET", "/recovery")
        self.assertEqual(status, 200)
        self.assertTrue(
            any("reclaimed" in m for m in report["recovered"]),
            report["recovered"],
        )
        conn = get_conn()
        try:
            names = _table_names(conn)
            self.assertNotIn(f"blocks_{self.old_vid}", names)
            self.assertIn(f"blocks_{new_vid}", names)
        finally:
            conn.close()
        # Active version survives recovery and stays searchable.
        status, body = wsgi_request(
            app2,
            "GET",
            f"/documents/{DOC}/search",
            query={"q": "更正後的全新版本內容"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["version_id"], new_vid)

    def test_restart_removes_orphan_tables_without_version_row(self):
        """Version-prefixed tables with no catalog row are swept on recovery."""
        new_vid = self._publish_correction()
        # Fabricate an orphan: a blocks table for an id absent from versions.
        orphan_vid = 9999
        conn = get_conn()
        try:
            db.create_version_tables(conn, orphan_vid)
            conn.commit()
            self.assertIn(f"blocks_{orphan_vid}", _table_names(conn))
        finally:
            conn.close()

        app2 = make_app()
        status, report = wsgi_request(app2, "GET", "/recovery")
        self.assertEqual(status, 200)
        self.assertTrue(
            any("orphan table" in m and str(orphan_vid) in m
                for m in report["recovered"]),
            report["recovered"],
        )
        conn = get_conn()
        try:
            names = _table_names(conn)
        finally:
            conn.close()
        self.assertNotIn(f"blocks_{orphan_vid}", names)
        self.assertIn(f"blocks_{new_vid}", names)


class ConcurrentStressTest(unittest.TestCase):
    """Genuine parallel contention: searches run while corrections republish.

    Unlike the scripted Event tests, here many reader threads and a publisher
    thread race without coordination. The invariants are that every search
    either succeeds against one complete version (HTTP 200 with internally
    consistent hits) or is a clean 404/400, never a 500/"no such table", and
    after everything settles the registry holds no pins and only the active
    version's tables remain.
    """

    def setUp(self) -> None:
        reset_db()
        self.reaper = VersionReaper()
        self.app = make_app(self.reaper)
        self.app.state.on_search_pinned = None
        status, body = wsgi_request(
            self.app,
            "PUT",
            f"/documents/{DOC}",
            ndjson(
                [
                    block("s-b1", 1, "共用關鍵字alpha"),
                    block("s-b2", 2, "共用關鍵字beta 尾句"),
                ]
            ),
        )
        self.assertEqual(status, 202, body)

    def test_many_readers_during_repeated_replacements(self):
        n_readers = 8
        rounds = 4
        errors: list[str] = []
        responses: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def reader() -> None:
            # Each reader issues several searches spanning the publications.
            for _ in range(rounds):
                try:
                    status, body = wsgi_request(
                        self.app,
                        "GET",
                        f"/documents/{DOC}/search",
                        query={"q": "共用關鍵字"},
                    )
                except Exception as exc:  # pragma: no cover - failure signal
                    with lock:
                        errors.append(f"reader raised {exc!r}")
                    return
                with lock:
                    responses.append((status, body))
                if status == 200:
                    # The snapshot must be self-consistent: every returned hit
                    # belongs to the response's pinned version and has spans.
                    for hit in body["hits"]:
                        if not hit["spans"]:
                            errors.append("hit without spans")
                        if "".join(hit["matched_text"].split()) != (
                            body["compact_query"]
                        ):
                            errors.append("matched_text inconsistent with query")

        def publisher() -> None:
            for i in range(rounds):
                status, body = wsgi_request(
                    self.app,
                    "PUT",
                    f"/documents/{DOC}",
                    ndjson(
                        [
                            block(f"r{i}-b1", 1, f"第{i}版共用關鍵字alpha"),
                            block(f"r{i}-b2", 2, f"第{i}版共用關鍵字beta"),
                        ]
                    ),
                )
                if status != 202:
                    with lock:
                        errors.append(f"publisher got {status} {body}")

        readers = [threading.Thread(target=reader) for _ in range(n_readers)]
        pub = threading.Thread(target=publisher)
        for t in readers:
            t.start()
        pub.start()
        for t in readers:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "reader timed out")
        pub.join(timeout=30)
        self.assertFalse(pub.is_alive(), "publisher timed out")

        self.assertEqual(errors, [], errors)
        # Every search was a clean 200 (the document always has an active
        # version); none leaked a 500 storage/"no such table" error.
        self.assertTrue(responses)
        statuses = {status for status, _ in responses}
        self.assertTrue(statuses <= {200}, statuses)

        # After quiescence: no leaked pins and only the final active version's
        # tables remain on disk.
        self.assertEqual(self.reaper.active_pins(), {})
        conn = get_conn()
        try:
            active = db.get_active_version(conn, DOC)
            names = _table_names(conn)
        finally:
            conn.close()
        self.assertIsNotNone(active)
        for prefix in ("blocks_", "unigrams_", "bigrams_"):
            leftover = [
                n for n in names if n.startswith(prefix) and n != f"{prefix}{active}"
            ]
            self.assertEqual(leftover, [], f"obsolete tables leaked: {leftover}")

        # The final version is searchable and reflects the last correction.
        status, body = wsgi_request(
            self.app,
            "GET",
            f"/documents/{DOC}/search",
            query={"q": f"第{rounds - 1}版"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["version_id"], active)
        self.assertGreaterEqual(body["result_count"], 1)


if __name__ == "__main__":
    unittest.main()
