# TikTok Username Checker

> v2.5 adds Discord alerts for watch mode (`--discord`), name patterns
> (`--pattern 'ab?#'`) and `--skip-checked` to skip names you checked recently.
>
> v2.4 added a type-and-check prompt, optional name suggestions (`--suggest`) and
> optional watch mode (`--watch`) that alerts you when a taken name becomes free.
>
> v2 is a rewrite. The old version checked the page's HTTP status code, which
> stopped working long ago because TikTok returns `200` for every username,
> taken or not.

## What it does

A CLI tool that tells you whether a TikTok username is taken or still
available, using the same data TikTok's own web app uses.

Every public profile page (`https://www.tiktok.com/@username`) embeds a JSON
blob (`__UNIVERSAL_DATA_FOR_REHYDRATION__`) that contains the verdict:

| Case           | What's in the JSON                              |
| -------------- | ----------------------------------------------- |
| Taken          | `webapp.user-detail.userInfo.user` (nickname, verified badge, signature) + `userInfo.stats` (follower count, video count) |
| Available      | no `userInfo` + `statusCode` `10202` / `10221` (user not found) |

The tool prints results live with colours and retries on rate limits with
exponential backoff. It double-checks every available username with a second
independent request, evicts dead proxies automatically, slows down when TikTok
starts blocking, and saves every available username to `hits.txt`. Full
results can be exported to JSON, CSV or HTML.

## Why not the official TikTok API?

TikTok for Developers (the official API) has no endpoint for checking username
availability. It requires an approved app plus OAuth and only exposes data about
users and videos you're authorized for. So this tool reads the public profile
page instead.

## Requirements

- Python 3.8 or newer
- No third-party packages, standard library only. Nothing to install.

## Installation

The checker is pure Python and runs on Windows, macOS and Linux. You only need
Python 3.8+, which most systems already have. No `pip install`, no virtualenv.

```sh
git clone https://github.com/retroul/TikTok-Username-Checker.git
cd TikTok-Username-Checker
```

### Windows

1. Check if Python is installed. Open a terminal (press `Win + R`, type
   `cmd`, press Enter) and run:

   ```bat
   python --version
   ```

   (if that fails, try `py --version`)

2. Install Python if it's missing. Download it from
   <https://www.python.org/downloads/> and run the installer. On the first
   screen, tick "Add Python to PATH" or the commands below won't work.
   Alternatively, from a terminal: `winget install Python.Python.3.12`

3. Go to the project folder:

   ```bat
   cd Documents\GitHub\TikTok-Username-Checker
   ```

4. Run it:

   ```bat
   python main.py
   ```

   (or `py main.py`)

   Edit `usernames.txt` with Notepad (right-click → Open with → Notepad).
   Colours work out of the box on Windows 10+ (the tool enables ANSI support
   automatically).

### macOS

1. Check if Python is installed. Open Terminal and run:

   ```sh
   python3 --version
   ```

