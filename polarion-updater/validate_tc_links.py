#!/usr/bin/env python3
"""
Polarion Test Case Link Validator

Fetches all TC (test case) work items and validates their linked work items.

Expected links for HLTC:
  - verifies        (HLR)   — 1 or more
  - is implemented by (TP)  — 1 (unless unimplemented)
  - contains        (Checklist) — 1

Expected links for LLTC:
  - verifies        (LLR)   — 1 or more
  - is implemented by (TP)  — 1 (unless unimplemented)
  - contains        (Checklist) — 1

If the TC has the 'unimplemented' custom field set to true, it should NOT have
an 'is implemented by' link to a TP.

Environment Variables:
    POLARION_API_BASE, POLARION_PAT, POLARION_PROJECT_ID

Usage:
    python validate_tc_links.py
    python validate_tc_links.py --component BOOT_APP0
    python validate_tc_links.py --title-filter bootloaderStart
    python validate_tc_links.py -v
"""

import os
import sys
import json
import re
import argparse
import requests


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


def get_linked_work_items(session, base_url, project_id, short_id, verify_ssl, verbose=False):
    """Get all linked work items (outgoing + reverse via query) for a given work item."""
    all_links = []

    # Outgoing links (e.g. TC verifies Requirement)
    url = f"{base_url}/projects/{project_id}/workitems/{short_id}/linkedworkitems"
    params = {"fields[linkedworkitems]": "@all"}
    resp = session.get(url, params=params, verify=verify_ssl)
    if resp.status_code == 200:
        outgoing = resp.json().get("data", [])
        all_links.extend(outgoing)
        if verbose:
            print(f"      [VERBOSE] Outgoing links ({len(outgoing)}):")
            for link in outgoing:
                role = link.get("attributes", {}).get("role", "?")
                lid = link.get("id", "?")
                print(f"        - {role}: {lid}")

    # Reverse links: query for work items that link TO this TC
    escaped_id = short_id.replace("-", "\\-")
    reverse_query = f"NOT HAS_VALUE:resolution AND NOT status:deleted AND linkedWorkItems:{escaped_id}"
    if verbose:
        print(f"      [VERBOSE] Reverse query: {reverse_query}")

    rev_url = f"{base_url}/projects/{project_id}/workitems"
    rev_params = {
        "query": reverse_query,
        "fields[workitems]": "id,title,type",
    }
    reverse_items = list(paginated_get(session, rev_url, rev_params, verify_ssl, verbose=verbose))

    if verbose:
        print(f"      [VERBOSE] Reverse-linked items ({len(reverse_items)}):")

    # For each reverse-linked item, find the specific link role pointing to our TC
    for rev_item in reverse_items:
        rev_id = rev_item.get("id", "")
        rev_short = extract_short_id(rev_id)
        rev_title = rev_item.get("attributes", {}).get("title", "")
        rev_type = rev_item.get("attributes", {}).get("type", "")

        # Fetch outgoing links from this reverse item to find the one pointing to our TC
        rev_links_url = f"{base_url}/projects/{project_id}/workitems/{rev_short}/linkedworkitems"
        rev_links_resp = session.get(rev_links_url, params={"fields[linkedworkitems]": "@all"}, verify=verify_ssl)
        if rev_links_resp.status_code != 200:
            continue

        for link in rev_links_resp.json().get("data", []):
            # Check if this link points to our TC
            target = link.get("relationships", {}).get("workItem", {}).get("data", {}).get("id", "")
            target_short = extract_short_id(target)
            if target_short == short_id:
                role = link.get("attributes", {}).get("role", "?")
                # Map outgoing role to the reverse role as shown in Polarion UI
                reverse_role_map = {
                    "implements": "is_implemented_by",
                    "has_parent": "contains",
                }
                mapped_role = reverse_role_map.get(role, f"reverse_{role}")
                # Add as a synthetic backlink
                backlink = {
                    "id": link.get("id", ""),
                    "attributes": {"role": mapped_role},
                    "reverse_from": rev_short,
                    "reverse_title": rev_title,
                }
                all_links.append(backlink)
                if verbose:
                    print(f"        - {rev_short} ({rev_title}): {role} → mapped to '{mapped_role}'")

    return all_links


def classify_tc(title: str) -> str:
    """Classify a TC as HLTC or LLTC based on title."""
    if "_HLTC_" in title:
        return "HLTC"
    elif "_LLTC_" in title:
        return "LLTC"
    return "UNKNOWN"


