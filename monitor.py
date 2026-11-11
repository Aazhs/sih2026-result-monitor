#!/usr/bin/env python3
"""
SIH 2026 result monitor.

Watches sih.gov.in for:
  1. New links appearing on watched pages (results are usually posted as new links, in batches)
  2. New text appearing on watched pages
  3. Expected result URLs (currently 404) going live

Sends a push notification via ntfy.sh (and Telegram, if configured) the moment something changes.
Stdlib only, so it runs anywhere with Python 3.9+.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

# ---------------------------------------------------------------- config

# Pages whose links + visible text are diffed every cycle.
WATCH_PAGES = [
    "https://sih.gov.in/",
    "https://finale.sih.gov.in/",
]

# URLs that are 404 today but where 2026 results are likely to appear
# (based on how SIH 2023/2024/2025 results were published).
PROBE_URLS = [
    "https://sih.gov.in/sih2026",
    "https://sih.gov.in/sih2026/sih2026-grand-finale-result",
    "https://sih.gov.in/sih2026/nodalcenter",
    "https://sih.gov.in/sih2026-screening-result",
    "https://sih.gov.in/sih2026-screening-final-result",
    "https://sih.gov.in/sih2026-screening-result-batch2",
    "https://sih.gov.in/sih2026-grand-finale-result",
    "https://sih.gov.in/sih2026/screening-result",
]

HOT_WORDS = re.compile(
    r"result|shortlist|selected|finalist|batch|nodal|grand\s*finale|waitlist|2026",
    re.I,
)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
WHERE = os.environ.get("MONITOR_NAME", "cloud")

# Lines that change on every request and would cause false alarms.
NOISE_LINE = re.compile(r"^\s*(\d[\d,]*\s*)?(visitors?|hits|views?)\b|csrf|__RequestVerification", re.I)


# ---------------------------------------------------------------- html parsing

class Extractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template"}
    BLOCK = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
             "br", "section", "article", "header", "footer", "a", "span", "option", "label"}

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.skip_depth = 0
        self.parts = []
        self.links = {}          # absolute url -> anchor text
        self._href = None
        self._atext = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            href = href.strip()
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self._href = urllib.parse.urljoin(self.base, href)
                self._atext = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href:
            text = " ".join("".join(self._atext).split())
            # normalise trivial differences (trailing slash, www.)
            key = self._href.replace("://www.", "://").rstrip("/")
            if key not in self.links or (text and not self.links[key]):
                self.links[key] = text
            self._href = None

    def handle_data(self, data):
        if self.skip_depth:
            return
        self.parts.append(data)
        if self._href is not None:
            self._atext.append(data)


def extract(html, base):
    p = Extractor(base)
    try:
        p.feed(html)
    except Exception:
        pass
    lines = []
    seen = set()
    for raw in "".join(p.parts).split("\n"):
        line = " ".join(raw.split())
        if len(line) < 3 or NOISE_LINE.search(line) or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines, p.links


# ---------------------------------------------------------------- network

def fetch(url, timeout=30):
    """Returns (status_code, body_text). status 0 = network error."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, body.decode(r.headers.get_content_charset() or "utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        log(f"  fetch error {url}: {e!r}")
        return 0, ""


def notify(title, message, click=None, urgent=False):
    """Push via ntfy.sh and/or Telegram. Never raises."""
    log(f"NOTIFY [{('URGENT' if urgent else 'normal')}] {title}\n{message}")
    sent = False

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {
            "Title": title.encode("ascii", "ignore").decode() or "SIH monitor",
            "Priority": "5" if urgent else "4",
            "Tags": "rotating_light,mortar_board" if urgent else "eyes",
        }
        if click:
            headers["Click"] = click
        try:
            req = urllib.request.Request(f"{server}/{topic}", data=message.encode("utf-8"),
                                         headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:
            log(f"  ntfy failed: {e!r}")

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        text = f"{'🚨 ' if urgent else '👀 '}{title}\n\n{message}" + (f"\n\n{click}" if click else "")
        try:
            data = urllib.parse.urlencode({"chat_id": tg_chat, "text": text[:4000],
                                           "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                                   data=data, timeout=20).read()
            sent = True
        except Exception as e:
            log(f"  telegram failed: {e!r}")

    if os.environ.get("MAC_NOTIFY") == "1":
        try:
            subprocess.run(["osascript", "-e", "on run argv",
                            "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
                            "-e", "end run", title, message[:250]], timeout=10)
            sent = True
        except Exception as e:
            log(f"  mac notification failed: {e!r}")

    if not sent:
        log("  (no notification channel configured or all failed)")
    return sent


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state, reason):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True, ensure_ascii=False)
    if os.environ.get("COMMIT_STATE") == "1":
        try:
            d = os.path.dirname(STATE_FILE)
            subprocess.run(["git", "-C", d, "add", os.path.basename(STATE_FILE)], check=True)
            subprocess.run(["git", "-C", d, "commit", "-q", "-m", f"state: {reason}"[:120]], check=True)
            subprocess.run(["git", "-C", d, "pull", "-q", "--rebase", "-X", "theirs"], check=False)
            subprocess.run(["git", "-C", d, "push", "-q"], check=True)
        except Exception as e:
            log(f"  could not commit state: {e!r}")


def log(msg):
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S IST}] {msg}", flush=True)


