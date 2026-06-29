#!/usr/bin/env python3
"""
Weekly SP sprint report refresh.

Fetches the four status buckets fresh from Jira, parses counts deterministically
from each API response (no carried-forward values), and writes to two tabs of the
shared Google Sheet:

  - 'Current Ticket Status' : appends a new dated column each run
  - 'Tickets By Project'    : overwrites the whole table each run

All identity (Jira creds, Google service account, sheet ID) is read from
environment variables, which in CI are injected from GitHub repository secrets.
See HANDOVER.md for the full list and how to rotate them when ownership changes.

The 'Unsolved ZD Total' row is a Zendesk figure and is intentionally NOT written;
it is left blank for manual entry.
"""

import os
import sys
import datetime as dt
from collections import defaultdict

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# ----------------------------------------------------------------------------
# Configuration (all from environment / GitHub secrets)
# ----------------------------------------------------------------------------
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].rstrip("/")   # e.g. https://3sidedcube.atlassian.net
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GCP_SA_KEY_PATH = os.environ.get("GCP_SA_KEY_PATH", "service_account.json")

PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "SP")

STATUS_TAB = "Current Ticket Status"
PROJECT_TAB = "Tickets By Project"

# Component name -> dashboard label
COMP_MAP = {"iOS": "iOS", "Android": "Android", "React Native": "RN", "BED": "BED", "FED": "FED"}
IN_PROG_STATUSES = {"In Progress", "Awaiting Response"}

# Emoji / marker prefixes stripped from summaries
EMOJI_PREFIXES = ["🤝 ", "🛑 ", "🤝", "🛑", "🏎 🔄 ", "❓ ", "🔄 ", "🎓 ", "🎨 "]


# ----------------------------------------------------------------------------
# Jira
# ----------------------------------------------------------------------------
def jira_search(jql):
    """Page through a JQL search and return all issues. Verifies isLast."""
    issues = []
    next_token = None
    while True:
        payload = {
            "jql": jql,
            "maxResults": 100,
            "fields": ["summary", "status", "components", "parent"],
        }
        if next_token:
            payload["nextPageToken"] = next_token
        resp = requests.post(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            json=payload,
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data.get("issues", []))
        if data.get("isLast", True):
            break
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return issues


def slim(raw_issues, status_override=None):
    out = []
    for i in raw_issues:
        f = i["fields"]
        summary = f.get("summary", "") or ""
        for e in EMOJI_PREFIXES:
            summary = summary.replace(e, "")
        parent = f.get("parent")
        out.append({
            "key": i["key"],
            "summary": summary.strip(),
            "status": status_override or f["status"]["name"],
            "components": [c["name"] for c in (f.get("components") or [])],
            "parent_key": parent["key"] if parent else None,
            "parent_summary": parent["fields"]["summary"] if parent else None,
        })
    return out


def fetch_all():
    base = f'project = {PROJECT_KEY} AND sprint in openSprints() AND'
    delivered = slim(jira_search(f'{base} status = "Delivered" ORDER BY created ASC'), "Delivered")
    finished = slim(jira_search(f'{base} status = "Finished" ORDER BY created ASC'), "Finished")
    in_prog = slim(jira_search(f'{base} status in ("In Progress","Awaiting Response") ORDER BY status ASC'))
    open_i = slim(jira_search(f'{base} status = "Open" ORDER BY created ASC'), "Open")
    return delivered, finished, in_prog, open_i


# ----------------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------------
def get_tech(components):
    return [COMP_MAP[c] for c in components if c in COMP_MAP]


def is_sec(components):
    return "Security" in components


def build_status_column(delivered, finished, in_prog, open_i):
    """Returns the ordered values for one weekly column of 'Current Ticket Status'."""
    comp = {"iOS": 0, "Android": 0, "RN": 0, "BED": 0, "FED": 0}
    for i in open_i:
        for c in get_tech(i["components"]):
            comp[c] += 1

    needs_testing = len(delivered)
    awaiting_pr = len(finished)
    in_progress = len(in_prog)
    jira_total = needs_testing + awaiting_pr + in_progress + len(open_i)

    # Order matches the sheet rows top-to-bottom.
    # 'Unsolved ZD Total' is intentionally left blank (manual entry).
    return [
        needs_testing,      # Needs Testing
        awaiting_pr,        # Awaiting PR
        in_progress,        # In Progress
        comp["iOS"],        # iOS Pending
        comp["Android"],    # Android Pending
        comp["RN"],         # RN Pending
        comp["BED"],        # BED Pending
        comp["FED"],        # FED Pending
        jira_total,         # JIRA Total
        "",                 # Unsolved ZD Total (manual)
    ]


# Strip the [TAG] prefix and known project-name normalisations for display
def project_display_name(parent_summary):
    if not parent_summary:
        return "No parent"
    # remove leading [..] tag
    import re
    name = re.sub(r"^\[.*?\]\s*", "", parent_summary).strip()
    return name or parent_summary


