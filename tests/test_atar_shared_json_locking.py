"""Shared ATaR JSON writes are serialised and atomic.

atar_drafts.json and search_contexts.json were read, modified, and written
without any lock. Two approvals both read the same max prompt index and one
overwrote the other's query; concurrent upserts could drop drafts. Every
read-modify-write now runs under one process-wide lock and lands through an
atomic temp-file rename.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

import atar
from atar import AtarDraft, AtarRuling


def _draft(ref: str) -> AtarDraft:
    ruling = AtarRuling(
        ref=ref,
        commodity_code="8471500000",
        description=f"widget {ref}",
        justification="",
    )
    return AtarDraft(
        ref=ref,
        ruling=atar.asdict(ruling),
        raw_query=f"query {ref}",
        gold_code="8471500000",
        oracle_text=f"oracle {ref}",
        gold_facts=[{"slot": "use", "answer": "machine"}],
        formatted_results=[{"code": "8471500000", "text": f"candidate {ref}"}],
        status="pending",
    )


class AtarSharedJsonLockingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._saved_paths = (atar.DRAFTS_PATH, atar.SEARCH_CONTEXTS_PATH)
        atar.DRAFTS_PATH = root / "atar_drafts.json"
        atar.SEARCH_CONTEXTS_PATH = root / "search_contexts.json"
        atar.SEARCH_CONTEXTS_PATH.write_text(json.dumps({"queries": []}))

    def tearDown(self):
        atar.DRAFTS_PATH, atar.SEARCH_CONTEXTS_PATH = self._saved_paths

    def _seed_drafts(self, refs):
        with atar._ATAR_LOCK:
            data = {"drafts": [atar.asdict(_draft(ref)) for ref in refs]}
            atar._write_drafts_file(data)
        return data

    def test_concurrent_approvals_allocate_distinct_indexes(self):
        refs = [f"ref-{i}" for i in range(8)]
        self._seed_drafts(refs)
        barrier = threading.Barrier(len(refs))
        results = {}
        errors = []

        def approve(ref: str) -> None:
            try:
                barrier.wait(timeout=10)
                results[ref] = atar.approve_draft(ref)
            except Exception as exc:  # noqa: BLE001 - surfaced by assertions
                errors.append(exc)

        threads = [threading.Thread(target=approve, args=(ref,)) for ref in refs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        indexes = sorted(result["prompt_index"] for result in results.values())
        self.assertEqual(indexes, list(range(1, len(refs) + 1)))

        contexts = json.loads(atar.SEARCH_CONTEXTS_PATH.read_text())
        self.assertEqual(len(contexts["queries"]), len(refs))
        self.assertEqual(
            sorted(q["index"] for q in contexts["queries"]),
            list(range(1, len(refs) + 1)),
        )
        # Every approved query survived; none was overwritten by another.
        raw_queries = sorted(q["raw_query"] for q in contexts["queries"])
        self.assertEqual(raw_queries, sorted(f"query {ref}" for ref in refs))

        drafts = atar.list_drafts()
        approved = sorted(d["approved_prompt_index"] for d in drafts)
        self.assertEqual(approved, list(range(1, len(refs) + 1)))
        self.assertTrue(all(d["status"] == "approved" for d in drafts))

    def test_concurrent_upserts_keep_every_draft(self):
        refs = [f"up-{i}" for i in range(8)]
        barrier = threading.Barrier(len(refs))
        errors = []

        def upsert(ref: str) -> None:
            try:
                barrier.wait(timeout=10)
                atar.upsert_draft(_draft(ref))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=upsert, args=(ref,)) for ref in refs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(d["ref"] for d in atar.list_drafts()),
            sorted(refs),
        )

    def test_update_and_discard_run_safely_together(self):
        refs = [f"mix-{i}" for i in range(8)]
        self._seed_drafts(refs)
        barrier = threading.Barrier(len(refs))
        errors = []

        def op(i: int) -> None:
            ref = refs[i]
            try:
                barrier.wait(timeout=10)
                atar.update_draft_fields(ref, oracle_text=f"updated {ref}")
                if i % 2:
                    atar.discard_draft(ref)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=op, args=(i,)) for i in range(len(refs))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        remaining = {d["ref"]: d for d in atar.list_drafts()}
        self.assertEqual(len(remaining), len(refs) - len(refs) // 2)
        for i, ref in enumerate(refs):
            if i % 2 == 0:
                self.assertEqual(remaining[ref]["oracle_text"], f"updated {ref}")

    def test_prompt_save_and_approval_do_not_lose_prompts(self):
        # main.py's api_prompts_save appends to the same
        # search_contexts.json as approve_draft, so a save racing an approval
        # used to allocate the same index and overwrite the other's prompt.
        import asyncio

        import main as product_main

        refs = [f"race-{i}" for i in range(4)]
        self._seed_drafts(refs)
        barrier = threading.Barrier(len(refs) * 2)
        errors = []

        def approve(ref: str) -> None:
            try:
                barrier.wait(timeout=10)
                atar.approve_draft(ref)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def save(i: int) -> None:
            try:
                barrier.wait(timeout=10)
                asyncio.run(
                    product_main.api_prompts_save(
                        {
                            "raw_query": f"manual query {i}",
                            "formatted_results": [{"code": "8471500000", "text": "candidate"}],
                        }
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=approve, args=(ref,)) for ref in refs]
        threads += [threading.Thread(target=save, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        contexts = json.loads(atar.SEARCH_CONTEXTS_PATH.read_text())
        self.assertEqual(len(contexts["queries"]), 8)
        self.assertEqual(
            sorted(q["index"] for q in contexts["queries"]),
            list(range(1, 9)),
        )
        raw_queries = sorted(q["raw_query"] for q in contexts["queries"])
        expected = sorted(
            [f"query {ref}" for ref in refs] + [f"manual query {i}" for i in range(4)]
        )
        self.assertEqual(raw_queries, expected)

    def test_double_approval_is_rejected(self):
        self._seed_drafts(["dup"])
        atar.approve_draft("dup")
        with self.assertRaises(ValueError):
            atar.approve_draft("dup")

    def test_writes_leave_no_temp_files_and_parseable_json(self):
        refs = [f"tmp-{i}" for i in range(3)]
        self._seed_drafts(refs)
        for ref in refs:
            atar.approve_draft(ref)
        leftovers = [p.name for p in Path(self.tmp.name).iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])
        json.loads(atar.DRAFTS_PATH.read_text())
        json.loads(atar.SEARCH_CONTEXTS_PATH.read_text())

    def test_discard_reports_whether_it_removed_anything(self):
        self._seed_drafts(["gone", "kept"])
        self.assertTrue(atar.discard_draft("gone"))
        self.assertFalse(atar.discard_draft("never-existed"))
        self.assertEqual([d["ref"] for d in atar.list_drafts()], ["kept"])


if __name__ == "__main__":
    unittest.main()
