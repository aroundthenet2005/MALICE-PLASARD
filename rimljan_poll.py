import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# Source page (contains the date heading + <li> list in HTML)
MENU_URL = os.getenv("MENU_URL") or "https://rimljan.si/"

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

DISCORD_API = "https://discord.com/api/v10"
HEADERS = {
    "Authorization": f"Bot {BOT_TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "rimljan-malica-poll/1.0",
}

# Discord poll limits
MAX_ANSWERS = 10
MAX_ANSWER_LEN = 55

# Slovene weekday date header, case-insensitive
DATE_RE = re.compile(
    r"\b(PONEDELJEK|TOREK|SREDA|CETRTEK|ČETRTEK|PETEK|SOBOTA|NEDELJA)\b\s+\d{1,2}\.\d{1,2}\.\d{4}\b",
    re.IGNORECASE,
)


def die(msg: str, code: int = 1):
    print(msg)
    raise SystemExit(code)


def discord_request(method: str, path: str, *, json_body=None):
    url = f"{DISCORD_API}{path}"

    while True:
        r = requests.request(method, url, headers=HEADERS, json=json_body, timeout=30)

        # rate limit handling
        if r.status_code == 429:
            try:
                retry = float(r.json().get("retry_after", 1.0))
            except Exception:
                retry = 1.5
            time.sleep(retry + 0.2)
            continue

        if r.status_code >= 400:
            die(f"Discord API error {r.status_code}: {r.text}")

        if not r.text:
            return None
        return r.json()


def get_recent_messages(limit: int = 15):
    return discord_request("GET", f"/channels/{CHANNEL_ID}/messages?limit={limit}") or []


def already_posted_for(date_text: str) -> bool:
    marker = f"Rimljan malice — {date_text}"
    for m in get_recent_messages(15):
        if marker in (m.get("content") or ""):
            return True
    return False


def clean_text(s: str) -> str:
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def truncate_to_two_words_if_needed(item: str) -> str:
    item = clean_text(item)

    # If too long, keep only first 2 words (your rule)
    if len(item) > MAX_ANSWER_LEN:
        words = item.split()
        item = " ".join(words[:2]) if len(words) >= 2 else item

    item = clean_text(item)

    # Final safety clamp
    if len(item) > MAX_ANSWER_LEN:
        item = item[:MAX_ANSWER_LEN].rstrip()

    return item


def dedupe_with_suffix(items: list[str]) -> list[str]:
    # Avoid duplicate answer texts
    seen = {}
    out = []
    for it in items:
        if it not in seen:
            seen[it] = 1
            out.append(it)
        else:
            seen[it] += 1
            suffix = f" ({seen[it]})"
            candidate = (it[: MAX_ANSWER_LEN - len(suffix)] + suffix).rstrip()
            out.append(candidate)
    return out


def fetch_menu():
    r = requests.get(MENU_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Find first heading that looks like "Sreda 4.3.2026"
    heading = None
    date_text = None
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        t = clean_text(tag.get_text(" ", strip=True))
        m = DATE_RE.search(t)
        if m:
            heading = tag
            date_text = m.group(0).upper()
            break

    if not date_text:
        # fallback: search whole page text
        page_text = clean_text(soup.get_text("\n"))
        m = DATE_RE.search(page_text)
        if not m:
            die("Ne najdem dnevnih malic (datum) na rimljan.si. (Struktura se je verjetno spremenila.)")
        date_text = m.group(0).upper()

    # Collect <li> items after the heading until next date heading
    items = []
    if heading:
        for el in heading.find_all_next():
            if el.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                t = clean_text(el.get_text(" ", strip=True))
                if DATE_RE.search(t) and el is not heading:
                    break
            if el.name == "li":
                txt = clean_text(el.get_text(" ", strip=True))
                if txt:
                    items.append(txt)
            if len(items) >= 40:
                break

    # fallback: take lines after date in raw text
    if not items:
        lines = [clean_text(x) for x in soup.get_text("\n").splitlines()]
        lines = [x for x in lines if x]
        idx = None
        for i, ln in enumerate(lines):
            if DATE_RE.search(ln):
                idx = i
                break
        if idx is None:
            die("Datum sem našel, jedi pa ne. (Stran je verjetno spremenjena.)")

        for ln in lines[idx + 1 :]:
            if DATE_RE.search(ln):
                break
            if ln.lower().startswith("uporabljamo piškotke"):
                break
            if len(ln) > 2:
                items.append(ln)
            if len(items) >= 40:
                break

    if not items:
        die("Našel sem datum, ne pa jedi. (Stran je verjetno spremenjena.)")

    items = items[:MAX_ANSWERS]
    items = [truncate_to_two_words_if_needed(x) for x in items]
    items = dedupe_with_suffix(items)

    return date_text, items


def post_poll(date_text: str, items: list[str], duration_hours: int = 3):
    payload = {
        "content": f"🍽️ Rimljan malice — {date_text}",
        "poll": {
            "question": {"text": f"Kaj boš jedel danes? ({date_text})"},
            "answers": [{"poll_media": {"text": it}} for it in items],
            "duration": duration_hours,
            "allow_multiselect": False,
            "layout_type": 1,
        },
    }
    discord_request("POST", f"/channels/{CHANNEL_ID}/messages", json_body=payload)


def main():
    if not BOT_TOKEN or not CHANNEL_ID:
        die("Manjka DISCORD_BOT_TOKEN ali DISCORD_CHANNEL_ID (GitHub Secrets).")

    # Run only Mon-Fri, at ~06:10 Europe/Ljubljana.
    # GitHub schedule is UTC, so we run twice and gate by local time window.
    now_local = datetime.now(ZoneInfo("Europe/Ljubljana"))
    if now_local.weekday() >= 5:
        print("Weekend -> skip.")
        return

    print("TEST MODE -> time gate bypassed")

    date_text, items = fetch_menu()
    print("DATE:", date_text)
    print("ITEMS:", items)

    if already_posted_for(date_text):
        print("Already posted for this date -> skip.")
        return

    post_poll(date_text, items, duration_hours=3)
    print("Posted poll.")


if __name__ == "__main__":
    main()
