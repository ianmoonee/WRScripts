#!/usr/bin/env python3
"""
Polarion LLR Description Comparator

Queries Polarion for Low Level Requirement (LLR) work items matching given
function names in both PRJ_CFG0 and PRJ_CFG1 components, matches them by title,
and reports description differences.

Environment Variables Required:
    POLARION_API_BASE   - Base URL for Polarion REST API
    POLARION_PAT        - Personal Access Token
    POLARION_PROJECT_ID - Project ID (e.g. Shallowford_BSP)

Usage:
    python polarionLLRCompare.py edrKernelFatalPolicyHandler usrBanner
    python polarionLLRCompare.py -f functions.txt
    python polarionLLRCompare.py -f functions.txt edrKernelFatalPolicyHandler -v
    python polarionLLRCompare.py -f functions.txt -d
"""

import os
import sys
import re
import io
import json
import argparse
import contextlib
import difflib
from typing import List, Dict, Any, Tuple

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DONE_PREFIX_RE = re.compile(r'^\s*DONE\s*-\s*', re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    """Remove HTML tags, returning plain text."""
    return HTML_TAG_RE.sub(' ', text)


def extract_description(item: Dict[str, Any]) -> str:
    """Extract plain-text description from a work item."""
    attrs = item.get('attributes', {})
    desc = attrs.get('description', '')
    if isinstance(desc, dict):
        desc = desc.get('value', '') or ''
    if desc is None:
        desc = ''
    return strip_html(str(desc)).strip()


def extract_title(item: Dict[str, Any]) -> str:
    """Extract title from a work item."""
    return (item.get('attributes', {}).get('title', '') or '').strip()


def make_session(pat: str, verify_ssl: bool) -> requests.Session:
    """Create an authenticated requests session."""
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {pat}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    session.verify = verify_ssl
    return session


def load_function_names(file_path: str = None, cli_names: List[str] = None) -> List[str]:
    """Load function names from file and/or CLI args, stripping DONE prefix."""
    names = []

    if file_path:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                line = DONE_PREFIX_RE.sub('', line).strip()
                if line:
                    names.append(line)

    if cli_names:
        for name in cli_names:
            name = DONE_PREFIX_RE.sub('', name).strip()
            if name:
                names.append(name)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


# ---------------------------------------------------------------------------
# Polarion Query
# ---------------------------------------------------------------------------

def fetch_llrs_for_component(
    session: requests.Session,
    base_url: str,
    project_id: str,
    component: str,
    function_name: str,
    verbose: bool,
    debug: bool,
) -> List[Dict[str, Any]]:
    """Fetch LLR work items for a function name in a specific component."""
    url = f"{base_url}/projects/{project_id}/workitems"
    query = (
        f'NOT status:deleted AND fld_component.KEY:comp_{component} '
        f'AND type:wi_lowLevelReq AND ({function_name}*)'
    )
    params = {
        'query': query,
        'fields[workitems]': 'id,title,description',
        'sort': 'id',
    }

    if debug:
        print(f"    [DEBUG] GET {url}")
        print(f"    [DEBUG] query: {query}")

    resp = session.get(url, params=params)

    if debug:
        print(f"    [DEBUG] status: {resp.status_code}")

    if resp.status_code != 200:
        print(f"    [ERROR] {resp.status_code} querying {component} for '{function_name}'")
        if debug:
            print(f"    [DEBUG] response: {resp.text[:500]}")
        return []

    data = resp.json()

    if debug:
        print(f"    [DEBUG] raw response: {json.dumps(data, indent=2)[:2000]}")

    items = data.get('data', [])
    if not isinstance(items, list):
        return []

    if verbose:
        print(f"    [{component}] {len(items)} LLR(s) found for '{function_name}'")

    return items


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_llrs(
    items_cfg0: List[Dict[str, Any]],
    items_cfg1: List[Dict[str, Any]],
    function_name: str,
    verbose: bool,
    debug: bool,
    ignore_missing: bool = False,
    diff_lines: List[str] = None,
    missing_in_cfg0: List[str] = None,
    matched_titles: List[str] = None,
) -> Tuple[int, int]:
    """
    Compare LLRs between PRJ_CFG0 and PRJ_CFG1 by matching titles.
    Returns (differences_count, missing_count).
    If diff_lines is provided, appends unified diff output to it.
    """
    # Build dicts keyed by title
    cfg0_by_title: Dict[str, Dict[str, Any]] = {}
    for item in items_cfg0:
        title = extract_title(item)
        if title:
            cfg0_by_title[title] = item

    cfg1_by_title: Dict[str, Dict[str, Any]] = {}
    for item in items_cfg1:
        title = extract_title(item)
        if title:
            cfg1_by_title[title] = item

    all_titles = set(cfg0_by_title.keys()) | set(cfg1_by_title.keys())
    differences = 0
    missing = 0

    for title in sorted(all_titles):
        in_cfg0 = title in cfg0_by_title
        in_cfg1 = title in cfg1_by_title

        if in_cfg0 and not in_cfg1:
            if not ignore_missing:
                print(f'  "{title}" exists in PRJ_CFG0 but missing in PRJ_CFG1')
                missing += 1
        elif in_cfg1 and not in_cfg0:
            if not ignore_missing:
                print(f'  "{title}" exists in PRJ_CFG1 but missing in PRJ_CFG0')
                missing += 1
            if missing_in_cfg0 is not None:
                missing_in_cfg0.append(title)
        else:
            # Both exist — compare descriptions
            desc0 = extract_description(cfg0_by_title[title])
            desc1 = extract_description(cfg1_by_title[title])

            if desc0 != desc1:
                print(f'  "{title}" description differs between PRJ_CFG0 and PRJ_CFG1')
                differences += 1
                if diff_lines is not None:
                    diff_lines.extend(difflib.unified_diff(
                        desc0.splitlines(keepends=True),
                        desc1.splitlines(keepends=True),
                        fromfile=f'PRJ_CFG0: {title}',
                        tofile=f'PRJ_CFG1: {title}',
                        lineterm='',
                    ))
                    diff_lines.append('')
                if debug:
                    print(f'    [DEBUG] PRJ_CFG0 description:')
                    print(f'      {desc0[:500]}')
                    print(f'    [DEBUG] PRJ_CFG1 description:')
                    print(f'      {desc1[:500]}')
            else:
                if verbose:
                    print(f'  "{title}" OK (descriptions match)')
                if matched_titles is not None:
                    matched_titles.append(title)

    return differences, missing


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Compare LLR descriptions between PRJ_CFG0 and PRJ_CFG1 components.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s edrKernelFatalPolicyHandler usrBanner
  %(prog)s -f functions.txt
  %(prog)s -f functions.txt edrKernelFatalPolicyHandler -v
  %(prog)s -f functions.txt -d
        """,
    )
    parser.add_argument(
        'names', nargs='*', default=[],
        help='Function names to search (wildcarded automatically)',
    )
    parser.add_argument(
        '-f', '--file', default=None,
        help='Path to a file with function names, one per line. '
             'Lines prefixed with "DONE - " are stripped and still searched.',
    )
    parser.add_argument(
        '--verify-ssl', action='store_true', default=False,
        help='Enable SSL certificate verification (disabled by default)',
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Verbose output: show query details, item counts, matched items',
    )
    parser.add_argument(
        '-d', '--debug', action='store_true',
        help='Debug output: full queries, raw responses, side-by-side descriptions (implies -v)',
    )
    parser.add_argument(
        '--ignore-missing', action='store_true',
        help='Do not report work items that exist in one component but not the other',
    )
    parser.add_argument(
        '--diff', action='store_true',
        help='Generate a .diff file with all description differences and open it in VS Code',
    )
    args = parser.parse_args()

    if args.debug:
        args.verbose = True

    # --- Env vars ---
    base_url = os.environ.get('POLARION_API_BASE')
    pat = os.environ.get('POLARION_PAT')
    project_id_cfg0 = 'Shallowford_BL'
    project_id_cfg1 = 'Shallowford_BSP'

    missing_env = []
    if not base_url:
        missing_env.append('POLARION_API_BASE')
    if not pat:
        missing_env.append('POLARION_PAT')
    if missing_env:
        print("Error: Missing required environment variables:")
        for v in missing_env:
            print(f"  - {v}")
        sys.exit(1)

    base_url = base_url.rstrip("/")

    # --- Load function names ---
    function_names = load_function_names(args.file, args.names)
    if not function_names:
        print("Error: No function names provided. Use positional args or --file.")
        sys.exit(1)

    if args.verbose:
        print(f"PRJ_CFG0 project : {project_id_cfg0}")
        print(f"PRJ_CFG1 project : {project_id_cfg1}")
        print(f"Functions to check: {len(function_names)}")
        print()

    # --- Process ---
    session = make_session(pat, args.verify_ssl)
    total_diffs = 0
    total_missing = 0
    all_diff_lines: List[str] = [] if args.diff else None
    all_missing_in_cfg0: List[str] = [] if args.diff else None
    all_matched_titles: List[str] = [] if args.diff else None

    for func_name in function_names:
        if args.verbose:
            print(f"[{func_name}]")
        else:
            pass  # Only print if there are differences

        items_cfg0 = fetch_llrs_for_component(
            session, base_url, project_id_cfg0, 'PRJ_CFG0', func_name, args.verbose, args.debug
        )
        items_cfg1 = fetch_llrs_for_component(
            session, base_url, project_id_cfg1, 'PRJ_CFG1', func_name, args.verbose, args.debug
        )

        if not items_cfg0 and not items_cfg1:
            if args.verbose:
                print(f"  No LLRs found in either component for '{func_name}'")
                print()
            continue

        # In non-verbose mode, print function header before comparison output
        if not args.verbose:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                diffs, miss = compare_llrs(items_cfg0, items_cfg1, func_name, False, args.debug, args.ignore_missing, all_diff_lines, all_missing_in_cfg0, all_matched_titles)
            output = buf.getvalue()
            if output.strip():
                print(f"[{func_name}]")
                print(output, end='')
        else:
            diffs, miss = compare_llrs(items_cfg0, items_cfg1, func_name, args.verbose, args.debug, args.ignore_missing, all_diff_lines, all_missing_in_cfg0, all_matched_titles)
            print()

        total_diffs += diffs
        total_missing += miss

    # --- Summary ---
    print("=" * 60)
    print(f"Summary: {len(function_names)} function(s) checked, "
          f"{total_diffs} difference(s), {total_missing} missing item(s)")
    print("=" * 60)

    # --- Generate diff file ---
    if args.diff and (all_diff_lines or all_missing_in_cfg0 or all_matched_titles):
        diff_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'polarion_llr_compare.diff')
        with open(diff_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(all_diff_lines))
            if all_missing_in_cfg0:
                f.write('\n\n')
                f.write('=' * 60 + '\n')
                f.write(f'Items in PRJ_CFG1 but missing in PRJ_CFG0 ({len(all_missing_in_cfg0)}):\n')
                f.write('=' * 60 + '\n')
                for title in all_missing_in_cfg0:
                    f.write(f'  {title}\n')
            if all_matched_titles:
                f.write('\n\n')
                f.write('=' * 60 + '\n')
                f.write(f'Items with matching descriptions ({len(all_matched_titles)}):\n')
                f.write('=' * 60 + '\n')
                for title in all_matched_titles:
                    f.write(f'  {title}\n')
        print(f"\nDiff file written to: {diff_path}")
    elif args.diff:
        print("\nNo differences to write to diff file.")


if __name__ == '__main__':
    main()
