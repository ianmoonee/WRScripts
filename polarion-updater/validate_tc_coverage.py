#!/usr/bin/env python3
"""
Polarion Test Case Log Coverage Checker

Fetches all TC (test case) work items from Polarion and checks whether each
TC title appears in at least one .log file under a given directory.

Log files are expected to contain lines like:
    Implements Test Case: bootloaderStart_HLTC_36
    Implements Test Case: elfIdentRead_LLTC_2 (also covered by this TP)

Both formats are matched — the TC name is extracted regardless of the
trailing "(also covered ...)" text.

Environment Variables:
    POLARION_API_BASE, POLARION_PAT, POLARION_PROJECT_ID

Usage:
    python validate_tc_coverage.py --log-dir path/to/logs
    python validate_tc_coverage.py --log-dir path/to/logs --component BOOT_APP0
    python validate_tc_coverage.py --log-dir path/to/logs --title-filter bootloaderStart
    python validate_tc_coverage.py --log-dir path/to/logs -v
"""

import os
import sys
import re
import argparse
import requests
from pathlib import Path


# ---------------------------------------------------------------------------
# Polarion helpers (same as validate_tc_links.py)
# ---------------------------------------------------------------------------

def create_polarion_session(base_url, pat, verify_ssl=False):
    """Create an authenticated requests session for the Polarion REST API."""
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {pat}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    session.verify = verify_ssl
    if not verify_ssl:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def paginated_get(session, url, params, verify_ssl, data_key="data", verbose=False):
    """Single GET request for Polarion REST API. Yields all items from the response."""
    resp = session.get(url, params=params, verify=verify_ssl)
    if verbose:
        print(f"  [VERBOSE] GET {resp.request.url}")
        print(f"  [VERBOSE] Status: {resp.status_code}")
    if resp.status_code != 200:
        if verbose:
            print(f"  [VERBOSE] Response preview: {resp.text[:500]}")
        return
    body = resp.json()
    items = body.get(data_key, [])
    if verbose:
        total = body.get("meta", {}).get("totalCount")
        print(f"  [VERBOSE] Items: {len(items)}, totalCount: {total}")
    yield from items


def query_work_items_paginated(session, base_url, project_id, query, verify_ssl, verbose=False):
    """Query Polarion for work items, working around the 100-item API limit.
    Splits queries by title prefix (a*, b*, ...) and recurses deeper if a
    prefix hits the 100-item cap.
    Returns list of IDs."""
    url = f"{base_url}/projects/{project_id}/workitems"
    wi_ids_set = set()

    def query_by_prefix(prefix):
        sub_query = f"{query} AND title:{prefix}*"
        params = {
            "query": sub_query,
            "fields[workitems]": "id",
        }
        items = list(paginated_get(session, url, params, verify_ssl, verbose=verbose))
        ids = [item["id"] for item in items if isinstance(item, dict) and "id" in item]
        if len(ids) >= 100:
            if verbose:
                print(f"  [VERBOSE] title:{prefix}* → {len(ids)} (capped), splitting deeper...")
            for c in "abcdefghijklmnopqrstuvwxyz":
                query_by_prefix(f"{prefix}{c}")
        else:
            wi_ids_set.update(ids)
            if ids and verbose:
                print(f"  [VERBOSE] title:{prefix}* → {len(ids)} items")

    for c in "abcdefghijklmnopqrstuvwxyz":
        query_by_prefix(c)

    if verbose:
        print(f"  [VERBOSE] Total unique items fetched: {len(wi_ids_set)}")
    return list(wi_ids_set)


def extract_short_id(full_id: str) -> str:
    if "/" in full_id:
        return full_id.split("/")[-1]
    return full_id


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

TC_RE = re.compile(r'Implements\s+Test\s+Case:\s*(\S+)', re.IGNORECASE)


def find_log_files(root_dir: str) -> list:
    """Recursively find all .log files under root_dir."""
    return sorted(Path(root_dir).rglob("*.log"))


