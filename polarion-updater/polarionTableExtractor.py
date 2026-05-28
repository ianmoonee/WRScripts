#!/usr/bin/env python3
"""
Polarion TC Table Name Extractor

For a given component, discovers all Test Cases (HLTC + LLTC), fetches their
Inputs and Outputs HTML fields, and extracts table titles (matching "Table ..."
pattern).  Reports all unique table names found, ordered alphabetically.

With --show-tcs, displays which TCs contain each table, grouped by function.

Environment Variables:
    POLARION_API_BASE   - Base URL for Polarion REST API
    POLARION_PAT        - Personal Access Token (Bearer token)
    POLARION_PROJECT_ID - Project ID (e.g. Shallowford_BSP)

Usage:
    python polarionTableExtractor.py --component SSD_NVME0
    python polarionTableExtractor.py --component SSD_NVME0 --show-tcs
    python polarionTableExtractor.py --component SSD_NVME0 --dump-html
    python polarionTableExtractor.py --component SSD_NVME0 --debug
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
LLR_TYPE = "wi_lowLevelReq"
DEFAULT_API_TYPE = "wi_API"

# Regex to extract function name and TC type from a TC title
# e.g. "nvmeXbdStrategy_HLTC_1" -> ("nvmeXbdStrategy", "HLTC")
TC_TITLE_RE = re.compile(r'^(.+?)_(HLTC|LLTC)_\d+$')

# Regex to detect table titles in HTML content.
# Matches text like "Table example_EXAMPLE_1", "Table foo bar", etc.
# Case-insensitive to handle common typos (e.g. "table", "TABLE", "Tabl").
TABLE_TITLE_RE = re.compile(r'(?i)\b(tabl?e\s+[\w_][\w_ ]*[\w_])')


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
        need_split = (total is not None and total > len(ids)) or len(ids) >= 95
        if need_split:
            if verbose:
                print(f"  [DEBUG] title:{prefix}* -> {len(ids)} returned, total={total}, splitting deeper...")
            for c in "abcdefghijklmnopqrstuvwxyz0123456789_":
                query_by_prefix(f"{prefix}{c}")
        else:
            wi_ids_set.update(ids)
            if ids and verbose:
                print(f"  [DEBUG] title:{prefix}* -> {len(ids)} items")

    for c in "abcdefghijklmnopqrstuvwxyz0123456789_":
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
# HTML table title extraction
# ---------------------------------------------------------------------------

def extract_html_value(field: Any) -> str:
    """Extract the raw HTML string from a Polarion rich-text field."""
    if isinstance(field, dict):
        return field.get("value", "") or ""
    if isinstance(field, str):
        return field
    return ""


def extract_table_titles(html: str) -> List[str]:
    """
    Extract table title strings from HTML content.

    Searches for:
      1. <caption> elements inside tables
      2. Text matching "Table <name>" pattern in paragraphs, headings, bold text
      3. General regex fallback across the full text content
    """
    if not html:
        return []

    titles = []

    # Strategy 1: Look for <caption> elements
    caption_re = re.compile(r'<caption[^>]*>(.*?)</caption>', re.IGNORECASE | re.DOTALL)
    for match in caption_re.finditer(html):
        caption_text = re.sub(r'<[^>]+>', '', match.group(1)).strip()
        if caption_text:
            titles.append(caption_text)

    # Strategy 2: Look for text before/above <table> that matches "Table ..."
    # Check paragraphs, headings, bold/strong text containing "Table"
    block_re = re.compile(
        r'<(?:p|h[1-6]|b|strong|span|div)[^>]*>(.*?)</(?:p|h[1-6]|b|strong|span|div)>',
        re.IGNORECASE | re.DOTALL
    )
    for match in block_re.finditer(html):
        inner_text = re.sub(r'<[^>]+>', ' ', match.group(1)).strip()
        for table_match in TABLE_TITLE_RE.finditer(inner_text):
            titles.append(table_match.group(1).strip())

    # Strategy 3: Fallback — strip all HTML and search plain text
    plain_text = re.sub(r'<[^>]+>', ' ', html)
    for match in TABLE_TITLE_RE.finditer(plain_text):
        titles.append(match.group(1).strip())

    # Deduplicate while preserving first-seen order
    seen = set()
    unique = []
    for t in titles:
        normalized = t.strip()
        if normalized.lower() not in seen:
            seen.add(normalized.lower())
            unique.append(normalized)
    return unique


# ---------------------------------------------------------------------------
# API-driven function discovery
# ---------------------------------------------------------------------------

def discover_function_names_via_api(session: requests.Session, base_url: str,
                                    project_id: str, component: str,
                                    api_type: str, verify_ssl: bool,
                                    verbose: bool = False) -> List[str]:
    """
    Fetch all API work items (one per function) for the given component and
    return their titles as a list of function names.

    Uses paginated prefix-splitting if the component has >100 API items.
    """
    query = (
        f"type:{api_type} AND "
        f"fld_component.KEY:comp_{component} AND "
        f"NOT status:deleted"
    )

    # First try a single query to see if it fits in one page
    url = f"{base_url}/projects/{project_id}/workitems"
    params = {"query": query, "fields[workitems]": "id,title"}
    if verbose:
        print(f"  [DEBUG] API query: {query}")
    resp = session.get(url, params=params, verify=verify_ssl)
    if resp.status_code != 200:
        print(f"  Error fetching API items: HTTP {resp.status_code}")
        if verbose:
            print(f"  Response: {resp.text[:500]}")
        return []
    body = resp.json()
    items = body.get("data", [])
    total = body.get("meta", {}).get("totalCount")

    if total is not None and total > len(items):
        # Need paginated approach
        if verbose:
            print(f"  [DEBUG] API items truncated ({len(items)}/{total}), using paginated fetch...")
        all_ids = query_work_items_paginated(
            session, base_url, project_id, query, verify_ssl, verbose=verbose
        )
        # Now fetch titles for each discovered ID
        names = []
        for wi_id in all_ids:
            short_id = extract_short_id(wi_id)
            data = fetch_work_item(session, base_url, project_id, short_id,
                                   "title", verify_ssl, verbose=verbose)
            if data:
                title = data.get("attributes", {}).get("title", "").strip()
                if title:
                    names.append(title)
        return names
    else:
        names = []
        for item in items:
            title = item.get("attributes", {}).get("title", "").strip()
            if title:
                names.append(title)
        return names


def fetch_tcs_for_function(session: requests.Session, base_url: str,
                           project_id: str, component: str,
                           func_name: str, verify_ssl: bool,
                           verbose: bool = False) -> List[str]:
    """
    Fetch all TC work item IDs for a single function via title:<func>_*.
    Uses paginated prefix-splitting starting from the function name to
    handle functions with >100 TCs.
    """
    base_query = (
        f"type:{TC_TYPE} AND "
        f"fld_component.KEY:comp_{component} AND "
        f"NOT status:deleted"
    )
    url = f"{base_url}/projects/{project_id}/workitems"
    wi_ids_set: set = set()

    def query_by_prefix(prefix: str) -> None:
        sub_query = f"{base_query} AND title:{prefix}*"
        params = {"query": sub_query, "fields[workitems]": "id"}
        resp = session.get(url, params=params, verify=verify_ssl)
        if verbose:
            print(f"  [DEBUG] title:{prefix}* -> status {resp.status_code}")
        if resp.status_code != 200:
            return
        body = resp.json()
        items = body.get("data", [])
        total = body.get("meta", {}).get("totalCount")
        ids = [item["id"] for item in items if isinstance(item, dict) and "id" in item]
        need_split = (total is not None and total > len(ids)) or len(ids) >= 95
        if need_split:
            if verbose:
                print(f"  [DEBUG] title:{prefix}* -> {len(ids)} returned, total={total}, splitting deeper...")
            for c in "abcdefghijklmnopqrstuvwxyz0123456789_":
                query_by_prefix(f"{prefix}{c}")
        else:
            wi_ids_set.update(ids)
            if ids and verbose:
                print(f"  [DEBUG] title:{prefix}* -> {len(ids)} items")

    # Start splitting from the function name prefix (e.g. "nvmeInit_")
    # This searches for title:nvmeInit_a*, title:nvmeInit_b*, etc.
    title_prefix = f"{func_name}_"
    for c in "abcdefghijklmnopqrstuvwxyz0123456789_":
        query_by_prefix(f"{title_prefix}{c}")

    if verbose:
        print(f"  [DEBUG] Function '{func_name}': {len(wi_ids_set)} TC(s) found")
    return list(wi_ids_set)


def fetch_tcs_grouped(session: requests.Session, base_url: str,
                      project_id: str, component: str,
                      func_names: List[str], verify_ssl: bool,
                      prefix_len: int = 6,
                      verbose: bool = False) -> Dict[str, List[str]]:
    """
    Fetch TCs for all functions, grouping by common prefix to reduce API calls.

    Groups functions that share the same first `prefix_len` characters and
    issues a single query per group.  If a group query returns >=100 items
    (API cap), falls back to per-function queries for that group.

    Returns: {func_name: [wi_id, ...]}
    """
    base_query = (
        f"type:{TC_TYPE} AND "
        f"fld_component.KEY:comp_{component} AND "
        f"NOT status:deleted"
    )
    url = f"{base_url}/projects/{project_id}/workitems"

    # Group functions by common prefix
    groups: Dict[str, List[str]] = defaultdict(list)
    for name in func_names:
        prefix = name[:prefix_len] if len(name) >= prefix_len else name
        groups[prefix].append(name)

    result: Dict[str, List[str]] = {}

    for prefix, group_funcs in sorted(groups.items()):
        # Try fetching all TCs for this prefix group in one call
        group_query = f"{base_query} AND title:{prefix}*"
        params = {"query": group_query, "fields[workitems]": "id,title"}
        resp = session.get(url, params=params, verify=verify_ssl)

        if resp.status_code != 200:
            # Fall back to per-function
            if verbose:
                print(f"  [DEBUG] Group '{prefix}' query failed ({resp.status_code}), splitting per function...")
            for func_name in group_funcs:
                tc_ids = fetch_tcs_for_function(
                    session, base_url, project_id, component, func_name, verify_ssl, verbose=verbose
                )
                result[func_name] = tc_ids
                print(f"  Fetched TCs for function {func_name}: found {len(tc_ids)}")
            continue

        body = resp.json()
        items = body.get("data", [])
        total = body.get("meta", {}).get("totalCount")
        need_split = (total is not None and total > len(items)) or len(items) >= 95

        if need_split:
            # Group is too large, fall back to per-function queries
            if verbose:
                print(f"  [DEBUG] Group '{prefix}' has {total} TCs (capped), splitting per function...")
            for func_name in group_funcs:
                tc_ids = fetch_tcs_for_function(
                    session, base_url, project_id, component, func_name, verify_ssl, verbose=verbose
                )
                result[func_name] = tc_ids
                print(f"  Fetched TCs for function {func_name}: found {len(tc_ids)}")
        else:
            # Assign TCs to their respective functions based on title
            func_ids: Dict[str, List[str]] = {fn: [] for fn in group_funcs}
            for item in items:
                wi_id = item.get("id", "")
                title = item.get("attributes", {}).get("title", "")
                if not title or not wi_id:
                    continue
                # Match title to a function: title should start with func_name + "_"
                matched = False
                for func_name in group_funcs:
                    if title.startswith(func_name + "_"):
                        m = TC_TITLE_RE.match(title)
                        if m:
                            func_ids[func_name].append(wi_id)
                            matched = True
                            break
                if not matched and verbose:
                    print(f"  [DEBUG] TC '{title}' in group '{prefix}' didn't match any known function")

            for func_name in group_funcs:
                result[func_name] = func_ids[func_name]

            # Print grouped result
            group_total = sum(len(ids) for ids in func_ids.values())
            func_details = ", ".join(f"{fn}={len(func_ids[fn])}" for fn in sorted(group_funcs))
            print(f"  Fetched TCs for group '{prefix}*' ({len(group_funcs)} functions): "
                  f"found {group_total} ({func_details})")

    return result


# ---------------------------------------------------------------------------
# Link checking
# ---------------------------------------------------------------------------

def fetch_llrs_for_function(session: requests.Session, base_url: str,
                            project_id: str, component: str,
                            func_name: str, verify_ssl: bool,
                            verbose: bool = False) -> List[str]:
    """
    Fetch all LLR work item IDs for a single function via title:<func>_*.
    Uses paginated prefix-splitting for functions with >95 LLRs.
    """
    base_query = (
        f"type:{LLR_TYPE} AND "
        f"fld_component.KEY:comp_{component} AND "
        f"NOT status:deleted"
    )
    url = f"{base_url}/projects/{project_id}/workitems"

    # Try single query first
    query = f"{base_query} AND title:{func_name}_*"
    params = {"query": query, "fields[workitems]": "id"}
    resp = session.get(url, params=params, verify=verify_ssl)
    if resp.status_code != 200:
        if verbose:
            print(f"  [DEBUG] LLR query for '{func_name}' failed: {resp.status_code}")
        return []
    body = resp.json()
    items = body.get("data", [])
    total = body.get("meta", {}).get("totalCount")
    ids = [item["id"] for item in items if isinstance(item, dict) and "id" in item]

    if (total is not None and total > len(ids)) or len(ids) >= 95:
        # Need pagination
        wi_ids_set: set = set()

        def query_by_prefix(prefix: str) -> None:
            sub_query = f"{base_query} AND title:{prefix}*"
            params = {"query": sub_query, "fields[workitems]": "id"}
            resp = session.get(url, params=params, verify=verify_ssl)
            if resp.status_code != 200:
                return
            body = resp.json()
            items = body.get("data", [])
            total = body.get("meta", {}).get("totalCount")
            ids = [item["id"] for item in items if isinstance(item, dict) and "id" in item]
            need_split = (total is not None and total > len(ids)) or len(ids) >= 95
            if need_split:
                for c in "abcdefghijklmnopqrstuvwxyz0123456789_":
                    query_by_prefix(f"{prefix}{c}")
            else:
                wi_ids_set.update(ids)

        title_prefix = f"{func_name}_"
        for c in "abcdefghijklmnopqrstuvwxyz0123456789_":
            query_by_prefix(f"{title_prefix}{c}")
        return list(wi_ids_set)

    return ids


def fetch_linked_work_items(session: requests.Session, base_url: str,
                           project_id: str, short_id: str,
                           verify_ssl: bool, verbose: bool = False) -> List[Dict[str, Any]]:
    """Fetch all linked work items (outgoing + reverse) for a given work item.

    Outgoing links are fetched from /linkedworkitems endpoint.
    Incoming (reverse) links are discovered via a query for work items that
    link TO this WI, then their outgoing links are inspected and roles mapped.
    """
    all_links: List[Dict[str, Any]] = []
    url = f"{base_url}/projects/{project_id}/workitems"

    # 1. Outgoing links
    out_url = f"{base_url}/projects/{project_id}/workitems/{short_id}/linkedworkitems"
    params = {"fields[linkedworkitems]": "@all"}
    resp = session.get(out_url, params=params, verify=verify_ssl)
    if resp.status_code == 200:
        outgoing = resp.json().get("data", [])
        all_links.extend(outgoing)
        if verbose:
            print(f"    [DEBUG] Outgoing links for {short_id}: {len(outgoing)}")
    elif verbose:
        print(f"    [DEBUG] Could not fetch outgoing links for {short_id}: {resp.status_code}")

    # 2. Reverse links: find work items that link TO this WI
    escaped_id = short_id.replace("-", "\\-")
    reverse_query = f"NOT HAS_VALUE:resolution AND NOT status:deleted AND linkedWorkItems:{escaped_id}"
    rev_params = {
        "query": reverse_query,
        "fields[workitems]": "id,title,type",
    }
    rev_resp = session.get(url, params=rev_params, verify=verify_ssl)
    if rev_resp.status_code != 200:
        if verbose:
            print(f"    [DEBUG] Reverse query for {short_id} failed: {rev_resp.status_code}")
        return all_links

    reverse_items = rev_resp.json().get("data", [])
    if verbose:
        print(f"    [DEBUG] Reverse-linked items for {short_id}: {len(reverse_items)}")

    # Map outgoing roles to reverse (incoming) roles
    reverse_role_map = {
        "implements": "is_implemented_by",
        "verifies": "is_verified_by",
        "has_parent": "contains",
    }

    # For each reverse-linked WI, fetch its outgoing links to find the one pointing to us
    for rev_item in reverse_items:
        rev_id = rev_item.get("id", "")
        rev_short = extract_short_id(rev_id)
        rev_title = rev_item.get("attributes", {}).get("title", "")

        rev_links_url = f"{base_url}/projects/{project_id}/workitems/{rev_short}/linkedworkitems"
        rev_links_resp = session.get(rev_links_url, params={"fields[linkedworkitems]": "@all"}, verify=verify_ssl)
        if rev_links_resp.status_code != 200:
            continue

        for link in rev_links_resp.json().get("data", []):
            # Check if this link points to our WI
            target = link.get("relationships", {}).get("workItem", {}).get("data", {}).get("id", "")
            target_short = extract_short_id(target)
            if target_short == short_id:
                role = link.get("attributes", {}).get("role", "")
                mapped_role = reverse_role_map.get(role, f"reverse_{role}")
                backlink = {
                    "id": link.get("id", ""),
                    "attributes": {"role": mapped_role},
                    "reverse_from": rev_short,
                    "reverse_title": rev_title,
                }
                all_links.append(backlink)
                if verbose:
                    print(f"      [DEBUG] {rev_short} ({rev_title}): {role} → '{mapped_role}'")

    return all_links


def check_llr_links(session: requests.Session, base_url: str,
                    project_id: str, component: str,
                    func_names: List[str], verify_ssl: bool,
                    verbose: bool = False) -> None:
    """Check LLRs for missing 'is implemented by' or 'is verified by' links."""
    print("Fetching LLRs per function...")

    missing_impl = []   # (title, short_id)
    missing_verified = []  # (title, short_id)
    total_checked = 0

    for i, func_name in enumerate(sorted(func_names)):
        llr_ids = fetch_llrs_for_function(
            session, base_url, project_id, component, func_name, verify_ssl, verbose=verbose
        )
        if not llr_ids:
            continue

        for wi_id in llr_ids:
            short_id = extract_short_id(wi_id)
            # Fetch title
            data = fetch_work_item(session, base_url, project_id, short_id,
                                   "title", verify_ssl, verbose=verbose)
            title = short_id
            if data:
                title = data.get("attributes", {}).get("title", short_id)

            links = fetch_linked_work_items(session, base_url, project_id, short_id, verify_ssl, verbose=verbose)

            has_implemented_by = False
            has_verified_by = False
            for link in links:
                if not isinstance(link, dict):
                    continue
                role = link.get("attributes", {}).get("role", "") if "attributes" in link else ""
                if not role:
                    role = link.get("role", "")
                if role == "is_implemented_by":
                    has_implemented_by = True
                if role == "is_verified_by":
                    has_verified_by = True

            if not has_implemented_by:
                missing_impl.append((title, short_id))
            if not has_verified_by:
                missing_verified.append((title, short_id))

            total_checked += 1
            if total_checked % 20 == 0:
                print(f"  ... checked {total_checked} LLRs")

    # Print results
    print(f"\n{'=' * 70}")
    print(f"LINK CHECK RESULTS: {total_checked} LLR(s) checked")
    print(f"{'=' * 70}")

    if missing_impl:
        print(f"\nLLRs missing 'is implemented by' ({len(missing_impl)}):")
        print("-" * 70)
        for title, short_id in sorted(missing_impl):
            print(f"  {title} ({short_id})")

    if missing_verified:
        print(f"\nLLRs missing 'is verified by' ({len(missing_verified)}):")
        print("-" * 70)
        for title, short_id in sorted(missing_verified):
            print(f"  {title} ({short_id})")

    if not missing_impl and not missing_verified:
        print("\n  All LLRs have both 'is implemented by' and 'is verified by' links.")

    print(f"\n{'=' * 70}")
    print(f"Summary: {total_checked} LLR(s) checked, "
          f"{len(missing_impl)} missing implementation, "
          f"{len(missing_verified)} missing verification")
    print(f"{'=' * 70}")


# ---------------------------------------------------------------------------
# Source annotation check
# ---------------------------------------------------------------------------

def check_llr_source(session: requests.Session, base_url: str,
                     project_id: str, component: str,
                     func_names: List[str], verify_ssl: bool,
                     verbose: bool = False) -> None:
    """Check that each LLR's Source Annotation field matches its title."""
    print("Fetching LLRs per function...")

    mismatches = []      # (title, short_id, actual_annotation)
    missing_source = []  # (title, short_id)
    total_checked = 0

    for func_name in sorted(func_names):
        llr_ids = fetch_llrs_for_function(
            session, base_url, project_id, component, func_name, verify_ssl, verbose=verbose
        )
        if not llr_ids:
            continue

        for wi_id in llr_ids:
            short_id = extract_short_id(wi_id)
            data = fetch_work_item(session, base_url, project_id, short_id,
                                   "title,fld_srcAnnotation", verify_ssl, verbose=verbose)
            if not data:
                continue

            attrs = data.get("attributes", {})
            title = attrs.get("title", short_id)
            source_annotation = attrs.get("fld_srcAnnotation", None)

            # Extract plain text if it's a rich-text dict
            if isinstance(source_annotation, dict):
                source_annotation = source_annotation.get("value", "") or ""
            if source_annotation is None:
                source_annotation = ""
            source_annotation = source_annotation.strip()

            if not source_annotation:
                missing_source.append((title, short_id))
                total_checked += 1
                continue

            if source_annotation != title:
                mismatches.append((title, short_id, source_annotation))

            total_checked += 1
            if total_checked % 20 == 0:
                print(f"  ... checked {total_checked} LLRs")

    # Print results
    print(f"\n{'=' * 70}")
    print(f"SOURCE ANNOTATION CHECK: {total_checked} LLR(s) checked")
    print(f"{'=' * 70}")

    if missing_source:
        print(f"\nLLRs with empty Source Annotation ({len(missing_source)}):")
        print("-" * 70)
        for title, short_id in sorted(missing_source):
            print(f"  {title} ({short_id})")

    if mismatches:
        print(f"\nLLRs where Source Annotation does not match title ({len(mismatches)}):")
        print("-" * 70)
        for title, short_id, actual in sorted(mismatches):
            print(f"  {title} ({short_id})")
            print(f"    Source Annotation: {actual}")

    if not missing_source and not mismatches:
        print("\n  All LLRs have Source Annotation matching their title.")

    print(f"\n{'=' * 70}")
    print(f"Summary: {total_checked} LLR(s) checked, "
          f"{len(missing_source)} empty, "
          f"{len(mismatches)} mismatch(es)")
    print(f"{'=' * 70}")


