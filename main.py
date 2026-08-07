#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikTok Username Checker — v2
============================

Checks whether TikTok usernames are taken or still available, for real.

How it works
------------
TikTok has no official public API for username availability, so this tool
reads the *same* data TikTok's own web app uses. Every public profile page
(https://www.tiktok.com/@username) embeds a JSON blob called
``__UNIVERSAL_DATA_FOR_REHYDRATION__``. Inside it:

    * username taken  ->  "webapp.user-detail" contains a "userInfo" object
                          (with nickname, verified flag, follower count, ...)
    * username free   ->  no "userInfo" and statusCode 10202 / 10221 (not found)

This is far more reliable than checking the HTTP status code of the page
(which returns 200 for every username, existing or not) — the old method
this project used and which stopped working long ago.

Available usernames are double-checked with a second, independent request
(through another proxy when available) before being reported as free.

This is not affiliated with or endorsed by TikTok. Use it responsibly and
at your own risk — mass checking may lead to rate limiting of your IP.
Educational purposes only.

Requirements
------------
* Python 3.8 or newer
* No third-party packages (standard library only)

Usage
-----
    python main.py                        # check usernames.txt, save to hits.txt
    python main.py -f usernames.txt --json results.json --csv results.csv
    python main.py --proxies proxies.txt --max-retries 3

Run ``python main.py --help`` for all options.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import Dict, List, Optional, Tuple

VERSION = "2.1.1"

HOME_URL = "https://www.tiktok.com"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# statusCode values TikTok returns when a username does not exist
TIKTOK_NOT_FOUND_CODES = {10202, 10221, 10222, 10223, 10224}

# Usernames: 2-24 chars, letters / digits / . / _
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{2,24}$")

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
  _____ _ _  _____        _              ____ _                     _
 |_   _| (_)_   _|_      | | __ _ _ __  / ___| |__   ___  _ __   __| | __ _ _ __
   | | | | | | | \ \ /\ / / |/ _` | '__| |   | '_ \ / _ \| '_ \ / _` |/ _` | '_ \
   | | | | | | |  \ V  V /| | (_| | |  | |___| | | | (_) | | | | (_| | (_| | |_) |
   |_| |_|_| |_|   \_/\_/ |_|\__,_|_|   \____|_| |_|\___/|_| |_|\__,_|\__,_| .__/
                                                                           |_|
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
        if self.status == "available":
            label = paint("[AVAILABLE]", Palette.GREEN)
            extra = " — looks free, grab it fast!"
            if self.double_checked and not self.confirmed:
                extra += paint(" (unconfirmed)", Palette.GRAY)
        elif self.status == "taken":
            label = paint("[TAKEN]", Palette.RED)
            extra = ""
            if self.nickname:
                extra = f" — {self.nickname}"
                if self.verified:
                    extra += paint(" ✓verified", Palette.CYAN)
                if self.followers:
                    extra += f" · {format_count(self.followers)} followers"
            if self.error == "corrected by double-check":
                extra += paint(" [corrected]", Palette.GRAY)
        elif self.status == "blocked":
            label = paint("[BLOCKED]", Palette.YELLOW)
            extra = f" — {self.error or 'rate limited / captcha'}"
        else:
            label = paint("[ERROR]", Palette.YELLOW)
            extra = f" — {self.error}"
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
            print(paint(f"  [proxy evicted] {proxy}", Palette.YELLOW))
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
            "Accept-Encoding": "identity",
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
            return data.decode("utf-8", errors="replace"), "ok"
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                return None, "rate-limited"
            return None, "http"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
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
                    f"Removed dead proxy(ies): {', '.join(self._proxy_pool.removed)}",
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
            return (
                "taken",
                str(user.get("nickname", "")),
                bool(user.get("verified")),
                int(stats.get("followerCount", 0) or 0),
                str(user.get("signature", "")),
            )

        status_code = user_detail.get("statusCode")
        if status_code in TIKTOK_NOT_FOUND_CODES:
            return "available", "", False, 0, ""

        # Login wall / generic page without a clear verdict
        return "blocked", "", False, 0, f"unexpected response (statusCode={status_code})"

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
            # first verdict was wrong (edge case) — trust the second one
            self._report_outcome("taken")
            return CheckResult(
                username, "taken", nickname, verified, followers, signature,
                double_checked=True, confirmed=False, error="corrected by double-check",
            )
        if status == "available":
            self._report_outcome("available")
            return CheckResult(username, "available", double_checked=True, confirmed=True)
        # second check inconclusive — keep the first verdict, flagged as unconfirmed
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
                        f"({last_error}) — attempt {attempt + 2}/{self.max_retries + 1}"
                    )

                if attempt < self.max_retries:
                    time.sleep(min(base_backoff * (2 ** attempt), 10.0))
        except Exception as exc:  # a failed check must never kill the whole run
            return CheckResult(username, "error", error=f"unexpected error: {exc}")

        self._report_outcome("blocked")
        return CheckResult(username, "blocked", error=last_error or "rate limited")

    def run(self, usernames: List[str]) -> List[CheckResult]:
        """Check a list of usernames concurrently, printing as they finish."""
        start = time.monotonic()
        results: List[CheckResult] = []
        if self.verbose:
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
                        if self.verbose:
                            print(result.to_line())
                except KeyboardInterrupt:
                    print("\n" + paint("Interrupted — finishing and saving results...", Palette.YELLOW))
                    self.stop_event.set()
                    for future in futures:
                        future.cancel()
                # exiting the `with` waits for in-flight checks to finish
            # collect results from futures that finished during shutdown
            for future in futures:
                if future not in collected and not future.cancelled() and future.done():
                    result = future.result()
                    results.append(result)
                    if self.verbose:
                        print(result.to_line())
        except KeyboardInterrupt:
            # Ctrl+C pressed again while shutting down
            print("\n" + paint("Stopped.", Palette.YELLOW))
            self.stop_event.set()

        self.elapsed = time.monotonic() - start
        self.stats = {"available": 0, "taken": 0, "blocked": 0, "error": 0}
        for result in results:
            self.stats[result.status] = self.stats.get(result.status, 0) + 1
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
        self.stats = {"available": 0, "taken": 0, "blocked": 0, "error": 0}
        for result in merged:
            self.stats[result.status] = self.stats.get(result.status, 0) + 1
        return merged


def load_usernames(path: Path) -> Tuple[List[str], List[str]]:
    """Load usernames from a file. Returns (valid, invalid)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        print(paint(f"Error: {path} not found!", Palette.RED))
        sys.exit(1)

    seen: set = set()
    valid, invalid = [], []
    for raw in lines:
        username = raw.strip().lstrip("@").strip()
        if not username or username.startswith("#"):
            continue
        if username in seen:
            continue
        seen.add(username)
        if USERNAME_RE.fullmatch(username):
            valid.append(username)
        else:
            invalid.append(username)
    return valid, invalid


def load_existing_hits(path: Path) -> set:
    """Return usernames already saved to the output file (to avoid duplicates)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {ln.strip().lstrip("@").strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def save_hits(path: Path, results: List[CheckResult], existing: set) -> int:
    """Append newly found available usernames. Returns number written."""
    new = [r.username for r in results if r.status == "available" and r.username not in existing]
    if not new:
        return 0
    with path.open("a", encoding="utf-8") as f:
        for username in new:
            f.write(f"{username}\n")
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
            writer.writerow(_result_dict(r))


_STATUS_COLORS = {
    "available": "#22c55e",
    "taken": "#ef4444",
    "blocked": "#f59e0b",
    "error": "#9ca3af",
}


def export_html(path: Path, results: List[CheckResult], checked_at: str) -> None:
    """Write a self-contained, styled HTML report of all results."""
    total = len(results) or 1
    stats = {"available": 0, "taken": 0, "blocked": 0, "error": 0}
    for r in results:
        stats[r.status] = stats.get(r.status, 0) + 1

    bars = []
    for label, key, color in (
        ("Available", "available", "#22c55e"),
        ("Taken", "taken", "#ef4444"),
        ("Blocked", "blocked", "#f59e0b"),
        ("Errors", "error", "#9ca3af"),
    ):
        count = stats[key]
        pct = 100 * count / total
        bars.append(
            f"""<div class="stat">
      <div class="stat-head"><span>{label}</span><span class="stat-num">{count} · {pct:.0f}%</span></div>
      <div class="bar"><div class="bar-fill" style="width:{pct:.1f}%;background:{color}"></div></div>
    </div>"""
        )

    rows = []
    for r in results:
        color = _STATUS_COLORS.get(r.status, "#9ca3af")
        details = []
        if r.nickname:
            details.append(html.escape(r.nickname) + (" ✓" if r.verified else ""))
        if r.followers:
            details.append(f"{format_count(r.followers)} followers")
        if r.double_checked:
            details.append("double-checked" if r.confirmed else "double-checked (unconfirmed)")
        if r.error:
            details.append(html.escape(r.error))
        rows.append(
            f"""<tr>
      <td><span class="status-dot" style="background:{color}"></span>{html.escape(r.username)}</td>
      <td><span class="badge" style="color:{color};border-color:{color}">{r.status}</span></td>
      <td><a href="{r.url}" target="_blank" rel="noopener">{html.escape(r.username)}</a></td>
      <td>{" · ".join(details) if details else "—"}</td>
    </tr>"""
        )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TikTok Username Checker — Report</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 32px 16px; font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
         background: #0f1115; color: #e5e7eb; }}
  .wrap {{ max-width: 880px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: #9ca3af; font-size: 13px; margin-bottom: 24px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .stat {{ background: #1a1d24; border: 1px solid #262a33; border-radius: 10px; padding: 12px 14px; }}
  .stat-head {{ display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 8px; }}
  .stat-num {{ color: #9ca3af; }}
  .bar {{ height: 6px; background: #262a33; border-radius: 3px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 3px; }}
  table {{ width: 100%; border-collapse: collapse; background: #1a1d24; border: 1px solid #262a33; border-radius: 10px; overflow: hidden; }}
  th, td {{ padding: 10px 14px; text-align: left; font-size: 13px; }}
  th {{ background: #232733; color: #d1d5db; font-weight: 600; }}
  td {{ border-top: 1px solid #262a33; }}
  a {{ color: #60a5fa; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .status-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px; }}
  .badge {{ display: inline-block; padding: 2px 10px; border: 1px solid; border-radius: 999px; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>TikTok Username Checker — Report</h1>
  <div class="sub">Checked {len(results)} username(s) · {html.escape(checked_at)}</div>
  <div class="stats">
    {''.join(bars)}
  </div>
  <table>
    <thead><tr><th>Username</th><th>Status</th><th>Profile</th><th>Details</th></tr></thead>
    <tbody>
    {''.join(rows)}
    </tbody>
  </table>
</div>
</body>
</html>
"""
    with path.open("w", encoding="utf-8") as f:
        f.write(doc)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Real TikTok username availability checker (standard library only).",
        epilog="Examples:\n"
        "  python main.py\n"
        "  python main.py -f usernames.txt --json results.json --csv results.csv\n"
        "  python main.py --proxies proxies.txt --max-retries 5\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-f", "--file", type=Path, default=Path("usernames.txt"),
                        help="file with usernames, one per line (default: usernames.txt)")
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
                        help="disable the adaptive slowdown on repeated blocks")
    parser.add_argument("--retry-blocked", action="store_true",
                        help="after the run, re-check blocked usernames with slower settings")
    parser.add_argument("--no-color", action="store_true",
                        help="disable coloured output")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="suppress per-username output (summary only)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    global USE_COLOR
    args = parse_args(argv)

    if os.name == "nt":
        os.system("")  # enable ANSI colour support in the Windows console

    if args.no_color or not sys.stdout.isatty():
        USE_COLOR = False

    if not args.quiet:
        print(paint(BANNER, Palette.CYAN))
        print(paint(f"TikTok Username Checker v{VERSION} — real availability check via "
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
        if not proxies:
            print(paint(f"Warning: {args.proxies} is empty — running without proxies.", Palette.YELLOW))

    valid, invalid = load_usernames(args.file)

    if not valid:
        print(paint(f"No valid usernames found in {args.file}.", Palette.RED))
        sys.exit(1)

    if invalid:
        print(
            paint(
                f"Warning: skipped {len(invalid)} invalid username(s) "
                f"(2-24 chars, letters/digits/./_ only): {', '.join(invalid[:5])}"
                + (" ..." if len(invalid) > 5 else ""),
                Palette.YELLOW,
            )
        )

    checker = UsernameChecker(
        threads=args.threads,
        delay=args.delay,
        timeout=args.timeout,
        max_retries=args.max_retries,
        proxies=proxies,
        proxy_max_failures=args.proxy_max_failures,
        double_check=not args.no_double_check,
        auto_slowdown=not args.no_auto_slowdown,
        verbose=not args.quiet,
    )

    if proxies and not args.no_proxy_check:
        checker.probe_proxies()

    existing_hits = load_existing_hits(args.output)
    results = checker.run(valid)
    if args.retry_blocked:
        results = checker.retry_blocked(results)
    written = save_hits(args.output, results, existing_hits)

    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if args.json_out:
        export_json(args.json_out, results, checked_at)
    if args.csv_out:
        export_csv(args.csv_out, results)
    if args.html_out:
        export_html(args.html_out, results, checked_at)

    total = len(results)
    elapsed = checker.elapsed
    speed = total / elapsed * 60 if elapsed > 0 else 0.0

    def pct(count: int) -> str:
        return f"{100 * count / total:.0f}%" if total else "0%"

    print()
    print(paint("=" * 60, Palette.BOLD))
    print(paint("Summary", Palette.BOLD))
    print(f"  Total checked : {total}")
    print(f"  Time          : {elapsed:.1f}s ({speed:.0f} usernames/min)")
    print(f"  {paint('Available', Palette.GREEN)}     : {checker.stats['available']} ({pct(checker.stats['available'])})")
    print(f"  {paint('Taken', Palette.RED)}        : {checker.stats['taken']} ({pct(checker.stats['taken'])})")
    print(f"  {paint('Blocked', Palette.YELLOW)}     : {checker.stats['blocked']} ({pct(checker.stats['blocked'])})")
    print(f"  {paint('Errors', Palette.YELLOW)}      : {checker.stats['error']} ({pct(checker.stats['error'])})")
    print(f"  Saved to {args.output}: {written} new hit(s)"
          + (f" ({len(existing_hits)} already known)" if existing_hits else ""))
    if args.json_out:
        print(f"  JSON export   : {args.json_out}")
    if args.csv_out:
        print(f"  CSV export    : {args.csv_out}")
    if args.html_out:
        print(f"  HTML report   : {args.html_out}")
    if checker.stats["blocked"] or checker.stats["error"]:
        print(
            paint(
                "  Tip: if you see many BLOCKED, raise --delay and lower --threads, "
                "or add --proxies. TikTok rate-limits aggressively.",
                Palette.GRAY,
            )
        )
    print(paint("=" * 60, Palette.BOLD))


if __name__ == "__main__":
    main()