def extract_tc_names_from_logs(log_files: list, verbose: bool = False) -> dict:
    """Parse all log files and return a dict mapping TC name → set of log file paths."""
    tc_to_files = {}
    for log_path in log_files:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = TC_RE.search(line)
                    if m:
                        tc_name = m.group(1)
                        tc_to_files.setdefault(tc_name, set()).add(str(log_path))
        except OSError as e:
            if verbose:
                print(f"  [VERBOSE] Could not read {log_path}: {e}")
    return tc_to_files


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Check whether Polarion test cases appear in log files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--log-dir", required=True, help="Root directory to recursively search for .log files")
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--component", default=None, help="Filter by component (e.g. BOOT_APP0)")
    parser.add_argument("--title-filter", default=None, help="Filter TCs by title substring")
    parser.add_argument("--query", default=None, help="Use a custom Lucene query (overrides --component and --title-filter)")
    parser.add_argument("--verify-ssl", action="store_true", default=False)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of TCs to check")
    args = parser.parse_args()

    # --- Validate log directory ---
    log_root = Path(args.log_dir)
    if not log_root.is_dir():
        print(f"Error: --log-dir '{args.log_dir}' is not a directory")
        sys.exit(1)

    # --- Polarion setup ---
    base_url = os.environ.get("POLARION_API_BASE")
    pat = os.environ.get("POLARION_PAT")
    project_id = args.project_id or os.environ.get("POLARION_PROJECT_ID")

    if not all([base_url, pat, project_id]):
        print("Set POLARION_API_BASE, POLARION_PAT, POLARION_PROJECT_ID")
        sys.exit(1)

    base_url = base_url.rstrip('/')
    verify_ssl = args.verify_ssl
    session = create_polarion_session(base_url, pat, verify_ssl=verify_ssl)

    # --- Build query ---
    if args.query:
        query = args.query
    else:
        query_parts = [
            "NOT HAS_VALUE:resolution",
            "NOT status:deleted",
            "type:wi_testCase",
        ]
        if args.component:
            query_parts.append(f"fld_component.KEY:comp_{args.component}")
        if args.title_filter:
            query_parts.append(f"title:{args.title_filter}*")
        query = " AND ".join(query_parts)

    print(f"Query: {query}")

    # --- Fetch TC IDs from Polarion ---
    print("Fetching test cases from Polarion...")
    wi_ids = query_work_items_paginated(session, base_url, project_id, query, verify_ssl, verbose=args.verbose)
    wi_ids = sorted(set(wi_ids))
    print(f"Found {len(wi_ids)} test case(s) in Polarion\n")

    if not wi_ids:
        return

    # --- Fetch TC titles ---
    print("Fetching TC titles...")
    tc_titles = {}  # short_id -> title
    unimplemented = []  # (short_id, title) for TCs marked unimplemented
    for i, wi_id in enumerate(wi_ids):
        if args.limit and i >= args.limit:
            print(f"  Limit of {args.limit} reached.")
            break

        short_id = extract_short_id(wi_id)
        url = f"{base_url}/projects/{project_id}/workitems/{short_id}"
        params = {"fields[workitems]": "title,fld_passFailCriteria"}
        resp = session.get(url, params=params, verify=verify_ssl)
        if resp.status_code != 200:
            print(f"  ✗ {short_id} - Error fetching: {resp.status_code}")
            continue

        attrs = resp.json().get("data", {}).get("attributes", {})
        title = attrs.get("title", "")
        if not title:
            continue

        # Skip unimplemented TCs (pass/fail criteria contains "unimplemented")
        pf_criteria = attrs.get("fld_passFailCriteria", {})
        pf_text = pf_criteria.get("value", "") if isinstance(pf_criteria, dict) else str(pf_criteria or "")
        if "unimplemented" in pf_text.lower():
            unimplemented.append((short_id, title))
            continue

        tc_titles[short_id] = title

    print(f"Retrieved {len(tc_titles)} TC title(s) ({len(unimplemented)} unimplemented, excluded)\n")

    # --- Scan log files ---
    print(f"Scanning log files under: {log_root}")
    log_files = find_log_files(str(log_root))
    print(f"Found {len(log_files)} .log file(s)")

    if not log_files:
        print("No log files found. Nothing to check.")
        return

    tc_in_logs = extract_tc_names_from_logs(log_files, verbose=args.verbose)
    print(f"Found {len(tc_in_logs)} unique TC name(s) in logs\n")

    # --- Cross-reference ---
    found = []
    missing = []

    for short_id, title in sorted(tc_titles.items(), key=lambda x: x[1]):
        if title in tc_in_logs:
            found.append((short_id, title))
        else:
            missing.append((short_id, title))

    # --- Report ---
    if args.verbose and found:
        print("TCs found in logs:")
        for short_id, title in found:
            log_count = len(tc_in_logs[title])
            print(f"  ✓ {short_id} - {title}  (in {log_count} log file(s))")
        print()

    if missing:
        print("TCs NOT found in any log file:")
        for short_id, title in missing:
            print(f"  ✗ {short_id} - {title}")
        print()

    # --- Summary ---
    total = len(tc_titles)
    found_count = len(found)
    missing_count = len(missing)
    print(f"{'=' * 60}")
    print(f"Coverage: {found_count}/{total} TCs found in logs ({missing_count} missing)")
    if unimplemented:
        print(f"Excluded: {len(unimplemented)} unimplemented TC(s)")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
