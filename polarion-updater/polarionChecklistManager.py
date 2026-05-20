#!/usr/bin/env python3
"""
Polarion Checklist Manager

Manages wi_testcase_checklist work items in Polarion. For a given component,
the script:
  1. Fetches all test cases (wi_testCase) and groups them by function name.
  2. Fetches all existing checklists (wi_testcase_checklist).
  3. Identifies missing checklists based on function grouping.
  4. Creates missing checklists and links them to their TCs
     (checklist "contains" TC via has_parent role).

Checklist naming convention:
  - Function has both HLTC and LLTC TCs -> {function}_HLTC_LLTC_Checklist
  - Function has only HLTC TCs          -> {function}_HLTC_Checklist
  - Function has only LLTC TCs          -> {function}_LLTC_Checklist

Use --dump-fields to print all fields of an existing checklist for discovery.

Environment Variables:
    POLARION_API_BASE   - Base URL for Polarion REST API
    POLARION_PAT        - Personal Access Token (Bearer token)
    POLARION_PROJECT_ID - Project ID (e.g. Shallowford_BSP)

Usage:
    # Dump all fields of an existing checklist (discovery mode)
    python polarionChecklistManager.py --component SSD_NVME0 --dump-fields

    # Dry-run: show what would be created (default, no changes made)
    python polarionChecklistManager.py --component SSD_NVME0

    # Execute: actually create checklists and links
    python polarionChecklistManager.py --component SSD_NVME0 --execute

    # With pattern filter and verbose output
    python polarionChecklistManager.py --component SSD_NVME0 --pattern nvme_ --execute -v
"""

import os
import sys
import re
import json
import argparse
from collections import defaultdict
from typing import List, Dict, Any, Optional, Tuple

import requests
import urllib3


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TC_TYPE = "wi_testCase"
CHECKLIST_TYPE = "wi_testcase_checklist"
# Default Polarion work item type for "Application Programming Interface" items
# (one item per function in a component). Overridable with --api-type.
DEFAULT_API_TYPE = "wi_API"
# Regex to extract function name and TC type from a TC title
# e.g. "nvmeXbdStrategy_HLTC_1" -> ("nvmeXbdStrategy", "HLTC")
TC_TITLE_RE = re.compile(r'^(.+?)_(HLTC|LLTC)_\d+$')

SEP = '=' * 70


# ---------------------------------------------------------------------------
# Polarion session & helpers
# ---------------------------------------------------------------------------

def create_polarion_session(base_url: str, pat: str, verify_ssl: bool = False) -> requests.Session:
    """Create an authenticated requests session for the Polarion REST API."""
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {pat}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    session.verify = verify_ssl
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def paginated_get(session: requests.Session, url: str, params: dict,
                  verify_ssl: bool, data_key: str = "data",
                  verbose: bool = False) -> list:
    """Single GET request for Polarion REST API. Returns all items from the response."""
    resp = session.get(url, params=params, verify=verify_ssl)
    if verbose:
        print(f"  [DEBUG] GET {resp.request.url}")
        print(f"  [DEBUG] Status: {resp.status_code}")
    if resp.status_code != 200:
        if verbose:
            print(f"  [DEBUG] Response preview: {resp.text[:500]}")
        return []
    body = resp.json()
    items = body.get(data_key, [])
    if verbose:
        total = body.get("meta", {}).get("totalCount")
        print(f"  [DEBUG] Items: {len(items)}, totalCount: {total}")
    return items


def query_work_items_paginated(session: requests.Session, base_url: str,
                               project_id: str, query: str,
                               verify_ssl: bool,
                               verbose: bool = False) -> List[str]:
    """
    Query Polarion for work items, working around the 100-item API limit.
    Splits queries by title prefix (a*, b*, ...) and recurses deeper if a
    prefix hits the 100-item cap.
    Returns a list of unique work item IDs.
    """
    url = f"{base_url}/projects/{project_id}/workitems"
    wi_ids_set: set = set()

    def query_by_prefix(prefix: str) -> None:
        sub_query = f"{query} AND title:{prefix}*"
        params = {
            "query": sub_query,
            "fields[workitems]": "id",
        }
        resp = session.get(url, params=params, verify=verify_ssl)
        if verbose:
            print(f"  [DEBUG] GET {resp.request.url}")
            print(f"  [DEBUG] Status: {resp.status_code}")
        if resp.status_code != 200:
            if verbose:
                print(f"  [DEBUG] Response preview: {resp.text[:500]}")
            return
        body = resp.json()
        items = body.get("data", [])
        total = body.get("meta", {}).get("totalCount")
        ids = [item["id"] for item in items if isinstance(item, dict) and "id" in item]
        # Check if there are more items than returned (API page limit)
        need_split = (total is not None and total > len(ids)) or len(ids) >= 100
        if need_split:
            if verbose:
                print(f"  [DEBUG] title:{prefix}* -> {len(ids)} returned, total={total}, splitting deeper...")
            for c in "abcdefghijklmnopqrstuvwxyz0123456789":
                query_by_prefix(f"{prefix}{c}")
        else:
            wi_ids_set.update(ids)
            if ids and verbose:
                print(f"  [DEBUG] title:{prefix}* -> {len(ids)} items")

    for c in "abcdefghijklmnopqrstuvwxyz0123456789":
        query_by_prefix(c)

    if verbose:
        print(f"  [DEBUG] Total unique items fetched: {len(wi_ids_set)}")
    return list(wi_ids_set)


def extract_short_id(full_id: str) -> str:
    """Extract the short ID from a full 'project/SHORT-123' format."""
    if "/" in full_id:
        return full_id.split("/")[-1]
    return full_id


