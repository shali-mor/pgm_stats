#!/usr/bin/env python3
"""Splice pi_config.json + initiatives.json into pi-dashboard.html.

Rebuilds the Cloud DLP and DLP On-Prem tab bodies (between
<div id="tab-cloud"/<div id="tab-onprem" and the matching closing </div>)
from the configured section structure, using the live Jira status from
initiatives.json. Header title + date stamp are also refreshed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
CONFIG = ROOT / "pi_config.json"
DATA = ROOT / "initiatives.json"
DASHBOARD = REPO / "pi-dashboard.html"

# Jira status -> (badge label, badge CSS class, row CSS class)
STATUS_MAP = {
    "Closed":             ("DONE",                "status-done",     "row-done"),
    "Done":               ("DONE",                "status-done",     "row-done"),
    "In Progress":        ("IN PROGRESS",         "status-progress", "row-incomplete"),
    "Open":               ("OPEN",                "status-todo",     "row-incomplete"),
    "In Review":          ("IN REVIEW",           "status-review",   "row-incomplete"),
    "READY TO IMPLEMENT": ("READY TO IMPLEMENT",  "status-review",   "row-incomplete"),
    "IN REFINEMENT":      ("IN REFINEMENT",       "status-progress", "row-incomplete"),
    "Blocked":            ("BLOCKED",             "status-blocked",  "row-incomplete"),
}


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_unplanned(initiative: dict, pi_start: str) -> bool:
    """Unplanned = created on/after PI start (mid-PI additions).

    We deliberately ignore Jira's customfield_11385 ("Planned Work") because
    pre-PI carryover items that Jira marks Planned=No are *not* mid-PI noise —
    they are scoped backlog. The dashboard's "Unplanned" stat is about scope
    drift during PI execution, which is exclusively "created mid-PI".
    """
    created = initiative.get("created") or ""
    return bool(created) and created >= pi_start


def row_html(entry: dict, initiative: dict, pi_start: str) -> str:
    key = entry["key"]
    label = entry.get("label") or initiative.get("summary", key)
    status = initiative.get("status", "Unknown")
    badge_label, badge_cls, row_cls = STATUS_MAP.get(status, (status.upper(), "status-todo", "row-incomplete"))
    planned_attr = "" if is_unplanned(initiative, pi_start) else ' data-planned="yes"'

    # Decide the rightmost "Notes" cell: explicit override > resolution date > created date for unplanned > em dash
    if entry.get("note"):
        note = entry["note"]
    elif initiative.get("resolutiondate"):
        note = f'Closed {initiative["resolutiondate"]}'
    elif is_unplanned(initiative, pi_start) and initiative.get("created", "") >= pi_start:
        note = f'Unplanned &mdash; added {initiative["created"]}'
    else:
        note = "&mdash;"
    note_cell = f'<td class="note">{note}</td>' if note != "&mdash;" else f'<td>{note}</td>'

    return (
        f'    <tr class="{row_cls}"{planned_attr}>\n'
        f'      <td><a href="https://forcepoint.atlassian.net/browse/{key}">{key}</a></td>\n'
        f'      <td>{html_escape(label)}</td>\n'
        f'      <td><span class="status-badge {badge_cls}">{badge_label}</span></td>\n'
        f'      {note_cell}\n'
        f'    </tr>'
    )


def section_html(section: dict, initiatives: dict, pi_start: str) -> str:
    rows = []
    missing = []
    for entry in section["keys"]:
        ini = initiatives.get(entry["key"])
        if not ini:
            missing.append(entry["key"])
            continue
        rows.append(row_html(entry, ini, pi_start))
    if missing:
        print(f'  WARN: section "{section["title"]}" missing Jira data for: {missing}', file=sys.stderr)
    if not rows:
        return ""
    badge = section.get("badge") or ""
    badge_html = f' <span class="team-badge">{html_escape(badge)}</span>' if badge else ""
    title = html_escape(section["title"])
    return (
        '  <div class="section-group">\n'
        f'  <div class="section-title"><div class="section-header" onclick="toggleSection(this)">{title}{badge_html} <span class="section-chevron">&#9660;</span></div></div>\n'
        '  <table>\n'
        '    <tr><th>Key</th><th>Initiative</th><th>Status</th><th>Notes</th></tr>\n'
        + "\n".join(rows) + "\n"
        '  </table>\n'
        '  </div>'
    )


def build_tab(tab_id: str, tab_config: dict, initiatives: dict, pi_start: str) -> str:
    active_attr = " active" if tab_id == "cloud" else ""
    cap = tab_id.capitalize()
    head = (
        f'<div id="tab-{tab_id}" class="tab-content{active_attr}">\n'
        '\n'
        f'  <div class="summary-bar" id="{tab_id}-summary"></div>\n'
        f'  <div class="progress-bar-container" id="{tab_id}-progress">\n'
        f'    <div class="title">{html_escape(tab_config["title"])} &mdash; Initiative Completion</div>\n'
        f'    <div class="progress-bar-outer" id="{tab_id}-bar"></div>\n'
        f'    <div class="progress-legend" id="{tab_id}-legend"></div>\n'
        f'    <div class="plan-pills" id="{tab_id}-plan-pills"></div>\n'
        '  </div>\n'
        '\n'
        '  <div class="filter-bar">\n'
        f"    <button class=\"filter-btn active\" onclick=\"filter{cap}('all')\">All</button>\n"
        f"    <button class=\"filter-btn\" onclick=\"filter{cap}('incomplete')\">Incomplete Only</button>\n"
        f"    <button class=\"filter-btn\" onclick=\"filter{cap}('done')\">Done Only</button>\n"
        f"    <button class=\"collapse-all-btn\" onclick=\"toggleAllSections('tab-{tab_id}')\">Collapse All</button>\n"
        '  </div>\n'
        '\n'
    )
    body = "\n\n".join(s for s in (section_html(sec, initiatives, pi_start) for sec in tab_config["sections"]) if s)
    return head + body + "\n\n</div>"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--dashboard", default=str(DASHBOARD))
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text())
    initiatives = json.loads(Path(args.data).read_text())
    pi_start = config["pi"]["start"]
    pi_name = config["pi"]["name"]

    html = Path(args.dashboard).read_text()

    # Refresh title + date stamp
    today = dt.date.today().isoformat()
    pretty_today = dt.date.fromisoformat(today).strftime("%B %-d, %Y") if sys.platform != "win32" else today
    subtitle = config["pi"]["header_subtitle"].format(today=pretty_today)

    html = re.sub(
        r"<title>[^<]*Initiative Completion Dashboard</title>",
        f"<title>{pi_name} Initiative Completion Dashboard</title>",
        html, count=1,
    )
    html = re.sub(
        r"<h1>[^<]*Initiative Completion Dashboard</h1>",
        f"<h1>{pi_name} Initiative Completion Dashboard</h1>",
        html, count=1,
    )
    html = re.sub(
        r'<div class="date-stamp">[^<]*</div>',
        f'<div class="date-stamp">{html_escape(subtitle)}</div>',
        html, count=1,
    )

    cloud_tab = build_tab("cloud", config["tabs"]["cloud"], initiatives, pi_start)
    onprem_tab = build_tab("onprem", config["tabs"]["onprem"], initiatives, pi_start)

    html, n1 = re.subn(
        r'<div id="tab-cloud" class="tab-content active">[\s\S]*?\n</div>\n\n<!-- ==================== DLP ON-PREM',
        cloud_tab + "\n\n<!-- ==================== DLP ON-PREM",
        html, count=1,
    )
    if n1 != 1:
        sys.exit(f"Could not locate the Cloud tab block to replace (matches={n1}).")

    html, n2 = re.subn(
        r'<div id="tab-onprem" class="tab-content">[\s\S]*?\n</div>\n\n<script>',
        onprem_tab + "\n\n<script>",
        html, count=1,
    )
    if n2 != 1:
        sys.exit(f"Could not locate the On-Prem tab block to replace (matches={n2}).")

    Path(args.dashboard).write_text(html)
    cloud_count = sum(len(s["keys"]) for s in config["tabs"]["cloud"]["sections"])
    onprem_count = sum(len(s["keys"]) for s in config["tabs"]["onprem"]["sections"])
    print(f"Patched {args.dashboard} — Cloud {cloud_count} / On-Prem {onprem_count} initiatives.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
