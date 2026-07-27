"""
Cineplex Showtime Monitor -- The Odyssey (IMAX 70mm) @ SilverCity Riverport
----------------------------------------------------------------------------
Checks the exact list of bookable dates for "The Odyssey" at SilverCity
Riverport Cinemas (Richmond, BC), and sends a free Telegram message the
moment that list changes -- e.g. when Cineplex adds dates beyond the
current cutoff.

Confirmed live (July 24, 2026): with the movie filtered specifically to
"The Odyssey", the date picker currently lists every day from today
through August 20, 2026, with no gaps, and nothing beyond that. Cineplex's
own site states showtimes are updated weekly, no later than Thursday --
so that's when a new/later cutoff date is likely to appear.

No AI/LLM involved, no API tokens (besides your own free Telegram bot),
no ongoing cost. Just a headless browser that loads the page, clicks
through to the date list, and compares it to what it saw last time.

SETUP (local):
1. pip3 install playwright requests
2. playwright install chromium
3. Create a Telegram bot via @BotFather, get your chat ID via
   @userinfobot (see Claude's step-by-step for details)
4. Set two environment variables before running:
     export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"
     export TELEGRAM_CHAT_ID="123456789"
5. Run: python3 cineplex_monitor.py

SETUP (GitHub Actions / cloud, no laptop needed):
See the accompanying .github/workflows/check.yml file and Claude's
step-by-step instructions.
"""

import hashlib
import os
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ---------- CONFIG ----------
# Telegram credentials come from environment variables so this file
# never has to contain (or leak) your actual bot token -- locally you
# export them in your shell, in GitHub Actions they're repo secrets.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# -----------------------------

URL = "https://www.cineplex.com/theatre/silvercity-riverport-cinemas?openTM=true"
STATE_FILE = Path(__file__).parent / "last_dates.txt"


def js_click_button(page, text_pattern: str, description: str, exact_start: bool = True) -> bool:
    """
    Find a visible <button> whose text matches, and click it via JavaScript.

    Two problems this solves, both confirmed from real GitHub Actions logs:

    1. Google ad iframes and the sticky header overlay the page and
       "intercept pointer events", so normal Playwright clicks retry
       forever and time out. A JS .click() dispatches directly on the
       element and ignores whatever is painted on top of it.

    2. Matching on page text alone can resolve to the wrong element --
       e.g. the theatre page's own <h1> heading rather than the option
       inside the picker. Restricting to <button> avoids that.
    """
    try:
        clicked = page.evaluate(
            """
            ({ pattern, exactStart }) => {
                const re = new RegExp(exactStart ? '^' + pattern : pattern, 'i');
                const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                const match = buttons.find(b =>
                    re.test((b.textContent || '').trim()) && b.offsetParent !== null
                );
                if (match) { match.click(); return true; }
                return false;
            }
            """,
            {"pattern": text_pattern, "exactStart": exact_start},
        )
        if not clicked:
            print(f"Warning: no visible button matched '{description}'.")
        return bool(clicked)
    except Exception as e:
        print(f"Warning: couldn't click '{description}' ({e}).")
        return False