def fetch_work_item(session: requests.Session, base_url: str, project_id: str,
                    short_id: str, fields: str,
                    verify_ssl: bool, verbose: bool = False) -> Optional[dict]:
    """Fetch a single work item by short ID with the requested fields."""
    url = f"{base_url}/projects/{project_id}/workitems/{short_id}"
    params = {"fields[workitems]": fields}
    resp = session.get(url, params=params, verify=verify_ssl)
    if verbose:
        print(f"  [DEBUG] GET {url} -> {resp.status_code}")
    if resp.status_code != 200:
        return None
    return resp.json().get("data", {})


# ---------------------------------------------------------------------------
# API-driven function discovery
# ---------------------------------------------------------------------------

def list_component_types(session: requests.Session, base_url: str,
                         project_id: str, component: str,
                         verify_ssl: bool, verbose: bool = False) -> None:
    """
    Discovery helper: list distinct work item types found in the component,
    with counts. Useful for identifying the correct --api-type value the
    first time the script is used against a new Polarion setup.
    """
    print(f"\n{SEP}")
    print(f"LIST TYPES MODE — distinct work item types for component: {component}")
    print(SEP)

    url = f"{base_url}/projects/{project_id}/workitems"
    query = (
        f"fld_component.KEY:comp_{component} AND "
        f"NOT status:deleted AND NOT HAS_VALUE:resolution"
    )
    params = {"query": query, "fields[workitems]": "id,type"}
    if verbose:
        print(f"  [DEBUG] Query: {query}")
    resp = session.get(url, params=params, verify=verify_ssl)
    if resp.status_code != 200:
        print(f"  Error: HTTP {resp.status_code}")
        print(f"  {resp.text[:500]}")
        return
    body = resp.json()
    items = body.get("data", [])
    total = body.get("meta", {}).get("totalCount")
    counts: Dict[str, int] = defaultdict(int)
    for item in items:
        wi_type = item.get("attributes", {}).get("type", "<unknown>")
        counts[wi_type] += 1
    print(f"\n  Sampled {len(items)} item(s) (totalCount={total}) — counts may be truncated:")
    for wi_type in sorted(counts.keys()):
        print(f"    {wi_type:40s} {counts[wi_type]:5d}")
    if total is not None and total > len(items):
        print(f"\n  NOTE: Only the first page was sampled. Real counts per type")
        print(f"        will be higher. The list of distinct types above is")
        print(f"        usually still complete enough to identify --api-type.")
    print(f"\n{SEP}")


def discover_function_names_via_api(session: requests.Session, base_url: str,
                                    project_id: str, component: str,
                                    api_type: str, verify_ssl: bool,
                                    verbose: bool = False
                                    ) -> Optional[List[str]]:
    """
    Fetch all API work items (one per function) for the given component and
    return their titles as a list of function names.

    Returns None if the result is truncated (totalCount > returned items),
    in which case the caller should print a helpful message asking the user
    to narrow with --pattern. Returns an empty list if the type yields
    nothing (likely wrong --api-type).
    """
    url = f"{base_url}/projects/{project_id}/workitems"
    query = (
        f"type:{api_type} AND "
        f"fld_component.KEY:comp_{component} AND "
        f"NOT status:deleted"
    )
    params = {"query": query, "fields[workitems]": "id,title"}
    if verbose:
        print(f"  [DEBUG] Query: {query}")
    resp = session.get(url, params=params, verify=verify_ssl)
    if verbose:
        print(f"  [DEBUG] GET {resp.request.url} -> {resp.status_code}")
    if resp.status_code != 200:
        print(f"  Error fetching API items: HTTP {resp.status_code}")
        if verbose:
            print(f"  Response: {resp.text[:500]}")
        return []
    body = resp.json()
    items = body.get("data", [])
    total = body.get("meta", {}).get("totalCount")
    names: List[str] = []
    for item in items:
        title = item.get("attributes", {}).get("title", "").strip()
        if title:
            names.append(title)
    if verbose:
        print(f"  [DEBUG] API items returned: {len(items)}, totalCount={total}")
    if total is not None and total > len(items):
        # Truncated — caller should abort with a useful message
        print(f"\n  WARNING: Component '{component}' has {total} API work items, but")
        print(f"  only {len(items)} were returned in a single page. The script")
        print(f"  cannot reliably enumerate every function without a recursive")
        print(f"  search. Re-run with --pattern <prefix> to narrow the scope,")
        print(f"  e.g. --pattern nvme_ (multiple prefixes are allowed).")
        return None
    return names


def fetch_tcs_for_function(session: requests.Session, base_url: str,
                           project_id: str, component: str,
                           func_name: str, verify_ssl: bool,
                           verbose: bool = False) -> List[str]:
    """
    Fetch all TC work item IDs for a single function via title:<func>_*.
    Returns a list of full WI IDs. Prints a warning if a single function's
    TC count exceeds one page (very rare).
    """
    url = f"{base_url}/projects/{project_id}/workitems"
    query = (
        f"type:{TC_TYPE} AND "
        f"fld_component.KEY:comp_{component} AND "
        f"NOT status:deleted AND NOT HAS_VALUE:resolution AND "
        f"title:{func_name}_*"
    )
    params = {"query": query, "fields[workitems]": "id"}
    resp = session.get(url, params=params, verify=verify_ssl)
    if resp.status_code != 200:
        if verbose:
            print(f"  [DEBUG] GET {url} -> {resp.status_code}")
            print(f"  [DEBUG] Response: {resp.text[:500]}")
        return []
    body = resp.json()
    items = body.get("data", [])
    total = body.get("meta", {}).get("totalCount")
    ids = [item["id"] for item in items if isinstance(item, dict) and "id" in item]
    if verbose:
        print(f"  [DEBUG] title:{func_name}_* -> {len(ids)} TCs (totalCount={total})")
    if total is not None and total > len(ids):
        print(f"    WARNING: function '{func_name}' has {total} TCs, only {len(ids)}")
        print(f"             were returned. Some TCs will be missed for this function.")
    return ids


# ---------------------------------------------------------------------------
# Dump fields mode
# ---------------------------------------------------------------------------

