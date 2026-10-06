#!/usr/bin/env python3
"""Fetch every Nomad outlet x brand Zomato order page, parse menu items and
ratings, and upload the result to the Apps Script dashboard.

Env vars:
  INGEST_URL   Apps Script web app URL (the one ending in /exec)
  INGEST_KEY   shared secret, must match the INGEST_KEY script property
  DRY_RUN=1    fetch and parse but do not upload
  MIN_OK       fraction of outlets that must succeed before uploading (default 0.9)
"""
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CITY_LABELS = {
    "ncr": "Delhi NCR", "mumbai": "Mumbai", "pune": "Pune", "bangalore": "Bengaluru",
    "hyderabad": "Hyderabad", "chennai": "Chennai", "kolkata": "Kolkata", "jaipur": "Jaipur",
    "chandigarh": "Chandigarh", "lucknow": "Lucknow", "dehradun": "Dehradun",
    "ahmedabad": "Ahmedabad", "indore": "Indore", "goa": "Goa", "visakhapatnam": "Visakhapatnam",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
NON_ITEMS = {"Report an error in this listing"}
H4_RE = re.compile(r"<h4[^>]*>([^<]+)</h4>")
SECTION_RE = re.compile(r"<section[^>]*>")
TYPE_RE = re.compile(r'<div type="(veg|non-veg|egg)"')
RATING_RE = re.compile(
    r'aggregate_rating\\?":\\?"([^"\\]*)\\?",\\?"rating_text\\?":\\?"[^"\\]*\\?",'
    r'\\?"rating_subtitle\\?":\\?"[^"\\]*\\?",\\?"rating_color\\?":\\?"[^"\\]*\\?",'
    r'\\?"votes\\?":\\?"?([0-9.,]+K?)\\?"?'
)


def field(page, key):
    m = re.search(r'\\?"' + key + r'\\?":\s*\\?"?([^,}\\"]*)', page)
    return m.group(1).strip() if m else ""


def page_res_id(page):
    """The res_id Zomato itself embeds in the page, as an int (or None)."""
    raw = field(page, "res_id")
    return int(raw) if raw.isdigit() else None


def page_status(page, n_items):
    """-> (state, reason) from the outlet's own Zomato page."""
    if n_items == 0:
        return "closed", "No menu on the Zomato link"
    if field(page, "is_perm_closed") == "true":
        return "closed", "Permanently closed"
    if field(page, "is_temp_closed") == "true":
        return "closed", "Temporarily closed"
    low = field(page, "res_status_text").lower()
    if "clos" in low or "not available" in low or "not accepting" in low:
        return "closed", "Offline now"
    return "live", "Live now"


def city_of(url):
    m = re.match(r"https://www\.zomato\.com/([^/]+)/", url)
    slug = m.group(1) if m else "other"
    return CITY_LABELS.get(slug, slug.title())


def fetch(url):
    base = url.rstrip("/")
    if not base.endswith("/order"):
        base += "/order"
    req = urllib.request.Request(base, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.status, resp.read().decode("utf-8", errors="ignore")


def parse_items(page):
    # Fixed 2026-09-30: this used to call any <h4> within 80 chars of a <section> tag a
    # category and everything else an item - an arbitrary distance guess that misclassifies
    # the very first item under a category as a *second* category whenever there isn't much
    # markup between them (confirmed: a minimal-markup page with one category immediately
    # followed by its first item dropped that item entirely and miscategorized the rest).
    # Zomato's template wraps one category per <section>, so the real structural rule is:
    # the first <h4> after a <section> is that section's category header; every <h4> after
    # it, up to the next <section>, is an item - no distance threshold needed.
    events = sorted(
        [(m.start(), 'section', None) for m in SECTION_RE.finditer(page)] +
        [(m.start(), 'h4', htmlmod.unescape(m.group(1)).strip()) for m in H4_RE.finditer(page)]
    )
    cur_cat, expect_cat, items = "", False, []
    for pos, kind, name in events:
        if kind == 'section':
            expect_cat = True
            continue
        if name in NON_ITEMS:
            continue
        if expect_cat:
            cur_cat = name
            expect_cat = False
        else:
            items.append((pos, name, cur_cat))
    types = [m.group(1) for m in TYPE_RE.finditer(page)]
    out = []
    for i, (pos, name, cat) in enumerate(items):
        out.append((cat.strip().rstrip(".").strip() or "Uncategorized", name,
                    types[i] if i < len(types) else "na"))
    return out


def items_types_mismatch(page, n_items):
    """Diagnostic only, not used for the actual data: TYPE_RE count vs item count.
    parse_items() zips types[] to items[] by position, not by any explicit link between
    a given <h4> and its veg/non-veg marker. If the page ever has a stray type-div or h4
    unrelated to this outlet's own menu (a "you might also like" widget, etc.), every
    later item's type silently shifts. A mismatch here doesn't prove misassignment
    (categories can also lack a marker) but is the cheapest signal that this assumption
    may be breaking somewhere - see the DIAGNOSTIC line in main()'s output."""
    return n_items != len(TYPE_RE.findall(page))


def work(o):
    err = ""
    for attempt in range(4):
        try:
            status, page = fetch(o["url"])
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            m = RATING_RE.search(page)
            rating = (m.group(1) or None, m.group(2).rstrip(",")) if m else (None, None)
            items = parse_items(page)
            type_mismatch = items_types_mismatch(page, len(items))
            # Outlets are matched by name/URL slug, which is ambiguous (Zomato
            # slugs get reused or redirected); res_id is the one unambiguous key.
            # Only trust this page's status as "verified" when the res_id the
            # page itself reports matches the res_id we recorded for this
            # outlet+brand (from Atlas / the outlet-links sheet) - otherwise the
            # URL may have quietly started pointing somewhere else.
            expected = o.get("res_id")
            verified = expected is not None and page_res_id(page) == expected
            return o, items, rating, None, page_status(page, len(items)), verified, type_mismatch
        except Exception as e:  # noqa: BLE001
            err = str(e)
            time.sleep((12 if "429" in err else 2) * (attempt + 1))
    return o, [], (None, None), err, ("closed", "Could not read the page"), False, False


def load_outlets():
    """Outlet list (brand, outlet, url, res_id). Fetched from the dashboard's zomato_links
    platform so this script - and a public scraper repo - carries no outlet data; falls back
    to a local zomato_outlet_links.json when present (older setups / offline runs)."""
    url, key = os.environ.get("INGEST_URL"), os.environ.get("INGEST_KEY")
    last = None
    if url and key:
        body = json.dumps({"key": key, "platform": "zomato_links"}).encode()
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "text/plain"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    reply = json.loads(resp.read().decode())
                if reply.get("ok") and reply.get("links"):
                    print(f"outlet list: {len(reply['links'])} from the dashboard")
                    return reply["links"]
                last = f"unexpected reply (ok={reply.get('ok')}, error={reply.get('error')})"
            except Exception as e:  # noqa: BLE001
                last = type(e).__name__ + ": " + str(e)[:120]
            time.sleep(5 * (attempt + 1))
        print("could not fetch the outlet list from the dashboard:", last)
    local = os.path.join(os.path.dirname(__file__), "zomato_outlet_links.json")
    if os.path.exists(local):
        print("using the local zomato_outlet_links.json")
        return json.load(open(local))
    raise SystemExit("no outlet list: the dashboard fetch failed and there is no local zomato_outlet_links.json")


def main():
    outlets = load_outlets()
    started = time.time()
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(work, outlets))

    menu, ratings, statuses, ok, failed = [], [], [], 0, []
    mismatched = []
    for o, items, (rating, votes), err, status, verified, type_mismatch in results:
        if err:
            failed.append(f"{o['brand']}/{o['outlet']}: {err}")
            continue
        ok += 1
        statuses.append([o["brand"], o["outlet"], status[0], status[1], "Y" if verified else "N"])
        if type_mismatch and items:
            mismatched.append(f"{o['brand']}/{o['outlet']}")
        c = city_of(o["url"])
        for cat, name, typ in items:
            menu.append([o["brand"], o["outlet"], c, cat, name, typ])
        if rating:
            ratings.append([o["brand"], o["outlet"], rating, votes or ""])

    frac = ok / len(outlets)
    live_n = sum(1 for s in statuses if s[2] == "live")
    verified_n = sum(1 for s in statuses if s[4] == "Y")
    print(f"fetched {ok}/{len(outlets)} outlets ({frac:.0%}) in {time.time()-started:.0f}s; "
          f"{len(menu)} menu rows, {len(ratings)} ratings; {live_n} live / {len(statuses)-live_n} closed; "
          f"{verified_n}/{len(statuses)} res_id-verified")
    if mismatched:
        print(f"DIAGNOSTIC: {len(mismatched)}/{ok} outlets have a veg/non-veg marker count that "
              f"doesn't match their item count (positional item<->type zip may be misaligned): "
              + ", ".join(mismatched[:15]) + (" ..." if len(mismatched) > 15 else ""))
    else:
        print("DIAGNOSTIC: item count matched veg/non-veg marker count on every outlet with items")
    for f in failed[:10]:
        print("  FAILED", f)

    if os.environ.get("DRY_RUN"):
        print("DRY_RUN set: not uploading")
        return 0
    if frac < float(os.environ.get("MIN_OK", "0.9")):
        print("Too many failures; not uploading so existing dashboard data is kept.")
        return 1
    min_rows = int(os.environ.get("MIN_ROWS", "10000"))
    if len(menu) < min_rows:
        print(f"Only {len(menu)} menu rows (expected at least {min_rows}); the site may be "
              "serving empty pages. Not uploading so existing dashboard data is kept.")
        return 1

    url, key = os.environ["INGEST_URL"], os.environ["INGEST_KEY"]
    body = json.dumps({"key": key, "platform": "zomato", "menu": menu, "ratings": ratings,
                       "status": statuses}).encode()
    reply = ""
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "text/plain"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                reply = resp.read().decode()
            break
        except Exception as e:  # noqa: BLE001
            print(f"upload attempt {attempt + 1} failed: {e}")
            if attempt == 3:
                raise
            time.sleep(20 * (attempt + 1))
    print("upload reply:", reply[:300])
    return 0 if '"ok":true' in reply.replace(" ", "") else 1


if __name__ == "__main__":
    sys.exit(main())
