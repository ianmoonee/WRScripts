#!/usr/bin/env python3
"""
Polarion Test Result Work Item Manager

Fetches Test Cases (TCs) by a title pattern, creates one Test Result (TR)
work item per function, links each TR to all matching Test Procedures (TPs)
via derived_from, discovers log files in the integration testing repository,
and links relevant logs to TRs as source reference hyperlinks.

Environment Variables Required:
- POLARION_API_BASE: Base URL for Polarion API
- POLARION_PAT: Personal Access Token for authentication
- POLARION_PROJECT_ID: Project ID in Polarion (can also be provided via --project-id)
- INTEGRATION_REPO_PATH: Local path to the BSP_06 or SBL_06 Integration Testing repository

Usage:
    python polarionTestResultManager.py --component SSD_NVME0 --bsp --pattern nvme_qpair_ nvme_ctrlr_ [--dry-run|--execute]
    python polarionTestResultManager.py --component BOOT_APP0 --bl  --pattern bootApp_ --execute

TODO:
- How to deal with same as, some functions dont have TPs but they appear in the logs, should the TR be created and linked to the logs anyway? (probably yes, to capture the test results and logs even if no TPs exist yet)
- The links to .zip files point to the .zip and not the .log, this works but is it correct?
"""

import os
import sys
import re
import json
import argparse
import zipfile
from typing import List, Dict, Optional, Tuple

import subprocess

import requests
import urllib3


# ---------------------------------------------------------------------------
# Polarion session helpers
# ---------------------------------------------------------------------------

def create_polarion_session(base_url: str, pat: str, verify_ssl: bool = False) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    session.verify = verify_ssl
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def extract_short_id(full_id: str) -> str:
    if "/" in full_id:
        return full_id.split("/")[-1]
    return full_id


# ---------------------------------------------------------------------------
# Paginated query (works around the 100-item Polarion API cap)
# ---------------------------------------------------------------------------

def query_work_items_paginated(
    session: requests.Session,
    base_url: str,
    project_id: str,
    base_query: str,
    title_prefix: str,
    verify_ssl: bool,
    verbose: bool = False,
    debug: bool = False,
) -> List[str]:
    """Query Polarion for work items, splitting by title prefix to work around
    the 100-item API cap.  *base_query* must NOT contain a title: clause;
    *title_prefix* is the stem that gets extended with a-z/0-9."""
    url = f"{base_url}/projects/{project_id}/workitems"
    wi_ids_set: set = set()

    def query_by_prefix(prefix: str) -> None:
        sub_query = f"{base_query} AND title:{prefix}*"
        params = {"query": sub_query, "fields[workitems]": "id"}
        if debug:
            print(f"  [DEBUG] GET {url} params={params}")
        resp = session.get(url, params=params, verify=verify_ssl)
        if resp.status_code != 200:
            if verbose:
                print(f"  [VERBOSE] GET {resp.status_code} for prefix '{prefix}'")
            if debug:
                print(f"  [DEBUG] Response: {resp.text[:500]}")
            return
        body = resp.json()
        items = body.get("data", [])
        ids = [item["id"] for item in items if isinstance(item, dict) and "id" in item]
        if debug:
            total = body.get("meta", {}).get("totalCount")
            print(f"  [DEBUG] title:{prefix}* → {len(ids)} items (totalCount={total})")
        if len(ids) >= 100:
            if verbose:
                print(f"  [VERBOSE] title:{prefix}* → {len(ids)} (capped), splitting deeper...")
            for c in "abcdefghijklmnopqrstuvwxyz0123456789_":
                query_by_prefix(f"{prefix}{c}")
        else:
            wi_ids_set.update(ids)
            if ids and verbose:
                print(f"  [VERBOSE] title:{prefix}* → {len(ids)} items")

    for c in "abcdefghijklmnopqrstuvwxyz0123456789_":
        query_by_prefix(f"{title_prefix}{c}")

    if verbose:
        print(f"  [VERBOSE] Total unique items fetched: {len(wi_ids_set)}")
    return list(wi_ids_set)


# ---------------------------------------------------------------------------
# Phase 1 — Fetch Test Cases
# ---------------------------------------------------------------------------

_TC_LEVEL_RE = re.compile(r"_(LLTC|HLTC)_\d+$")


