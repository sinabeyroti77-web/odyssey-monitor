"""
Cineplex SEAT/SHOWTIME WATCHER -- The Odyssey @ SilverCity Riverport
---------------------------------------------------------------------
Companion to cineplex_monitor.py.

  cineplex_monitor.py  -> "did Cineplex open a NEW WEEK of dates?"
  seat_watcher.py      -> "did a seat free up on a date I already want?"

Most seats at Richmond/Langley come back through CANCELLATIONS on shows
that are already listed, not through new date drops. Per SeatDrop's
analysis of 5,600+ seat openings, ~50% of cancellations land in the
final 12 hours before showtime and ~29% in the final 4 hours, with a
secondary spike 1-2 days out. This script is what catches those.

HOW IT DETECTS AVAILABILITY
Cineplex does NOT print "Sold Out" anywhere in the showtime list
(verified against the live page). Instead, a showtime is represented as
a <button aria-label="Book show at 7:00 PM">. So we defensively track
BOTH possible representations of a sold-out show:

  1. the showtime BUTTON DISAPPEARS from the list, or
  2. the showtime button is present but DISABLED.

We record {time -> enabled?} per target date and alert when a showtime
appears, or flips from unavailable to available. That works regardless
of which representation Cineplex uses, without needing to open the
seat map itself.

CONFIG (environment variables):
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     required
  TARGET_DATES         optional, comma-separated, e.g.
                       "August 15, 2026|August 16, 2026"
                       (use | between dates, since dates contain commas)
                       If unset, defaults to the LAST date in the window.
"""

import hashlib
import json
import os
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TARGET_DATES_RAW = os.environ.get("TARGET_DATES", "").strip()

URL = "https://www.cineplex.com/theatre/silvercity-riverport-cinemas?openTM=true"
STATE_FILE = Path(__file__).parent / "last_showtimes.json"
BOOK_URL = "https://www.cineplex.com/theatre/silvercity-riverport-cinemas?openTM=true"

DATE_RE = (
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December) \d{1,2}, \d{4}"
)


def js_click_button(page, pattern: str, description: str, exact_start: bool = True) -> bool:
    """
    Click a visible <button> by text, via JavaScript.

    JS click (rather than Playwright's normal click) is essential here:
    Google ad iframes and the sticky header overlay the modal and
    "intercept pointer events", which makes ordinary clicks retry until
    they time out. Confirmed from real GitHub Actions logs.
    """
    try:
        clicked = page.evaluate(
            """
            ({ pattern, exactStart }) => {
                const esc = pattern.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                const re = new RegExp(exactStart ? '^' + esc : esc, 'i');
                const els = Array.from(document.querySelectorAll('button, [role="button"]'));
                const m = els.find(b => re.test((b.textContent || '').trim()) && b.offsetParent !== null);
                if (m) { m.click(); return true; }
                return false;
            }
            """,
            {"pattern": pattern, "exactStart": exact_start},
        )
        if not clicked:
            print(f"  ! no visible button matched '{description}'")
        return bool(clicked)
    except Exception as e:
        print(f"  ! couldn't click '{description}': {e}")
        return False


def read_showtimes(page) -> dict:
    """
    Return {"7:00 PM": True/False} for the currently displayed date,
    where the value is whether that showtime is bookable.
    """
    return page.evaluate(
        """
        () => {
            const out = {};
            const els = Array.from(document.querySelectorAll('button, [role="button"]'));
            for (const b of els) {
                const t = (b.textContent || '').trim();
                if (!/^\\d{1,2}:\\d{2}\\s*(AM|PM)$/i.test(t)) continue;
                if (b.offsetParent === null) continue;
                const ariaDisabled = b.getAttribute('aria-disabled') === 'true';
                const cls = (b.className || '').toLowerCase();
                const looksDisabled =
                    b.disabled === true ||
                    ariaDisabled ||
                    cls.includes('disabled') ||
                    cls.includes('soldout');
                out[t] = !looksDisabled;
            }
            return out;
        }
        """
    )