def get_odyssey_dates(playwright) -> list[str]:
    """
    Load the Cineplex ticket modal, lock the theatre to SilverCity
    Riverport, filter the movie to The Odyssey, open the date picker,
    and return the list of bookable dates as strings.
    """
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ))
    # Use "domcontentloaded" rather than "networkidle": Cineplex runs ads
    # and analytics that poll continuously, so the network may never go
    # fully idle and networkidle can hang until timeout. We wait for the
    # DOM, then explicitly wait for the ticket modal content to appear.
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)

    # Wait for the ticket modal to actually render (it's client-side).
    # If it doesn't show up we still continue -- the later steps print
    # their own diagnostics.
    try:
        page.wait_for_selector("text=Tickets", timeout=20000)
    except Exception as e:
        print(f"Note: ticket modal didn't appear within 20s ({e}). Continuing.")
    page.wait_for_timeout(3000)

    # Dismiss the cookie consent banner first. A completely fresh,
    # cookie-less session (like every GitHub Actions run) always shows
    # this, and it sits on top of the page -- if we don't close it,
    # every click below silently fails because the banner intercepts
    # the click instead of the real button underneath it.
    try:
        page.get_by_role("button", name=re.compile("^OK$", re.I)).first.click(timeout=5000)
        page.wait_for_timeout(1000)
    except Exception as e:
        print(f"Note: no cookie banner found/dismissed ({e}). Continuing.")

    # 1. Force the theatre to SilverCity Riverport.
    #
    #    IMPORTANT: the picker's "nearby" list is sorted by distance from
    #    whatever machine is running the browser. GitHub Actions runners
    #    live in Ontario, so that list comes back full of Welland,
    #    Hamilton, London etc. and Riverport (Richmond, BC) is ~750km
    #    away and never appears. So we must TYPE INTO THE SEARCH BOX
    #    rather than trying to pick it out of the nearby list.
    if js_click_button(page, "Theatre", "Theatre field"):
        page.wait_for_timeout(1500)
        try:
            search_box = page.get_by_placeholder(re.compile("Search by theatres", re.I))
            search_box.fill("Riverport", timeout=8000)
            page.wait_for_timeout(2500)
        except Exception as e:
            print(f"Warning: couldn't type into theatre search box ({e}).")
        js_click_button(page, "SilverCity Riverport Cinemas", "SilverCity Riverport option")
        page.wait_for_timeout(3000)

    # 2. Filter the movie to "The Odyssey" specifically -- "All Movies"
    #    shows a much longer date list for the theatre in general, which
    #    isn't what we care about.
    if js_click_button(page, "Movie", "Movie field"):
        page.wait_for_timeout(1500)
        js_click_button(page, "The Odyssey", "The Odyssey option")
        page.wait_for_timeout(3000)

    # 3. Open the date picker and read the list of bookable dates.
    js_click_button(page, "Date", "Date field")
    page.wait_for_timeout(2500)

    body_text = page.inner_text("body")
    browser.close()

    dates = re.findall(
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}",
        body_text,
    )

    # Safety check: the runner is in Ontario, so if theatre selection
    # silently failed we could end up reporting dates for a completely
    # different cinema. Better to report nothing than the wrong thing.
    if "Riverport" not in body_text:
        print("ERROR: 'Riverport' not found on the final page -- theatre selection likely failed.")
        print("Refusing to report dates that may belong to the wrong theatre.")
        print("---- DEBUG: first 1500 chars of page text: ----")
        print(body_text[:1500])
        print("---- END DEBUG ----")
        return []

    if not dates:
        # Dump what we actually saw, so a failed run is diagnosable from
        # the GitHub Actions log instead of being a silent mystery.
        print("---- DEBUG: no dates matched. First 1500 chars of page text: ----")
        print(body_text[:1500])
        print("---- END DEBUG ----")

    # de-duplicate while preserving order
    seen = set()
    ordered_dates = []
    for d in dates:
        if d not in seen:
            seen.add(d)
            ordered_dates.append(d)
    return ordered_dates


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) -- skipping notification, printing instead:")
        print(message)
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
        timeout=10,
    )
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text}")


def main():
    with sync_playwright() as p:
        current_dates = get_odyssey_dates(p)

    if not current_dates:
        print("No dates found at all -- something likely went wrong loading the page. Skipping this check.")
        return

    current_last = current_dates[-1]
    current_blob = "|".join(current_dates)
    current_hash = hashlib.sha256(current_blob.encode("utf-8")).hexdigest()

    print(f"Checked. {len(current_dates)} bookable dates found. Last date: {current_last}")

    if not STATE_FILE.exists():
        STATE_FILE.write_text(current_hash + "\n" + current_blob)
        print("First run: saved baseline. Will alert on the next change.")
        send_telegram(
            f"🎬 Odyssey monitor started. Currently bookable through {current_last} at SilverCity Riverport."
        )
        return

    saved_hash, saved_blob = STATE_FILE.read_text().split("\n", 1)
    saved_dates = saved_blob.split("|")
    saved_last = saved_dates[-1] if saved_dates else "(none)"

    if current_hash != saved_hash:
        print("CHANGE DETECTED — sending Telegram message.")
        send_telegram(
            f"🚨 New Odyssey IMAX 70mm dates! Was through {saved_last}, now through {current_last}. Go book now:\n"
            f"https://www.cineplex.com/theatre/silvercity-riverport-cinemas?openTM=true"
        )
        STATE_FILE.write_text(current_hash + "\n" + current_blob)
    else:
        print("No change -- still only through", current_last)


if __name__ == "__main__":
    main()