def extract_function_name(tc_title: str) -> Optional[str]:
    m = _TC_LEVEL_RE.search(tc_title)
    if m:
        return tc_title[: m.start()]
    return None


def fetch_test_cases(
    session: requests.Session,
    base_url: str,
    project_id: str,
    patterns: List[str],
    component: str,
    verify_ssl: bool,
    verbose: bool = False,
    debug: bool = False,
) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """
    Fetch TCs whose titles start with any of the given *patterns* and group
    them by function name.  Filters by *component*.
    Returns (tc_by_function, tc_title_to_id):
        tc_by_function: {function_name: [tc_title, ...]}
        tc_title_to_id: {tc_title: wi_full_id}
    """
    all_wi_ids: set = set()

    base_query = f"type:wi_testCase AND NOT status:deleted AND NOT HAS_VALUE:resolution AND fld_component.KEY:comp_{component}"

    if not patterns:
        # No pattern — single unfiltered query (count already verified in main)
        _url = f"{base_url}/projects/{project_id}/workitems"
        _params = {"query": base_query, "fields[workitems]": "id"}
        if debug:
            print(f"  [DEBUG] GET {_url} params={_params}")
        _resp = session.get(_url, params=_params, verify=verify_ssl)
        if _resp.status_code == 200:
            items = _resp.json().get("data", [])
            all_wi_ids.update(item["id"] for item in items if isinstance(item, dict) and "id" in item)
        print(f"  Total WI IDs (no pattern filter): {len(all_wi_ids)}")
    else:
        for pattern in patterns:
            print(f"  Pattern: {pattern}")

            wi_ids = query_work_items_paginated(
                session, base_url, project_id, base_query, pattern,
                verify_ssl, verbose, debug
            )
            print(f"    → {len(wi_ids)} WI(s) found")

            all_wi_ids.update(wi_ids)

        print(f"  Total unique WI IDs across all patterns: {len(all_wi_ids)}")

    # Fetch titles and verify pattern match against at least one pattern
    tc_titles: List[str] = []
    tc_title_to_id: Dict[str, str] = {}
    for wi_id in all_wi_ids:
        short_id = extract_short_id(wi_id)
        url = f"{base_url}/projects/{project_id}/workitems/{short_id}"
        params = {"fields[workitems]": "title"}
        if debug:
            print(f"  [DEBUG] GET {url}")
        resp = session.get(url, params=params, verify=verify_ssl)
        if resp.status_code != 200:
            if verbose:
                print(f"  [VERBOSE] Could not fetch {short_id}: {resp.status_code}")
            continue
        title = resp.json().get("data", {}).get("attributes", {}).get("title", "")
        if not patterns or any(p in title for p in patterns):
            tc_titles.append(title)
            tc_title_to_id[title] = wi_id
        elif verbose:
            print(f"  [VERBOSE] Skipping '{title}' — does not contain any pattern")

    # Group by function name
    grouped: Dict[str, List[str]] = {}
    for title in sorted(tc_titles):
        func = extract_function_name(title)
        if func:
            grouped.setdefault(func, []).append(title)
        elif verbose:
            print(f"  [VERBOSE] Could not extract function name from '{title}'")

    return grouped, tc_title_to_id


# ---------------------------------------------------------------------------
# Phase 2 — Create or reuse Test Results
# ---------------------------------------------------------------------------

def find_existing_tr(
    session: requests.Session,
    base_url: str,
    project_id: str,
    function_name: str,
    tr_title_prefix: str,
    verify_ssl: bool,
    verbose: bool = False,
    debug: bool = False,
) -> Optional[Tuple[str, str]]:
    """
    Search Polarion for an existing TR matching *tr_title_prefix**function_name*_TR_*.
    Returns (wi_full_id, title) of the first match, or None.
    """
    query = (
        f"type:wi_testResult AND NOT status:deleted AND NOT HAS_VALUE:resolution"
    )
    search_prefix = f"{tr_title_prefix}{function_name}_TR_"
    if debug:
        print(f"    [DEBUG] Existing TR query prefix: {search_prefix}")

    wi_ids = query_work_items_paginated(
        session, base_url, project_id, query, search_prefix,
        verify_ssl, verbose, debug
    )

    for wi_id in wi_ids:
        short_id = extract_short_id(wi_id)
        url = f"{base_url}/projects/{project_id}/workitems/{short_id}"
        params = {"fields[workitems]": "title"}
        resp = session.get(url, params=params, verify=verify_ssl)
        if resp.status_code != 200:
            continue
        title = resp.json().get("data", {}).get("attributes", {}).get("title", "")
        if title.startswith(f"{tr_title_prefix}{function_name}_TR_"):
            return (wi_id, title)
        elif verbose:
            print(f"    [VERBOSE] Skipping '{title}' — does not match {tr_title_prefix}{function_name}_TR_*")

    return None