# ---------------------------------------------------------------- checks

def check_page(url, state, changes):
    status, html = fetch(url)
    if status != 200 or not html:
        return status
    lines, links = extract(html, url)
    if not lines:  # JS-only app shell: fingerprint the raw HTML instead
        lines = ["(page source fingerprint " + hashlib.sha256(html.encode()).hexdigest()[:12] + ")"]
    prev = state.get(url)
    state[url] = {"lines": lines, "links": links, "checked": datetime.now(IST).isoformat()}
    if prev is None:
        log(f"  baseline saved for {url} ({len(lines)} lines, {len(links)} links)")
        return status

    old_links = prev.get("links", {})
    new_links = {u: t for u, t in links.items() if u not in old_links}
    old_lines = set(prev.get("lines", []))
    new_lines = [l for l in lines if l not in old_lines]

    if new_links or new_lines:
        changes.append({"url": url, "new_links": new_links, "new_lines": new_lines})
    return status


def check_probe(url, state, changes):
    status, html = fetch(url)
    key = "probe:" + url
    prev = state.get(key, {}).get("status")
    state[key] = {"status": status}
    if status == 0:
        state[key]["status"] = prev  # network error: keep last known
        return status
    if prev is not None and prev != 200 and status == 200:
        lines, _ = extract(html, url)
        changes.append({"url": url, "went_live": True, "new_links": {}, "new_lines": lines[:15]})
    return status


def format_changes(changes):
    msgs, urgent, click = [], False, None
    for c in changes:
        click = click or c["url"]
        if c.get("went_live"):
            urgent = True
            msgs.append(f"NEW PAGE IS LIVE:\n{c['url']}")
        else:
            msgs.append(f"Change on {c['url']}")
        for u, t in list(c["new_links"].items())[:10]:
            msgs.append(f"+ link: {t or '(no text)'}\n  {u}")
            if HOT_WORDS.search(u) or HOT_WORDS.search(t or ""):
                urgent = True
                click = u
        for l in c["new_lines"][:8]:
            msgs.append(f"+ {l[:200]}")
            if HOT_WORDS.search(l):
                urgent = True
    return "\n".join(msgs)[:3500], urgent, click


def run_once(state):
    changes = []
    statuses = {}
    for url in WATCH_PAGES:
        statuses[url] = check_page(url, state, changes)
    for url in PROBE_URLS:
        statuses[url] = check_probe(url, state, changes)
    # any probe that just went live becomes a watched page from now on
    for c in changes:
        if c.get("went_live") and c["url"] not in WATCH_PAGES:
            WATCH_PAGES.append(c["url"])
    return changes, statuses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop-minutes", type=float, default=0, help="keep checking for this long (0 = single check)")
    ap.add_argument("--interval", type=int, default=60, help="seconds between checks")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

    if args.test_notify:
        ok = notify("SIH monitor test", "If you can read this, notifications work. 🎉",
                    click="https://sih.gov.in/", urgent=True)
        sys.exit(0 if ok else 1)

    state = load_state()
    first_ever = not state
    # pages that went live on a previous run stay watched
    for k, v in state.items():
        if k.startswith("probe:") and v.get("status") == 200:
            u = k[len("probe:"):]
            if u not in WATCH_PAGES:
                WATCH_PAGES.append(u)

    deadline = time.time() + args.loop_minutes * 60
    fail_streak = 0
    down_alerted = False
    cycle = 0

    while True:
        cycle += 1
        log(f"check #{cycle}")
        changes, statuses = run_once(state)

        home_ok = statuses.get("https://sih.gov.in/") == 200
        fail_streak = 0 if home_ok else fail_streak + 1
        # only meaningful if this machine has ever loaded the homepage (403 = blocked, not down)
        if fail_streak >= 15 and not down_alerted and "https://sih.gov.in/" in state \
                and statuses.get("https://sih.gov.in/") != 403:
            notify("sih.gov.in is not responding",
                   f"Homepage has failed for ~{fail_streak} checks in a row (status {statuses.get('https://sih.gov.in/')}). "
                   "Heavy traffic sometimes means results just dropped - check manually.",
                   click="https://sih.gov.in/")
            down_alerted = True
        if home_ok and down_alerted:
            down_alerted = False

        if first_ever:
            save_state(state, "baseline")
            notify(f"SIH 2026 monitor is live ({WHERE})",
                   f"Watching {len(WATCH_PAGES)} pages + {len(PROBE_URLS)} expected result URLs every "
                   f"{args.interval}s. You'll get an alert the moment anything changes.",
                   click="https://sih.gov.in/")
            first_ever = False
        elif changes:
            body, urgent, click = format_changes(changes)
            title = "SIH 2026: RESULTS may be out!" if urgent else "SIH website changed"
            notify(title, body, click=click, urgent=urgent)
            save_state(state, f"change on {', '.join(c['url'] for c in changes)}")
        else:
            log("  no changes")

        if time.time() + args.interval >= deadline:
            break
        time.sleep(args.interval)

    # persist latest snapshot (timestamps) quietly without a commit
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True, ensure_ascii=False)


if __name__ == "__main__":
    main()