def dump_checklist_fields(session: requests.Session, base_url: str,
                          project_id: str, component: str,
                          verify_ssl: bool, verbose: bool) -> None:
    """
    Fetch one existing checklist for the given component and print ALL its
    fields (attributes, relationships, linked work items). Useful for
    discovering what fields a checklist has before creating new ones.
    """
    print(f"\n{SEP}")
    print(f"DUMP FIELDS MODE — fetching one checklist for component: {component}")
    print(SEP)

    # Query for one checklist
    query = (
        f"type:{CHECKLIST_TYPE} AND "
        f"fld_component.KEY:comp_{component} AND "
        f"NOT status:deleted"
    )
    url = f"{base_url}/projects/{project_id}/workitems"
    params = {
        "query": query,
        "fields[workitems]": "id",
    }
    if verbose:
        print(f"  [DEBUG] Query: {query}")

    items = paginated_get(session, url, params, verify_ssl, verbose=verbose)
    if not items:
        print("  No checklists found for this component. Cannot dump fields.")
        return

    # Pick the first one
    first_id = items[0].get("id", "")
    short_id = extract_short_id(first_id)
    print(f"\n  Selected checklist: {short_id}")

    # Fetch with ALL fields
    wi_url = f"{base_url}/projects/{project_id}/workitems/{short_id}"
    resp = session.get(wi_url, params={"fields[workitems]": "@all"}, verify=verify_ssl)
    if resp.status_code != 200:
        print(f"  Error fetching checklist: {resp.status_code}")
        return

    data = resp.json().get("data", {})

    # Print attributes
    print(f"\n  --- ATTRIBUTES ---")
    attrs = data.get("attributes", {})
    for key in sorted(attrs.keys()):
        val = attrs[key]
        # Truncate long rich-text values for readability
        if isinstance(val, dict) and "value" in val:
            text = val["value"]
            if len(str(text)) > 200:
                text = str(text)[:200] + "..."
            print(f"    {key}: {text}")
        elif isinstance(val, str) and len(val) > 200:
            print(f"    {key}: {val[:200]}...")
        else:
            print(f"    {key}: {val}")

    # Print relationships
    print(f"\n  --- RELATIONSHIPS ---")
    rels = data.get("relationships", {})
    for key in sorted(rels.keys()):
        rel_data = rels[key].get("data")
        if rel_data is None:
            print(f"    {key}: (none)")
        elif isinstance(rel_data, list):
            print(f"    {key}: [{len(rel_data)} items]")
            for item in rel_data[:10]:
                print(f"      - type={item.get('type')}, id={item.get('id')}")
        elif isinstance(rel_data, dict):
            print(f"    {key}: type={rel_data.get('type')}, id={rel_data.get('id')}")
        else:
            print(f"    {key}: {rel_data}")

    # Print linked work items
    print(f"\n  --- LINKED WORK ITEMS ---")
    links_url = f"{base_url}/projects/{project_id}/workitems/{short_id}/linkedworkitems"
    links_resp = session.get(links_url, params={"fields[linkedworkitems]": "@all"}, verify=verify_ssl)
    if links_resp.status_code == 200:
        links = links_resp.json().get("data", [])
        print(f"    Outgoing links: {len(links)}")
        for link in links:
            role = link.get("attributes", {}).get("role", "?")
            suspect = link.get("attributes", {}).get("suspect", "?")
            link_id = link.get("id", "?")
            target = link.get("relationships", {}).get("workItem", {}).get("data", {}).get("id", "?")
            print(f"      role={role}, suspect={suspect}, target={target}, id={link_id}")
    else:
        print(f"    Error fetching links: {links_resp.status_code}")

    # Reverse links: work items that link TO this checklist
    print(f"\n  --- REVERSE-LINKED WORK ITEMS (items linking TO this checklist) ---")
    escaped_id = short_id.replace("-", "\\-")
    rev_query = f"NOT HAS_VALUE:resolution AND NOT status:deleted AND linkedWorkItems:{escaped_id}"
    rev_params = {
        "query": rev_query,
        "fields[workitems]": "id,title,type",
    }
    rev_items = paginated_get(session, f"{base_url}/projects/{project_id}/workitems",
                              rev_params, verify_ssl, verbose=verbose)
    print(f"    Reverse-linked items: {len(rev_items)}")
    for item in rev_items[:20]:
        item_short = extract_short_id(item.get("id", ""))
        title = item.get("attributes", {}).get("title", "")
        wi_type = item.get("attributes", {}).get("type", "")
        print(f"      {item_short} [{wi_type}] {title}")

    print(f"\n{SEP}")
    print("Done. Use the above to decide which fields to set on new checklists.")
    print(SEP)