def get_or_create_test_result(
    session: requests.Session,
    base_url: str,
    project_id: str,
    function_name: str,
    component: str,
    tr_title_prefix: str,
    is_bsp: bool,
    verify_ssl: bool,
    dry_run: bool = True,
    verbose: bool = False,
    debug: bool = False,
) -> Tuple[Optional[str], bool]:
    """
    Find an existing TR or create a new one for *function_name*.
    Returns (wi_full_id, is_new).  wi_full_id may be 'DRY_RUN' in dry-run mode.
    """
    title = f"{tr_title_prefix}{function_name}_TR_1"

    # Check for existing TR first
    existing = find_existing_tr(
        session, base_url, project_id, function_name, tr_title_prefix,
        verify_ssl, verbose, debug,
    )
    if existing:
        ex_id, ex_title = existing
        ex_short = extract_short_id(ex_id)
        print(f"\n  Existing TR found: {ex_short} — {ex_title}")
        print(f"    Reusing existing work item (no creation needed)")
        return (ex_id, False)

    # No existing TR — create one
    print(f"\n  Creating TR: {title}")
    print(f"    Component: {component}")

    if dry_run:
        print(f"    [DRY RUN] Would create work item '{title}'")
        return ("DRY_RUN", True)

    url = f"{base_url}/projects/{project_id}/workitems"
    wi_data: dict = {
        "type": "workitems",
        "attributes": {
            "type": "wi_testResult",
            "title": title,
            "status": "draft",
            "fld_component": f"comp_{component}",
        },
    }
    if is_bsp:
        wi_data["relationships"] = {
            "categories": {
                "data": [{"type": "categories", "id": f"{project_id}/cat_BSP_POS"}]
            },
            "fld_category": {
                "data": {"type": "categories", "id": f"{project_id}/cat_BSP_POS"}
            },
        }
    payload = {"data": [wi_data]}

    if verbose:
        print(f"    [VERBOSE] POST {url}")
    if debug:
        print(f"    [DEBUG] Payload: {json.dumps(payload, indent=2)}")

    resp = session.post(url, json=payload, verify=verify_ssl)
    if debug:
        print(f"    [DEBUG] Response {resp.status_code}: {resp.text[:500]}")
    if resp.status_code in (200, 201):
        resp_data = resp.json().get("data", [])
        if isinstance(resp_data, list) and resp_data:
            created_data = resp_data[0]
        else:
            created_data = resp_data if isinstance(resp_data, dict) else {}
        created_id = created_data.get("id", "?")
        short_id = extract_short_id(created_id)
        print(f"    ✓ Created: {short_id}")
        return (created_id, True)
    else:
        print(f"    ✗ Error creating work item: {resp.status_code}")
        print(f"      Response: {resp.text[:500]}")
        return (None, False)


# ---------------------------------------------------------------------------
# Phase 3 — Link TR → TP (derived_from)
# ---------------------------------------------------------------------------