# ---------------------------------------------------------------------------
# Sequence number check
# ---------------------------------------------------------------------------

def check_llr_sequence(session: requests.Session, base_url: str,
                       project_id: str, component: str,
                       func_names: List[str], verify_ssl: bool,
                       verbose: bool = False) -> None:
    """Check that each LLR's Sequence Number matches the number after _LLR_ in its title."""
    print("Fetching LLRs per function...")

    mismatches = []      # (title, short_id, expected_seq, actual_seq)
    missing_seq = []     # (title, short_id)
    no_number = []       # (title, short_id) — title doesn't match _LLR_<n> pattern
    total_checked = 0

    llr_number_re = re.compile(r'_LLR_(\d+)$')

    for func_name in sorted(func_names):
        llr_ids = fetch_llrs_for_function(
            session, base_url, project_id, component, func_name, verify_ssl, verbose=verbose
        )
        if not llr_ids:
            continue

        for wi_id in llr_ids:
            short_id = extract_short_id(wi_id)
            data = fetch_work_item(session, base_url, project_id, short_id,
                                   "title,fld_sequence", verify_ssl, verbose=verbose)
            if not data:
                continue

            attrs = data.get("attributes", {})
            title = attrs.get("title", short_id)

            # Extract expected sequence number from title
            m = llr_number_re.search(title)
            if not m:
                no_number.append((title, short_id))
                total_checked += 1
                continue
            expected_seq = m.group(1)

            # Get actual sequence number field
            seq_value = attrs.get("fld_sequence", None)
            if seq_value is None or seq_value == "":
                missing_seq.append((title, short_id))
                total_checked += 1
                continue

            actual_seq = str(seq_value).strip()

            if actual_seq != expected_seq:
                mismatches.append((title, short_id, expected_seq, actual_seq))

            total_checked += 1
            if total_checked % 20 == 0:
                print(f"  ... checked {total_checked} LLRs")

    # Print results
    print(f"\n{'=' * 70}")
    print(f"SEQUENCE NUMBER CHECK: {total_checked} LLR(s) checked")
    print(f"{'=' * 70}")

    if no_number:
        print(f"\nLLRs with no _LLR_<number> pattern in title ({len(no_number)}):")
        print("-" * 70)
        for title, short_id in sorted(no_number):
            print(f"  {title} ({short_id})")

    if missing_seq:
        print(f"\nLLRs with empty Sequence Number ({len(missing_seq)}):")
        print("-" * 70)
        for title, short_id in sorted(missing_seq):
            print(f"  {title} ({short_id})")

    if mismatches:
        print(f"\nLLRs where Sequence Number does not match title ({len(mismatches)}):")
        print("-" * 70)
        for title, short_id, expected, actual in sorted(mismatches):
            print(f"  {title} ({short_id})")
            print(f"    Expected: {expected}  Actual: {actual}")

    if not no_number and not missing_seq and not mismatches:
        print("\n  All LLRs have correct Sequence Number matching their title.")

    print(f"\n{'=' * 70}")
    print(f"Summary: {total_checked} LLR(s) checked, "
          f"{len(no_number)} no pattern, "
          f"{len(missing_seq)} empty, "
          f"{len(mismatches)} mismatch(es)")
    print(f"{'=' * 70}")


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract table names from Polarion TC Inputs/Outputs fields.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python polarionTableExtractor.py --component SSD_NVME0
  python polarionTableExtractor.py --component SSD_NVME0 --show-tcs
  python polarionTableExtractor.py --component SSD_NVME0 --dump-html
  python polarionTableExtractor.py --component SSD_NVME0 --debug
  python polarionTableExtractor.py --component SSD_NVME0 --api-type wi_API
