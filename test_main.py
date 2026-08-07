#!/usr/bin/env python3
"""Unit tests for main.py. Run with:  python3 -m unittest test_main -v

No network access is needed — page parsing, proxy eviction, file handling,
exports and formatting are all tested against local fixtures.
"""

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

import main as m

m.USE_COLOR = False  # keep to_line() output plain in tests


def _page_taken(username="tiktok", nickname="TikTok", verified=True,
                followers=1234567, signature="hi") -> str:
    data = {
        "__DEFAULT_SCOPE__": {
            "webapp.user-detail": {
                "userInfo": {
                    "user": {
                        "uniqueId": username, "nickname": nickname,
                        "verified": verified, "signature": signature,
                    },
                    "stats": {"followerCount": followers},
                },
                "statusCode": 0,
            }
        }
    }
    body = json.dumps(data)
    return f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{body}</script>'


def _page_available(status_code=10221) -> str:
    data = {
        "__DEFAULT_SCOPE__": {
            "webapp.user-detail": {"statusCode": status_code, "statusMsg": "user not found"},
        }
    }
    body = json.dumps(data)
    return f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{body}</script>'


class ParsePageTests(unittest.TestCase):
    def test_taken(self):
        status, nick, verified, followers, sig = m.UsernameChecker._parse_page(_page_taken())
        self.assertEqual(status, "taken")
        self.assertEqual(nick, "TikTok")
        self.assertTrue(verified)
        self.assertEqual(followers, 1234567)
        self.assertEqual(sig, "hi")

    def test_taken_uses_userinfo_stats(self):
        # follower count lives at userInfo.stats, not user.stats
        status, _n, _v, followers, _s = m.UsernameChecker._parse_page(_page_taken(followers=42))
        self.assertEqual(followers, 42)

    def test_available(self):
        status, nick, verified, followers, _sig = m.UsernameChecker._parse_page(_page_available())
        self.assertEqual(status, "available")
        self.assertEqual((nick, verified, followers), ("", False, 0))

    def test_available_other_not_found_codes(self):
        for code in (10202, 10222, 10223, 10224):
            status, *_ = m.UsernameChecker._parse_page(_page_available(code))
            self.assertEqual(status, "available", f"code {code}")

    def test_blocked_no_rehydration(self):
        status, *_ = m.UsernameChecker._parse_page("<html><body>verify...</body></html>")
        self.assertEqual(status, "blocked")

    def test_blocked_bad_json(self):
        page = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{not json}</script>'
        status, *_ = m.UsernameChecker._parse_page(page)
        self.assertEqual(status, "blocked")

    def test_blocked_unexpected_structure(self):
        page = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{"__DEFAULT_SCOPE__": null}</script>'
        status, *_ = m.UsernameChecker._parse_page(page)
        self.assertEqual(status, "blocked")

    def test_blocked_unknown_status_code(self):
        data = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"statusCode": 99999}}}
        page = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{json.dumps(data)}</script>'
        status, *_ = m.UsernameChecker._parse_page(page)
        self.assertEqual(status, "blocked")


class ProxyPoolTests(unittest.TestCase):
    def test_round_robin(self):
        pool = m.ProxyPool(["a", "b", "c"], max_failures=2)
        self.assertEqual([pool.get() for _ in range(5)], ["a", "b", "c", "a", "b"])

    def test_eviction_after_max_failures(self):
        pool = m.ProxyPool(["a", "b"], max_failures=2)
        self.assertFalse(pool.mark_failure("a"))
        self.assertTrue(pool.mark_failure("a"))  # 2nd failure evicts
        self.assertNotIn("a", pool.snapshot())
        self.assertIn("a", pool.removed)

    def test_success_resets_failure_count(self):
        pool = m.ProxyPool(["a"], max_failures=2)
        pool.mark_failure("a")
        pool.mark_success("a")
        pool.mark_failure("a")
        self.assertIn("a", pool.snapshot())  # not evicted yet

    def test_force_evicts_immediately(self):
        pool = m.ProxyPool(["a"], max_failures=5)
        self.assertTrue(pool.mark_failure("a", force=True))
        self.assertEqual(pool.snapshot(), [])

    def test_empty_pool(self):
        pool = m.ProxyPool([], max_failures=2)
        self.assertIsNone(pool.get())
        self.assertEqual(pool.count, 0)

    def test_dedupe_on_construction(self):
        pool = m.ProxyPool(["a", "a", "b"], max_failures=1)
        self.assertEqual(pool.snapshot(), ["a", "b"])


class UsernameFileTests(unittest.TestCase):
    def _tmp(self, content: str) -> Path:
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write(content)
        f.close()
        return Path(f.name)

    def test_load_valid_and_invalid(self):
        p = self._tmp("# comment\ntiktok\n@mrbeast\nbad name!\nx\n\n   \n")
        valid, invalid = m.load_usernames(p)
        self.assertEqual(valid, ["tiktok", "mrbeast"])
        self.assertEqual(sorted(invalid), ["bad name!", "x"])

    def test_load_dedupes(self):
        p = self._tmp("tiktok\ntiktok\n")
        valid, _ = m.load_usernames(p)
        self.assertEqual(valid, ["tiktok"])

    def test_existing_hits_and_save(self):
        out = self._tmp("")
        existing = m.load_existing_hits(out)
        results = [
            m.CheckResult("a", "available"),
            m.CheckResult("b", "available"),
            m.CheckResult("tiktok", "taken"),
        ]
        self.assertEqual(m.save_hits(out, results, existing), 2)
        # second save must not duplicate
        self.assertEqual(m.save_hits(out, results, existing | {"a", "b"}), 0)
        self.assertEqual(out.read_text().splitlines(), ["a", "b"])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.results = [
            m.CheckResult("tiktok", "taken", nickname="TikTok", verified=True,
                          followers=95200000),
            m.CheckResult("free_name", "available", double_checked=True, confirmed=True),
        ]

    def test_json_roundtrip(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            path = Path(f.name)
        m.export_json(path, self.results, "2026-08-07T10:00:00+03:00")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["results"][0]["followers"], 95200000)
        self.assertTrue(data["results"][1]["confirmed"])
        path.unlink()

    def test_csv_roundtrip(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            path = Path(f.name)
        m.export_csv(path, self.results)
        with path.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["username"], "tiktok")
        self.assertEqual(rows[0]["status"], "taken")
        self.assertEqual(rows[0]["followers"], "95200000")
        path.unlink()


class MiscTests(unittest.TestCase):
    def test_format_count(self):
        self.assertEqual(m.format_count(0), "0")
        self.assertEqual(m.format_count(1500), "1.5K")
        self.assertEqual(m.format_count(12000000), "12.0M")
        self.assertEqual(m.format_count(2500000000), "2.5B")

    def test_to_line_plain(self):
        r = m.CheckResult("tiktok", "taken", nickname="TikTok", verified=True,
                          followers=1000)
        line = r.to_line()
        self.assertIn("[TAKEN]", line)
        self.assertIn("https://www.tiktok.com/@tiktok", line)
        self.assertIn("1.0K followers", line)


if __name__ == "__main__":
    unittest.main()