def build_project_table(delivered, finished, in_prog, open_i):
    """Returns rows for 'Tickets By Project', sorted by total desc, plus a Total row."""
    all_issues = delivered + finished + in_prog + open_i
    projects = {}
    for i in all_issues:
        pk = i["parent_key"] or "__none__"
        if pk not in projects:
            projects[pk] = {
                "name": project_display_name(i["parent_summary"]),
                "nt": 0, "pr": 0, "ip": 0, "td": 0, "sec": 0,
            }
        p = projects[pk]
        s = i["status"]
        if s == "Delivered":
            p["nt"] += 1
        elif s == "Finished":
            p["pr"] += 1
        elif s in IN_PROG_STATUSES:
            p["ip"] += 1
        elif s == "Open":
            p["td"] += 1
        if is_sec(i["components"]):
            p["sec"] += 1

    rows = []
    grand_total = 0
    ordered = sorted(projects.values(), key=lambda p: -(p["nt"] + p["pr"] + p["ip"] + p["td"]))
    for p in ordered:
        total = p["nt"] + p["pr"] + p["ip"] + p["td"]
        if total == 0:
            continue
        grand_total += total
        # Columns: Project | Tickets | Security | To Do | In Progress | Awaiting PR | Delivered
        rows.append([
            p["name"],
            total,
            p["sec"] if p["sec"] > 0 else "-",
            p["td"] if p["td"] > 0 else "-",
            p["ip"] if p["ip"] > 0 else "-",
            p["pr"] if p["pr"] > 0 else "-",
            p["nt"] if p["nt"] > 0 else "-",
        ])

    total_row = ["Total", grand_total, "", "", "", "", ""]
    return rows, total_row


# ----------------------------------------------------------------------------
# Google Sheets
# ----------------------------------------------------------------------------
def sheets_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(GCP_SA_KEY_PATH, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def col_letter(idx):
    """1-based column index -> A1 letter(s)."""
    s = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def append_status_column(svc, status_values, date_label):
    """Append a new dated column to 'Current Ticket Status'.

    Assumes row 1 holds the date headers and rows 2..11 hold the metric values,
    matching the sheet layout (Needs Testing in row 2 ... Unsolved ZD Total in row 11).
    Finds the first empty header column and writes there.
    """
    # Read row 1 to find how many columns are populated
    header_resp = svc.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{STATUS_TAB}'!1:1",
    ).execute()
    header_row = header_resp.get("values", [[]])
    existing = header_row[0] if header_row else []
    target_col = len(existing) + 1  # next empty column (1-based)
    letter = col_letter(target_col)

    # Column payload: header + 10 metric rows (rows 1..11)
    column = [[date_label]] + [[v] for v in status_values]
    svc.spreadsheets().values().update(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{STATUS_TAB}'!{letter}1:{letter}{len(column)}",
        valueInputOption="USER_ENTERED",
        body={"values": column},
    ).execute()
    return letter


def overwrite_project_table(svc, rows, total_row):
    """Overwrite 'Tickets By Project' data rows. Assumes row 1 is the header.

    Clears existing data (rows 2 down) then writes the new table + total row.
    """
    # Clear a generous range first (rows 2..200, cols A..G)
    svc.spreadsheets().values().clear(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{PROJECT_TAB}'!A2:G200",
    ).execute()

    body_rows = rows + [total_row]
    end_row = 1 + len(body_rows)
    svc.spreadsheets().values().update(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{PROJECT_TAB}'!A2:G{end_row}",
        valueInputOption="USER_ENTERED",
        body={"values": body_rows},
    ).execute()


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def thursday_label(today=None):
    today = today or dt.date.today()
    day = today.day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix} {today.strftime('%B')}"


def main():
    print("Fetching Jira buckets...", flush=True)
    delivered, finished, in_prog, open_i = fetch_all()
    print(f"  Delivered:   {len(delivered)}")
    print(f"  Finished:    {len(finished)}")
    print(f"  In Progress: {len(in_prog)}")
    print(f"  Open:        {len(open_i)}")

    status_values = build_status_column(delivered, finished, in_prog, open_i)
    proj_rows, total_row = build_project_table(delivered, finished, in_prog, open_i)
    print(f"  JIRA Total:  {status_values[8]}")
    print(f"  Projects:    {len(proj_rows)} rows, grand total {total_row[1]}")

    if status_values[8] != total_row[1]:
        print(
            f"WARNING: JIRA Total ({status_values[8]}) != project grand total "
            f"({total_row[1]}). Tickets with no parent are excluded from the "
            f"project table; investigate if unexpected.",
            file=sys.stderr,
        )

    svc = sheets_service()
    label = thursday_label()
    col = append_status_column(svc, status_values, label)
    print(f"Wrote status column '{label}' to {STATUS_TAB}!{col}")
    overwrite_project_table(svc, proj_rows, total_row)
    print(f"Overwrote {PROJECT_TAB} with {len(proj_rows)} project rows")
    print("Done.")


if __name__ == "__main__":
    main()
