#!/usr/bin/env python3
"""Fetch the current Jira status of every initiative listed in pi_config.json.

Output: scripts/initiatives.json — a per-key map of {status, planned, created,
resolutiondate, assignee, summary}. Consumed by splice_dashboard.py.

Auth: reads JIRA_EMAIL + JIRA_API_TOKEN from environment (or scripts/.env).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "pi_config.json"
OUT = ROOT / "initiatives.json"
ENV_FILE = ROOT / ".env"

# customfield_11385 is Forcepoint's "Planned Work" Yes/No flag.
FIELDS = ["summary", "status", "customfield_11385", "created", "resolutiondate", "assignee"]


def load_env_file(path: Path) -> None:
    """Populate os.environ from a simple KEY=VALUE .env file (no quoting/expansion)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def collect_keys(config: dict) -> list[str]:
    keys: list[str] = []
    for tab in config["tabs"].values():
        for section in tab["sections"]:
            for entry in section["keys"]:
                keys.append(entry["key"])
    # dedup, preserve order
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k); out.append(k)
    return out


def jira_search(base_url: str, auth_header: str, jql: str, fields: list[str]) -> list[dict]:
    """Page through /rest/api/3/search/jql until all matching issues are collected."""
    out: list[dict] = []
    next_token: str | None = None
    while True:
        body = {"jql": jql, "fields": fields, "maxResults": 100}
        if next_token:
            body["nextPageToken"] = next_token
        req = urllib.request.Request(
            f"{base_url}/rest/api/3/search/jql",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": auth_header,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            sys.exit(f"Jira API error {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
        out.extend(data.get("issues", []))
        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast"):
            break
    return out


def shape(issue: dict) -> dict:
    f = issue["fields"]
    pf = f.get("customfield_11385") or {}
    assignee = (f.get("assignee") or {}).get("displayName") or "Unassigned"
    return {
        "key": issue["key"],
        "summary": f.get("summary", ""),
        "status": (f.get("status") or {}).get("name") or "Unknown",
        "planned": pf.get("value") or "—",
        "created": (f.get("created") or "")[:10] or None,
        "resolutiondate": (f.get("resolutiondate") or "")[:10] or None,
        "assignee": assignee,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    load_env_file(ENV_FILE)
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    if not email or not token:
        sys.exit("Set JIRA_EMAIL and JIRA_API_TOKEN (in environment or scripts/.env).")

    config = json.loads(Path(args.config).read_text())
    base_url = config["jira"]["base_url"].rstrip("/")
    auth_header = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    keys = collect_keys(config)
    print(f"Fetching {len(keys)} initiatives from {base_url} …", file=sys.stderr)
    jql = "key in (" + ", ".join(keys) + ")"
    issues = jira_search(base_url, auth_header, jql, FIELDS)

    by_key = {issue["key"]: shape(issue) for issue in issues}
    missing = [k for k in keys if k not in by_key]
    if missing:
        print(f"WARN: {len(missing)} keys not returned by Jira: {missing}", file=sys.stderr)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(by_key, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {len(by_key)} initiatives → {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
