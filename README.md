# TikTok Username Checker

> **v2 — rebuilt.** The old version (checking the page's HTTP status code) stopped
> working long ago because TikTok returns `200` for *every* username, taken or not.
> This rewrite actually works.

## Overview

A fast, reliable CLI tool that tells you whether a TikTok username is **taken** or
still **available** — using the *same data* TikTok's own web app uses.

For every public profile page (`https://www.tiktok.com/@username`), TikTok embeds a
JSON blob (`__UNIVERSAL_DATA_FOR_REHYDRATION__`) that contains the verdict:

| Case           | What's in the JSON                              |
| -------------- | ----------------------------------------------- |
| **Taken**      | `webapp.user-detail.userInfo.user` (nickname, verified badge, signature) + `userInfo.stats` (follower count, video count) |
| **Available**  | no `userInfo` + `statusCode` `10202` / `10221` (user not found) |

The tool prints the results live with colours, retries on rate limits with
exponential backoff, **double-checks every available username** with a second
independent request, **auto-evicts dead proxies**, adaptively slows down when
TikTok starts blocking, and saves every available username to `hits.txt`.
Full results can be exported to JSON and/or CSV.

## Why not the "official TikTok API"?

TikTok for Developers (the official API) has **no endpoint for checking username
availability** — it requires an approved app + OAuth and only exposes data about
users/videos you're authorized for. Every real username checker on the market uses
the same public profile-page approach this tool uses.

## Requirements

- **Python 3.8 or newer**
- **No third-party packages** — standard library only. Nothing to install.

## Installation

The checker is pure Python (standard library only) and runs on **Windows, macOS
and Linux** — the only thing you need is Python 3.8+, which most systems already
have. No `pip install`, no virtualenv, no fighting with Python versions.

```sh
git clone https://github.com/<you>/TikTok-Username-Checker.git
cd TikTok-Username-Checker
```

### Windows

1. **Check if Python is installed** — open a terminal (press `Win + R`, type
   `cmd`, press Enter) and run:

   ```bat
   python --version
   ```

   (if that fails, try `py --version`)

2. **Install Python if missing** — download it from
   <https://www.python.org/downloads/> and run the installer. ⚠️ **Important:
   tick "Add Python to PATH"** on the first screen of the installer.
   Alternatively, from a terminal: `winget install Python.Python.3.12`

3. **Go to the project folder**:

   ```bat
   cd Documents\GitHub\TikTok-Username-Checker
   ```

4. **Run it**:

   ```bat
   python main.py
   ```

   (or `py main.py`)

   Edit `usernames.txt` with Notepad (right-click → Open with → Notepad).
   Colours work out of the box on Windows 10+ (the tool enables ANSI support
   automatically).

### macOS

1. **Check if Python is installed** — open Terminal and run:

   ```sh
   python3 --version
   ```

2. **Install Python if missing** — easiest via Homebrew:

   ```sh
   brew install python
   ```

   (No Homebrew? Install it from <https://brew.sh> first, or use the installer
   from <https://www.python.org/downloads/>)

3. **Go to the project folder**:

   ```sh
   cd ~/Documents/GitHub/TikTok-Username-Checker
   ```

4. **Run it**:

   ```sh
   python3 main.py
   ```

### Linux

Python is almost always pre-installed on Linux — check first:

```sh
python3 --version
```

If it's missing, install it with your package manager:

```sh
# Debian / Ubuntu / Linux Mint
sudo apt install python3

# Fedora / RHEL / CentOS
sudo dnf install python3

# Arch / Manjaro
sudo pacman -S python

# openSUSE
sudo zypper install python3

# Alpine
sudo apk add python3
```

Then run from the project folder:

```sh
cd ~/Documents/GitHub/TikTok-Username-Checker
python3 main.py
```

### Any other system

If `python3 --version` (or `python --version`) reports **3.8 or newer**, the tool
works as-is: `python3 main.py` or `python main.py`. If your terminal ever shows
strange `←[32m`-style characters, run with `--no-color`.

## Usage

1. Add the usernames you want to check to `usernames.txt`, one per line:

   ```
   tiktok
   mycoolname
   mycoolname2
   ```

2. Run it:

   ```sh
   python main.py
   ```

   (the same command works on Windows, macOS and Linux; on Windows you can also
   use `py main.py`)

3. When it finishes, every **available** username is saved to `hits.txt`
   (already-known hits are never duplicated).

### Options

```sh
python main.py -f usernames.txt -o hits.txt --threads 15 --delay 0.5
python main.py --proxies proxies.txt --max-retries 5
python main.py --json results.json --csv results.csv   # full export of all results
python main.py --html report.html                      # styled HTML report
python main.py --retry-blocked                         # re-check blocked ones slower at the end
python main.py --quiet          # no banner/per-username lines, summary only (good for scripts)
python main.py --no-color       # plain output (e.g. for piping to a file)
```

| Flag            | Default | What it does                                    |
| --------------- | ------- | ----------------------------------------------- |
| `-f, --file`    | `usernames.txt` | input file, one username per line (lines starting with `#` are ignored) |
| `-o, --output`  | `hits.txt` | where available usernames are saved |
| `--json`        | — | export **all** results (with details) to a JSON file |
| `--csv`         | — | export **all** results (with details) to a CSV file |
| `--html`        | — | export a styled, self-contained HTML report (open it in any browser) |
| `--retry-blocked` | — | after the run, re-check `Blocked` usernames with half the threads, 3× the delay and more retries |
| `-t, --threads` | `10` | number of concurrent workers |
| `-d, --delay`   | `0.6` | seconds between requests, per worker (`0` disables) |
| `--timeout`     | `15` | per-request timeout in seconds |
| `-r, --max-retries` | `3` | retries per username on rate-limit/block |
| `-p, --proxies` | — | file with HTTP(S) proxies, one per line (rotated round-robin) |
| `--proxy-max-failures` | `3` | consecutive failures before a proxy is evicted automatically |
| `--no-proxy-check` | — | skip the startup proxy health check (dead proxies are dropped before checking starts) |
| `--no-double-check` | — | disable the second verification of available usernames |
| `--no-auto-slowdown` | — | disable the adaptive slowdown when TikTok blocks many requests |
| `--no-color`    | — | disable coloured output |
| `-q, --quiet`   | — | suppress per-username lines |

### Double verification

Every username reported as **available** is fetched a **second time** (through
a different proxy when you use several) before being trusted. If the second
check disagrees, the result is corrected automatically. In the console you may
occasionally see `(unconfirmed)` next to an available name — that means the
second request was inconclusive (e.g. rate limited) but the first verdict still
stood. Disable with `--no-double-check`.

### Dead proxy detection

With `--proxies`, every proxy is health-checked at startup (and dropped if it
fails), and proxies are evicted automatically after `--proxy-max-failures`
consecutive failures during the run. If all proxies die, the run reports an
error for the remaining usernames instead of silently going direct.

### Proxies (optional)

TikTok aggressively rate-limits datacenter IPs. For large lists, use proxies:

```txt
# proxies.txt
http://user:pass@1.2.3.4:8080
http://5.6.7.8:3128
```

```sh
python main.py --proxies proxies.txt
```

## Export

`--json`, `--csv` and `--html` write every result with its details (status,
url, nickname, verified badge, follower count, double-check status, error).
The HTML report is a single self-contained page (dark theme, stat bars,
colour-coded table) you can open in any browser or share directly.

Example JSON:

```json
{
  "checked_at": "2026-08-07T12:00:00+03:00",
  "results": [
    {
      "username": "tiktok",
      "status": "taken",
      "url": "https://www.tiktok.com/@tiktok",
      "nickname": "TikTok",
      "verified": true,
      "followers": 95200000,
      "double_checked": false,
      "confirmed": null
    }
  ]
}
```

## Example run (real)

A real run against 21 usernames — 8 well-known (expected `Taken`), 10 random
(expected `Available`), 3 invalid (skipped by validation):

```
Warning: skipped 3 invalid username(s) (2-24 chars, letters/digits/./_ only): bad name!, x, toolongusername1234567890123456789
Checking 18 username(s) (4 thread(s), 0.4s delay, max 1 retries)...
[TAKEN] https://www.tiktok.com/@mrbeast — MrBeast ✓verified · 133.5M followers
[TAKEN] https://www.tiktok.com/@tiktok — TikTok ✓verified · 95.2M followers
[TAKEN] https://www.tiktok.com/@charlidamelio — charli d'amelio ✓verified · 159.4M followers
[TAKEN] https://www.tiktok.com/@billieeilish — BILLIE EILISH ✓verified · 75.1M followers
[AVAILABLE] https://www.tiktok.com/@TikTokCoderX — looks free, grab it fast!
[AVAILABLE] https://www.tiktok.com/@57qysh93l9ai83 — looks free, grab it fast!
...

================================================================
Summary
  Total checked : 18
  Time          : 6.1s (178 usernames/min)
  Available     : 11 (61%)
  Taken        : 7 (39%)
  Blocked     : 0 (0%)
  Errors      : 0 (0%)
  Saved to hits.txt: 11 new hit(s)
  JSON export   : results.json
  CSV export    : results.csv
================================================================
```

Every `Available` in that run was confirmed by the double check
(`double_checked: true, confirmed: true` in the JSON export).

## Tips to avoid getting blocked

- Start with `--threads 5 --delay 1.0` and adjust from there.
- The **`Blocked`** counter in the summary tells you how many results are
  inconclusive — if it's high, slow down (or add proxies).
- Don't check the same IP with thousands of usernames in one go.

## Disclaimer

**Educational purposes only.** This tool is not affiliated with, endorsed by, or
sponsored by TikTok. Checking username availability may violate TikTok's Terms of
Service in some jurisdictions; you are responsible for how you use this tool. It
does not create accounts and does not reserve usernames — an "available" result
means TikTok reports no account with that username *at the moment of the check*.