""",
    )
    parser.add_argument(
        "--component", required=True,
        help="Component name (e.g. SSD_NVME0).",
    )
    parser.add_argument(
        "--show-tcs", action="store_true",
        help="Show TC names associated with each table, grouped by function.",
    )
    parser.add_argument(
        "--dump-html", action="store_true",
        help="Print raw HTML of the first TC's Inputs/Outputs for inspection.",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable verbose debug output.",
    )
    parser.add_argument(
        "--api-type", default=DEFAULT_API_TYPE,
        help=f"Polarion work item type for API items (default: {DEFAULT_API_TYPE}).",
    )
    parser.add_argument(
        "--check-links", action="store_true",
        help="Check LLRs for missing 'is implemented by' or 'is verified by' links.",
    )
    parser.add_argument(
        "--check-source", action="store_true",
        help="Check that each LLR's Source Annotation field matches its title.",
    )
    parser.add_argument(
        "--check-sequence", action="store_true",
        help="Check that each LLR's Sequence Number matches the number after _LLR_ in its title.",
    )
    parser.add_argument(
        "--dump-llr", action="store_true",
        help="Fetch the first LLR with all fields and print its full JSON response for debug.",
    )
    args = parser.parse_args()

    # --- Environment variables ---
    base_url = os.environ.get("POLARION_API_BASE", "").rstrip("/")
    pat = os.environ.get("POLARION_PAT", "")
    project_id = os.environ.get("POLARION_PROJECT_ID", "")

    if not base_url:
        print("ERROR: POLARION_API_BASE environment variable is not set.")
        sys.exit(1)
    if not pat:
        print("ERROR: POLARION_PAT environment variable is not set.")
        sys.exit(1)
    if not project_id:
        print("ERROR: POLARION_PROJECT_ID environment variable is not set.")
        sys.exit(1)

    component = args.component
    debug = args.debug
    verify_ssl = False

    session = create_polarion_session(base_url, pat, verify_ssl)

    # =======================================================================
    # STEP 1 — Discover function names from API work items
    # =======================================================================
    print(f"Discovering functions for component '{component}' (type: {args.api_type})...")
    func_names = discover_function_names_via_api(
        session, base_url, project_id, component, args.api_type, verify_ssl, verbose=debug
    )
    if not func_names:
        print(f"ERROR: No API work items found for component '{component}' with type '{args.api_type}'.")
        print("  Try --api-type to specify a different work item type.")
        sys.exit(1)
    print(f"  Found {len(func_names)} function(s).")

    # =======================================================================
    # STEP 1a — Dump LLR JSON (if --dump-llr)
    # =======================================================================
    if args.dump_llr:
        # Fetch the first LLR for the first function with all fields
        for func_name in sorted(func_names):
            llr_ids = fetch_llrs_for_function(
                session, base_url, project_id, component, func_name, verify_ssl, verbose=debug
            )
            if llr_ids:
                short_id = extract_short_id(llr_ids[0])
                url = f"{base_url}/projects/{project_id}/workitems/{short_id}"
                params = {"fields[workitems]": "@all"}
                resp = session.get(url, params=params, verify=verify_ssl)
                print(f"GET {url}")
                print(f"Status: {resp.status_code}")
                print(json.dumps(resp.json(), indent=2))
                return
        print("No LLRs found to dump.")
        return

    # =======================================================================
    # STEP 1b — Link check mode (if --check-links)
    # =======================================================================
    if args.check_links:
        check_llr_links(
            session, base_url, project_id, component, func_names, verify_ssl, verbose=debug
        )
        return

    # =======================================================================
    # STEP 1c — Source check mode (if --check-source)
    # =======================================================================
    if args.check_source:
        check_llr_source(
            session, base_url, project_id, component, func_names, verify_ssl, verbose=debug
        )
        return

    # =======================================================================
    # STEP 1d — Sequence number check mode (if --check-sequence)
    # =======================================================================
    if args.check_sequence:
        check_llr_sequence(
            session, base_url, project_id, component, func_names, verify_ssl, verbose=debug
        )
        return

    # =======================================================================
    # STEP 2 — Fetch TCs for each function (grouped by prefix for speed)
    # =======================================================================
    print("Fetching TCs per function (grouped by common prefix)...")
    func_tc_ids = fetch_tcs_grouped(
        session, base_url, project_id, component, sorted(func_names),
        verify_ssl, prefix_len=6, verbose=debug
    )
    total_tcs = sum(len(ids) for ids in func_tc_ids.values())
    # Remove functions with no TCs
    func_tc_ids = {k: v for k, v in func_tc_ids.items() if v}

    print(f"  Found {total_tcs} TC(s) across {len(func_tc_ids)} function(s).")

    # =======================================================================
    # STEP 3 — Fetch Inputs/Outputs for each TC and extract table titles
    # =======================================================================
    print("Fetching Inputs/Outputs fields and extracting table names...")

    # table_name -> set of "Inputs"/"Outputs"
    table_fields: Dict[str, set] = defaultdict(set)
    # table_name -> { func_name -> [tc_title, ...] }
    table_tcs: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))

    processed = 0
    dumped = False

    for func_name, tc_ids in sorted(func_tc_ids.items()):
        for wi_id in tc_ids:
            short_id = extract_short_id(wi_id)
            data = fetch_work_item(session, base_url, project_id, short_id,
                                   "title,fld_inputs,fld_outputs", verify_ssl, verbose=debug)
            processed += 1
            if processed % 50 == 0:
                print(f"  ... processed {processed}/{total_tcs} TCs")

            if not data:
                continue
            attrs = data.get("attributes", {})
            tc_title = attrs.get("title", short_id)

            inputs_html = extract_html_value(attrs.get("fld_inputs"))
            outputs_html = extract_html_value(attrs.get("fld_outputs"))

            # --dump-html: print the first TC's raw HTML and exit
            if args.dump_html and not dumped:
                dumped = True
                print(f"\n{'=' * 70}")
                print(f"RAW HTML — TC: {tc_title} (function: {func_name})")
                print(f"{'=' * 70}")
                print(f"\n--- fld_inputs ---")
                print(inputs_html if inputs_html else "(empty)")
                print(f"\n--- fld_outputs ---")
                print(outputs_html if outputs_html else "(empty)")
                print(f"{'=' * 70}\n")
                sys.exit(0)

            # Extract table titles from Inputs
            for table_name in extract_table_titles(inputs_html):
                table_fields[table_name].add("Inputs")
                table_tcs[table_name][func_name].append(tc_title)

            # Extract table titles from Outputs
            for table_name in extract_table_titles(outputs_html):
                table_fields[table_name].add("Outputs")
                table_tcs[table_name][func_name].append(tc_title)

    print(f"  Done. Processed {processed} TC(s).")

    # =======================================================================
    # STEP 4 — Output results (grouped by function)
    # =======================================================================
    if not table_tcs:
        print("\nNo tables found matching the 'Table ...' pattern.")
        print("Try running with --dump-html to inspect the raw HTML structure.")
        sys.exit(0)

    # Reorganize data: func_name -> [(table_name, field_label, [tc_titles])]
    func_tables: Dict[str, List[Tuple[str, str, List[str]]]] = defaultdict(list)
    for table_name, funcs in table_tcs.items():
        field_label = ", ".join(sorted(table_fields[table_name]))
        for func_name, tc_titles in funcs.items():
            func_tables[func_name].append((table_name, field_label, sorted(tc_titles)))

    # Sort tables within each function alphabetically
    for func_name in func_tables:
        func_tables[func_name].sort(key=lambda x: x[0].lower())

    all_tables = sorted(set(table_tcs.keys()), key=str.lower)
    print(f"\n{'=' * 70}")
    print(f"RESULTS: {len(all_tables)} unique table(s) across {len(func_tables)} function(s)")
    print(f"{'=' * 70}")

    separator = "-" * 70
    for func_name in sorted(func_tables.keys()):
        entries = func_tables[func_name]
        print(f"\nAll tables for function: {func_name}")
        for table_name, field_label, tc_titles in entries:
            print(f"  {table_name} ({field_label})")
            if args.show_tcs:
                print(f"    TCs: {', '.join(tc_titles)}")
        print(separator)


if __name__ == "__main__":
    main()
