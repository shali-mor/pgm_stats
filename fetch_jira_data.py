#!/usr/bin/env python3
"""
fetch_jira_data.py — Fetch Jira data and update DLP Program dashboards.

Updates three HTML files in-place:
  escalation-dashboard.html  — cloudData + onpremData arrays
  stalled.html               — ISSUES array
  pi-dashboard.html          — individual issue status badges

Configuration (env vars or .env file in same directory):
  JIRA_URL             https://forcepoint.atlassian.net
  JIRA_EMAIL           your-email@forcepoint.com
  JIRA_TOKEN           your Atlassian API token
  JIRA_CUSTOMER_FIELD  custom field ID for customer name (default: customfield_10400)
  STALE_DAYS           issues not updated in N days are "stalled" (default: 30)

Usage:
  python3 fetch_jira_data.py [--dry-run] [--skip-escalation] [--skip-stalled] [--skip-pi] [--list-fields]

Options:
  --dry-run          Print what would be written without modifying any files
  --skip-escalation  Skip updating escalation-dashboard.html
  --skip-stalled     Skip updating stalled.html
  --skip-pi          Skip updating pi-dashboard.html status badges
  --list-fields      Print all Jira custom field IDs and exit
"""

import os, sys, re, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote
from urllib.error import HTTPError
from base64 import b64encode

# ─── Configuration ────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent


def load_dotenv():
    """Load .env file from script directory into os.environ."""
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_dotenv()

JIRA_URL = os.environ.get("JIRA_URL", "https://forcepoint.atlassian.net").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN = os.environ.get("JIRA_TOKEN", "")
CUSTOMER_FIELD = os.environ.get("JIRA_CUSTOMER_FIELD", "customfield_10400")
STALE_DAYS = int(os.environ.get("STALE_DAYS", "30"))

# Assignee "Last, First" → team lead name (edit as needed)
TEAM_MAP = {
    "Srur, Saar":           "Saar Srur",
    "Osadchy, Victor":      "Saar Srur",
    "Hasson, Itzhak":       "Itzhak Hasson",
    "Kekatpure, Vineet":    "Itzhak Hasson",
    "Ron, Ayval":           "Ron, Ayval",
    "Simhi, Ohad":          "Ron Direct",
    "Hazan, Shahal":        "Hazan, Shahal",
    "Jivan Joshi, Prasad":  "Hazan, Shahal",
    "Lev, Tal":             "Hazan, Shahal",
    "Margolin, Shimon":     "Hazan, Shahal",
    "Shafrir, Shira":       "Hazan, Shahal",
    "Sastiel, Gadi":        "Sastiel, Gadi",
}

# Jira status → (badge_class, display_text, row_class)
# Rows with these badge texts are editorial annotations — skip auto-update
SKIP_PI_BADGES = {"DEFERRED TO PI 2026.2", "DROPPED FROM PI"}

PI_STATUS_MAP = {
    "Done":                  ("status-done",     "DONE",              "row-done"),
    "Closed":                ("status-done",     "DONE",              "row-done"),
    "Resolved":              ("status-done",     "DONE",              "row-done"),
    "Fixed":                 ("status-done",     "DONE",              "row-done"),
    "In Review":             ("status-review",   "IN REVIEW",         "row-incomplete"),
    "In Code Review":        ("status-review",   "IN REVIEW",         "row-incomplete"),
    "Code Review":           ("status-review",   "IN REVIEW",         "row-incomplete"),
    "Ready for QA":          ("status-review",   "READY FOR QA",      "row-incomplete"),
    "In Progress":           ("status-progress", "IN PROGRESS",       "row-incomplete"),
    "In Development":        ("status-progress", "IN PROGRESS",       "row-incomplete"),
    "Open":                  ("status-todo",     "OPEN",              "row-incomplete"),
    "To Do":                 ("status-todo",     "TO DO",             "row-incomplete"),
    "In Refinement":         ("status-todo",     "IN REFINEMENT",     "row-incomplete"),
    "Ready to Implement":    ("status-todo",     "READY TO IMPLEMENT","row-incomplete"),
    "Backlog":               ("status-todo",     "BACKLOG",           "row-incomplete"),
    "Blocked":               ("status-blocked",  "BLOCKED",           "row-incomplete"),
}

# ─── Jira API Helpers ─────────────────────────────────────────────────────────