def setup_page(page):
    """Load the modal, dismiss cookies, lock theatre + movie."""
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector("text=Tickets", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(3000)

    # Cookie banner: always present on a fresh cookie-less runner, and
    # it swallows clicks until dismissed.
    js_click_button(page, "OK", "cookie OK")
    page.wait_for_timeout(1000)

    # Theatre. NOTE: the "nearby" list is sorted by distance from the
    # RUNNER, and GitHub's runners are in Ontario -- Richmond BC is
    # ~3,400km away and never appears in that list. We must search.
    if js_click_button(page, "Theatre", "Theatre field"):
        page.wait_for_timeout(1500)
        try:
            page.get_by_placeholder(re.compile("Search by theatres", re.I)).fill(
                "Riverport", timeout=8000
            )
            page.wait_for_timeout(2500)
        except Exception as e:
            print(f"  ! theatre search box: {e}")
        js_click_button(page, "SilverCity Riverport Cinemas", "Riverport option")
        page.wait_for_timeout(3000)

    # Movie
    if js_click_button(page, "Movie", "Movie field"):
        page.wait_for_timeout(1500)
        js_click_button(page, "The Odyssey", "The Odyssey option")
        page.wait_for_timeout(3000)


def collect(page, target_dates: list[str]) -> dict:
    """Return {date_string: {showtime: bookable_bool}}."""
    results = {}
    for d in target_dates:
        print(f"Checking {d} ...")
        if not js_click_button(page, "Date", "Date field"):
            print("  ! couldn't open date picker; skipping this date")
            continue
        page.wait_for_timeout(2000)
        # Date buttons read like "Saturday August 15, 2026", so match
        # the date substring rather than the start of the label.
        if not js_click_button(page, d, f"date {d}", exact_start=False):
            print(f"  ! date {d} not offered (may not be bookable yet)")
            continue
        page.wait_for_timeout(3500)

        body = page.inner_text("body")
        if "Riverport" not in body:
            print("  ! Riverport missing from page -- wrong theatre, skipping")
            continue

        times = read_showtimes(page)
        print(f"  -> {len(times)} showtime(s): {times}")
        results[d] = times
    return results


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; printing instead:\n" + message)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
        timeout=10,
    )
    if not r.ok:
        print(f"Telegram send failed: {r.status_code} {r.text}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        setup_page(page)

        # Work out which dates to watch.
        if TARGET_DATES_RAW:
            targets = [d.strip() for d in TARGET_DATES_RAW.split("|") if d.strip()]
        else:
            js_click_button(page, "Date", "Date field")
            page.wait_for_timeout(2000)
            all_dates = re.findall(DATE_RE, page.inner_text("body"))
            seen, ordered = set(), []
            for d in all_dates:
                if d not in seen:
                    seen.add(d)
                    ordered.append(d)
            targets = ordered[-1:] if ordered else []
            print(f"TARGET_DATES unset; defaulting to last bookable date: {targets}")

        if not targets:
            print("No target dates resolved. Exiting without alerting.")
            browser.close()
            return

        current = collect(page, targets)
        browser.close()

    if not current:
        print("Collected nothing -- not updating state, not alerting.")
        return

    previous = {}
    if STATE_FILE.exists():
        try:
            previous = json.loads(STATE_FILE.read_text())
        except Exception:
            previous = {}

    if not previous:
        STATE_FILE.write_text(json.dumps(current, indent=1, sort_keys=True))
        summary = "\n".join(
            f"{d}: " + ", ".join(f"{t}{'' if ok else ' (unavailable)'}" for t, ok in sorted(v.items()))
            for d, v in current.items()
        )
        print("First run: baseline saved.")
        send_telegram(f"🎟️ Seat watcher started.\nWatching:\n{summary}")
        return

    # Diff: what became available that wasn't before?
    good_news = []
    for date, times in current.items():
        old = previous.get(date, {})
        for t, available in times.items():
            was = old.get(t)          # None = showtime wasn't listed at all
            if available and was is not True:
                reason = "NEW showtime" if was is None else "freed up"
                good_news.append(f"{date} — {t} ({reason})")

    if good_news:
        msg = "🚨 SEATS AVAILABLE — The Odyssey 70mm, SilverCity Riverport\n\n"
        msg += "\n".join(good_news)
        msg += f"\n\nBook now: {BOOK_URL}"
        print("CHANGE DETECTED:\n" + msg)
        send_telegram(msg)
    else:
        print("No new availability.")

    STATE_FILE.write_text(json.dumps(current, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
