#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikTok Username Checker v2
==========================

Checks whether TikTok usernames are taken or still available.

How it works
------------
TikTok has no official public API for username availability, so this tool
reads the *same* data TikTok's own web app uses. Every public profile page
(https://www.tiktok.com/@username) embeds a JSON blob called
``__UNIVERSAL_DATA_FOR_REHYDRATION__``. Inside it:

    * username taken  ->  "webapp.user-detail" contains a "userInfo" object
                          (with nickname, verified flag, follower count, ...)
    * username free   ->  no "userInfo" and statusCode 10202 / 10221 (not found)

Checking the page's HTTP status code, the method this project used before,
stopped working long ago: TikTok returns 200 for every username, existing
or not.

Available usernames are double-checked with a second, independent request
(through another proxy when available) before being reported as free.

This is not affiliated with or endorsed by TikTok. Use it responsibly and
at your own risk. Mass checking may lead to rate limiting of your IP.
Educational purposes only.

Requirements
------------
* Python 3.8 or newer
* No third-party packages (standard library only)

Usage
-----
    python main.py                        # type usernames at a prompt (or check usernames.txt)
    python main.py coolname cool.name     # check names straight away
    python main.py -f usernames.txt --json results.json --csv results.csv
    python main.py --proxies proxies.txt --max-retries 3

Run ``python main.py --help`` for all options.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import Dict, List, Optional, Tuple

VERSION = "2.4.0"

HOME_URL = "https://www.tiktok.com"
DEFAULT_NAMES_FILE = Path("usernames.txt")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# statusCode values TikTok returns when a username does not exist
TIKTOK_NOT_FOUND_CODES = {10202, 10221, 10222, 10223, 10224}

# Usernames: 2-24 chars, letters / digits / . / _
USERNAME_CHARS_RE = re.compile(r"^[A-Za-z0-9._]+$")


def username_problem(name: str) -> Optional[str]:
    """Return why TikTok would reject this username, or None if it is valid."""
    if len(name) < 2:
        return "too short (min 2 characters)"
    if len(name) > 24:
        return "too long (max 24 characters)"
    if not USERNAME_CHARS_RE.fullmatch(name):
        return "only letters, numbers, periods and underscores are allowed"
    if name.endswith("."):
        return "can't end with a period"
    return None


def clean_text(value: object) -> str:
    """Text from TikTok pages, safe to print: control / escape characters become spaces."""
    return "".join(ch if ch.isprintable() else " " for ch in str(value))


def to_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def mask_proxy(proxy: str) -> str:
    """Hide the password part of a proxy address before printing it."""
    return re.sub(r"^((?:[a-z0-9+.-]+://)?)[^/]+@", r"\1***@", proxy, flags=re.I)


REHYDRATION_RE = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S
)

USE_COLOR = sys.stdout.isatty()


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


class Palette:
    GREEN = "32;1"
    RED = "31;1"
    YELLOW = "33;1"
    CYAN = "36;1"
    GRAY = "90"
    BOLD = "1"


BANNER = r"""
  _______ _ _ _______    _        _____  _                  _
 |__   __(_) |__   __|  | |      / ____|| |                | |
    | |   _| | _| | ___ | | __  | |     | |__    ___   ___ | | __  ___  _ __
    | |  | | |/ / |/ _ \| |/ /  | |     | '_ \  / _ \ / __|| |/ / / _ \| '__|
    | |  | |   <| | (_) |   <   | |____ | | | ||  __/| (__ |   < |  __/| |
    |_|  |_|_|\_\_|\___/|_|\_\   \_____||_| |_| \___| \___||_|\_\ \___||_|
"""


@dataclass
class CheckResult:
    username: str
    status: str  # "available" | "taken" | "blocked" | "error"
    nickname: str = ""
    verified: bool = False
    followers: int = 0
    signature: str = ""
    error: str = ""
    double_checked: bool = False
    confirmed: Optional[bool] = None

    @property
    def url(self) -> str:
        return f"{HOME_URL}/@{self.username}"

    def to_line(self) -> str:
        width = len("[AVAILABLE]")  # pad labels so the URLs line up
        if self.status == "available":
            label = paint("[AVAILABLE]", Palette.GREEN)
            extra = " | free right now"
            if self.double_checked and not self.confirmed:
                extra += paint(" (unconfirmed)", Palette.GRAY)
        elif self.status == "taken":
            label = paint("[TAKEN]".ljust(width), Palette.RED)
            extra = ""
            if self.nickname:
                extra = f" | {self.nickname}"
                if self.verified:
                    extra += paint(" ✓verified", Palette.CYAN)
                if self.followers:
                    extra += f" · {format_count(self.followers)} followers"
            if self.error == "corrected by double-check":
                extra += paint(" [corrected]", Palette.GRAY)
        elif self.status == "blocked":
            label = paint("[BLOCKED]".ljust(width), Palette.YELLOW)
            extra = f" | {self.error or 'rate limited / captcha'}"
        else:
            label = paint("[ERROR]".ljust(width), Palette.YELLOW)
            extra = f" | {self.error}"
        return f"{label} {self.url}{extra}"