def _auth_header():
    if not JIRA_EMAIL or not JIRA_TOKEN:
        print("ERROR: Set JIRA_EMAIL and JIRA_TOKEN (env vars or .env file).", file=sys.stderr)
        sys.exit(1)
    creds = b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


def jira_get(path, params=None):
    """GET from Jira REST API, return parsed JSON."""
    url = f"{JIRA_URL}/rest/api/3/{path}"
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers=_auth_header())
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"HTTP {e.code} on GET {path}: {body[:300]}", file=sys.stderr)
        raise


def jira_search(jql, fields, max_results=500):
    """Paginated JQL search, returns list of issue dicts."""
    results = []
    start = 0
    page_size = 100
    while True:
        data = jira_get("search", {
            "jql":        jql,
            "fields":     ",".join(fields),
            "startAt":    start,
            "maxResults": page_size,
        })
        issues = data.get("issues", [])
        results.extend(issues)
        start += len(issues)
        if start >= data.get("total", 0) or not issues:
            break
        if start >= max_results:
            print(f"  Capped at {max_results} results — consider narrowing JQL.", file=sys.stderr)
            break
    return results


def batch_fetch_statuses(keys):
    """Fetch status for a list of issue keys. Returns {key: status_name}."""
    if not keys:
        return {}
    result = {}
    chunk_size = 100
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i:i + chunk_size]
        jql = f"key in ({','.join(chunk)})"
        issues = jira_search(jql, ["status"], max_results=len(chunk) + 10)
        for issue in issues:
            result[issue["key"]] = issue["fields"]["status"]["name"]
    return result


def list_custom_fields():
    """Print all custom field IDs and names (for discovery)."""
    fields = jira_get("field")
    custom = [(f["id"], f["name"]) for f in fields if f["id"].startswith("customfield_")]
    custom.sort(key=lambda x: x[0])
    print(f"\n{'Field ID':<30} Name")
    print("-" * 60)
    for fid, name in custom:
        print(f"{fid:<30} {name}")
    print(f"\nTotal: {len(custom)} custom fields")

# ─── Field Extractors ─────────────────────────────────────────────────────────

def get_field(fields, *keys, default=None):
    """Safely traverse nested dicts."""
    cur = fields
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def extract_assignee(fields):
    name = get_field(fields, "assignee", "displayName") or ""
    # Jira displayName is usually "First Last"; convert to "Last, First" if no comma
    if name and "," not in name:
        parts = name.split()
        if len(parts) >= 2:
            name = f"{parts[-1]}, {' '.join(parts[:-1])}"
    return name or "Unassigned"


def extract_customer(fields):
    """Try custom field, fall back to parsing [Customer] from summary."""
    val = get_field(fields, CUSTOMER_FIELD)
    if val:
        # May be a string, list, or object
        if isinstance(val, str):
            return val.strip() or "—"
        if isinstance(val, list) and val:
            first = val[0]
            return (first.get("value") or first.get("name") or str(first)).strip() or "—"
        if isinstance(val, dict):
            return (val.get("value") or val.get("name") or "—").strip()

    # Fallback: parse [Customer Name] from summary. Summaries may carry one or
    # more leading [..] tags (e.g. "[Customer Bug] [BBVA] …" or "[FE] [Acme] …");
    # walk them and return the first that isn't a generic/category label.
    summary = get_field(fields, "summary") or ""
    generic = {"POC", "ENG", "INTERNAL", "REGRESSION", "DOCS", "HOTFIX",
               "ENTERPRISE", "FEATURE", "BUG", "CUSTOMER BUG", "BE", "FE",
               "P1", "P2", "P3", "BLOCKER"}
    # Drop a leading "FE | " / "BE | " style prefix when a [tag] follows it.
    summary = re.sub(r"^\s*[A-Za-z0-9]+\s*\|\s*(?=\[)", "", summary)
    leading = re.match(r"^\s*((?:\[[^\]]+\]\s*)+)", summary)
    if leading:
        for tag in re.findall(r"\[([^\]]+)\]", leading.group(1)):
            tag = tag.strip()
            # Skip generic category labels and bare version tags (e.g. "10.2", "v10.5").
            if tag.upper() in generic or re.fullmatch(r"v?\d+(?:\.\d+)*", tag, re.I):
                continue
            return tag
    return "—"


def extract_status(fields):
    return get_field(fields, "status", "name") or "Unknown"


def extract_priority(fields):
    return get_field(fields, "priority", "name") or "Major"


