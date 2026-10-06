"""
Atlas live-status refresher.
-----------------------------
Logs into UrbanPiper Atlas with a real browser (Playwright), scrapes the
/locations page for every store's actual current enabled/disabled state on
Zomato and Swiggy, and posts the results into the SAME webhook the Nomad
Store Status Tracker Apps Script already listens on (doPost -> applyBatch).

This exists because the Apps Script's own auto-open path (UrlFetchApp calls
straight to UrbanPiper's API) is blocked by a Google Workspace policy that
won't grant the external-request OAuth scope. This script runs outside
Apps Script entirely -- a normal Python process in GitHub Actions -- so it
never touches that restriction. It also gives TRUE current state (scraped
from the same page a human would look at), not a webhook's last-known state.

Why Playwright and not requests+BeautifulSoup: Atlas is a client-rendered
React app; the status dots don't exist until JS builds the table. Swiggy's
own pages block plain HTTP clients too (see refresh_swiggy.py), so the whole
cloud-refresh setup already assumes a real browser is needed.

Secrets (repo Settings -> Secrets and variables -> Actions):
  WEBHOOK_URL              -- the Nomad Store Status Apps Script /exec URL. Doubles as
                              where the saved session is fetched from (see
                              load_storage_state) - do NOT reuse INGEST_URL here, that
                              points at a different, standalone Apps Script project.
  DASHBOARD_INGEST_KEY     -- PREFERRED (added 2026-09-30). Must match the *main
                              dashboard's* INGEST_KEY script property (Apps Script
                              editor -> Project Settings -> Script Properties - NOT the
                              same value as the standalone Swiggy-reviews project's
                              INGEST_KEY, even though the property name is the same in
                              both). Session is produced by running
                              save_atlas_session.py locally (real browser, you log in)
                              and uploaded straight to the dashboard - see that file.
  ATLAS_STORAGE_STATE_B64  -- fallback only: a small/trimmed session pasted in
                              directly. A *working* full session runs well past
                              GitHub's 48KB secret cap, so this path is mostly useful
                              for testing, not day-to-day use.
  ATLAS_EMAIL              -- fallback only: UrbanPiper Atlas login email
  ATLAS_PASSWORD           -- fallback only: UrbanPiper Atlas login password
  ATLAS_BUSINESS_NAME      -- fallback only: exact label of the business to
                              select after login, e.g. "Nomad by UrbanPiper"
  WEBHOOK_TOKEN            -- optional: matches WEBHOOK_TOKEN script property,
                              appended as ?token=... if the doPost check is on

WHY THE FALLBACK LOGIN KEEPS FAILING: repeated live runs confirmed a fresh
automated login gets silently bounced back to login.urbanpiper.com shortly
after reaching /locations -- UrbanPiper's session detection rejecting the
headless browser, not a timing bug. This is the exact same problem
save_swiggy_session.py exists for. Run save_atlas_session.py locally (real
browser, you log in by hand) and set ATLAS_STORAGE_STATE_B64 instead of
relying on ATLAS_EMAIL/PASSWORD.
"""
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

from playwright.sync_api import sync_playwright

ATLAS_LOGIN_URL = "https://login.urbanpiper.com/login/email-mobile/?redirect=atlas"
ATLAS_LOCATIONS_URL = "https://atlas.urbanpiper.com/locations"
PLATFORMS = {"zomato", "swiggy"}  # matches PLATFORMS in the Apps Script

# --- selectors that need verifying against the real login form, see docstring ---
LOGIN_EMAIL_SEL = 'input[type="email"], input[type="text"][name*="mail" i], input[placeholder*="mail" i]'
LOGIN_CONTINUE_SEL = 'button:has-text("Continue"), button:has-text("Next"), button[type="submit"]'
LOGIN_PASSWORD_SEL = 'input[type="password"]'
LOGIN_SUBMIT_SEL = 'button:has-text("Login"), button:has-text("Log in"), button:has-text("Sign in"), button[type="submit"]'
# ---------------------------------------------------------------------------

HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def login(page, email, password, business_name):
    page.goto(ATLAS_LOGIN_URL, wait_until="networkidle")

    # Some UrbanPiper accounts get the email/password combined on one screen,
    # others split email -> continue -> password. Handle both.
    if page.locator(LOGIN_EMAIL_SEL).count():
        page.locator(LOGIN_EMAIL_SEL).first.fill(email)
        if page.locator(LOGIN_CONTINUE_SEL).count():
            page.locator(LOGIN_CONTINUE_SEL).first.click()
            page.wait_for_timeout(1500)

    if page.locator(LOGIN_PASSWORD_SEL).count():
        page.locator(LOGIN_PASSWORD_SEL).first.fill(password)
        page.locator(LOGIN_SUBMIT_SEL).first.click()

    page.wait_for_load_state("networkidle", timeout=30000)

    # Business selector page (account has multiple businesses).
    if "business" in page.url:
        page.wait_for_timeout(1000)

        def scan():
            els = page.locator('button, [role="option"], [role="button"], li')
            n = els.count()
            return els, [els.nth(i).inner_text().strip() for i in range(n)]

        wanted = business_name.strip().lower()

        def find_match(texts):
            for i, t in enumerate(texts):
                if wanted and wanted in t.strip().lower():
                    return i
            return None

        els, texts = scan()
        log(f"login: business page (1st look) has {len(texts)} candidate(s): {texts}")
        idx = find_match(texts)

        # First look often only shows a status-filter tab (e.g. "Active"), not
        # the businesses themselves -- click it, and/or use the search box,
        # then re-scan before giving up.
        if idx is None:
            active_tab = page.locator('button:has-text("Active")')
            if active_tab.count():
                active_tab.first.click()
                page.wait_for_timeout(1000)
                els, texts = scan()
                log(f"login: business page (after 'Active' tab) has {len(texts)} candidate(s): {texts}")
                idx = find_match(texts)

        if idx is None:
            search_box = page.locator('input[type="text"], input[type="search"]')
            if search_box.count():
                search_box.first.fill(business_name.strip())
                page.wait_for_timeout(1200)
                els, texts = scan()
                log(f"login: business page (after search) has {len(texts)} candidate(s): {texts}")
                idx = find_match(texts)

        if idx is None:
            raise RuntimeError(
                f"Business '{business_name}' not found among any candidates logged above. "
                "Check ATLAS_BUSINESS_NAME (no extra whitespace) matches one of them."
            )
        els.nth(idx).click()
        # networkidle alone can settle before the SPA's post-click auth
        # handshake actually persists the session -- wait for the URL to
        # actually leave the login domain first, then let it settle, then
        # give the cookie/session write a moment before navigating away.
        try:
            page.wait_for_url(lambda u: "login.urbanpiper.com" not in u, timeout=30000)
        except Exception as e:
            log(f"login: URL never left login.urbanpiper.com after clicking business: {e}")
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(2000)
        log(f"login: post-business-click URL is {page.url}")


def set_max_page_size(page):
    """Best-effort: bump 'results per page' so fewer page loads are needed."""
    try:
        page.locator('text=results per page').first.wait_for(timeout=5000)
        size_dropdown = page.locator('div:has-text("results per page") >> xpath=preceding-sibling::*[1]')
        if size_dropdown.count():
            size_dropdown.first.click()
            page.wait_for_timeout(300)
            option = page.locator('text="100"').first
            if option.count():
                option.click()
                page.wait_for_timeout(1000)
    except Exception as e:
        log("set_max_page_size: non-fatal, continuing with default page size:", e)


def scrape_page(page):
    """Pull {ref_id: {platform: 'enabled'|'disabled'}} for every row on the current page."""
    rows = page.eval_on_selector_all(
        "tr[data-row-key]",
        """
        (trs) => trs.map(tr => {
          const posIdEl = tr.querySelector('.location_id .text--light');
          const posId = posIdEl ? posIdEl.textContent.replace('POS ID: ', '').trim() : null;
          const platforms = [];
          tr.querySelectorAll('.platform').forEach(p => {
            const img = p.querySelector('img');
            const stateEl = p.querySelector('.state');
            if (!img || !stateEl) return;
            const src = img.getAttribute('src') || '';
            // Root-caused 2026-09-30 (live icon src dump): the real icon is
            // ".../zomato2.png" - the old regex's greedy [a-z0-9]+ swallowed the
            // trailing "2" into the name itself ("zomato2"), which then matched
            // nothing in PLATFORMS and got silently dropped on every single row.
            // Letters-only name, digit(s) captured separately and ignored.
            const m = src.match(/platforms\\/([a-z]+)\\d*\\.png/i);
            const name = m ? m[1].toLowerCase() : null;
            const enabled = stateEl.classList.contains('enabled');
            if (name) platforms.push({ name, enabled });
          });
          return { posId, platforms };
        })
        """,
    )
    return [r for r in rows if r["posId"]]