def dump_tc_fields(session: requests.Session, base_url: str,
                   project_id: str, component: str,
                   verify_ssl: bool, verbose: bool,
                   pattern: Optional[List[str]] = None) -> None:
    """
    Fetch one existing TC for the given component and print ALL its
    fields (attributes, relationships). Useful for discovering TC field names.
    """
    print(f"\n{SEP}")
    print(f"DUMP TC FIELDS MODE — fetching one TC for component: {component}")
    print(SEP)

    query = (
        f"type:{TC_TYPE} AND "
        f"fld_component.KEY:comp_{component} AND "
        f"NOT status:deleted AND "
        f"NOT HAS_VALUE:resolution"
    )
    if pattern:
        query += f" AND title:{pattern[0].rstrip('*')}*"

    url = f"{base_url}/projects/{project_id}/workitems"
    params = {
        "query": query,
        "fields[workitems]": "id",
    }
    if verbose:
        print(f"  [DEBUG] Query: {query}")

    items = paginated_get(session, url, params, verify_ssl, verbose=verbose)
    if not items:
        print("  No TCs found for this component. Cannot dump fields.")
        return

    first_id = items[0].get("id", "")
    short_id = extract_short_id(first_id)
    print(f"\n  Selected TC: {short_id}")

    wi_url = f"{base_url}/projects/{project_id}/workitems/{short_id}"
    resp = session.get(wi_url, params={"fields[workitems]": "@all"}, verify=verify_ssl)
    if resp.status_code != 200:
        print(f"  Error fetching TC: {resp.status_code}")
        return

    data = resp.json().get("data", {})

    print(f"\n  --- ATTRIBUTES ---")
    attrs = data.get("attributes", {})
    for key in sorted(attrs.keys()):
        val = attrs[key]
        if isinstance(val, dict) and "value" in val:
            text = val["value"]
            if len(str(text)) > 200:
                text = str(text)[:200] + "..."
            print(f"    {key}: {text}")
        elif isinstance(val, str) and len(val) > 200:
            print(f"    {key}: {val[:200]}...")
        else:
            print(f"    {key}: {val}")

    print(f"\n  --- RELATIONSHIPS ---")
    rels = data.get("relationships", {})
    for key in sorted(rels.keys()):
        rel_data = rels[key].get("data")
        if rel_data is None:
            print(f"    {key}: (none)")
        elif isinstance(rel_data, list):
            print(f"    {key}: [{len(rel_data)} items]")
            for item in rel_data[:10]:
                print(f"      - type={item.get('type')}, id={item.get('id')}")
        elif isinstance(rel_data, dict):
            print(f"    {key}: type={rel_data.get('type')}, id={rel_data.get('id')}")
        else:
            print(f"    {key}: {rel_data}")

    print(f"\n{SEP}")
    print("Done.")
    print(SEP)


# ---------------------------------------------------------------------------
# TC fetching & grouping
# ---------------------------------------------------------------------------

def fetch_tc_titles(session: requests.Session, base_url: str, project_id: str,
                    wi_ids: List[str], verify_ssl: bool,
                    verbose: bool = False,
                    limit: int = 0,
                    tc_io: Optional[Dict[str, dict]] = None) -> Dict[str, str]:
    """
    Fetch titles (and optionally inputs/outputs) for a list of TC work item IDs.
    Returns dict: short_id -> title.
    If tc_io dict is provided, populates it with {short_id: {"inputs": ..., "outputs": ...}}.
    """
    fields = "title,fld_inputs,fld_outputs,fld_initialConditions" if tc_io is not None else "title"
    titles: Dict[str, str] = {}
    for i, wi_id in enumerate(wi_ids):
        if limit and i >= limit:
            break
        short_id = extract_short_id(wi_id)
        data = fetch_work_item(session, base_url, project_id, short_id,
                               fields, verify_ssl, verbose=verbose)
        if data:
            attrs = data.get("attributes", {})
            title = attrs.get("title", "")
            if title:
                titles[short_id] = title
            if tc_io is not None:
                tc_io[short_id] = {
                    "inputs": attrs.get("fld_inputs"),
                    "outputs": attrs.get("fld_outputs"),
                    "initialConditions": attrs.get("fld_initialConditions"),
                }
    return titles


def group_tcs_by_function(tc_titles: Dict[str, str],
                          verbose: bool = False
                          ) -> Dict[str, Dict[str, List[Tuple[str, str]]]]:
    """
    Group test cases by function name extracted from their title.
    Returns: {function_name: {"HLTC": [(short_id, title), ...],
                              "LLTC": [(short_id, title), ...]}}
    """
    groups: Dict[str, Dict[str, List[Tuple[str, str]]]] = defaultdict(
        lambda: {"HLTC": [], "LLTC": []}
    )

    for short_id, title in tc_titles.items():
        m = TC_TITLE_RE.match(title)
        if m:
            func_name = m.group(1)
            tc_type = m.group(2)  # "HLTC" or "LLTC"
            groups[func_name][tc_type].append((short_id, title))
        elif verbose:
            print(f"  [DEBUG] TC title does not match pattern: {short_id} - {title}")

    # Sort TC lists within each group by title
    for func_name in groups:
        for tc_type in ("HLTC", "LLTC"):
            groups[func_name][tc_type].sort(key=lambda x: x[1])

    return dict(groups)


def expected_checklist_name(func_name: str,
                            has_hltc: bool, has_lltc: bool) -> Optional[str]:
    """
    Determine the expected checklist title for a function based on its TC types.
    Returns None if the function has no TCs (shouldn't happen).
    """
    if has_hltc and has_lltc:
        return f"{func_name}_HLTC_LLTC_Checklist"
    elif has_hltc:
        return f"{func_name}_HLTC_Checklist"
    elif has_lltc:
        return f"{func_name}_LLTC_Checklist"
    return None


# ---------------------------------------------------------------------------
# TC input/output analysis
# ---------------------------------------------------------------------------

MINMAX_TOKEN_RE = re.compile(
    r'\[(INT_MIN|INT_MAX|UINT_MAX|UINT_MIN|LONG_MIN|LONG_MAX|ULONG_MAX|'
    r'SHORT_MIN|SHORT_MAX|USHORT_MAX|CHAR_MIN|CHAR_MAX|UCHAR_MAX|SIZE_MAX)\]',
    re.IGNORECASE,
)
STANDALONE_NUMBER_RE = re.compile(r'^-?\d+$')


def _extract_cell_values(html_field: Any) -> List[str]:
    """Extract individual cell/element values from an HTML rich-text field."""
    if isinstance(html_field, dict):
        html_field = html_field.get("value", "")
    if not isinstance(html_field, str):
        return []
    # Replace HTML tags with newlines to isolate individual cell values
    text = re.sub(r'<[^>]+>', '\n', html_field)
    return [v.strip() for v in text.splitlines() if v.strip()]


def _has_minmax_values(html_field: Any) -> bool:
    """Check if a TC field contains standalone min/max tokens, zero, or bare numbers."""
    for val in _extract_cell_values(html_field):
        if MINMAX_TOKEN_RE.search(val):
            return True
        if val.lower() == "zero":
            return True
        if STANDALONE_NUMBER_RE.match(val):
            return True
    return False