def date_str(iso):
    """Return ISO datetime string as-is (keep timezone info)."""
    return iso or None


def short_date(iso):
    """Return YYYY-MM-DD from an ISO datetime or date string."""
    if not iso:
        return ""
    return iso[:10]

# ─── Escalation Dashboard ─────────────────────────────────────────────────────

ESCALATION_FIELDS = [
    "summary", "priority", "status", "created", "resolutiondate",
    "assignee", "project", CUSTOMER_FIELD,
]

# Dashboard scope = Escalations PLUS customer-reported bugs (cf[10846] = "Customer"),
# limited to the last 6 months (created >= -26w).
_ESC_TYPES = '(issuetype = Escalation OR (issuetype = Bug AND cf[10846] = "Customer"))'
ESC_JQL_CLOUD  = f"project in (NEO, DPS) AND {_ESC_TYPES} AND created >= -26w ORDER BY created DESC"
ESC_JQL_ONPREM = f"project in (DLP, FSM) AND {_ESC_TYPES} AND created >= -26w ORDER BY created DESC"


def escalation_record(issue):
    """Transform a raw Jira issue dict into a dashboard record."""
    f = issue["fields"]
    return {
        "key":            issue["key"],
        "customer":       extract_customer(f),
        "summary":        get_field(f, "summary") or "",
        "priority":       extract_priority(f),
        "status":         extract_status(f),
        "created":        date_str(get_field(f, "created")),
        "resolutiondate": date_str(get_field(f, "resolutiondate")),
        "assignee":       extract_assignee(f),
        "project":        get_field(f, "project", "key") or issue["key"].split("-")[0],
    }


def fetch_escalation_data(jql, label):
    print(f"  Fetching {label} escalations…")
    issues = jira_search(jql, ESCALATION_FIELDS)
    print(f"    → {len(issues)} issues")
    return [escalation_record(i) for i in issues]


def load_escalation_json(path, label):
    """Load raw Jira issue dicts (shape: {key, fields:{...}}) from a JSON file
    and transform them with the same logic used for live API results."""
    print(f"  Loading {label} escalations from {path}…")
    issues = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"    → {len(issues)} issues")
    return [escalation_record(i) for i in issues]


def records_to_js_array(records, var_name):
    """Render a list of dicts as a JS const array."""
    lines = [f"const {var_name} = ["]
    for r in records:
        # Build key:value pairs with JS-style quoting
        parts = []
        for k, v in r.items():
            if v is None:
                parts.append(f'{k}:null')
            elif isinstance(v, bool):
                parts.append(f'{k}:{str(v).lower()}')
            else:
                escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'{k}:"{escaped}"')
        lines.append("  {" + ", ".join(parts) + "},")
    lines.append("];")
    return "\n".join(lines)


def update_escalation_html(html, cloud_records, onprem_records):
    today = datetime.now().strftime("%Y-%m-%d")

    # Replace cloudData array
    cloud_js = records_to_js_array(cloud_records, "cloudData")
    html = re.sub(
        r"const cloudData = \[[\s\S]*?\n\];",
        cloud_js,
        html,
        count=1,
    )

    # Replace onpremData array
    onprem_js = records_to_js_array(onprem_records, "onpremData")
    html = re.sub(
        r"const onpremData = \[[\s\S]*?\n\];",
        onprem_js,
        html,
        count=1,
    )

    # Update "Data as of" date in the date stamp
    html = re.sub(
        r"Data as of: \d{4}-\d{2}-\d{2}",
        f"Data as of: {today}",
        html,
    )

    return html

# ─── Stalled Items ────────────────────────────────────────────────────────────

STALLED_FIELDS = [
    "summary", "project", "assignee", "status", "issuetype",
    "priority", "created", "updated",
]

PROJECT_NAMES = {
    "NEO":  "NEO",
    "DPS":  "DPS",
    "DLP":  "Forcepoint DLP",
    "DSA":  "DSA",
    "FSM":  "FSM",
    "CASB": "CASB",
}

STALLED_JQL = (
    "project in (NEO, DPS, DLP, DSA) "
    "AND status not in (Closed, Resolved, Done, Fixed) "
    f"AND updated <= -{STALE_DAYS}d "
    "AND issuetype in (Bug, Story, Task, Sub-task, Epic) "
    "ORDER BY updated ASC"
)


def resolve_team(assignee):
    return TEAM_MAP.get(assignee, assignee)