def go_next_page(page):
    """Confirmed 2026-09-30 via log_pagination_dom: there is no Next button at all -
    pagination is a row of numbered divs (.at-paginator-item.paginator-number, one
    with an extra 'active' class), e.g. "1 2 3 ... 53" for 524 stores at 10/page.
    Read the active page number and click the div for current+1."""
    try:
        current_text = page.eval_on_selector(".at-paginator-item.active", "el => el.textContent.trim()")
    except Exception:
        return False
    try:
        next_num = int(current_text) + 1
    except (TypeError, ValueError):
        return False
    clicked = page.evaluate(
        """(nextNum) => {
            const items = document.querySelectorAll('.at-paginator-item.paginator-number');
            for (const el of items) {
                if (el.textContent.trim() === String(nextNum)) { el.click(); return true; }
            }
            return false;
        }""",
        next_num,
    )
    if clicked:
        page.wait_for_timeout(1200)
    return clicked


def scrape_all_locations(page):
    page.goto(ATLAS_LOCATIONS_URL, wait_until="networkidle")
    if "login.urbanpiper.com" in page.url:
        log("scrape_all_locations: bounced back to login, retrying once after a pause")
        page.wait_for_timeout(3000)
        page.goto(ATLAS_LOCATIONS_URL, wait_until="networkidle")
        if "login.urbanpiper.com" in page.url:
            raise RuntimeError(
                "Still on login.urbanpiper.com after retry -- the session from "
                "login() did not carry over. Check ATLAS_EMAIL/PASSWORD are correct "
                "and the account isn't hitting a 2FA/CAPTCHA challenge."
            )
    # Confirmed 2026-09-30: with a genuinely correct, freshly-fetched session (auth is not
    # the issue here), Atlas's own React app still sometimes renders a totally blank page
    # under automation - roughly half of recent runs, same URL/title/session each time.
    # One reload before giving up rides out that intermittent render failure.
    try:
        page.wait_for_selector("tr[data-row-key]", timeout=20000)
    except Exception:
        log("scrape_all_locations: locations table did not render, reloading once")
        page.reload(wait_until="networkidle")
        page.wait_for_selector("tr[data-row-key]", timeout=20000)
    set_max_page_size(page)

    all_rows = []
    seen_pages = 0
    while True:
        page_rows = scrape_page(page)
        if not page_rows:
            # The same intermittent blank-render Atlas is known to do on the
            # first load (see above) can also hit mid-pagination -- a page
            # transition that silently renders zero rows instead of erroring.
            # NOT a page.reload() here: pagination is client-side React state
            # tied to this page instance (the URL never changes), so a reload
            # would reset back to page 1 and desync seen_pages from the
            # paginator's actual position. Just wait and rescan instead.
            log(f"scrape_all_locations: page {seen_pages + 1} rendered 0 rows, waiting and rescanning")
            page.wait_for_timeout(2000)
            try:
                page.wait_for_selector("tr[data-row-key]", timeout=15000)
            except Exception:
                log(f"scrape_all_locations: page {seen_pages + 1} still empty, moving on")
            page_rows = scrape_page(page)
        all_rows.extend(page_rows)
        seen_pages += 1
        if seen_pages > 200:  # sanity guard, ~524 stores / 10 per page = ~53 pages worst case
            log("scrape_all_locations: page guard tripped, stopping")
            break
        if not go_next_page(page):
            break
    log(f"scrape_all_locations: {seen_pages} page(s), {len(all_rows)} row(s), "
        f"{len(set(r['posId'] for r in all_rows))} distinct posId(s)")
    return all_rows


def build_events(rows):
    now_ms = int(time.time() * 1000)
    events = []
    for r in rows:
        ref = r["posId"]
        for p in r["platforms"]:
            if p["name"] not in PLATFORMS:
                continue
            events.append({
                "location_ref_id": ref,
                "platform": p["name"],
                "action": "enable" if p["enabled"] else "disable",
                "action_src": "atlas-scrape",
                "ts_utc": now_ms,
            })
    return events


