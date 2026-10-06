# nomad-status-scrapers

Scheduled scrapers that feed a restaurant live-status dashboard (outlet online/offline on
Zomato and Swiggy, plus menus). This repo holds **only the scripts and their schedules** - it
contains no outlet data, no credentials and no business identifiers. Everything the scripts
need at run time (outlet lists, reference maps, saved sessions) is fetched from the
dashboard's own authenticated API, and secrets live in this repo's encrypted Actions secrets.

| Workflow | Schedule | What it does |
|---|---|---|
| `refresh-atlas-status` | every 5 min | Reads every store's Zomato/Swiggy enabled state from the aggregator's console (real browser, saved session fetched from the dashboard) and posts it to the dashboard |
| `refresh-zomato` | every 10 min | Fetches each outlet's public Zomato page - menu, rating, open/closed - and uploads it |
| `refresh-swiggy` | every 15 min | Same for Swiggy (needs a real browser to pass Swiggy's bot check) |
| `keepalive` | monthly | One commit a month so GitHub doesn't disable the schedules (public repos lose them after 60 days of inactivity) |

## Secrets (Settings -> Secrets and variables -> Actions)

| Secret | What it is |
|---|---|
| `DASHBOARD_URL` | The dashboard's web-app `/exec` URL |
| `DASHBOARD_INGEST_KEY` | Shared secret; must match the `INGEST_KEY` script property of the dashboard project |
| `NOMAD_STATUS_WEBHOOK_URL` | Endpoint of the downstream store-status tracker |
| `NOMAD_STATUS_WEBHOOK_TOKEN` | Token for that endpoint |

## Keep it safe

- **Actions logs and artifacts on a public repo are readable by any GitHub user.** Never print
  a secret, a session or a response body; the scripts log counts and statuses only. Don't add
  `upload-artifact` steps or screenshots.
- No data files belong here. Outlet lists come from the dashboard at run time.
- Manual run: Actions tab -> pick a workflow -> *Run workflow*.
