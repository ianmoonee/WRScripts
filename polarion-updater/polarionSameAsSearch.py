#!/usr/bin/env python3
"""
Polarion "Same As" testCase Searcher

Queries Polarion for testCase work items belonging to a specific component,
finds items whose title or description contains "same as <name>", and outputs:
  1. Items grouped by the referenced test name (when name contains HLTC or LLTC)
  2. Items where the "same as" reference has no HLTC/LLTC (unresolved)

Environment Variables Required:
    POLARION_API_BASE   - Base URL for Polarion REST API
    POLARION_PAT        - Personal Access Token
    POLARION_PROJECT_ID - Project ID (e.g. Shallowford_BSP)

Usage:
    python polarionSameAsSearch.py --component SSD_NVME0
    python polarionSameAsSearch.py --component SSD_NVME0 --project-id Shallowford_BSP -v
    python polarionSameAsSearch.py --component SSD_NVME0 --type wi_testCase
"""

import os
import sys
import re
import string
import argparse
from collections import defaultdict
from typing import List, Dict, Any, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SAME_AS_RE = re.compile(r'same\s+as\s+([\w]+)', re.IGNORECASE)
HLTC_LLTC_RE = re.compile(r'(hltc|lltc)', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def make_session(pat: str, verify_ssl: bool) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {pat}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    session.verify = verify_ssl
    return session


def fetch_work_items_for_phrase(
    session: requests.Session,
    url: str,
    base_query: str,
    phrase: str,
    verbose: bool,
) -> List[Dict[str, Any]]:
    """Single request: fetch work items containing 'same as' AND a wildcard prefix term."""
    # Use "same as" as a quoted phrase + wildcard term for the prefix.
    # Wildcards are not allowed inside Lucene phrase quotes, so split them.
    query = f'{base_query} AND "same as" AND {phrase}*'
    params: Dict[str, Any] = {
        'query': query,
        'fields[workitems]': 'id,title,description,fld_initialCondition,fld_passfailCriteria,fld_expectedResults',
        'sort': 'id',
    }
    if verbose:
        print(f"  [VERBOSE] query: {query}")
    resp = session.get(url, params=params)
    if resp.status_code != 200:
        print(f"  Error {resp.status_code} for phrase '{phrase}'")
        if verbose:
            print(f"  [VERBOSE] {resp.text[:300]}")
        return []
    data = resp.json()
    items = data.get('data', [])
    if not isinstance(items, list):
        return []
    if verbose:
        print(f"  [VERBOSE] '{phrase}*': got {len(items)} items")
    return items


def fetch_all_work_items(
    session: requests.Session,
    base_url: str,
    project_id: str,
    wi_type: str,
    component: str,
    pattern: Optional[str],
    verbose: bool,
) -> List[Dict[str, Any]]:
    """
    Fetch work items containing 'same as <pattern>*'.
    If pattern is given, expands to 36 sub-queries (a-z, 0-9) to stay under the
    100-item API cap. Deduplicates results by work item ID.
    """
    url = f"{base_url}/projects/{project_id}/workitems"

    if component:
        base_query = f'type:{wi_type} AND fld_component.KEY:comp_{component} AND NOT status:deleted'
    else:
        base_query = f'type:{wi_type} AND NOT status:deleted'

    if verbose:
        print(f"  [VERBOSE] GET {url}")
        print(f"  [VERBOSE] base query: {base_query}")

    # Strip trailing wildcard if user passed e.g. 'nvme_*' or 'nvme*'
    prefixes = [p.rstrip('*') for p in pattern] if pattern else None

    seen_ids: set = set()
    all_items: List[Dict[str, Any]] = []

    if prefixes:
        # Expand: one request per prefix × suffix character (a-z, 0-9)
        suffixes = string.ascii_lowercase + string.digits
        for prefix in prefixes:
            for char in suffixes:
                phrase = f'{prefix}{char}'
                items = fetch_work_items_for_phrase(session, url, base_query, phrase, verbose)
                for item in items:
                    item_id = item.get('id', '')
                    if item_id not in seen_ids:
                        seen_ids.add(item_id)
                        all_items.append(item)
    else:
        # No pattern: single request with generic 'same as' phrase only
        query = f'{base_query} AND "same as"'
        params: Dict[str, Any] = {
            'query': query,
            'fields[workitems]': 'id,title,description,fld_initialCondition,fld_passfailCriteria,fld_expectedResults',
            'sort': 'id',
        }
        if verbose:
            print(f"  [VERBOSE] query: {query}")
        resp = session.get(url, params=params)
        if resp.status_code == 200:
            items = resp.json().get('data', [])
            if isinstance(items, list):
                for item in items:
                    item_id = item.get('id', '')
                    if item_id not in seen_ids:
                        seen_ids.add(item_id)
                        all_items.append(item)
                if verbose:
                    print(f"  [VERBOSE] 'same as': got {len(items)} items")

    return all_items


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    return re.sub(r'<[^>]+>', ' ', text)


def _extract_rich_text(field: Any) -> str:
    """Extract plain text from a rich-text field (dict with 'value') or plain string."""
    if isinstance(field, dict):
        return strip_html(field.get('value', '') or '')
    return strip_html(str(field)) if field else ''


def extract_same_as(item: Dict[str, Any], verbose: bool) -> Optional[str]:
    """
    Search for 'same as <name>' in title, description, initialConditions, passFail.
    Returns the captured name, or None if not found.
    """
    attributes = item.get('attributes', {})
    short_id = item.get('id', '').split('/')[-1]

    candidates = [
        ('title',                attributes.get('title', '') or ''),
        ('description',          _extract_rich_text(attributes.get('description', ''))),
        ('fld_initialCondition', _extract_rich_text(attributes.get('fld_initialCondition', ''))),
        ('fld_passfailCriteria', _extract_rich_text(attributes.get('fld_passfailCriteria', ''))),
        ('fld_expectedResults',  _extract_rich_text(attributes.get('fld_expectedResults', ''))),
    ]

    for field_name, text in candidates:
        if not text.strip():
            continue
        m = SAME_AS_RE.search(text)
        if m:
            if verbose:
                print(f"  [VERBOSE] {short_id}  matched in {field_name}: {text[:120].strip()!r}")
            return m.group(1)

    return None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

SEP = '=' * 60


def print_results(
    grouped: Dict[str, List[Tuple[str, str]]],
    unresolved: List[Tuple[str, str, str]],
) -> None:
    total_grouped_items = sum(len(v) for v in grouped.values())

    print()
    print(SEP)
    print(f"Groups by 'same as' reference ({len(grouped)} groups, {total_grouped_items} items):")
    print(SEP)

    for ref_key in sorted(grouped.keys(), key=str.lower):
        entries = grouped[ref_key]
        print(f"\n{ref_key}  ({len(entries)} item{'s' if len(entries) != 1 else ''})")
        for short_id, title in sorted(entries):
            print(f"    {short_id:<20}  {title}")

    print()
    print(SEP)
    print(f"'same as' found but no HLTC/LLTC in reference ({len(unresolved)} items):")
    print(SEP)

    if unresolved:
        print()
        for short_id, title, ref in sorted(unresolved):
            print(f"  {short_id:<20}  {title}  |  same as: \"{ref}\"")
    else:
        print("\n  (none)")

    print()


def print_clear_results(
    grouped: Dict[str, List[Tuple[str, str]]],
) -> None:
    """Print groups with TDK_CERT_TC_LOG lines instead of WI id/title."""
    total_grouped_items = sum(len(v) for v in grouped.values())

    print()
    print(SEP)
    print(f"Groups by 'same as' reference ({len(grouped)} groups, {total_grouped_items} items):")
    print(SEP)

    for ref_key in sorted(grouped.keys(), key=str.lower):
        entries = grouped[ref_key]
        print(f"\n{ref_key}  ({len(entries)} item{'s' if len(entries) != 1 else ''})")
        for _short_id, title in sorted(entries):
            print(f'    TDK_CERT_TC_LOG("Implements Test Case: {title} (also covered by this TP)\\n");')

    print()


def print_c_array(
    grouped: Dict[str, List[Tuple[str, str]]],
) -> None:
    """Print a C array of structs mapping each main TC to its 'same as' TCs."""
    max_same_as = max((len(v) for v in grouped.values()), default=1)

    print()
    print(f"#define MAX_SAME_AS_TCS {max_same_as}")
    print()
    print("typedef struct {")
    print("    const char *main_tc;")
    print("    const char *same_as_tcs[MAX_SAME_AS_TCS];")
    print("} SameAsGroup;")
    print()
    print(f"static const SameAsGroup same_as_groups[{len(grouped)}] = {{")

    for ref_key in sorted(grouped.keys(), key=str.lower):
        entries = grouped[ref_key]
        titles = [title for _short_id, title in sorted(entries)]
        tc_list = ", ".join(f'"{t}"' for t in titles)
        print(f'    {{ .main_tc = "{ref_key}", .same_as_tcs = {{ {tc_list} }} }},')

    print("};")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Find testCase work items containing "same as <name>" for a given component.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --component SSD_NVME0
  %(prog)s --component SSD_NVME0 --project-id Shallowford_BSP -v
  %(prog)s --component SSD_NVME0 --type wi_testCase
  %(prog)s  (no --component = all components)
        """,
    )
    parser.add_argument(
        '--component', default=None,
        help='Component to filter by (e.g. SSD_NVME0). If omitted, all components are included.',
    )
    parser.add_argument(
        '--project-id', default=None,
        help='Polarion project ID (overrides POLARION_PROJECT_ID env var)',
    )
    parser.add_argument(
        '--pattern', nargs='+', default=None,
        help='One or more prefixes for "same as" target (e.g. nvme_ arch_fdt). '
             'Each expands to 36 sub-queries (a-z, 0-9) to bypass the 100-item API cap.',
    )
    parser.add_argument(
        '--clear', action='store_true',
        help='Output one TDK_CERT_TC_LOG line per grouped reference instead of the full report.',
    )
    parser.add_argument(
        '--c-array', action='store_true',
        help='Output a C array of structs mapping each main TC to its same-as TCs.',
    )
    parser.add_argument(
        '--type', dest='wi_type', default='wi_testCase',
        help='Work item type to query (default: wi_testCase)',
    )
    parser.add_argument(
        '--verify-ssl', action='store_true', default=False,
        help='Enable SSL certificate verification (disabled by default)',
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Enable verbose output showing raw text being parsed',
    )
    args = parser.parse_args()

    base_url = os.environ.get('POLARION_API_BASE')
    pat = os.environ.get('POLARION_PAT')
    project_id = args.project_id or os.environ.get('POLARION_PROJECT_ID')

    missing = []
    if not base_url:
        missing.append('POLARION_API_BASE')
    if not pat:
        missing.append('POLARION_PAT')
    if not project_id:
        missing.append('POLARION_PROJECT_ID (or use --project-id)')
    if missing:
        print("Error: Missing required environment variables:")
        for v in missing:
            print(f"  - {v}")
        sys.exit(1)

    print(f"Component filter : {args.component or '(all)'}")
    print(f"Pattern          : {', '.join(args.pattern) if args.pattern else '(none — single query)'}")
    print(f"Project ID       : {project_id}")
    print(f"Work item type   : {args.wi_type}")
    print(f"Fetching work items...")

    session = make_session(pat, args.verify_ssl)
    component_items = fetch_all_work_items(
        session, base_url, project_id, args.wi_type, args.component, args.pattern, args.verbose
    )
    print(f"Total fetched    : {len(component_items)}")

    if args.verbose:
        print()

    # Parse "same as" references
    grouped: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    unresolved: List[Tuple[str, str, str]] = []
    no_same_as: List[Tuple[str, str]] = []

    for item in component_items:
        ref = extract_same_as(item, args.verbose)

        short_id = item.get('id', '').split('/')[-1]
        title = (item.get('attributes', {}).get('title', '') or '').strip()

        if ref is None:
            no_same_as.append((short_id, title))
            continue

        if HLTC_LLTC_RE.search(ref):
            grouped[ref].append((short_id, title))
        else:
            unresolved.append((short_id, title, ref))

    if args.verbose and no_same_as:
        print()
        print(SEP)
        print(f"No 'same as' found ({len(no_same_as)} items):")
        print(SEP)
        print()
        for short_id, title in sorted(no_same_as):
            print(f"  {short_id:<20}  {title}")
        print()

    if args.c_array:
        print_c_array(grouped)
    elif args.clear:
        print_clear_results(grouped)
    else:
        print_results(grouped, unresolved)


if __name__ == '__main__':
    main()