def fetch_location_ref_map(dashboard_url, dashboard_key):
    """{ref_id: {"store": ..., "brand": ...}} from the main dashboard's own
    LOCATION_REF_MAP_, via the location_ref_map_config platform it already exposes."""
    body = json.dumps({"key": dashboard_key, "platform": "location_ref_map_config"}).encode()
    req = urllib.request.Request(dashboard_url, data=body, headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        reply = json.loads(resp.read().decode())
    if not reply.get("ok"):
        raise RuntimeError(f"location_ref_map_config rejected: {reply}")
    return reply["map"]


def post_atlas_status_to_dashboard(dashboard_url, dashboard_key, rows, ref_map):
    """Direct feed for the main dashboard's own AtlasStatus sheet (added 2026-09-30) -
    post_events() above only ever reached a separate, standalone 'Nomad Live Store
    Status' project's own spreadsheet, which this dashboard's rec()/atlasStatusState_()
    never reads from. Without this, the dashboard's 'verified via Atlas' bucket runs on
    whatever old data got in there some other way and just goes stale (confirmed live:
    a fully successful scrape+post to the old target left the dashboard's Atlas bucket
    empty, falling back to a less trustworthy source). Filtered to the ~60 Traveller
    Series refs LOCATION_REF_MAP_ actually knows about, out of Atlas's 524 total stores.
    """
    by_platform = {"Zomato": [], "Swiggy": []}
    label = {"zomato": "Zomato", "swiggy": "Swiggy"}
    matched = 0
    for r in rows:
        loc = ref_map.get(r["posId"])
        if not loc:
            continue
        for p in r["platforms"]:
            platform = label.get(p["name"])
            if not platform:
                continue
            state = "live" if p["enabled"] else "closed"
            text = ("Online" if p["enabled"] else "Offline") + " per Atlas scrape"
            by_platform[platform].append([loc["brand"], loc["store"], r["posId"], state, text])
            matched += 1
    if not matched:
        log("post_atlas_status_to_dashboard: no scraped rows matched LOCATION_REF_MAP_, nothing to post")
        return
    body = json.dumps({"key": dashboard_key, "platform": "atlas_status", "byPlatform": by_platform}).encode()
    req = urllib.request.Request(dashboard_url, data=body, headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        reply = json.loads(resp.read().decode())
    log(f"post_atlas_status_to_dashboard: matched {matched} platform rows, dashboard replied: {reply}")


def post_events(webhook_url, token, events):
    if not events:
        log("post_events: nothing to send")
        return
    url = webhook_url.strip()
    if token:
        # A stray space/newline in the WEBHOOK_TOKEN secret (e.g. from how it was
        # originally copy-pasted in) breaks http.client's URL validation outright
        # ("URL can't contain control characters") - strip and percent-encode it
        # rather than assume it's already URL-safe.
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={urllib.parse.quote(token.strip(), safe='')}"

    CHUNK = 200  # keep each POST body well under Apps Script's doPost size cap
    for i in range(0, len(events), CHUNK):
        chunk = events[i:i + CHUNK]
        body = json.dumps(chunk).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                      headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                log(f"posted {len(chunk)} events, response: {resp.status}")
        except urllib.error.URLError as e:
            log(f"FAILED to post chunk starting at {i}: {e}")


def load_storage_state():
    """Returns a Playwright storage_state dict, or None.

    Preferred source (2026-09-30): fetched from the dashboard's own Apps Script, which
    stores it chunked across a Sheet - a real session's localStorage runs 100KB+, well
    past GitHub Actions' 48KB per-secret cap, so ATLAS_STORAGE_STATE_B64 can only ever
    hold a trimmed-down session that the UrbanPiper app renders blank for. Kept as a
    fallback for a small/trimmed session someone pastes in directly.

    Root-caused 2026-09-30 (after a long detour chasing phantom "transient Google
    flakiness"): this used to reuse INGEST_URL/INGEST_KEY, the same secrets
    scrape_swiggy_reviews.py uses - but those point at a completely separate,
    standalone Apps Script project (SwiggyReviews_standalone_deployed.gs) that only
    handles swiggy_reviews/_error and answers everything else with
    {ok:false, error:"unknown platform"}. Then tried WEBHOOK_URL on the (wrong)
    assumption that "the Nomad Store Status Apps Script /exec URL" meant the main
    dashboard - that got HTTP 403, meaning it's very likely yet a *third* distinct
    deployment (a dedicated store-status webhook receiver). Rather than guess a
    fourth time, DASHBOARD_URL is its own explicit secret, set to the exact main
    dashboard URL this whole cloud-refresh investigation has verified directly and
    repeatedly all session (clasp deployments, browser checks, manual curl/python
    tests) - no more inferring it from a same-project secret meant for something else.
    """
    dashboard_url = os.environ.get("DASHBOARD_URL")
    dashboard_key = os.environ.get("DASHBOARD_INGEST_KEY")
    if dashboard_url and dashboard_key:
        last_err = None
        attempts = 3
        for attempt in range(attempts):
            try:
                body = json.dumps({"key": dashboard_key, "platform": "atlas_session_fetch"}).encode()
                req = urllib.request.Request(dashboard_url, data=body, headers={"Content-Type": "text/plain"})
                with urllib.request.urlopen(req, timeout=45) as resp:
                    reply = json.loads(resp.read().decode())
                if not reply.get("ok"):
                    last_err = reply
                    log(f"load_storage_state: dashboard rejected the fetch: {reply}")
                    break  # a real auth/logic error, not worth retrying blindly
                b64 = reply.get("b64", "")
                if b64:
                    log(f"load_storage_state: fetched a session from the dashboard ({len(b64)} b64 chars)")
                    return json.loads(base64.b64decode(b64).decode())
                last_err = "dashboard has no session saved yet"
                log("load_storage_state: dashboard has no session saved yet")
                break
            except Exception as e:
                last_err = e
                log(f"load_storage_state: fetch attempt {attempt + 1}/{attempts} failed: {e}")
                if attempt < attempts - 1:
                    time.sleep(8 * (attempt + 1))
        else:
            log(f"load_storage_state: giving up on the dashboard after {attempts} attempts, falling back: {last_err}")

    b64 = os.environ.get("ATLAS_STORAGE_STATE_B64", "")
    if not b64:
        return None
    try:
        return json.loads(base64.b64decode(b64).decode())
    except Exception as e:
        log(f"load_storage_state: could not decode ATLAS_STORAGE_STATE_B64, ignoring it: {e}")
        return None


def main():
    webhook_url = os.environ["WEBHOOK_URL"]
    webhook_token = os.environ.get("WEBHOOK_TOKEN", "")
    storage_state = load_storage_state()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(storage_state=storage_state) if storage_state else browser.new_context()
        page = context.new_page()
        try:
            if storage_state:
                # Preferred path: reuse a session saved by save_atlas_session.py.
                # Fresh automated logins get silently bounced back to the login
                # page by UrbanPiper's session detection (same reason
                # save_swiggy_session.py exists) -- a real human-completed
                # session avoids that entirely.
                log("main: using saved session (see load_storage_state log line above for source)")
            else:
                email = os.environ.get("ATLAS_EMAIL")
                password = os.environ.get("ATLAS_PASSWORD")
                business_name = os.environ.get("ATLAS_BUSINESS_NAME", "Nomad by UrbanPiper")
                if not email or not password:
                    raise RuntimeError(
                        "Neither ATLAS_STORAGE_STATE_B64 nor ATLAS_EMAIL/ATLAS_PASSWORD are set."
                    )
                log("main: no saved session found, falling back to a fresh login "
                    "(run save_atlas_session.py locally if this keeps getting bounced back to login)")
                login(page, email, password, business_name)
            try:
                rows = scrape_all_locations(page)
            except Exception:
                try:
                    # URL + title only. The screenshot and page-text dump that used to be
                    # here showed business names/outlet lists - fine in a private repo, but
                    # public Actions logs and artifacts are readable by any GitHub user.
                    log(f"main: failure diagnostics - url={page.url} title={page.title()!r}")
                except Exception as diag_e:
                    log(f"main: could not capture failure diagnostics: {diag_e}")
                raise
        finally:
            browser.close()

    log(f"scraped {len(rows)} location rows")
    events = build_events(rows)
    log(f"built {len(events)} zomato/swiggy events")
    post_events(webhook_url, webhook_token, events)

    dashboard_url = os.environ.get("DASHBOARD_URL")
    dashboard_key = os.environ.get("DASHBOARD_INGEST_KEY")
    if dashboard_url and dashboard_key:
        try:
            ref_map = fetch_location_ref_map(dashboard_url, dashboard_key)
            post_atlas_status_to_dashboard(dashboard_url, dashboard_key, rows, ref_map)
        except Exception as e:
            log(f"post_atlas_status_to_dashboard: failed, dashboard's Atlas bucket will go stale: {e}")
    else:
        log("post_atlas_status_to_dashboard: DASHBOARD_URL/DASHBOARD_INGEST_KEY not set, skipping")


if __name__ == "__main__":
    main()