def fetch_stalled_issues():
    print(f"  Fetching stalled issues (not updated in {STALE_DAYS}d)…")
    issues = jira_search(STALLED_JQL, STALLED_FIELDS, max_results=2000)
    print(f"    → {len(issues)} issues")
    records = []
    for issue in issues:
        f = issue["fields"]
        project_key = get_field(f, "project", "key") or issue["key"].split("-")[0]
        assignee = extract_assignee(f)
        records.append({
            "key":         issue["key"],
            "url":         f"{JIRA_URL}/browse/{issue['key']}",
            "summary":     (get_field(f, "summary") or "").strip(),
            "project":     project_key,
            "projectName": PROJECT_NAMES.get(project_key, project_key),
            "assignee":    assignee,
            "status":      extract_status(f).upper(),
            "type":        get_field(f, "issuetype", "name") or "Bug",
            "priority":    extract_priority(f),
            "created":     short_date(get_field(f, "created")),
            "updated":     short_date(get_field(f, "updated")),
            "team":        resolve_team(assignee),
        })
    return records


def update_stalled_html(html, records):
    today = datetime.now().strftime("%Y-%m-%d")
    issues_json = json.dumps(records, indent=2, ensure_ascii=False)
    issues_js = f"const ISSUES = {issues_json};"

    html = re.sub(
        r"const ISSUES = \[[\s\S]*?\];",
        issues_js,
        html,
        count=1,
    )

    # Update hardcoded TODAY date used for staleness calculation
    html = re.sub(
        r"const TODAY = new Date\('[^']+'\);",
        f"const TODAY = new Date('{today}');",
        html,
    )
    return html

# ─── PI Dashboard ─────────────────────────────────────────────────────────────

def extract_pi_keys(html):
    """Extract all Jira issue keys referenced in pi-dashboard.html."""
    return list(dict.fromkeys(
        re.findall(r'/browse/([A-Z]+-\d+)', html)
    ))


def classify_jira_status(status_name):
    """Map Jira status to (badge_class, display_text, row_class)."""
    mapped = PI_STATUS_MAP.get(status_name)
    if mapped:
        return mapped
    # Fuzzy fallback
    s = status_name.lower()
    if "done" in s or "closed" in s or "resolved" in s or "fixed" in s:
        return ("status-done", status_name.upper(), "row-done")
    if "review" in s or "qa" in s or "test" in s:
        return ("status-review", status_name.upper(), "row-incomplete")
    if "progress" in s or "develop" in s or "implement" in s:
        return ("status-progress", status_name.upper(), "row-incomplete")
    if "block" in s:
        return ("status-blocked", status_name.upper(), "row-incomplete")
    return ("status-todo", status_name.upper(), "row-incomplete")


def update_pi_html(html, statuses_by_key):
    """
    Update status badges in pi-dashboard.html for each known Jira key.

    Rows whose current badge text is in SKIP_PI_BADGES (e.g. "DEFERRED TO PI 2026.2")
    are editorial annotations — they are left unchanged.
    """
    updated = skipped = not_found = 0

    for key, status_name in statuses_by_key.items():
        # Pattern matches the entire <tr> block for this key (initiative row or blocker-row)
        # We need to find the row containing this key's browse link and update its badge.
        row_pattern = re.compile(
            r'(<tr(?:[^>]*)>)((?:(?!</tr>)[\s\S])*?'
            + re.escape(f'/browse/{key}')
            + r'(?:(?!</tr>)[\s\S])*?)'
            r'(<span class="status-badge [^"]*">([^<]*)</span>)'
            r'((?:(?!</tr>)[\s\S])*?</tr>)',
            re.IGNORECASE,
        )

        match = row_pattern.search(html)
        if not match:
            not_found += 1
            continue

        current_badge_text = match.group(4).strip()
        if current_badge_text in SKIP_PI_BADGES:
            skipped += 1
            continue

        badge_class, display, row_class = classify_jira_status(status_name)
        new_badge = f'<span class="status-badge {badge_class}">{display}</span>'

        # Update the row class (row-done vs row-incomplete), preserving blocker-row if present
        tr_open = match.group(1)
        existing_classes = re.findall(r'class="([^"]*)"', tr_open)
        if existing_classes:
            cls_str = existing_classes[0]
            # Replace row-done/row-incomplete with the new row_class
            cls_str = re.sub(r'\b(row-done|row-incomplete)\b', row_class, cls_str)
            if "row-done" not in cls_str and "row-incomplete" not in cls_str:
                cls_str = (cls_str + " " + row_class).strip()
            new_tr_open = re.sub(r'class="[^"]*"', f'class="{cls_str}"', tr_open, count=1)
        else:
            new_tr_open = tr_open.rstrip(">") + f' class="{row_class}">'

        new_row = new_tr_open + match.group(2) + new_badge + match.group(5)
        html = html[:match.start()] + new_row + html[match.end():]
        updated += 1

    print(f"    → {updated} badges updated, {skipped} preserved (editorial), {not_found} keys not in HTML")
    return html