def _count_field_values(tc_io: Dict[str, dict], tc_ids: List[str], field: str) -> int:
    """Count total non-empty cell values across TCs for a given IO field."""
    count = 0
    for sid in tc_ids:
        count += len(_extract_cell_values(tc_io.get(sid, {}).get(field)))
    return count


def _has_pdi_in_initial_conditions(tc_io: Dict[str, dict], tc_ids: List[str]) -> bool:
    """Check if any TC's fld_initialConditions contains 'pdi'."""
    for sid in tc_ids:
        ic = tc_io.get(sid, {}).get("initialConditions")
        if isinstance(ic, dict):
            ic = ic.get("value", "")
        if isinstance(ic, str) and "pdi" in ic.lower():
            return True
    return False


# ---------------------------------------------------------------------------
# Checklist creation & linking
# ---------------------------------------------------------------------------

def _tc_level_value(has_hltc: bool, has_lltc: bool) -> str:
    """Determine the fld_testCaseLevel value based on TC types present."""
    if has_hltc and has_lltc:
        return "hltc_lltc"
    elif has_hltc:
        return "hltc"
    return "lltc"


def create_checklist(session: requests.Session, base_url: str, project_id: str,
                     title: str, component: str, author: Optional[str],
                     has_hltc: bool, has_lltc: bool,
                     has_minmax_inputs: bool, has_minmax_outputs: bool,
                     input_count: int, output_count: int,
                     has_integer_inflections: bool, has_pdi: bool,
                     verify_ssl: bool, dry_run: bool,
                     verbose: bool = False) -> Optional[str]:
    """
    Create a new wi_testcase_checklist work item in Polarion with all
    checklist-specific fields pre-filled.
    Returns the short ID of the created item, or None on failure / dry-run.
    """
    if dry_run:
        print(f"    [DRY RUN] Would create checklist: {title}")
        print(f"      minMaxInputs:  {'Pass' if has_minmax_inputs else 'N/A'}")
        print(f"      minMaxOutputs: {'Pass' if has_minmax_outputs else 'N/A'}")
        print(f"      twoInputValues:    {'Pass' if input_count >= 1 else 'N/A'} ({input_count} inputs)")
        print(f"      twoOutputValues:   {'Pass' if output_count >= 1 else 'N/A'} ({output_count} outputs)")
        print(f"      inputsIndependent: {'Pass' if input_count >= 2 else 'N/A'}")
        print(f"      outputsIndependent:{'Pass' if output_count >= 2 else 'N/A'}")
        print(f"      integerInflections:{'Pass' if has_integer_inflections else 'N/A'}")
        print(f"      PDI fields:        {'Pass' if has_pdi else 'N/A'}")
        return None

    tc_level = _tc_level_value(has_hltc, has_lltc)

    # --- Build attributes with all checklist fields pre-filled ---
    attributes = {
        "type": CHECKLIST_TYPE,
        "title": title,
        "status": "draft",
        "priority": 50.0,
        "severity": "normal",
        # Test case level (hltc / lltc / hltc_lltc)
        "fld_testCaseLevel": tc_level,
        # --- Fields that always pass ---
        "fld_boundary": "enu_chkPass",
        "fld_effects": "enu_chkPass",
        "fld_fullyVerified": "enu_chkPass",
        "fld_initialConditions": "enu_chkPass",
        "fld_robustnessChkList": "enu_chkPass",
        "fld_testCaseDescription": "enu_chkPass",
        "fld_traceability": "enu_chkPass",
        # --- Fields that always N/A ---
        "fld_floatInflections": "enu_chkNA",
        "fld_floatInflections_justification": "No float or real numbers are used to verify conditions.",
        "fld_multicore": "enu_chkNA",
        "fld_multicore_justification": "No multicore used.",
        "fld_outputCriteria": "enu_chkNA",
        "fld_outputCriteria_justification": "No PDI usage.",
        # --- Fields dependent on TC analysis ---
        "fld_minMaxInputs": "enu_chkPass" if has_minmax_inputs else "enu_chkNA",
        "fld_minMaxOutputs": "enu_chkPass" if has_minmax_outputs else "enu_chkNA",
        "fld_twoInputValues": "enu_chkPass" if input_count >= 1 else "enu_chkNA",
        "fld_twoOutputValues": "enu_chkPass" if output_count >= 1 else "enu_chkNA",
        "fld_inputsIndependent": "enu_chkPass" if input_count >= 2 else "enu_chkNA",
        "fld_outputsIndependent": "enu_chkPass" if output_count >= 2 else "enu_chkNA",
        "fld_integerInflections": "enu_chkPass" if has_integer_inflections else "enu_chkNA",
        "fld_inputSelectionCriteria": "enu_chkPass" if has_pdi else "enu_chkNA",
        "fld_outputSelectionCriteria": "enu_chkPass" if has_pdi else "enu_chkNA",
        "fld_testCaseSelectionCriteria": "enu_chkPass" if has_pdi else "enu_chkNA",
    }

    # --- Conditional N/A justifications based on TC analysis ---
    if not has_minmax_inputs:
        attributes["fld_minMaxInputs_justification"] = "No min/max boundary values found in TC inputs."
    if not has_minmax_outputs:
        attributes["fld_minMaxOutputs_justification"] = "No min/max boundary values found in TC outputs."
    if input_count < 1:
        attributes["fld_twoInputValues_justification"] = "No input values found in TCs."
    if output_count < 1:
        attributes["fld_twoOutputValues_justification"] = "No output values found in TCs."
    if input_count < 2:
        attributes["fld_inputsIndependent_justification"] = "Fewer than 2 input values found in TCs."
    if output_count < 2:
        attributes["fld_outputsIndependent_justification"] = "Fewer than 2 output values found in TCs."
    if not has_integer_inflections:
        attributes["fld_integerInflections_justification"] = "No integers are used to verify conditions."
    if not has_pdi:
        attributes["fld_inputSelectionCriteria_justification"] = "No PDI usage."
        attributes["fld_outputSelectionCriteria_justification"] = "No PDI usage."
        attributes["fld_testCaseSelectionCriteria_justification"] = "No PDI usage."

    # --- Build relationships ---
    relationships = {
        "fld_category": {
            "data": {
                "type": "categories",
                "id": f"{project_id}/cat_BSP_POS",
            }
        },
        "components": {
            "data": [
                {
                    "type": "components",
                    "id": f"{project_id}/{component}",
                }
            ]
        },
    }
    # Add author and verifier if provided
    if author:
        relationships["author"] = {"data": {"type": "users", "id": author}}
        relationships["fld_verifier"] = {"data": {"type": "users", "id": author}}

    payload = {
        "data": [
            {
                "type": "workitems",
                "attributes": attributes,
                "relationships": relationships,
            }
        ]
    }

    if verbose:
        print(f"  [DEBUG] POST payload:\n{json.dumps(payload, indent=2)}")

    url = f"{base_url}/projects/{project_id}/workitems"
    resp = session.post(url, json=payload, verify=verify_ssl)

    if verbose:
        print(f"  [DEBUG] POST {url} -> {resp.status_code}")

    if resp.status_code not in (200, 201):
        print(f"    ERROR creating checklist '{title}': {resp.status_code}")
        print(f"    Response: {resp.text[:500]}")
        return None

    # Extract created work item ID
    resp_data = resp.json().get("data", [])
    if isinstance(resp_data, list) and resp_data:
        created_id = resp_data[0].get("id", "")
        short_id = extract_short_id(created_id)
        print(f"    Created checklist: {short_id} - {title}")
        return short_id
    elif isinstance(resp_data, dict):
        created_id = resp_data.get("id", "")
        short_id = extract_short_id(created_id)
        print(f"    Created checklist: {short_id} - {title}")
        return short_id

    print(f"    WARNING: Created checklist but could not extract ID from response")
    return None