def find_tps_for_tcs(
    session: requests.Session,
    base_url: str,
    project_id: str,
    tc_wi_ids: List[str],
    verify_ssl: bool,
    verbose: bool = False,
    debug: bool = False,
) -> List[Tuple[str, str]]:
    """
    Find all TPs that implement the given TCs using reverse-link queries.
    This discovers cross-function TPs (e.g. "same as" scenarios where a TP
    from function B implements a TC from function A).
    Returns deduplicated list of (tp_wi_id, tp_title).
    """
    url = f"{base_url}/projects/{project_id}/workitems"
    seen_tp_ids: set = set()
    results: List[Tuple[str, str]] = []

    for tc_wi_id in tc_wi_ids:
        tc_short = extract_short_id(tc_wi_id)
        escaped_id = tc_short.replace("-", "\\-")
        query = (
            f"type:wi_testProcedure AND NOT status:deleted"
            f" AND NOT HAS_VALUE:resolution AND linkedWorkItems:{escaped_id}"
        )
        params = {"query": query, "fields[workitems]": "id,title"}
        if debug:
            print(f"    [DEBUG] Reverse-link query for TC {tc_short}: {query}")
        resp = session.get(url, params=params, verify=verify_ssl)
        if resp.status_code != 200:
            if verbose:
                print(f"    [VERBOSE] Reverse-link query failed for TC {tc_short}: {resp.status_code}")
            continue
        items = resp.json().get("data", [])
        for item in items:
            tp_id = item.get("id", "")
            if tp_id and tp_id not in seen_tp_ids:
                seen_tp_ids.add(tp_id)
                tp_title = item.get("attributes", {}).get("title", "")
                results.append((tp_id, tp_title))
                if verbose:
                    print(f"    [VERBOSE] TC {tc_short} → TP {extract_short_id(tp_id)} ({tp_title})")

    return sorted(results, key=lambda x: x[1])


def get_existing_linked_tp_ids(
    session: requests.Session,
    base_url: str,
    project_id: str,
    tr_wi_id: str,
    verify_ssl: bool,
    verbose: bool = False,
    debug: bool = False,
) -> set:
    """Fetch the set of WI short IDs already linked from *tr_wi_id* via derived_from."""
    tr_short = extract_short_id(tr_wi_id)
    url = f"{base_url}/projects/{project_id}/workitems/{tr_short}/linkedworkitems"
    params = {"fields[linkedworkitems]": "id", "query": "role:derived_from"}
    if debug:
        print(f"    [DEBUG] GET {url} params={params}")
    resp = session.get(url, params=params, verify=verify_ssl)
    if resp.status_code != 200:
        if verbose:
            print(f"    [VERBOSE] Could not fetch existing links for {tr_short}: {resp.status_code}")
        return set()
    items = resp.json().get("data", [])
    linked_ids = set()
    for item in items:
        if isinstance(item, dict) and "id" in item:
            # id format is typically "project/TR_ID/project/TP_ID" or similar
            linked_ids.add(item["id"])
    if debug:
        print(f"    [DEBUG] Existing derived_from links on {tr_short}: {linked_ids}")
    return linked_ids


def link_tr_to_tp(
    session: requests.Session,
    base_url: str,
    project_id: str,
    tr_wi_id: str,
    tp_wi_id: str,
    tp_title: str,
    existing_linked_ids: set,
    verify_ssl: bool,
    dry_run: bool = True,
    verbose: bool = False,
    debug: bool = False,
) -> bool:
    tr_short = extract_short_id(tr_wi_id)
    tp_short = extract_short_id(tp_wi_id)

    # Check if this TP is already linked (match tp_short anywhere in existing link IDs)
    if any(tp_short in linked_id for linked_id in existing_linked_ids):
        print(f"    ⏭ TR {tr_short} → TP {tp_short} already linked, skipping ({tp_title})")
        return True

    if dry_run:
        print(f"    [DRY RUN] Would link TR {tr_short} → derived_from → TP {tp_short} ({tp_title})")
        return True

    url = f"{base_url}/projects/{project_id}/workitems/{tr_short}/linkedworkitems"
    payload = {
        "data": [
            {
                "type": "linkedworkitems",
                "attributes": {"role": "derived_from", "suspect": False},
                "relationships": {
                    "workItem": {
                        "data": {
                            "type": "workitems",
                            "id": f"{project_id}/{tp_short}",
                        }
                    }
                },
            }
        ]
    }

    if verbose:
        print(f"    [VERBOSE] POST {url}")
    if debug:
        print(f"    [DEBUG] Payload: {json.dumps(payload, indent=2)}")

    resp = session.post(url, json=payload, verify=verify_ssl)
    if debug:
        print(f"    [DEBUG] Response {resp.status_code}: {resp.text[:300]}")
    if resp.status_code in (200, 201, 204):
        print(f"    ✓ Linked TR {tr_short} → derived_from → TP {tp_short} ({tp_title})")
        return True
    else:
        print(f"    ✗ Failed to link TR {tr_short} → TP {tp_short}: {resp.status_code}")
        if verbose:
            print(f"      Response: {resp.text[:300]}")
        return False