def fetch_and_update_pi(html):
    keys = extract_pi_keys(html)
    print(f"  Found {len(keys)} issue keys in pi-dashboard.html — fetching statuses…")
    statuses = batch_fetch_statuses(keys)
    print(f"    → Got statuses for {len(statuses)} keys")
    return update_pi_html(html, statuses)

# ─── Main ─────────────────────────────────────────────────────────────────────

def _opt_value(argv, name):
    """Return the value following `--name VALUE`, or None if absent."""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main():
    argv = sys.argv[1:]
    args = set(argv)
    dry_run           = "--dry-run"           in args
    skip_escalation   = "--skip-escalation"   in args
    skip_stalled      = "--skip-stalled"      in args
    skip_pi           = "--skip-pi"           in args

    # Offline mode: transform pre-fetched raw issue JSON instead of hitting the API.
    esc_json_cloud    = _opt_value(argv, "--esc-json-cloud")
    esc_json_onprem   = _opt_value(argv, "--esc-json-onprem")

    if "--list-fields" in args:
        list_custom_fields()
        return

    if dry_run:
        print("DRY RUN — no files will be modified\n")

    # ── Escalation Dashboard ─────────────────────────────────────────────────
    if not skip_escalation:
        print("\n[1/3] Escalation Dashboard")
        if esc_json_cloud or esc_json_onprem:
            cloud_records  = load_escalation_json(esc_json_cloud,  "Cloud (NEO, DPS)")   if esc_json_cloud  else []
            onprem_records = load_escalation_json(esc_json_onprem, "On-Prem (DLP, FSM)") if esc_json_onprem else []
        else:
            cloud_records  = fetch_escalation_data(ESC_JQL_CLOUD,  "Cloud (NEO, DPS)")
            onprem_records = fetch_escalation_data(ESC_JQL_ONPREM, "On-Prem (DLP, FSM)")

        esc_path = SCRIPT_DIR / "escalation-dashboard.html"
        esc_html = esc_path.read_text(encoding="utf-8")
        new_esc  = update_escalation_html(esc_html, cloud_records, onprem_records)

        if dry_run:
            print(f"  Would write {len(new_esc):,} bytes to {esc_path.name}")
        else:
            esc_path.write_text(new_esc, encoding="utf-8")
            print(f"  Wrote {esc_path.name}")
    else:
        print("\n[1/3] Escalation Dashboard — skipped")

    # ── Stalled Items ─────────────────────────────────────────────────────────
    if not skip_stalled:
        print("\n[2/3] Stalled Items")
        stalled_records = fetch_stalled_issues()

        stalled_path = SCRIPT_DIR / "stalled.html"
        stalled_html = stalled_path.read_text(encoding="utf-8")
        new_stalled  = update_stalled_html(stalled_html, stalled_records)

        if dry_run:
            print(f"  Would write {len(new_stalled):,} bytes to {stalled_path.name}")
        else:
            stalled_path.write_text(new_stalled, encoding="utf-8")
            print(f"  Wrote {stalled_path.name}")
    else:
        print("\n[2/3] Stalled Items — skipped")

    # ── PI Dashboard ─────────────────────────────────────────────────────────
    if not skip_pi:
        print("\n[3/3] PI Dashboard")
        pi_path = SCRIPT_DIR / "pi-dashboard.html"
        pi_html = pi_path.read_text(encoding="utf-8")
        new_pi  = fetch_and_update_pi(pi_html)

        if dry_run:
            print(f"  Would write {len(new_pi):,} bytes to {pi_path.name}")
        else:
            pi_path.write_text(new_pi, encoding="utf-8")
            print(f"  Wrote {pi_path.name}")
    else:
        print("\n[3/3] PI Dashboard — skipped")

    print("\nDone.")


if __name__ == "__main__":
    main()