def format_count(n: int) -> str:
    """Format 12_345_678 -> '12.3M'."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def count_statuses(results: List[CheckResult]) -> Dict[str, int]:
    stats = {"available": 0, "taken": 0, "blocked": 0, "error": 0}
    for result in results:
        stats[result.status] = stats.get(result.status, 0) + 1
    return stats


def _result_dict(r: CheckResult) -> Dict[str, object]:
    return {
        "username": r.username,
        "status": r.status,
        "url": r.url,
        "nickname": r.nickname,
        "verified": r.verified,
        "followers": r.followers,
        "signature": r.signature,
        "double_checked": r.double_checked,
        "confirmed": r.confirmed,
        "error": r.error,
    }


class ProxyPool:
    """Round-robin proxy pool that evicts dead proxies."""

    def __init__(self, proxies: List[str], max_failures: int = 3) -> None:
        self._proxies = list(dict.fromkeys(proxies))
        self._failures = {p: 0 for p in self._proxies}
        self._max_failures = max(1, max_failures)
        self._lock = Lock()
        self._index = 0
        self.removed: List[str] = []

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._proxies)

    def snapshot(self) -> List[str]:
        with self._lock:
            return list(self._proxies)

    def get(self) -> Optional[str]:
        with self._lock:
            if not self._proxies:
                return None
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
            return proxy

    def mark_success(self, proxy: str) -> None:
        with self._lock:
            if proxy in self._failures:
                self._failures[proxy] = 0

    def mark_failure(self, proxy: str, force: bool = False, verbose: bool = True) -> bool:
        """Record a failure; evict the proxy if needed. Returns True if evicted."""
        evicted = False
        with self._lock:
            if proxy not in self._failures:
                return False
            self._failures[proxy] += 1
            if force or self._failures[proxy] >= self._max_failures:
                self._proxies.remove(proxy)
                del self._failures[proxy]
                self.removed.append(proxy)
                evicted = True
        if evicted and verbose:
            print(paint(f"  [proxy evicted] {mask_proxy(proxy)}", Palette.YELLOW))
        return evicted


class UsernameChecker:
    """Checks username availability against TikTok's public profile pages."""

    def __init__(
        self,
        threads: int = 10,
        delay: float = 0.6,
        timeout: int = 15,
        max_retries: int = 3,
        proxies: Optional[List[str]] = None,
        proxy_max_failures: int = 3,
        double_check: bool = True,
        auto_slowdown: bool = True,
        verbose: bool = True,
    ) -> None:
        self.threads = max(1, threads)
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.double_check = double_check
        self.auto_slowdown = auto_slowdown
        self.verbose = verbose

        self._has_proxies = bool(proxies)
        self.proxy_max_failures = proxy_max_failures
        self._proxy_pool = ProxyPool(proxies or [], proxy_max_failures)
        self._slowdown = 1.0
        self._consecutive_ok = 0
        self._slowdown_lock = Lock()
        self.stop_event = Event()

        self.stats: Dict[str, int] = {
            "available": 0, "taken": 0, "blocked": 0, "error": 0,
        }
        self.elapsed = 0.0

    def _opener_for(self, proxy: Optional[str]) -> urllib.request.OpenerDirector:
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            return urllib.request.build_opener(handler)
        return urllib.request.build_opener()

    @staticmethod
    def _headers() -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",  # ~4x smaller pages (364 KB -> 83 KB)
            "Referer": HOME_URL + "/",
            "Connection": "keep-alive",
        }

    def _fetch_page(self, username: str, proxy: Optional[str]) -> Tuple[Optional[str], str]:
        """Return (html, error) where error is one of
        "ok" | "rate-limited" | "http" | "connection"."""
        req = urllib.request.Request(
            f"{HOME_URL}/@{username}", headers=self._headers(), method="GET"
        )
        try:
            opener = self._opener_for(proxy)
            with opener.open(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    return None, "http"
                data = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    data = gzip.decompress(data)
            return data.decode("utf-8", errors="replace"), "ok"
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                return None, "rate-limited"
            return None, "http"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return None, "connection"
        except (EOFError, zlib.error):  # truncated or corrupt compressed page
            return None, "connection"

    def _probe_proxy(self, proxy: str) -> bool:
        req = urllib.request.Request(HOME_URL + "/", headers=self._headers(), method="GET")
        try:
            opener = self._opener_for(proxy)
            with opener.open(req, timeout=min(self.timeout, 8)) as resp:
                return resp.status == 200
        except Exception:
            return False

    def probe_proxies(self) -> int:
        """Health-check every proxy once (concurrently); drop the dead ones."""
        pool = self._proxy_pool.snapshot()
        if not pool:
            return 0
        print(paint(f"Probing {len(pool)} proxy(ies)...", Palette.GRAY))
        workers = min(self.threads, len(pool))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            probes = {executor.submit(self._probe_proxy, p): p for p in pool}
            for future in as_completed(probes):
                if not future.result():
                    self._proxy_pool.mark_failure(probes[future], force=True, verbose=False)
        if self._proxy_pool.removed:
            print(
                paint(
                    f"Removed dead proxy(ies): {', '.join(map(mask_proxy, self._proxy_pool.removed))}",
                    Palette.YELLOW,
                )
            )
        return len(self._proxy_pool.removed)

    @staticmethod
    def _parse_page(html: str) -> Tuple[str, str, bool, int, str]:
        """Parse the rehydration JSON.

        Returns (status, nickname, verified, followers, signature) where
        status is one of "taken", "available", "blocked".
        """
        match = REHYDRATION_RE.search(html)
        if not match:
            return "blocked", "", False, 0, "no rehydration data"

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return "blocked", "", False, 0, "bad rehydration JSON"

        default_scope = data.get("__DEFAULT_SCOPE__")
        if not isinstance(default_scope, dict):
            return "blocked", "", False, 0, "unexpected page structure"
        user_detail = default_scope.get("webapp.user-detail", {})
        if not isinstance(user_detail, dict):
            return "blocked", "", False, 0, "missing user-detail scope"

        user_info = user_detail.get("userInfo")
        if isinstance(user_info, dict) and isinstance(user_info.get("user"), dict):
            user = user_info["user"]
            # follower/video counts live at userInfo.stats, not user.stats
            stats = user_info.get("stats") or user.get("stats") or {}
            if not isinstance(stats, dict):
                stats = {}
            return (
                "taken",
                clean_text(user.get("nickname", "")),
                bool(user.get("verified")),
                to_int(stats.get("followerCount", 0)),
                clean_text(user.get("signature", "")),
            )

        status_code = user_detail.get("statusCode")
        if status_code in TIKTOK_NOT_FOUND_CODES:
            return "available", "", False, 0, ""

        # Login wall / generic page without a clear verdict
        return "blocked", "", False, 0, f"unexpected response (statusCode={clean_text(status_code)})"

    def _pace(self) -> None:
        if not self.delay:
            return
        with self._slowdown_lock:
            slowdown = self._slowdown
        time.sleep(self.delay * slowdown * random.uniform(0.5, 1.5))

    def _report_outcome(self, status: str) -> None:
        if not self.auto_slowdown:
            return
        with self._slowdown_lock:
            if status == "blocked":
                self._consecutive_ok = 0
                self._slowdown = min(self._slowdown * 2, 8.0)
            else:
                self._consecutive_ok += 1
                if self._consecutive_ok >= 10 and self._slowdown > 1.0:
                    self._slowdown = max(self._slowdown / 2, 1.0)
                    self._consecutive_ok = 0

    def _double_check(self, username: str) -> CheckResult:
        """Second, independent fetch to confirm an 'available' verdict."""
        if self._has_proxies and not self._proxy_pool.snapshot():
            return CheckResult(username, "available", double_checked=True, confirmed=False)
        proxy = self._proxy_pool.get()
        html, err = self._fetch_page(username, proxy)
        if html is None:
            if err == "connection" and proxy:
                self._proxy_pool.mark_failure(proxy, verbose=self.verbose)
            self._report_outcome("available")
            return CheckResult(username, "available", double_checked=True, confirmed=False)
        if proxy:
            self._proxy_pool.mark_success(proxy)

        status, nickname, verified, followers, signature = self._parse_page(html)
        if status == "taken":
            # first verdict was wrong (edge case), so trust the second one
            self._report_outcome("taken")
            return CheckResult(
                username, "taken", nickname, verified, followers, signature,
                double_checked=True, confirmed=False, error="corrected by double-check",
            )
        if status == "available":
            self._report_outcome("available")
            return CheckResult(username, "available", double_checked=True, confirmed=True)
        # second check inconclusive: keep the first verdict, flagged as unconfirmed
        self._report_outcome("available")
        return CheckResult(username, "available", double_checked=True, confirmed=False)

    def check(self, username: str) -> CheckResult:
        """Check a single username with retries, backoff and double-check."""
        if self._has_proxies and not self._proxy_pool.snapshot():
            return CheckResult(username, "error", error="all proxies failed")
        self._pace()

        base_backoff = 1.0
        last_error = ""

        try:
            for attempt in range(self.max_retries + 1):
                if self.stop_event.is_set():
                    return CheckResult(username, "error", error="interrupted")

                proxy = self._proxy_pool.get()
                html, err = self._fetch_page(username, proxy)

                if html is not None:
                    if proxy:
                        self._proxy_pool.mark_success(proxy)
                    status, nickname, verified, followers, signature = self._parse_page(html)
                    if status in ("taken", "available"):
                        if status == "available" and self.double_check:
                            return self._double_check(username)
                        self._report_outcome(status)
                        return CheckResult(
                            username, status, nickname, verified, followers, signature
                        )
                    last_error = signature  # "blocked" -> retryable
                else:
                    if err == "connection" and proxy:
                        self._proxy_pool.mark_failure(proxy, verbose=self.verbose)
                    last_error = "network failure" if err == "connection" else "rate limited"

                if self.verbose and attempt < self.max_retries:
                    print(
                        f"  {paint('[retry]', Palette.GRAY)} {username} "
                        f"({last_error}), attempt {attempt + 2}/{self.max_retries + 1}"
                    )

                if attempt < self.max_retries:
                    time.sleep(min(base_backoff * (2 ** attempt), 10.0))
        except Exception as exc:  # a failed check must never kill the whole run
            return CheckResult(username, "error", error=f"unexpected error: {exc}")

        self._report_outcome("blocked")
        return CheckResult(username, "blocked", error=last_error or "rate limited")

    @staticmethod
    def _print_progress(result: CheckResult, done: int, total: int) -> None:
        if total == 1:
            print(result.to_line())
            return
        counter = f"{done:>{len(str(total))}}/{total}"
        print(f"{paint(counter, Palette.GRAY)} {result.to_line()}")

    def run(self, usernames: List[str], announce: bool = True, show_each: bool = True) -> List[CheckResult]:
        """Check a list of usernames concurrently, printing as they finish
        (announce = print the "Checking N..." header, show_each = print every result)."""
        start = time.monotonic()
        results: List[CheckResult] = []
        if self.verbose and announce:
            print(
                paint(
                    f"Checking {len(usernames)} username(s) "
                    f"({self.threads} thread(s), {self.delay}s delay, "
                    f"max {self.max_retries} retries)...",
                    Palette.BOLD,
                )
            )
            if self._has_proxies:
                print(
                    paint(
                        f"Using {self._proxy_pool.count} proxy(ies) (round-robin, "
                        f"auto-eviction on).",
                        Palette.GRAY,
                    )
                )

        futures: Dict = {}
        collected: set = set()
        try:
            with ThreadPoolExecutor(max_workers=self.threads) as pool:
                futures = {pool.submit(self.check, u): u for u in usernames}
                try:
                    for future in as_completed(futures):
                        collected.add(future)
                        result = future.result()
                        results.append(result)
                        if self.verbose and show_each:
                            self._print_progress(result, len(results), len(usernames))
                except KeyboardInterrupt:
                    print("\n" + paint("Interrupted. Finishing and saving results...", Palette.YELLOW))
                    self.stop_event.set()
                    for future in futures:
                        future.cancel()
                # exiting the `with` waits for in-flight checks to finish
            for future in futures:
                if future not in collected and not future.cancelled() and future.done():
                    result = future.result()
                    results.append(result)
                    if self.verbose and show_each:
                        self._print_progress(result, len(results), len(usernames))
        except KeyboardInterrupt:
            # Ctrl+C pressed again while shutting down
            print("\n" + paint("Stopped.", Palette.YELLOW))
            self.stop_event.set()

        self.elapsed = time.monotonic() - start
        self.stats = count_statuses(results)
        return results

    def retry_blocked(self, results: List[CheckResult]) -> List[CheckResult]:
        """Second, slower pass over BLOCKED results to reduce unknowns."""
        blocked = [r.username for r in results if r.status == "blocked"]
        if not blocked:
            return results
        if self.verbose:
            print(
                paint(
                    f"Retrying {len(blocked)} blocked username(s) "
                    f"with slower settings...",
                    Palette.YELLOW,
                )
            )
        retry_checker = UsernameChecker(
            threads=max(1, self.threads // 2),
            delay=max(self.delay * 3, 1.0),
            timeout=self.timeout,
            max_retries=self.max_retries + 2,
            proxies=self._proxy_pool.snapshot() or None,
            proxy_max_failures=self.proxy_max_failures,
            double_check=self.double_check,
            auto_slowdown=self.auto_slowdown,
            verbose=self.verbose,
        )
        fresh = {r.username: r for r in retry_checker.run(blocked)}
        self.elapsed += retry_checker.elapsed
        merged = [fresh.get(r.username, r) for r in results]
        self.stats = count_statuses(merged)
        return merged


def clean_names(raw_names: List[str]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Normalise and validate names. Returns (valid, [(invalid, reason), ...]).

    TikTok usernames are case-insensitive, so duplicates are dropped ignoring case.
    """
    seen: set = set()
    valid: List[str] = []
    invalid: List[Tuple[str, str]] = []
    for raw in raw_names:
        username = raw.strip().lstrip("@").strip()
        if not username or username.lower() in seen:
            continue
        seen.add(username.lower())
        problem = username_problem(username)
        if problem:
            invalid.append((username, problem))
        else:
            valid.append(username)
    return valid, invalid


def read_names_file(path: Path) -> Optional[List[str]]:
    """Return the non-comment lines of a usernames file, or None if it doesn't exist."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        sys.exit(f"Error: can't read {path} ({exc})")
    return [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def warn_invalid(invalid: List[Tuple[str, str]]) -> None:
    for name, reason in invalid[:10]:
        print(paint(f"Skipped '{name}': {reason}", Palette.YELLOW))
    if len(invalid) > 10:
        print(paint(f"...and {len(invalid) - 10} more invalid username(s)", Palette.YELLOW))


def load_existing_hits(path: Path) -> set:
    """Return usernames already saved to the output file (to avoid duplicates)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    except (OSError, UnicodeDecodeError) as exc:
        sys.exit(f"Error: can't read {path} ({exc})")
    return {ln.strip().lstrip("@").strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def save_hits(path: Path, results: List[CheckResult], existing: set) -> int:
    """Append newly found available usernames (and add them to `existing`).
    Returns number written."""
    new = [r.username for r in results if r.status == "available" and r.username not in existing]
    if not new:
        return 0
    with path.open("a", encoding="utf-8") as f:
        for username in new:
            f.write(f"{username}\n")
    existing.update(new)
    return len(new)


def export_json(path: Path, results: List[CheckResult], checked_at: str) -> None:
    payload = {
        "checked_at": checked_at,
        "total": len(results),
        "results": [_result_dict(r) for r in results],
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def export_csv(path: Path, results: List[CheckResult]) -> None:
    fieldnames = [
        "username", "status", "url", "nickname", "verified", "followers",
        "signature", "double_checked", "confirmed", "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            row = _result_dict(r)
            for key, value in row.items():
                if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
                    row[key] = "'" + value  # stops Excel / Sheets from running it as a formula
            writer.writerow(row)


_REPORT_STATUSES = (
    ("available", "Available", "✓"),
    ("taken", "Taken", "✕"),
    ("blocked", "Blocked", "!"),
    ("error", "Error", "?"),
)

_REPORT_CSS = """
:root {
  --color-paper: oklch(97.5% 0.006 255);
  --color-surface: oklch(99% 0.004 255);
  --color-ink: oklch(21% 0.02 255);
  --color-muted: oklch(44% 0.02 255);
  --color-rule: oklch(89% 0.01 255);
  --color-accent: oklch(50% 0.17 258);
  --color-available: oklch(44% 0.13 155);
  --color-available-bg: oklch(94% 0.045 155);
  --color-taken: oklch(50% 0.17 25);
  --color-taken-bg: oklch(94% 0.035 25);
  --color-blocked: oklch(46% 0.1 70);
  --color-blocked-bg: oklch(94% 0.05 80);
  --color-error: oklch(46% 0.02 255);
  --color-error-bg: oklch(93% 0.01 255);
  --font-sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
  --space-xs: 0.25rem;
  --space-sm: 0.5rem;
  --space-md: 1rem;
  --space-lg: 1.5rem;
  --space-xl: 2.5rem;
  --radius: 0.5rem;
  --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --color-paper: oklch(16% 0.012 255);
    --color-surface: oklch(20% 0.014 255);
    --color-ink: oklch(94% 0.008 255);
    --color-muted: oklch(72% 0.015 255);
    --color-rule: oklch(30% 0.014 255);
    --color-accent: oklch(74% 0.13 258);
    --color-available: oklch(78% 0.14 155);
    --color-available-bg: oklch(26% 0.05 155);
    --color-taken: oklch(75% 0.13 25);
    --color-taken-bg: oklch(27% 0.06 25);
    --color-blocked: oklch(79% 0.12 80);
    --color-blocked-bg: oklch(28% 0.05 80);
    --color-error: oklch(74% 0.02 255);
    --color-error-bg: oklch(27% 0.014 255);
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { overflow-x: clip; }
body {
  margin: 0;
  padding: var(--space-xl) max(var(--space-md), env(safe-area-inset-left));
  background: var(--color-paper);
  color: var(--color-ink);
  font: 400 0.9375rem/1.5 var(--font-sans);
}
.wrap { max-width: 60rem; margin: 0 auto; min-width: 0; }
h1 {
  margin: 0 0 var(--space-xs);
  font-size: clamp(1.75rem, 1rem + 3vw, 2.5rem);
  line-height: 1.1;
  letter-spacing: -0.03em;
  font-weight: 650;
  overflow-wrap: anywhere;
}
.sub { margin: 0 0 var(--space-lg); color: var(--color-muted); }
.meter {
  display: flex;
  height: 0.5rem;
  margin-bottom: var(--space-md);
  border-radius: 999px;
  overflow: hidden;
  background: var(--color-rule);
}
.seg { height: 100%; }
.seg.s-available { background: var(--color-available); }
.seg.s-taken { background: var(--color-taken); }
.seg.s-blocked { background: var(--color-blocked); }
.seg.s-error { background: var(--color-error); }
.chips { display: flex; flex-wrap: wrap; gap: var(--space-sm); margin-bottom: var(--space-md); }
.chip, .btn, .copy {
  font: inherit;
  white-space: nowrap;
  cursor: pointer;
  border-radius: var(--radius);
  border: 1px solid var(--color-rule);
  background: var(--color-surface);
  color: var(--color-ink);
  transition: background-color 120ms var(--ease-out), border-color 120ms var(--ease-out);
}
.chip { display: inline-flex; align-items: center; gap: var(--space-sm); padding: 0.4rem 0.75rem; }
.chip b { font-variant-numeric: tabular-nums; font-weight: 650; }
.chip .glyph { font-weight: 700; }
.chip[aria-pressed="true"] { border-color: var(--color-accent); box-shadow: inset 0 0 0 1px var(--color-accent); }
.chip.s-available .glyph { color: var(--color-available); }
.chip.s-taken .glyph { color: var(--color-taken); }
.chip.s-blocked .glyph { color: var(--color-blocked); }
.chip.s-error .glyph { color: var(--color-error); }
.btn { padding: 0.5rem 0.9rem; font-weight: 600; }
.btn-primary { background: var(--color-accent); border-color: var(--color-accent); color: var(--color-paper); }
.btn:disabled { cursor: not-allowed; opacity: 0.5; }
.copy { padding: 0.15rem 0.6rem; font-size: 0.8125rem; color: var(--color-muted); }
@media (hover: hover) {
  .chip:hover, .copy:hover { border-color: var(--color-accent); }
  .btn:not(:disabled):hover { filter: brightness(1.08); }
}
.chip:active, .btn:active, .copy:active { transform: translateY(1px); }
.chip:focus-visible, .btn:focus-visible, .copy:focus-visible, input:focus-visible, select:focus-visible, a:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 2px;
}
.tools { display: grid; gap: var(--space-sm); margin-bottom: var(--space-md); }
input[type="search"], select {
  width: 100%;
  min-width: 0;
  padding: 0.5rem 0.75rem;
  font: inherit;
  color: var(--color-ink);
  background: var(--color-surface);
  border: 1px solid var(--color-rule);
  border-radius: var(--radius);
}
.count { margin: 0 0 var(--space-sm); color: var(--color-muted); font-size: 0.8125rem; }
.sr-only {
  position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
.list { background: var(--color-surface); border: 1px solid var(--color-rule); border-radius: var(--radius); overflow: hidden; }
table, tbody, tr, td { display: block; }
tr { padding: var(--space-md); border-top: 1px solid var(--color-rule); }
tbody tr:first-child { border-top: 0; }
td { padding: 0.125rem 0; min-width: 0; overflow-wrap: anywhere; }
td[data-label]::before {
  content: attr(data-label);
  display: block;
  color: var(--color-muted);
  font-size: 0.75rem;
}
.user { display: inline-flex; align-items: center; gap: var(--space-sm); flex-wrap: wrap; }
.user a { font-family: var(--font-mono); font-weight: 600; color: var(--color-ink); text-decoration-color: var(--color-rule); text-underline-offset: 3px; }
.user a:hover { color: var(--color-accent); text-decoration-color: var(--color-accent); }
.badge {
  display: inline-flex; align-items: center; gap: var(--space-xs);
  padding: 0.1rem 0.6rem; border-radius: 999px;
  font-size: 0.8125rem; font-weight: 600; white-space: nowrap;
}
.badge.s-available { color: var(--color-available); background: var(--color-available-bg); }
.badge.s-taken { color: var(--color-taken); background: var(--color-taken-bg); }
.badge.s-blocked { color: var(--color-blocked); background: var(--color-blocked-bg); }
.badge.s-error { color: var(--color-error); background: var(--color-error-bg); }
.num { font-variant-numeric: tabular-nums; }
.dim { color: var(--color-muted); }
.empty { margin: 0; padding: var(--space-lg) var(--space-md); color: var(--color-muted); text-align: center; }
.retry { margin-top: var(--space-xl); }
.retry h2 { margin: 0 0 var(--space-xs); font-size: 1.125rem; letter-spacing: -0.01em; }
.retry p { margin: 0 0 var(--space-sm); color: var(--color-muted); }
.retry code {
  display: block; margin-bottom: var(--space-sm); padding: var(--space-sm) 0.75rem;
  font: 0.8125rem/1.6 var(--font-mono);
  background: var(--color-surface); border: 1px solid var(--color-rule); border-radius: var(--radius);
  overflow-wrap: anywhere;
}
@media (min-width: 40rem) {
  .tools { grid-template-columns: minmax(0, 1fr) 14rem auto; align-items: center; }
  table { display: table; width: 100%; border-collapse: collapse; }
  thead { display: table-header-group; }
  tbody { display: table-row-group; }
  tr { display: table-row; padding: 0; }
  th, td { display: table-cell; padding: 0.7rem var(--space-md); text-align: left; vertical-align: middle; }
  td[data-label]::before { display: none; }
  th { font-size: 0.8125rem; font-weight: 600; color: var(--color-muted); border-bottom: 1px solid var(--color-rule); }
  tbody tr { border-top: 1px solid var(--color-rule); }
  tbody tr:first-child { border-top: 0; }
  .num-col { text-align: right; }
  td.num-col { text-align: right; }
  .sr-only-th { position: static; width: auto; height: auto; margin: 0; clip: auto; overflow: visible; }
}
@media (pointer: coarse) {
  .chip, .btn, .copy, input[type="search"], select { min-height: 2.75rem; }
}
@media (prefers-reduced-motion: reduce) {
  * { transition-duration: 0.01ms !important; }
}
"""

_REPORT_JS = r"""
(() => {
  const body = document.querySelector("tbody");
  const rows = Array.from(body.rows);
  const chips = document.querySelectorAll("[data-filter]");
  const search = document.getElementById("q");
  const sort = document.getElementById("sort");
  const count = document.getElementById("count");
  const empty = document.getElementById("empty");
  const order = { available: 0, taken: 1, blocked: 2, error: 3 };
  const sorters = {
    index: (a, b) => a.dataset.i - b.dataset.i,
    name: (a, b) => a.dataset.name.localeCompare(b.dataset.name),
    followers: (a, b) => b.dataset.followers - a.dataset.followers || a.dataset.i - b.dataset.i,
    status: (a, b) => order[a.dataset.status] - order[b.dataset.status] || a.dataset.i - b.dataset.i,
  };
  let filter = "all";

  function apply() {
    const q = search.value.trim().toLowerCase();
    let shown = 0;
    for (const row of rows) {
      const show = (filter === "all" || row.dataset.status === filter) && (!q || row.dataset.search.includes(q));
      row.hidden = !show;
      if (show) shown++;
    }
    empty.hidden = shown > 0;
    count.textContent = shown + " of " + rows.length + " shown";
  }

  chips.forEach((chip) => chip.addEventListener("click", () => {
    filter = chip.dataset.filter;
    chips.forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
    apply();
  }));
  search.addEventListener("input", apply);
  sort.addEventListener("change", () => body.append(...rows.slice().sort(sorters[sort.value])));

  function fallbackCopy(text) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      return fallbackCopy(text);
    }
  }

  document.querySelectorAll("[data-copy], [data-copy-available]").forEach((btn) => {
    const label = btn.textContent;
    btn.addEventListener("click", async () => {
      const text = btn.hasAttribute("data-copy-available")
        ? rows.filter((r) => r.dataset.status === "available").map((r) => r.dataset.name).join("\n")
        : btn.dataset.copy;
      btn.textContent = (await copyText(text)) ? "Copied" : "Copy failed";
      setTimeout(() => { btn.textContent = label; }, 1500);
    });
  });

  apply();
})();
"""


def export_html(path: Path, results: List[CheckResult], checked_at: str) -> None:
    """Write a self-contained HTML report: filter, search, sort and copy built in."""
    total = len(results)
    stats = count_statuses(results)

    meter = "".join(
        f'<span class="seg s-{key}" style="width:{100 * stats[key] / total:.2f}%"></span>'
        for key, _, _ in _REPORT_STATUSES
        if stats[key]
    )
    meter_label = ", ".join(f"{stats[key]} {label.lower()}" for key, label, _ in _REPORT_STATUSES)

    chips = [
        f'<button type="button" class="chip" data-filter="all" aria-pressed="true">All <b>{total}</b></button>'
    ]
    for key, label, glyph in _REPORT_STATUSES:
        chips.append(
            f'<button type="button" class="chip s-{key}" data-filter="{key}" aria-pressed="false">'
            f'<span class="glyph" aria-hidden="true">{glyph}</span>{label} <b>{stats[key]}</b></button>'
        )

    rows = []
    for i, r in enumerate(results):
        label, glyph = next((l, g) for k, l, g in _REPORT_STATUSES if k == r.status) if r.status in stats else (r.status, "?")
        details = []
        if r.nickname:
            details.append(html.escape(r.nickname) + (" · verified" if r.verified else ""))
        if r.double_checked:
            details.append("double-checked" if r.confirmed else "double-check unconfirmed")
        if r.error:
            details.append(html.escape(r.error))
        copy_btn = (
            f'<button type="button" class="copy" data-copy="{html.escape(r.username)}" '
            f'aria-label="Copy {html.escape(r.username)}">Copy</button>'
            if r.status == "available" else ""
        )
        followers = format_count(r.followers) if r.followers else "—"
        search_text = html.escape(f"{r.username} {r.nickname}".lower())
        rows.append(
            f'<tr data-i="{i}" data-status="{html.escape(r.status)}" data-name="{html.escape(r.username)}" '
            f'data-followers="{r.followers}" data-search="{search_text}">'
            f'<td data-label="Username"><span class="user"><a href="{html.escape(r.url)}" target="_blank" rel="noopener">@{html.escape(r.username)}</a>{copy_btn}</span></td>'
            f'<td data-label="Status"><span class="badge s-{html.escape(r.status)}"><span aria-hidden="true">{glyph}</span>{label}</span></td>'
            f'<td class="num num-col" data-label="Followers">{followers}</td>'
            f'<td class="dim" data-label="Details">{" · ".join(details) if details else "—"}</td>'
            f"</tr>"
        )

    retry_names = [r.username for r in results if r.status in ("blocked", "error")]
    retry = ""
    if retry_names:
        command = "python main.py " + " ".join(retry_names)
        retry = f"""
  <section class="retry" aria-labelledby="retry-h">
    <h2 id="retry-h">Retry these {len(retry_names)}</h2>
    <p>TikTok blocked or failed these names. Run them again, ideally with a higher delay or proxies.</p>
    <code>{html.escape(command)}</code>
    <button type="button" class="btn" data-copy="{html.escape(command)}">Copy command</button>
  </section>"""

    noun = "username" if total == 1 else "usernames"
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>TikTok Username Checker: Report</title>
<style>
/* Hallmark · genre: modern-minimal · macrostructure: Long Document (report) · theme: custom cool-paper / cobalt accent
 * nav: none · footer: none · enrichment: none (typography only)
 * pre-emit critique: P4 H4 E4 S4 R5 V3 */
{_REPORT_CSS}
</style>
</head>
<body>
<div class="wrap">
  <h1>Username report</h1>
  <p class="sub">{total} {noun} checked · {html.escape(checked_at)}</p>
  <div class="meter" role="img" aria-label="{html.escape(meter_label)}">{meter}</div>
  <div class="chips" role="group" aria-label="Filter by status">
    {"".join(chips)}
  </div>
  <div class="tools">
    <label class="sr-only" for="q">Search usernames</label>
    <input type="search" id="q" placeholder="Search usernames" autocomplete="off">
    <label class="sr-only" for="sort">Sort by</label>
    <select id="sort">
      <option value="index">Original order</option>
      <option value="name">Username A–Z</option>
      <option value="followers">Followers, high to low</option>
      <option value="status">Status</option>
    </select>
    <button type="button" class="btn btn-primary" data-copy-available{"" if stats["available"] else " disabled"}>Copy available ({stats["available"]})</button>
  </div>
  <p class="count" id="count" aria-live="polite"></p>
  <div class="list">
    <table>
      <thead><tr class="sr-only sr-only-th"><th scope="col">Username</th><th scope="col">Status</th><th scope="col" class="num-col">Followers</th><th scope="col">Details</th></tr></thead>
      <tbody>
      {"".join(rows)}
      </tbody>
    </table>
    <p class="empty" id="empty" hidden>No usernames match.</p>
  </div>{retry}
</div>
<script>
{_REPORT_JS}
</script>
</body>
</html>
"""
    with path.open("w", encoding="utf-8") as f:
        f.write(doc)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="TikTok username availability checker (standard library only).",
        epilog="Examples:\n"
        "  python3 main.py                      # type usernames and check them live\n"
        "  python3 main.py coolname cool.name   # check names straight away\n"
        "  python3 main.py -f usernames.txt --html report.html\n"
        "  python3 main.py --proxies proxies.txt --max-retries 5\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("usernames", nargs="*",
                        help="usernames to check (skips the file and the prompt)")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="type usernames one after another, even if usernames.txt has names")
    parser.add_argument("--suggest", action="store_true",
                        help="when a name is taken, check ~10 similar names and show the free ones (off by default)")
    parser.add_argument("--watch", action="store_true",
                        help="keep re-checking taken names and alert you when one becomes free (off by default)")
    parser.add_argument("--every", type=float, default=30.0, metavar="MINUTES",
                        help="with --watch: minutes between checks (default: 30, minimum: 1)")
    parser.add_argument("-f", "--file", type=Path, default=None,
                        help=f"file with usernames, one per line (default: {DEFAULT_NAMES_FILE})")
    parser.add_argument("-o", "--output", type=Path, default=Path("hits.txt"),
                        help="file to save available usernames to (default: hits.txt)")
    parser.add_argument("--json", type=Path, default=None, dest="json_out",
                        help="export all results to a JSON file")
    parser.add_argument("--csv", type=Path, default=None, dest="csv_out",
                        help="export all results to a CSV file")
    parser.add_argument("--html", type=Path, default=None, dest="html_out",
                        help="export a styled HTML report of all results")
    parser.add_argument("-t", "--threads", type=int, default=10,
                        help="number of concurrent workers (default: 10)")
    parser.add_argument("-d", "--delay", type=float, default=0.6,
                        help="seconds to wait between requests, 0 to disable (default: 0.6)")
    parser.add_argument("--timeout", type=int, default=15,
                        help="per-request timeout in seconds (default: 15)")
    parser.add_argument("-r", "--max-retries", type=int, default=3,
                        help="retries per username on rate-limit/block (default: 3)")
    parser.add_argument("-p", "--proxies", type=Path, default=None,
                        help="file with HTTP(S) proxies, one per line (round-robin)")
    parser.add_argument("--proxy-max-failures", type=int, default=3,
                        help="consecutive failures before a proxy is evicted (default: 3)")
    parser.add_argument("--no-proxy-check", action="store_true",
                        help="skip the startup proxy health check")
    parser.add_argument("--no-double-check", action="store_true",
                        help="disable the second verification of available usernames")
    parser.add_argument("--no-auto-slowdown", action="store_true",
                        help="disable the slowdown on repeated blocks")
    parser.add_argument("--retry-blocked", action="store_true",
                        help="after the run, re-check blocked usernames with slower settings")
    parser.add_argument("--no-color", action="store_true",
                        help="disable coloured output")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="suppress per-username output (summary only)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(argv)


def split_names(text: str) -> List[str]:
    """Split user input like 'name1, name2 @name3' into separate names."""
    return [part for part in re.split(r"[\s,]+", text) if part]


SUGGEST_PREFIXES = ["its.", "the", "im.", "real", "hey."]
SUGGEST_SUFFIXES = [".tv", "_", "hq", "official", ".x", "tok", "_xo", ".fyp"]
SUGGEST_LIMIT = 10


def make_suggestions(name: str) -> List[str]:
    """Similar usernames to try when `name` is taken (valid ones only, max SUGGEST_LIMIT)."""
    base = name.lower()
    candidates = []
    # alternate suffix/prefix so a short limit still gives a mix of both
    for i in range(max(len(SUGGEST_PREFIXES), len(SUGGEST_SUFFIXES))):
        if i < len(SUGGEST_SUFFIXES):
            candidates.append(base + SUGGEST_SUFFIXES[i])
        if i < len(SUGGEST_PREFIXES):
            candidates.append(SUGGEST_PREFIXES[i] + base)
    valid, _ = clean_names(candidates)
    return [c for c in valid if c != base][:SUGGEST_LIMIT]


def suggest_for_taken(checker: UsernameChecker, batch: List[CheckResult]) -> List[CheckResult]:
    """For every taken name in `batch`, check similar names and print the free ones."""
    found: List[CheckResult] = []
    for result in batch:
        if result.status != "taken" or checker.stop_event.is_set():
            continue
        options = make_suggestions(result.username)
        if not options:
            continue
        print(paint(f"  Checking {len(options)} names similar to @{result.username}...", Palette.GRAY))
        checked = checker.run(options, announce=False, show_each=False)
        free = [r.username for r in checked if r.status == "available"]
        if free:
            print(f"  {paint('Free:', Palette.GREEN)} {', '.join(free)}")
        else:
            print(paint("  No free similar names found.", Palette.GRAY))
        found.extend(checked)
    return found


def interactive_session(checker: UsernameChecker, output: Path, hits: set, suggest: bool) -> Tuple[List[CheckResult], float]:
    """Prompt for usernames until the user quits. Hits are saved after every check."""
    try:
        import readline  # noqa: F401  (arrow keys + history in input() where available)
    except ImportError:
        pass

    print(paint("Type a username and press Enter. Several at once work too: name1 name2, name3", Palette.GRAY))
    print(paint("Free ones are saved to " + str(output) + ". Type q (or press Ctrl+D) to finish.", Palette.GRAY))
    print(paint(f"Name suggestions are {'on' if suggest else 'off'}. Type 'suggest on' or 'suggest off' to switch.", Palette.GRAY))
    print()

    results: List[CheckResult] = []
    elapsed = 0.0
    while True:
        try:
            line = input(paint("username › ", Palette.CYAN))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        command = " ".join(line.lower().split())
        if command in ("q", "quit", "exit"):
            break
        if command in ("suggest on", "suggest off"):
            suggest = command == "suggest on"
            print(paint(f"Name suggestions {'on' if suggest else 'off'}.", Palette.GRAY))
            print()
            continue
        valid, invalid = clean_names(split_names(line))
        warn_invalid(invalid)
        if not valid:
            continue
        checker.stop_event.clear()  # a Ctrl+C during the last check must not stop this one
        batch = checker.run(valid, announce=False)
        elapsed += checker.elapsed
        if suggest:
            start = time.monotonic()
            batch += suggest_for_taken(checker, batch)
            elapsed += time.monotonic() - start
        save_hits(output, batch, hits)
        results.extend(batch)
        print()
    return results, elapsed


def watch_names(checker: UsernameChecker, names: List[str], every_minutes: float,
                output: Path, hits: set) -> None:
    """Re-check `names` every `every_minutes` until each one is free (or Ctrl+C)."""
    remaining = list(names)
    print(paint(f"Watching {len(remaining)} name(s), checking every {every_minutes:g} minute(s). "
                "Leave this window open; press Ctrl+C to stop.", Palette.BOLD))
    round_no = 0
    while remaining:
        round_no += 1
        print()
        print(paint(f"[{datetime.now():%H:%M:%S}] Check #{round_no}: {len(remaining)} name(s)", Palette.GRAY))
        checker.stop_event.clear()
        batch = checker.run(remaining, announce=False)
        if checker.stop_event.is_set():  # Ctrl+C during the check
            break
        freed = [r for r in batch if r.status == "available"]
        for result in freed:
            remaining.remove(result.username)
            # \a rings the terminal bell so you notice even if the window is in the background
            print("\a" + paint(f">>> @{result.username} is FREE now! Grab it: {result.url}", Palette.GREEN))
        save_hits(output, freed, hits)
        if not remaining:
            break
        next_at = datetime.now().timestamp() + every_minutes * 60
        print(paint(f"Next check at {datetime.fromtimestamp(next_at):%H:%M}.", Palette.GRAY))
        try:
            time.sleep(every_minutes * 60)
        except KeyboardInterrupt:
            print()
            break

    print()
    freed_count = len(names) - len(remaining)
    print(paint(f"Watch ended. Freed: {freed_count}. Still taken or unknown: {len(remaining)}"
                + (f" ({', '.join(remaining)})" if remaining else ""), Palette.BOLD))
    if freed_count:
        print(paint(f"Free names were saved to {output}.", Palette.GRAY))


def print_summary(args: argparse.Namespace, results: List[CheckResult], elapsed: float, written: int, known: int) -> None:
    stats = count_statuses(results)
    total = len(results)
    speed = total / elapsed * 60 if elapsed > 0 else 0.0

    def row(label: str, value: str, color: str = Palette.BOLD) -> None:
        print(f"  {paint(label.ljust(13), color)} : {value}")

    def count(key: str) -> str:
        return f"{stats[key]} ({100 * stats[key] / total:.0f}%)" if total else "0"

    print()
    print(paint("=" * 60, Palette.BOLD))
    print(paint("Summary", Palette.BOLD))
    row("Total checked", str(total))
    row("Time", f"{elapsed:.1f}s ({speed:.0f} usernames/min)")
    row("Available", count("available"), Palette.GREEN)
    row("Taken", count("taken"), Palette.RED)
    row("Blocked", count("blocked"), Palette.YELLOW)
    row("Errors", count("error"), Palette.YELLOW)
    row("Saved hits", f"{written} new in {args.output}" + (f" ({known} already there)" if known else ""))
    if args.json_out:
        row("JSON export", str(args.json_out))
    if args.csv_out:
        row("CSV export", str(args.csv_out))
    if args.html_out:
        row("HTML report", str(args.html_out))
    if stats["available"]:
        print(paint("  Note: available = no account uses it right now. TikTok can still refuse\n"
                    "  a name that belonged to a banned account or is reserved.", Palette.GRAY))
    if stats["blocked"] or stats["error"]:
        print(paint("  Tip: if you see many BLOCKED, raise --delay and lower --threads, "
                    "or add --proxies. TikTok rate-limits quickly.", Palette.GRAY))
    print(paint("=" * 60, Palette.BOLD))


def main(argv: Optional[List[str]] = None) -> None:
    global USE_COLOR
    args = parse_args(argv)

    if os.name == "nt":
        os.system("")  # enable ANSI colour support in the Windows console
    for stream in (sys.stdout, sys.stderr):
        try:  # e.g. an old Windows console can't print ✓ or emoji in nicknames
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.no_color or not sys.stdout.isatty():
        USE_COLOR = False

    if not args.quiet:
        print(paint(BANNER, Palette.CYAN))
        print(paint(f"TikTok Username Checker v{VERSION}: checks availability via "
                    "profile page data", Palette.BOLD))
        print()

    proxies: List[str] = []
    if args.proxies:
        try:
            proxies = [
                ln.strip() for ln in args.proxies.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")
            ]
        except FileNotFoundError:
            print(paint(f"Error: proxies file {args.proxies} not found!", Palette.RED))
            sys.exit(1)
        except (OSError, UnicodeDecodeError) as exc:
            print(paint(f"Error: can't read proxies file {args.proxies} ({exc})", Palette.RED))
            sys.exit(1)
        if not proxies:
            print(paint(f"Warning: {args.proxies} is empty, running without proxies.", Palette.YELLOW))

    # Where the names come from: command line > -i prompt > file > prompt (if nothing in the file)
    raw_names: List[str] = []
    if args.usernames:
        raw_names = [n for arg in args.usernames for n in split_names(arg)]
    elif not args.interactive:
        names_file = args.file or DEFAULT_NAMES_FILE
        file_names = read_names_file(names_file)
        if file_names is None and args.file:
            print(paint(f"Error: {args.file} not found!", Palette.RED))
            sys.exit(1)
        raw_names = file_names or []

    if args.watch and not raw_names:
        print(paint("--watch needs names: python3 main.py --watch name1 name2 "
                    "(or put them in usernames.txt).", Palette.RED))
        sys.exit(1)

    interactive = not raw_names
    if interactive and not sys.stdin.isatty():
        print(paint("No usernames given. Pass them as arguments or put them in "
                    f"{args.file or DEFAULT_NAMES_FILE}.", Palette.RED))
        sys.exit(1)

    valid: List[str] = []
    if not interactive:
        valid, invalid = clean_names(raw_names)
        warn_invalid(invalid)
        if not valid:
            print(paint("No valid usernames to check.", Palette.RED))
            sys.exit(1)

    checker = UsernameChecker(
        threads=args.threads,
        delay=args.delay,
        timeout=args.timeout,
        max_retries=args.max_retries,
        proxies=proxies,
        proxy_max_failures=args.proxy_max_failures,
        double_check=not args.no_double_check,
        auto_slowdown=not args.no_auto_slowdown,
        verbose=not args.quiet or interactive,
    )

    if proxies and not args.no_proxy_check:
        checker.probe_proxies()

    hits = load_existing_hits(args.output)
    known = len(hits)
    if args.watch:
        watch_names(checker, valid, max(args.every, 1.0), args.output, hits)
        return
    if interactive:
        results, elapsed = interactive_session(checker, args.output, hits, args.suggest)
        if not results:
            print(paint("Nothing checked. Bye!", Palette.GRAY))
            return
    else:
        start = time.monotonic()
        results = checker.run(valid)
        if args.retry_blocked:
            results = checker.retry_blocked(results)
        if args.suggest and not args.quiet:
            results += suggest_for_taken(checker, results)
        elapsed = time.monotonic() - start
        save_hits(args.output, results, hits)

    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for attr, export in (("json_out", lambda path: export_json(path, results, checked_at)),
                         ("csv_out", lambda path: export_csv(path, results)),
                         ("html_out", lambda path: export_html(path, results, checked_at))):
        path = getattr(args, attr)
        if path:
            try:
                export(path)
            except OSError as exc:
                print(paint(f"Error: couldn't write {path} ({exc})", Palette.RED))
                setattr(args, attr, None)  # so the summary doesn't list it

    print_summary(args, results, elapsed, len(hits) - known, known)


if __name__ == "__main__":
    main()
