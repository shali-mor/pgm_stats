# PI Initiative Refresh Scripts

Standalone Python scripts that pull current Jira status for every initiative
on the PI dashboard and patch `pi-dashboard.html` in place. No Claude / MCP
involvement — just `requests`-free stdlib HTTP calls against the Jira REST API.

## Setup

```sh
cd scripts/
cp .env.example .env          # fill in JIRA_EMAIL + JIRA_API_TOKEN
```

Create an API token at <https://id.atlassian.com/manage-profile/security/api-tokens>.

Only stdlib is used — Python 3.10+ is enough, no `pip install` needed.

## Daily refresh

### Option 1 — Refresh button in the browser (recommended)

```sh
python3 scripts/serve.py            # listens on http://127.0.0.1:8765
```

Open <http://127.0.0.1:8765/pi-dashboard.html>. A **↻ Refresh from Jira** button
appears in the top-right of the header. Clicking it:

1. POSTs to `/api/refresh`, which runs `fetch_initiatives.py` + `splice_dashboard.py`
2. On success, reloads the page with a cache-busting query string

The button stays hidden when the page is served from anywhere else
(GitHub Pages, plain `python3 -m http.server`, etc.) — it probes `HEAD /api/refresh`
on load and only un-hides itself when the refresh backend responds.

### Option 2 — Run the scripts directly

```sh
python3 scripts/fetch_initiatives.py    # writes scripts/initiatives.json
python3 scripts/splice_dashboard.py     # patches pi-dashboard.html
```

Or in one shot:

```sh
python3 scripts/fetch_initiatives.py && python3 scripts/splice_dashboard.py
```

## What gets refreshed

- Per-initiative status badge (DONE / IN PROGRESS / IN REVIEW / OPEN / …)
- `row-done` vs `row-incomplete` row class
- `data-planned="yes"` attribute (auto-stripped when item is unplanned — see below)
- Notes cell: explicit override > "Closed YYYY-MM-DD" > "Unplanned — added YYYY-MM-DD"
- Title, h1, and date stamp at the top of `pi-dashboard.html`

## Unplanned detection

An initiative is treated as unplanned if **either**:

1. Jira `customfield_11385` (Planned Work) is `No`, **or**
2. The initiative was created on/after `pi.start` in `pi_config.json`.

## Adding / moving initiatives

Edit `scripts/pi_config.json`:

```jsonc
{
  "tabs": {
    "cloud": {
      "sections": [
        {
          "title": "Customer-Facing Features",
          "badge": "PI 2026.2 deliveries",
          "keys": [
            { "key": "DSA-1062", "label": "Cloud DLP Free Trial — Getting Started" },
            // add / remove / re-order entries here
            { "key": "DSA-XXXX", "label": "...", "note": "optional override" }
          ]
        }
      ]
    }
  }
}
```

The `label` is what shows in the table — keep it short. Add a `note` field to
override the auto-generated Notes cell.

## Rolling to a new PI

Edit `pi.name`, `pi.start`, `pi.end` in `pi_config.json`, then replace the
initiative keys per section. The fetcher and splicer don't care which PI it is.