2. Install Python if it's missing. Homebrew is the easiest route:

   ```sh
   brew install python
   ```

   (No Homebrew? Install it from <https://brew.sh> first, or use the installer
   from <https://www.python.org/downloads/>)

3. Go to the project folder:

   ```sh
   cd ~/Documents/GitHub/TikTok-Username-Checker
   ```

4. Run it:

   ```sh
   python3 main.py
   ```

### Linux

Python is almost always pre-installed on Linux, so check first:

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

If `python3 --version` (or `python --version`) reports 3.8 or newer, the tool
works as-is: `python3 main.py` or `python main.py`. If your terminal ever shows
strange `←[32m`-style characters, run with `--no-color`.

## Usage

There are three ways to give it usernames.

**1. Type them live (easiest).** Run it and type names as they come to mind:

```sh
python3 main.py
```

```
username › coolname
[TAKEN]     https://www.tiktok.com/@coolname | Cool Name · 12.4K followers

username › coolname.tv, its.coolname
1/2 [AVAILABLE] https://www.tiktok.com/@its.coolname | free right now
2/2 [TAKEN]     https://www.tiktok.com/@coolname.tv | ...

username › q
```

Type several at once separated by spaces or commas. Type `q` (or press
`Ctrl+D`) to finish and see the summary. Free names are saved to `hits.txt`
as you go.

**2. Straight from the command line:**

```sh
python3 main.py coolname its.coolname coolname.tv
```

**3. A whole list from a file.** Put one username per line in `usernames.txt`
and run `python3 main.py`. If the file has names in it, they're checked
instead of showing the prompt. Use `-i` to get the prompt anyway.

### Name suggestions (off by default)

With this on, when a name is taken the checker tries about 10 similar names
(`its.name`, `name.tv`, `thename`, `name_`, `nameofficial`, ...) and shows the
free ones:

```
username › coolname
[TAKEN]     https://www.tiktok.com/@coolname | Cool Name · 12.4K followers
  Checking 10 names similar to @coolname...
  Free: its.coolname, coolname.tv
```

It stays off until you ask for it, because each taken name costs about 10
extra checks, which makes the run slower and gets you blocked sooner. To use it:

- Add `--suggest` when you start it: `python3 main.py --suggest`
  (works with the prompt, names on the command line, or a file)
- Or switch it while in the prompt: type `suggest on` / `suggest off`

Free suggested names are saved to `hits.txt` too.

### Watch a taken name (off by default)

Accounts get deleted or renamed, and their username becomes free again.
Watch mode keeps checking the names you want and alerts you (a message plus a
terminal beep) the moment one becomes free:

```sh
python3 main.py --watch coolname othername              # check every 30 minutes
python3 main.py --watch coolname --every 10             # check every 10 minutes
python3 main.py --watch -f usernames.txt --every 60     # watch a whole list hourly
```

```
Watching 2 name(s), checking every 30 minute(s). Leave this window open; press Ctrl+C to stop.

[14:00:02] Check #1: 2 name(s)
[TAKEN]     https://www.tiktok.com/@coolname | Cool Name · 12.4K followers
[TAKEN]     https://www.tiktok.com/@othername | Other · 803 followers
Next check at 14:30.
...
[16:30:05] Check #6: 2 name(s)
>>> @coolname is FREE now! Grab it: https://www.tiktok.com/@coolname
```

- It only runs while the window is open. Closing it or pressing `Ctrl+C`
  stops it and shows which names were freed.
- A freed name is saved to `hits.txt` and no longer watched. The run ends on
  its own once every name is free.
- The minimum interval is 1 minute. Checking more often doesn't help and
  gets you rate-limited, so 15–60 minutes is a good range.
- `--json`, `--csv` and `--html` work here too. The files are rewritten after
  every check with the newest result for each name.

#### Discord alerts

Add a Discord webhook and watch mode posts to your channel when a name frees
up, so you get it on your phone. In Discord: channel settings → Integrations →
Webhooks → New Webhook → Copy Webhook URL.

```sh
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."   # Windows: set DISCORD_WEBHOOK_URL=...
python3 main.py --watch coolname
```

`--discord <url>` works too, but the environment variable keeps the URL out of
your shell history. Treat the URL like a password: anyone who has it can post
to your channel. A test message is sent when the watch starts.

### Name patterns

`--pattern` checks every name that fits a pattern. `?` is any letter, `#` any
digit, `*` either. Put the pattern in quotes so your shell leaves it alone.

```sh
python3 main.py --pattern 'ab?#'        # aba0 ... abz9 (260 names)
python3 main.py --pattern 'x**' --pattern 'y**'
```

Patterns are refused if together they make more than 1000 names (change it
with `--max-names`). Big lists get you rate-limited fast, so start small.

### Skip names you already checked

Every taken or available verdict is saved to `checked.json` in the current
folder. `--skip-checked` skips names found in the last 24 hours, plus names
already in `hits.txt`. `--skip-hours` changes the window:

```sh
python3 main.py -f usernames.txt --skip-checked       # last 24 hours
python3 main.py -f usernames.txt --skip-checked --skip-hours 6   # last 6 hours
```

Blocked, error and unconfirmed results are never saved, so those names are always checked
again. Entries older than 30 days are dropped.

Invalid names are skipped with the reason (too short or long, characters
TikTok doesn't allow, ending with a period). A leading `@` is fine, and
duplicates are ignored regardless of upper or lower case.

On Windows use `python` or `py` instead of `python3`.

> "Available" means no TikTok account uses that name right now. TikTok can
> still refuse a name when you try to claim it if it belonged to a banned
> account or is reserved, so the only 100% test is trying it in the app.

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
| `usernames...`  | none | usernames to check right away (skips the file and the prompt) |
| `-i, --interactive` | off | type usernames at a prompt, even if `usernames.txt` has names |
| `--suggest`     | off | check ~10 similar names when a name is taken |
| `--watch`       | off | keep re-checking the names and alert when one becomes free |
| `--every`       | `30` | with `--watch`: minutes between checks (minimum 1) |
| `--discord`     | off | with `--watch`: Discord webhook URL to alert (or set `DISCORD_WEBHOOK_URL`) |
| `--pattern`     | none | check every name a pattern makes (`?` letter, `#` digit, `*` either) |
| `--max-names`   | `1000` | with `--pattern`: most names the patterns may make |
| `--skip-checked` | off | skip names in `hits.txt` and names checked in the last `--skip-hours` |
| `--skip-hours`  | `24` | with `--skip-checked`: how many hours back to look |
| `-f, --file`    | `usernames.txt` | input file, one username per line (lines starting with `#` are ignored) |
| `-o, --output`  | `hits.txt` | where available usernames are saved |
| `--json`        | none | export all results (with details) to a JSON file |
| `--csv`         | none | export all results (with details) to a CSV file |
| `--html`        | none | export a styled, self-contained HTML report (open it in any browser) |
| `--retry-blocked` | off | after the run, re-check `Blocked` usernames with half the threads, 3× the delay and more retries |
| `-t, --threads` | `10` | number of concurrent workers |
| `-d, --delay`   | `0.6` | seconds between requests, per worker (`0` disables) |
| `--timeout`     | `15` | per-request timeout in seconds |
| `-r, --max-retries` | `3` | retries per username on rate-limit/block |
| `-p, --proxies` | none | file with HTTP(S) proxies, one per line (rotated round-robin) |
| `--proxy-max-failures` | `3` | consecutive failures before a proxy is evicted automatically |
| `--no-proxy-check` | off | skip the startup proxy health check (dead proxies are dropped before checking starts) |
| `--no-double-check` | off | disable the second verification of available usernames |
| `--no-auto-slowdown` | off | disable the slowdown when TikTok blocks many requests |
| `--no-color`    | off | disable coloured output |
| `-q, --quiet`   | off | suppress per-username lines |

### Double verification

Every username reported as available is fetched a second time (through a
different proxy when you use several) before it's trusted. If the second check
disagrees, the result is corrected automatically. In the console you may
occasionally see `(unconfirmed)` next to an available name. That means the
second request was inconclusive (rate limited, for example) but the first
verdict stood. Disable it with `--no-double-check`.

### Dead proxy detection

With `--proxies`, every proxy is health-checked at startup and dropped if it
fails. During the run, a proxy is evicted after `--proxy-max-failures`
consecutive failures. If all proxies die, the run reports an error for the
remaining usernames instead of silently going direct.

### Proxies (optional)

TikTok rate-limits datacenter IPs hard. For large lists, use proxies:

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
The HTML report is a single self-contained page (no internet needed) that
opens in any browser and can be shared as-is. It follows your light or dark
system theme, works on phones, and lets you filter by status, search, sort,
copy every available name (or one at a time), and copy a ready-to-run command
for the blocked or errored names so you can retry just those.

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

## Example run

A run against 21 usernames: 8 well-known (expected `Taken`), 10 random
(expected `Available`), 3 invalid (skipped by validation):

```
Skipped 'bad name!': only letters, numbers, periods and underscores are allowed
Skipped 'x': too short (min 2 characters)
Skipped 'toolongusername1234567890123456789': too long (max 24 characters)
Checking 18 username(s) (4 thread(s), 0.4s delay, max 1 retries)...
 1/18 [TAKEN]     https://www.tiktok.com/@mrbeast | MrBeast ✓verified · 133.5M followers
 2/18 [TAKEN]     https://www.tiktok.com/@tiktok | TikTok ✓verified · 95.2M followers
 3/18 [TAKEN]     https://www.tiktok.com/@charlidamelio | charli d'amelio ✓verified · 159.4M followers
 4/18 [TAKEN]     https://www.tiktok.com/@billieeilish | BILLIE EILISH ✓verified · 75.1M followers
 5/18 [AVAILABLE] https://www.tiktok.com/@TikTokCoderX | free right now
 6/18 [AVAILABLE] https://www.tiktok.com/@57qysh93l9ai83 | free right now
...

============================================================
Summary
  Total checked : 18
  Time          : 6.1s (178 usernames/min)
  Available     : 11 (61%)
  Taken         : 7 (39%)
  Blocked       : 0 (0%)
  Errors        : 0 (0%)
  Saved hits    : 11 new in hits.txt
  JSON export   : results.json
  CSV export    : results.csv
  Note: available = no account uses it right now. TikTok can still refuse
  a name that belonged to a banned account or is reserved.
============================================================
```

Every `Available` in that run was confirmed by the double check
(`double_checked: true, confirmed: true` in the JSON export).

## Tips to avoid getting blocked

- Start with `--threads 5 --delay 1.0` and adjust from there.
- The `Blocked` counter in the summary counts inconclusive results. If it's
  high, slow down or add proxies.
- Don't check thousands of usernames from the same IP in one go.

## Disclaimer

Educational purposes only. This tool is not affiliated with, endorsed by, or
sponsored by TikTok. Checking username availability may violate TikTok's Terms of
Service in some jurisdictions, and you are responsible for how you use it. It
does not create accounts and does not reserve usernames. An "available" result
means TikTok reports no account with that username at the moment of the check.