# ---------------------------------------------------------------------------
# Phase 4 — Discover log files
# ---------------------------------------------------------------------------

def discover_log_files(
    repo_path: str,
    component: str,
    is_bsp: bool,
    verbose: bool = False,
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """
    Recursively search for log files under the component directory.

    Returns:
        plain_logs: list of absolute paths to .log files
        zip_logs: list of (zip_abs_path, entry_name) for .log entries inside .zip files
    """
    search_root = os.path.join(repo_path, "Informal_Test_Results", "Automated", component)
    if not os.path.isdir(search_root):
        print(f"  ⚠ Log directory not found: {search_root}")
        return [], []

    print(f"  Searching for logs under: {search_root}")

    plain_logs: List[str] = []
    zip_logs: List[Tuple[str, str]] = []

    for dirpath, _dirnames, filenames in os.walk(search_root):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            if fname.lower().endswith((".log", ".txt")):
                plain_logs.append(fpath)
            elif fname.lower().endswith(".zip"):
                try:
                    with zipfile.ZipFile(fpath, "r") as zf:
                        for entry in zf.namelist():
                            if entry.lower().endswith((".log", ".txt")):
                                zip_logs.append((fpath, entry))
                except (zipfile.BadZipFile, OSError) as e:
                    if verbose:
                        print(f"  [VERBOSE] Could not read zip {fpath}: {e}")

    print(f"  Found {len(plain_logs)} .log/.txt file(s) and {len(zip_logs)} .log/.txt entries inside .zip files")
    return plain_logs, zip_logs


# ---------------------------------------------------------------------------
# Phase 5 — Link logs → TR (ref_src hyperlinks)
# ---------------------------------------------------------------------------

def _read_log_content(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _read_zip_log_content(zip_path: str, entry_name: str) -> str:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return zf.read(entry_name).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, OSError, KeyError):
        return ""


def _build_relative_log_path(abs_path: str, repo_path: str) -> str:
    return os.path.relpath(abs_path, repo_path).replace("\\", "/")


def match_logs_to_trs(
    plain_logs: List[str],
    zip_logs: List[Tuple[str, str]],
    tc_by_function: Dict[str, List[str]],
    repo_path: str,
    verbose: bool = False,
) -> Dict[str, List[str]]:
    """
    For each log file, check if any TC name appears in its content.
    Returns {function_name: [relative_log_path, ...]}.
    """
    # Build a flat lookup: tc_title → function_name
    tc_to_func: Dict[str, str] = {}
    for func, titles in tc_by_function.items():
        for t in titles:
            tc_to_func[t] = func

    all_tc_names = list(tc_to_func.keys())
    func_logs: Dict[str, List[str]] = {}

    # Plain .log files
    for log_path in plain_logs:
        content = _read_log_content(log_path)
        if not content:
            continue
        matched_funcs: set = set()
        for tc_name in all_tc_names:
            if tc_name in content:
                matched_funcs.add(tc_to_func[tc_name])
        for func in matched_funcs:
            rel = _build_relative_log_path(log_path, repo_path)
            func_logs.setdefault(func, []).append(rel)
            if verbose:
                print(f"    [VERBOSE] Log '{rel}' matched function '{func}'")

    # Logs inside .zip files — URL points to the .zip file
    seen_zip_func: set = set()
    for zip_path, entry_name in zip_logs:
        content = _read_zip_log_content(zip_path, entry_name)
        if not content:
            continue
        matched_funcs = set()
        for tc_name in all_tc_names:
            if tc_name in content:
                matched_funcs.add(tc_to_func[tc_name])
        for func in matched_funcs:
            rel = _build_relative_log_path(zip_path, repo_path)
            key = (func, rel)
            if key not in seen_zip_func:
                seen_zip_func.add(key)
                func_logs.setdefault(func, []).append(rel)
                if verbose:
                    print(f"    [VERBOSE] Zip log '{rel}!{entry_name}' matched function '{func}'")

    return func_logs


def update_tr_hyperlinks(
    session: requests.Session,
    base_url: str,
    project_id: str,
    tr_wi_id: str,
    log_urls: List[str],
    verify_ssl: bool,
    dry_run: bool = True,
    verbose: bool = False,
    debug: bool = False,
) -> bool:
    tr_short = extract_short_id(tr_wi_id)

    if dry_run:
        for url in log_urls:
            print(f"    [DRY RUN] Would add hyperlink ref_src → {url}")
        return True

    # Fetch existing hyperlinks to preserve them
    wi_url = f"{base_url}/projects/{project_id}/workitems/{tr_short}"
    params = {"fields[workitems]": "hyperlinks"}
    resp = session.get(wi_url, params=params, verify=verify_ssl)
    existing_links = []
    if resp.status_code == 200:
        existing_links = resp.json().get("data", {}).get("attributes", {}).get("hyperlinks", [])

    existing_uris = {link.get("uri", "") for link in existing_links}
    new_links = list(existing_links)
    added = 0
    for url in log_urls:
        if url not in existing_uris:
            new_links.append({"role": "ref_src", "uri": url})
            added += 1

    if added == 0:
        print(f"    All log hyperlinks already present on {tr_short}, skipping update")
        return True

    patch_payload = {
        "data": {
            "type": "workitems",
            "id": f"{project_id}/{tr_short}",
            "attributes": {"hyperlinks": new_links},
        }
    }

    if verbose:
        print(f"    [VERBOSE] PATCH {wi_url}")
    if debug:
        print(f"    [DEBUG] Payload: {json.dumps(patch_payload, indent=2)}")

    resp = session.patch(wi_url, json=patch_payload, verify=verify_ssl)
    if debug:
        print(f"    [DEBUG] Response {resp.status_code}: {resp.text[:300]}")
    if resp.status_code in (200, 204):
        print(f"    ✓ Added {added} log hyperlink(s) to TR {tr_short}")
        return True
    else:
        print(f"    ✗ Failed to update hyperlinks on TR {tr_short}: {resp.status_code}")
        if verbose:
            print(f"      Response: {resp.text[:300]}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Create Polarion Test Result work items from Test Cases, link to TPs, and attach log references.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --component BOOT_APP0 --bsp --pattern nvme_qpair_ --dry-run
  %(prog)s --component BOOT_APP0 --bl  --pattern bootApp_ nvme_ctrlr_ --execute
  %(prog)s --component BOOT_APP0 --bsp --pattern nvme_ --debug
        """,
    )

    parser.add_argument(
        "--component", required=True,
        help="Component name (e.g. BOOT_APP0)",
    )
    repo_group = parser.add_mutually_exclusive_group(required=True)
    repo_group.add_argument(
        "--bl", action="store_true",
        help="Use SBL_06_Integration_Testing repository layout",
    )
    repo_group.add_argument(
        "--bsp", action="store_true",
        help="Use BSP_06_Integration_Testing repository layout",
    )
    parser.add_argument(
        "--pattern", nargs="*", default=[],
        help="One or more function prefixes to search TCs (e.g. nvme_qpair_ nvme_ctrlr_cmd). "
             "If omitted, all TCs for the component are used — fails if ≥100 are found.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Show what would be changed without making actual changes (default)",
    )
    mode_group.add_argument(
        "--execute", action="store_true",
        help="Actually execute the changes (overrides --dry-run)",
    )
    parser.add_argument(
        "--project-id",
        help="Polarion project ID (overrides POLARION_PROJECT_ID env var)",
    )
    parser.add_argument(
        "--verify-ssl", action="store_true", default=False,
        help="Enable SSL certificate verification",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose output",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug output (implies --verbose, shows raw API requests/responses)",
    )

    args = parser.parse_args()
    dry_run = not args.execute
    if args.debug:
        args.verbose = True

    # ---- Environment variables ----
    base_url = os.environ.get("POLARION_API_BASE", "").rstrip("/")
    pat = os.environ.get("POLARION_PAT", "")
    project_id = args.project_id or os.environ.get("POLARION_PROJECT_ID", "")
    integration_repo_path = os.environ.get("INTEGRATION_REPO_PATH", "")

    missing = []
    if not base_url:
        missing.append("POLARION_API_BASE")
    if not pat:
        missing.append("POLARION_PAT")
    if not project_id:
        missing.append("POLARION_PROJECT_ID (or use --project-id)")
    if not integration_repo_path:
        missing.append("INTEGRATION_REPO_PATH")
    if missing:
        print("Error: Missing required environment variables:")
        for var in missing:
            print(f"  - {var}")
        sys.exit(1)

    if not os.path.isdir(integration_repo_path):
        print(f"Error: INTEGRATION_REPO_PATH does not exist: {integration_repo_path}")
        sys.exit(1)

    # ---- Repository selection ----
    is_bsp = args.bsp
    if is_bsp:
        repo_name = "BSP_06_Integration_Testing"
        tr_title_prefix = f"POSBSP_{args.component}_"
    else:
        repo_name = "SBL_06_Integration_Testing"
        tr_title_prefix = ""

    gitlab_base = f"https://ccn-gitlab.wrs.com/shallowford/cert/{repo_name}/-/blob/main"

    print("=" * 60)
    if dry_run:
        print("DRY RUN MODE — No changes will be made")
    else:
        print("EXECUTE MODE — Changes will be applied!")
    print(f"Repository : {repo_name}")
    print(f"Component  : {args.component}")
    print(f"Pattern(s) : {', '.join(args.pattern) if args.pattern else '(none — all TCs)'}")
    print(f"Project    : {project_id}")
    if args.debug:
        print(f"Debug      : ON")
    print("=" * 60)

    session = create_polarion_session(base_url, pat, verify_ssl=args.verify_ssl)

    # ==================================================================
    # Git pull — update integration testing repository
    # ==================================================================
    print(f"\nGit pull: updating {integration_repo_path}")
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=integration_repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            print(f"  ✓ {result.stdout.strip()}")
        else:
            print(f"  ✗ git pull failed (exit {result.returncode})")
            print(f"    stderr: {result.stderr.strip()}")
            sys.exit(1)
    except FileNotFoundError:
        print("  ✗ git not found on PATH")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("  ✗ git pull timed out (120s)")
        sys.exit(1)

    # ==================================================================
    # No-pattern guard — verify TC count is manageable before proceeding
    # ==================================================================
    if not args.pattern:
        print(f"\nNo --pattern specified: checking TC count for component '{args.component}'...")
        _url = f"{base_url}/projects/{project_id}/workitems"
        _query = (
            f"type:wi_testCase AND NOT status:deleted AND NOT HAS_VALUE:resolution"
            f" AND fld_component.KEY:comp_{args.component}"
        )
        _resp = session.get(_url, params={"query": _query, "fields[workitems]": "id"}, verify=args.verify_ssl)
        if _resp.status_code != 200:
            print(f"  Error: Could not count TCs ({_resp.status_code}). Aborting.")
            sys.exit(1)
        _count = len(_resp.json().get("data", []))
        if _count >= 100:
            print(f"  Error: Component '{args.component}' has ≥100 Test Cases "
                  f"(API returned {_count}, likely more exist).")
            print("  Please use --pattern to narrow the search to a specific function prefix.")
            sys.exit(1)
        print(f"  {_count} TC(s) found — proceeding without pattern filter")

    # ==================================================================
    # Phase 1 — Fetch Test Cases
    # ==================================================================
    if args.pattern:
        patterns_display = ', '.join(f"'{p}*'" for p in args.pattern)
        print(f"\nPhase 1: Fetching Test Cases matching {patterns_display}")
    else:
        print(f"\nPhase 1: Fetching all Test Cases for component '{args.component}'")
    tc_by_function, tc_title_to_id = fetch_test_cases(
        session, base_url, project_id, args.pattern, args.component,
        args.verify_ssl, verbose=args.verbose, debug=args.debug,
    )

    total_tcs = sum(len(v) for v in tc_by_function.values())
    if not tc_by_function:
        print("  No matching Test Cases found. Nothing to do.")
        return

    print(f"\n  Found {total_tcs} TC(s) across {len(tc_by_function)} function(s):")
    for func in sorted(tc_by_function):
        tcs = tc_by_function[func]
        print(f"    {func}: {len(tcs)} TC(s)")
        for tc in tcs:
            print(f"      - {tc}")

    # ==================================================================
    # Phase 2 — Create or reuse Test Results
    # ==================================================================
    print(f"\nPhase 2: Resolving Test Results (create new or reuse existing)")
    tr_map: Dict[str, Optional[str]] = {}  # function_name → TR WI id
    trs_created = 0
    trs_reused = 0

    for func in sorted(tc_by_function):
        tr_id, is_new = get_or_create_test_result(
            session, base_url, project_id, func, args.component, tr_title_prefix,
            is_bsp, args.verify_ssl, dry_run=dry_run, verbose=args.verbose,
            debug=args.debug,
        )
        tr_map[func] = tr_id
        if tr_id:
            if is_new:
                trs_created += 1
            else:
                trs_reused += 1

    # ==================================================================
    # Phase 3 — Link TR → TP (derived_from)
    # ==================================================================
    print(f"\nPhase 3: Linking TRs to Test Procedures (derived_from)")
    tp_links_created = 0

    for func in sorted(tc_by_function):
        tr_id = tr_map.get(func)
        if not tr_id:
            print(f"  Skipping TP linking for '{func}' — TR creation failed")
            continue

        tr_short = extract_short_id(tr_id) if tr_id != "DRY_RUN" else f"{tr_title_prefix}{func}_TR_1"
        print(f"\n  TR {tr_short}: finding TPs that implement TCs of function '{func}'")

        # Collect TC WI IDs for this function
        tc_wi_ids = [tc_title_to_id[t] for t in tc_by_function[func] if t in tc_title_to_id]
        if not tc_wi_ids:
            print(f"    No TC WI IDs available for function '{func}'")
            continue

        tps = find_tps_for_tcs(
            session, base_url, project_id, tc_wi_ids,
            args.verify_ssl, verbose=args.verbose, debug=args.debug,
        )

        if not tps:
            print(f"    No TPs found for function '{func}'")
            continue

        print(f"    Found {len(tps)} TP(s)")

        # Fetch existing links once per TR to avoid duplicates
        existing_linked_ids = set()
        if tr_id != "DRY_RUN":
            existing_linked_ids = get_existing_linked_tp_ids(
                session, base_url, project_id, tr_id,
                args.verify_ssl, verbose=args.verbose, debug=args.debug,
            )

        for tp_id, tp_title in tps:
            ok = link_tr_to_tp(
                session, base_url, project_id, tr_id, tp_id, tp_title,
                existing_linked_ids,
                args.verify_ssl, dry_run=dry_run, verbose=args.verbose,
                debug=args.debug,
            )
            if ok:
                tp_links_created += 1

    # ==================================================================
    # Phase 4 — Discover log files
    # ==================================================================
    print(f"\nPhase 4: Discovering log files")
    plain_logs, zip_logs = discover_log_files(
        integration_repo_path, args.component, is_bsp, verbose=args.verbose,
    )

    # ==================================================================
    # Phase 5 — Link logs → TR
    # ==================================================================
    print(f"\nPhase 5: Matching logs to TRs and creating hyperlinks")
    log_links_created = 0

    if plain_logs or zip_logs:
        func_logs = match_logs_to_trs(
            plain_logs, zip_logs, tc_by_function,
            integration_repo_path, verbose=args.verbose,
        )

        for func in sorted(func_logs):
            tr_id = tr_map.get(func)
            if not tr_id:
                print(f"  Skipping log linking for '{func}' — TR not available")
                continue

            rel_paths = func_logs[func]
            log_urls = [f"{gitlab_base}/{rel}" for rel in rel_paths]

            tr_short = extract_short_id(tr_id) if tr_id != "DRY_RUN" else f"{tr_title_prefix}{func}_TR_1"
            print(f"\n  TR {tr_short}: {len(log_urls)} log reference(s) for function '{func}'")

            ok = update_tr_hyperlinks(
                session, base_url, project_id, tr_id, log_urls,
                args.verify_ssl, dry_run=dry_run, verbose=args.verbose,
                debug=args.debug,
            )
            if ok:
                log_links_created += len(log_urls)
    else:
        print("  No log files found — skipping log linking")

    # ==================================================================
    # Summary
    # ==================================================================
    print(f"\n{'=' * 60}")
    print("Summary:")
    print(f"  Test Cases fetched   : {total_tcs}")
    print(f"  Functions identified : {len(tc_by_function)}")
    print(f"  Test Results created : {trs_created}")
    print(f"  Test Results reused  : {trs_reused}")
    print(f"  TR → TP links        : {tp_links_created}")
    print(f"  Log files discovered : {len(plain_logs) + len(zip_logs)}")
    print(f"  Log → TR hyperlinks  : {log_links_created}")
    if dry_run:
        print(f"\nThis was a DRY RUN. Use --execute to apply changes.")
    print("=" * 60)


if __name__ == "__main__":
    main()