def validate_links(tc_type: str, links: list, is_unimplemented: bool) -> dict:
    """
    Validate linked work items for a TC.
    Returns a dict with issues found.
    """
    roles = {}
    for link in links:
        attrs = link.get("attributes", {})
        role = str(attrs.get("role", "")).lower()
        roles.setdefault(role, []).append(link)

    issues = []

    # Check 'verifies' links
    verifies = roles.get("verifies", [])
    if not verifies:
        issues.append("MISSING 'verifies' link (should link to requirement)")
    elif tc_type == "HLTC" and len(verifies) < 1:
        issues.append(f"Expected at least 1 'verifies' link, found {len(verifies)}")

    # Check 'is implemented by' link (TP link)
    implemented_by = roles.get("is_implemented_by", [])
    if not implemented_by:
        # Try alternate role names
        for role_name in roles:
            if "implemented" in role_name:
                implemented_by = roles[role_name]
                break

    if is_unimplemented:
        if implemented_by:
            issues.append("UNEXPECTED 'is implemented by' link — TC is marked as unimplemented")
    else:
        if not implemented_by:
            issues.append("MISSING 'is implemented by' link (should link to TP)")
        elif len(implemented_by) > 1:
            issues.append(f"Expected 1 'is implemented by' link, found {len(implemented_by)}")

    # Check 'contains' link (Checklist)
    contains = roles.get("contains", [])
    if not contains:
        # Try alternate
        for role_name in roles:
            if "contain" in role_name:
                contains = roles[role_name]
                break

    if not contains:
        issues.append("MISSING 'contains' link (should link to Checklist)")

    return {
        "verifies_count": len(verifies),
        "implemented_by_count": len(implemented_by),
        "contains_count": len(contains),
        "total_links": len(links),
        "roles_found": {k: len(v) for k, v in roles.items()},
        "issues": issues,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Validate linked work items on Polarion test cases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--component", default=None, help="Filter by component (e.g. BOOT_APP0)")
    parser.add_argument("--title-filter", default=None, help="Filter TCs by title substring")
    parser.add_argument("--query", default=None, help="Use a custom Lucene query (overrides --component and --title-filter)")
    parser.add_argument("--verify-ssl", action="store_true", default=False)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of TCs to check")
    args = parser.parse_args()

    base_url = os.environ.get("POLARION_API_BASE")
    pat = os.environ.get("POLARION_PAT")
    project_id = args.project_id or os.environ.get("POLARION_PROJECT_ID")

    if not all([base_url, pat, project_id]):
        print("Set POLARION_API_BASE, POLARION_PAT, POLARION_PROJECT_ID")
        sys.exit(1)

    base_url = base_url.rstrip('/')
    verify_ssl = args.verify_ssl
    session = create_polarion_session(base_url, pat, verify_ssl=verify_ssl)

    # Build query
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
    print(f"Fetching test cases...")

    wi_ids = query_work_items_paginated(session, base_url, project_id, query, verify_ssl, verbose=args.verbose)
    wi_ids = sorted(set(wi_ids))

    print(f"Found {len(wi_ids)} test case(s)\n")

    if not wi_ids:
        return

    # Stats
    ok_count = 0
    warn_count = 0
    error_count = 0
    processed = 0
    issues_summary = []

    for wi_id in wi_ids:
        if args.limit and processed >= args.limit:
            print(f"\nLimit of {args.limit} reached.")
            break

        short_id = extract_short_id(wi_id)

        # Fetch title and custom fields from work item
        url = f"{base_url}/projects/{project_id}/workitems/{short_id}"
        params = {"fields[workitems]": "title,status,fld_passFailCriteria,fld_initialCondition"}
        resp = session.get(url, params=params, verify=verify_ssl)
        if resp.status_code != 200:
            print(f"  ✗ {short_id} - Error fetching: {resp.status_code}")
            error_count += 1
            processed += 1
            continue

        attrs = resp.json().get("data", {}).get("attributes", {})
        title = attrs.get("title", "")
        status = attrs.get("status", "")

        # TC is only truly unimplemented (no TP link expected) when
        # the pass/fail criteria mentions "unimplemented"
        pf_criteria = attrs.get("fld_passFailCriteria", {})
        if isinstance(pf_criteria, dict):
            pf_text = pf_criteria.get("value", "")
        else:
            pf_text = str(pf_criteria) if pf_criteria else ""
        is_unimplemented = "unimplemented" in pf_text.lower()

        # Extract initial condition (often contains "Same as ..." references)
        ic_field = attrs.get("fld_initialCondition", {})
        if isinstance(ic_field, dict):
            ic_text = ic_field.get("value", "")
        else:
            ic_text = str(ic_field) if ic_field else ""
        # Strip HTML tags, then extract only the "Same as ..." reference
        ic_plain = re.sub(r'<[^>]+>', ' ', ic_text).strip() if ic_text else ""
        ic_plain = re.sub(r'\s+', ' ', ic_plain).strip()
        same_as_match = re.search(r'(?i)(same\s+as\s+.+)', ic_plain)
        ic_same_as = same_as_match.group(1).strip().rstrip('.') if same_as_match else ""

        tc_type = classify_tc(title)

        # Get linked work items
        links = get_linked_work_items(session, base_url, project_id, short_id, verify_ssl, verbose=args.verbose)
        result = validate_links(tc_type, links, is_unimplemented)

        has_issues = len(result["issues"]) > 0
        unimp_label = " [UNIMPLEMENTED]" if is_unimplemented else ""
        status_icon = "✗" if has_issues else "✓"

        if has_issues:
            error_count += 1
            print(f"  {status_icon} {short_id} - {title} ({tc_type}){unimp_label}")
            if ic_same_as:
                print(f"      {ic_same_as}")
            print(f"      Links: {result['total_links']} total — {result['roles_found']}")
            for issue in result["issues"]:
                print(f"      ⚠ {issue}")
            print()
            issues_summary.append((short_id, title, result["issues"], ic_same_as))
        elif args.verbose:
            print(f"  {status_icon} {short_id} - {title} ({tc_type}){unimp_label}")
            print(f"      Links: {result['total_links']} total — {result['roles_found']}")
            print()
            ok_count += 1
        else:
            ok_count += 1

        processed += 1

    # Summary
    print(f"\n{'=' * 60}")
    print(f"Summary: {processed} TC(s) checked")
    print(f"  OK:     {ok_count}")
    print(f"  Issues: {error_count}")
    print(f"{'=' * 60}")

    if issues_summary:
        print(f"\nTCs with issues:")
        for short_id, title, issues, ic_same_as in issues_summary:
            print(f"  {short_id} - {title}")
            if ic_same_as:
                print(f"    {ic_same_as}")
            for issue in issues:
                print(f"    - {issue}")
            print()


if __name__ == "__main__":
    main()