def link_checklist_to_tc(session: requests.Session, base_url: str,
                         project_id: str, checklist_short_id: str,
                         tc_short_id: str, verify_ssl: bool,
                         dry_run: bool, verbose: bool = False) -> bool:
    """
    Create a linked work item from checklist -> TC with role 'has_parent'.
    Polarion displays this as: checklist "contains" TC / TC "is contained by" checklist.
    Returns True on success.
    """
    if dry_run:
        print(f"      [DRY RUN] Would link {checklist_short_id} -> {tc_short_id} (has_parent)")
        return True

    payload = {
        "data": [
            {
                "type": "linkedworkitems",
                "attributes": {
                    "role": "has_parent",
                    "suspect": False,
                },
                "relationships": {
                    "workItem": {
                        "data": {
                            "type": "workitems",
                            "id": f"{project_id}/{tc_short_id}",
                        }
                    }
                },
            }
        ]
    }

    url = f"{base_url}/projects/{project_id}/workitems/{checklist_short_id}/linkedworkitems"

    if verbose:
        print(f"  [DEBUG] POST {url}")
        print(f"  [DEBUG] Payload: {json.dumps(payload, indent=2)}")

    resp = session.post(url, json=payload, verify=verify_ssl)

    if verbose:
        print(f"  [DEBUG] -> {resp.status_code}")

    if resp.status_code not in (200, 201):
        print(f"      ERROR linking {checklist_short_id} -> {tc_short_id}: {resp.status_code}")
        if verbose:
            print(f"      Response: {resp.text[:500]}")
        return False

    print(f"      Linked {checklist_short_id} -> {tc_short_id} (has_parent)")
    return True


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    """Main entry point after argument parsing."""

    # --- Environment & session setup ---
    base_url = os.environ.get("POLARION_API_BASE")
    pat = os.environ.get("POLARION_PAT")
    project_id = args.project_id or os.environ.get("POLARION_PROJECT_ID")

    missing_vars = []
    if not base_url:
        missing_vars.append("POLARION_API_BASE")
    if not pat:
        missing_vars.append("POLARION_PAT")
    if not project_id:
        missing_vars.append("POLARION_PROJECT_ID (or use --project-id)")
    if missing_vars:
        print("Error: Missing required environment variables:")
        for v in missing_vars:
            print(f"  - {v}")
        sys.exit(1)

    base_url = base_url.rstrip('/')
    verify_ssl = args.verify_ssl
    verbose = args.verbose
    dry_run = not args.execute
    component = args.component

    session = create_polarion_session(base_url, pat, verify_ssl=verify_ssl)

    print(f"Component        : {component}")
    print(f"Project ID       : {project_id}")
    print(f"Mode             : {'EXECUTE' if not dry_run else 'DRY RUN'}")
    if args.pattern:
        print(f"Pattern filter   : {', '.join(args.pattern)}")
    if verbose:
        print(f"Verbose          : ON")

    # --- Dump fields mode ---
    if args.dump_fields:
        dump_checklist_fields(session, base_url, project_id, component,
                              verify_ssl, verbose)
        return

    if args.dump_tc:
        dump_tc_fields(session, base_url, project_id, component,
                       verify_ssl, verbose, pattern=args.pattern)
        return

    if args.list_types:
        list_component_types(session, base_url, project_id, component,
                             verify_ssl, verbose)
        return

    # ---------------------------------------------------------------
    # Phase 1: Fetch all TCs for the component
    # ---------------------------------------------------------------
    print(f"\n{SEP}")
    print("Phase 1: Fetching test cases...")
    print(SEP)

    tc_query_parts = [
        "NOT HAS_VALUE:resolution",
        "NOT status:deleted",
        f"type:{TC_TYPE}",
        f"fld_component.KEY:comp_{component}",
    ]
    tc_query = " AND ".join(tc_query_parts)

    # Strategy:
    #   - If --pattern is given, fall back to the legacy prefix-recursion path
    #     (the user has already narrowed the scope, so recursion is cheap).
    #   - Otherwise, ask Polarion for the list of "Application Programming
    #     Interface" work items for this component — one per function — and
    #     fetch TCs per function via title:<func>_*. This avoids the
    #     blow-up of recursive prefix search on components whose function
    #     names share a long common prefix (e.g. nvme_*).
    if args.pattern:
        print(f"  Using --pattern path (prefix recursion).")
        all_tc_ids: List[str] = []
        seen: set = set()
        for prefix in args.pattern:
            prefix = prefix.rstrip('*')
            filtered_query = f"{tc_query} AND title:{prefix}*"
            ids = query_work_items_paginated(session, base_url, project_id,
                                             filtered_query, verify_ssl,
                                             verbose=verbose)
            for wid in ids:
                if wid not in seen:
                    seen.add(wid)
                    all_tc_ids.append(wid)
        tc_ids = all_tc_ids
    else:
        print(f"  Discovering functions via API work items (type={args.api_type})...")
        func_names = discover_function_names_via_api(
            session, base_url, project_id, component, args.api_type,
            verify_ssl, verbose=verbose,
        )
        if func_names is None:
            # Truncated — message already printed by helper
            sys.exit(2)
        if not func_names:
            print(f"  No API work items found for component '{component}' with")
            print(f"  type='{args.api_type}'. Run with --list-types to discover")
            print(f"  the correct value, then pass it via --api-type.")
            return
        print(f"  Discovered {len(func_names)} function(s) via API items.")
        all_tc_ids = []
        seen = set()
        for func_name in func_names:
            ids = fetch_tcs_for_function(
                session, base_url, project_id, component, func_name,
                verify_ssl, verbose=verbose,
            )
            for wid in ids:
                if wid not in seen:
                    seen.add(wid)
                    all_tc_ids.append(wid)
        tc_ids = all_tc_ids

    tc_ids = sorted(set(tc_ids))
    print(f"  Found {len(tc_ids)} test case(s)")

    if not tc_ids:
        print("  No test cases found. Nothing to do.")
        return

    # Fetch titles and inputs/outputs
    print(f"  Fetching TC titles and inputs/outputs...")
    tc_io: Dict[str, dict] = {}
    tc_titles = fetch_tc_titles(session, base_url, project_id, tc_ids,
                                verify_ssl, verbose=verbose, limit=args.limit,
                                tc_io=tc_io)
    print(f"  Retrieved {len(tc_titles)} TC title(s)")

    # Group by function
    groups = group_tcs_by_function(tc_titles, verbose=verbose)
    print(f"  Grouped into {len(groups)} function(s)")

    # Print grouped TCs (sorted by function name)
    for func_name in sorted(groups.keys()):
        grp = groups[func_name]
        hltc_count = len(grp["HLTC"])
        lltc_count = len(grp["LLTC"])
        print(f"    {func_name}: {hltc_count} HLTC, {lltc_count} LLTC")
        if verbose:
            for tc_type in ("HLTC", "LLTC"):
                for short_id, title in grp[tc_type]:
                    print(f"      {short_id} - {title}")

    # ---------------------------------------------------------------
    # Phase 2: Fetch existing checklists for the component
    # ---------------------------------------------------------------
    print(f"\n{SEP}")
    print("Phase 2: Fetching existing checklists...")
    print(SEP)

    cl_query = (
        f"NOT HAS_VALUE:resolution AND "
        f"NOT status:deleted AND "
        f"type:{CHECKLIST_TYPE} AND "
        f"fld_component.KEY:comp_{component}"
    )
    cl_ids = query_work_items_paginated(session, base_url, project_id,
                                        cl_query, verify_ssl, verbose=verbose)
    cl_ids = sorted(set(cl_ids))
    print(f"  Found {len(cl_ids)} existing checklist(s)")

    # Fetch checklist titles
    cl_titles: Dict[str, str] = {}
    for cl_id in cl_ids:
        short_id = extract_short_id(cl_id)
        data = fetch_work_item(session, base_url, project_id, short_id,
                               "title", verify_ssl, verbose=verbose)
        if data:
            title = data.get("attributes", {}).get("title", "")
            if title:
                cl_titles[short_id] = title

    # Sort and build lookup set
    existing_cl_names = set(cl_titles.values())
    for short_id, title in sorted(cl_titles.items(), key=lambda x: x[1]):
        if verbose:
            print(f"    {short_id} - {title}")

    print(f"  Retrieved {len(cl_titles)} checklist title(s)")

    # ---------------------------------------------------------------
    # Phase 3: Identify missing checklists
    # ---------------------------------------------------------------
    print(f"\n{SEP}")
    print("Phase 3: Identifying missing checklists...")
    print(SEP)

    # Build list of (expected_title, func_name, tc_ids, has_hltc, has_lltc)
    missing: List[Tuple[str, str, List[str], bool, bool]] = []
    existing_matches: List[Tuple[str, str]] = []

    for func_name in sorted(groups.keys()):
        grp = groups[func_name]
        has_hltc = len(grp["HLTC"]) > 0
        has_lltc = len(grp["LLTC"]) > 0
        exp_name = expected_checklist_name(func_name, has_hltc, has_lltc)
        if exp_name is None:
            continue

        # Collect all TC short IDs for this function
        func_tc_ids = [sid for sid, _ in grp["HLTC"]] + [sid for sid, _ in grp["LLTC"]]

        if exp_name in existing_cl_names:
            existing_matches.append((exp_name, func_name))
            if verbose:
                print(f"    EXISTS: {exp_name}")
        else:
            missing.append((exp_name, func_name, func_tc_ids, has_hltc, has_lltc))
            print(f"    MISSING: {exp_name}  ({len(func_tc_ids)} TCs)")

    print(f"\n  Existing: {len(existing_matches)}, Missing: {len(missing)}")

    if not missing:
        print("  All checklists already exist. Nothing to create.")
        print(f"\n{SEP}")
        print("Done.")
        print(SEP)
        return

    # ---------------------------------------------------------------
    # Phase 4: Create missing checklists and link TCs
    # ---------------------------------------------------------------
    print(f"\n{SEP}")
    print(f"Phase 4: {'Creating' if not dry_run else 'Would create'} missing checklists...")
    print(SEP)

    created_count = 0
    links_created = 0
    errors = 0

    for exp_name, func_name, func_tc_ids, has_hltc, has_lltc in missing:
        print(f"\n  Checklist: {exp_name}")
        print(f"    Function: {func_name}, TCs to link: {len(func_tc_ids)}")

        # Analyze TC inputs/outputs for this function
        func_has_minmax_inputs = any(
            _has_minmax_values(tc_io.get(sid, {}).get("inputs"))
            for sid in func_tc_ids
        )
        func_has_minmax_outputs = any(
            _has_minmax_values(tc_io.get(sid, {}).get("outputs"))
            for sid in func_tc_ids
        )
        func_input_count = _count_field_values(tc_io, func_tc_ids, "inputs")
        func_output_count = _count_field_values(tc_io, func_tc_ids, "outputs")
        func_has_integer_inflections = func_has_minmax_inputs or func_has_minmax_outputs
        func_has_pdi = _has_pdi_in_initial_conditions(tc_io, func_tc_ids)

        # Create the checklist
        cl_short_id = create_checklist(
            session, base_url, project_id,
            title=exp_name,
            component=component,
            author=args.author,
            has_hltc=has_hltc,
            has_lltc=has_lltc,
            has_minmax_inputs=func_has_minmax_inputs,
            has_minmax_outputs=func_has_minmax_outputs,
            input_count=func_input_count,
            output_count=func_output_count,
            has_integer_inflections=func_has_integer_inflections,
            has_pdi=func_has_pdi,
            verify_ssl=verify_ssl,
            dry_run=dry_run,
            verbose=verbose,
        )

        if dry_run:
            # In dry-run, show what links would be created
            for tc_sid in func_tc_ids:
                tc_title = tc_titles.get(tc_sid, tc_sid)
                print(f"      [DRY RUN] Would link -> {tc_sid} ({tc_title})")
            created_count += 1
            links_created += len(func_tc_ids)
            continue

        if cl_short_id is None:
            print(f"    SKIPPED linking — checklist creation failed")
            errors += 1
            continue

        created_count += 1

        # Link each TC to the checklist
        for tc_sid in func_tc_ids:
            ok = link_checklist_to_tc(
                session, base_url, project_id,
                checklist_short_id=cl_short_id,
                tc_short_id=tc_sid,
                verify_ssl=verify_ssl,
                dry_run=dry_run,
                verbose=verbose,
            )
            if ok:
                links_created += 1
            else:
                errors += 1

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    print(f"\n{SEP}")
    print("Summary:")
    print(f"  Total functions       : {len(groups)}")
    print(f"  Total TCs             : {len(tc_titles)}")
    print(f"  Existing checklists   : {len(existing_matches)}")
    print(f"  Missing checklists    : {len(missing)}")
    label = "Created" if not dry_run else "Would create"
    print(f"  {label} checklists : {created_count}")
    print(f"  {label} links      : {links_created}")
    if errors:
        print(f"  Errors                : {errors}")
    if dry_run:
        print(f"\n  This was a DRY RUN. Use --execute to apply changes.")
    print(SEP)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Manage Polarion checklist (wi_testcase_checklist) work items. "
            "Finds missing checklists for TC functions and creates them with "
            "proper 'contains' links."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dump all fields of an existing checklist (discovery)
  %(prog)s --component SSD_NVME0 --dump-fields

  # Dry-run: show what would be created (default)
  %(prog)s --component SSD_NVME0

  # Execute: create checklists and links
  %(prog)s --component SSD_NVME0 --execute

  # With pattern filter and verbose
  %(prog)s --component SSD_NVME0 --pattern nvme_ --execute -v

  # Limit to first 5 functions
  %(prog)s --component SSD_NVME0 --execute --limit 50 -v
        """,
    )
    parser.add_argument(
        "--component", required=True,
        help="Component to filter by (e.g. SSD_NVME0, BOOT_APP0)",
    )
    parser.add_argument(
        "--pattern", nargs="+", default=None,
        help="One or more title prefixes to filter TCs (e.g. nvme_ arch_). "
             "Only TCs whose title starts with a prefix are included.",
    )
    parser.add_argument(
        "--project-id", default=None,
        help="Polarion project ID (overrides POLARION_PROJECT_ID env var)",
    )
    parser.add_argument(
        "--author", default=None,
        help="Polarion user ID to set as author on created checklists",
    )
    parser.add_argument(
        "--execute", action="store_true", default=False,
        help="Actually create checklists and links (default is dry-run)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        dest="dry_run",
        help="Show what would be created without making changes (default behavior)",
    )
    parser.add_argument(
        "--dump-fields", action="store_true", default=False,
        help="Fetch one existing checklist and print ALL its fields, then exit",
    )
    parser.add_argument(
        "--dump-tc", action="store_true", default=False,
        help="Fetch one existing TC and print ALL its fields, then exit",
    )
    parser.add_argument(
        "--list-types", action="store_true", default=False,
        help="List distinct work item types found in the component (with counts) "
             "and exit. Use to discover the correct value for --api-type.",
    )
    parser.add_argument(
        "--api-type", default=DEFAULT_API_TYPE,
        help=f"Polarion work item type for 'Application Programming Interface' "
             f"items used to enumerate functions (default: {DEFAULT_API_TYPE}). "
             f"Run --list-types if unsure.",
    )
    parser.add_argument(
        "--verify-ssl", action="store_true", default=False,
        help="Enable SSL certificate verification (disabled by default)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose/debug output",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Limit number of TCs to fetch (0 = no limit)",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
