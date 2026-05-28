#!/usr/bin/env python3
"""
Polarion LLR Description Linter

Fetches Low Level Requirement (LLR) work items for a given component and
checks their descriptions for formatting issues, typos, and structural problems.

Environment Variables Required:
    POLARION_API_BASE   - Base URL for Polarion REST API
    POLARION_PAT        - Personal Access Token

Usage:
    python polarionLLRLint.py --component PRJ_CFG1 -f functions.txt
    python polarionLLRLint.py --component PRJ_CFG1 -f functions.txt -v
    python polarionLLRLint.py --component PRJ_CFG1 -f functions.txt --project-id Shallowford_BSP
"""

import os
import sys
import re
import json
import argparse
from typing import List, Dict, Any, Set, Tuple
from collections import defaultdict

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DONE_PREFIX_RE = re.compile(r'^\s*DONE\s*-\s*', re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')
HTML_ENTITY_RE = re.compile(r'&(?:amp|lt|gt|nbsp|quot|apos|#\d+|#x[0-9a-fA-F]+);')
MULTI_SPACE_RE = re.compile(r'  +')
DUPLICATE_WORD_RE = re.compile(r'\b(\w+)\s+\1\b', re.IGNORECASE)
BRACKET_FUNC_RE = re.compile(r'\[([^\]]*)')
TODO_RE = re.compile(r'\b(TODO|FIXME|TBD|TBC|HACK|XXX)\b', re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r'\b(insert here|update this|fill in|placeholder)\b', re.IGNORECASE)
LINE_PREFIX_RE = re.compile(r'^(?:(?: {4})*- )')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    """Remove HTML tags, returning plain text."""
    text = HTML_TAG_RE.sub('', text)
    # Normalize non-breaking spaces and tabs to regular spaces
    text = text.replace('\xa0', ' ').replace('\t', '    ')
    return text


def extract_description_raw(item: Dict[str, Any]) -> str:
    """Extract raw description (HTML) from a work item."""
    attrs = item.get('attributes', {})
    desc = attrs.get('description', '')
    if isinstance(desc, dict):
        desc = desc.get('value', '') or ''
    if desc is None:
        desc = ''
    return str(desc)


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


def load_function_names(file_path: str) -> List[str]:
    """Load function names from file, stripping DONE prefix."""
    names = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            line = DONE_PREFIX_RE.sub('', line).strip()
            if line:
                names.append(line)
    # Deduplicate
    seen = set()
    unique = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


def get_function_name_from_title(title: str) -> str:
    """Extract the base function name from an LLR title (before _LLR_ suffix)."""
    # Titles like: functionName_LLR_1
    parts = title.split('_LLR_')
    return parts[0] if parts else title


# ---------------------------------------------------------------------------
# Polarion Query
# ---------------------------------------------------------------------------

def fetch_llrs(
    session: requests.Session,
    base_url: str,
    project_id: str,
    component: str,
    function_name: str,
    verbose: bool,
    debug: bool,
) -> List[Dict[str, Any]]:
    """Fetch LLR work items for a function name in a component."""
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
        print(f"    [DEBUG] query: {query}")

    resp = session.get(url, params=params)

    if resp.status_code != 200:
        print(f"    [ERROR] {resp.status_code} querying {component} for '{function_name}'")
        return []

    data = resp.json()
    items = data.get('data', [])
    if not isinstance(items, list):
        return []

    if verbose:
        print(f"    [{component}] {len(items)} LLR(s) for '{function_name}'")

    return items


# ---------------------------------------------------------------------------
# Lint Rules
# ---------------------------------------------------------------------------

def lint_description(
    title: str,
    raw_html: str,
    known_symbols: Set[str],
    verbose: bool,
) -> List[str]:
    """
    Run all lint rules on a description. Returns list of issue strings.
    """
    issues = []
    plain = strip_html(raw_html).strip()
    func_name = get_function_name_from_title(title)

    if not plain:
        issues.append("Description is empty")
        return issues

    # Split into lines
    lines = plain.split('\n')
    lines = [l.rstrip() for l in lines]
    # Remove empty lines for structural check
    non_empty_lines = [l for l in lines if l.strip()]

    # --- Rule: First line must start with "[function_name] shall do the following:" ---
    if non_empty_lines:
        first_line = non_empty_lines[0].strip()
        expected_prefix = f"[{func_name}] shall do the following:"
        if not first_line.startswith(f"[{func_name}]"):
            issues.append(
                f"First line doesn't start with [{func_name}]: \"{first_line[:80]}\""
            )
        elif "shall" not in first_line.lower():
            issues.append(
                f"First line missing 'shall': \"{first_line[:80]}\""
            )

    # --- Rule: Subsequent lines must start with "- " or (4*N spaces + "- ") ---
    for i, line in enumerate(non_empty_lines[1:], start=2):
        stripped = line
        if not stripped:
            continue
        # Normalize any remaining non-standard whitespace
        normalized = stripped.replace('\xa0', ' ').replace('\t', '    ')
        # Check line starts with proper prefix
        if not LINE_PREFIX_RE.match(normalized):
            # Check if it's indented with spaces but wrong pattern
            leading_spaces = len(normalized) - len(normalized.lstrip())
            if leading_spaces > 0 and leading_spaces % 4 != 0:
                issues.append(
                    f"Line {i}: indentation not multiple of 4 ({leading_spaces} spaces): \"{stripped[:60]}\""
                )
            elif not normalized.lstrip().startswith('- '):
                issues.append(
                    f"Line {i}: doesn't start with '- ': \"{stripped[:60]}\""
                )
            else:
                # 4*N spaces + "- " but regex still didn't match — unexpected chars
                issues.append(
                    f"Line {i}: unexpected characters in indentation: \"{stripped[:60]}\""
                )

    # --- Rule: Unresolved HTML entities ---
    entities_found = HTML_ENTITY_RE.findall(raw_html)
    # After stripping, check if entities remain in plain text
    entities_in_plain = HTML_ENTITY_RE.findall(plain)
    if entities_in_plain:
        issues.append(
            f"Unresolved HTML entities: {', '.join(set(entities_in_plain))}"
        )

    # --- Rule: Multiple consecutive spaces (after leading whitespace) ---
    for i, line in enumerate(non_empty_lines, start=1):
        content_after_indent = line.lstrip()
        if MULTI_SPACE_RE.search(content_after_indent):
            issues.append(
                f"Line {i}: multiple consecutive spaces: \"{content_after_indent[:80]}\""
            )
            break  # Report once

    # --- Rule: Duplicate consecutive words ---
    dups = DUPLICATE_WORD_RE.findall(plain)
    if dups:
        issues.append(
            f"Duplicate consecutive words: {', '.join(set(d.lower() for d in dups))}"
        )

    # --- Rule: Unmatched brackets ---
    open_count = plain.count('[')
    close_count = plain.count(']')
    if open_count != close_count:
        issues.append(
            f"Unmatched brackets: {open_count} '[' vs {close_count} ']'"
        )
    open_paren = plain.count('(')
    close_paren = plain.count(')')
    if open_paren != close_paren:
        issues.append(
            f"Unmatched parentheses: {open_paren} '(' vs {close_paren} ')'"
        )

    # --- Rule: Function name not referenced in description ---
    if func_name and func_name not in plain:
        issues.append(
            f"Function name '{func_name}' not found in description"
        )

    # --- Rule: TODO/FIXME/TBD markers ---
    todos = TODO_RE.findall(plain)
    if todos:
        issues.append(
            f"Markers found: {', '.join(set(t.upper() for t in todos))}"
        )

    # --- Rule: Placeholder text ---
    placeholders = PLACEHOLDER_RE.findall(plain)
    if placeholders:
        issues.append(
            f"Placeholder text: {', '.join(set(placeholders))}"
        )

    # --- Rule: Description is just the title ---
    if plain.strip() == title.strip():
        issues.append("Description is just the title repeated")

    # --- Rule: Trailing/leading whitespace in lines ---
    for i, line in enumerate(lines, start=1):
        if line and line != line.rstrip():
            issues.append(f"Line {i}: trailing whitespace")
            break  # Report once
        if line and line != line.lstrip() and i == 1:
            issues.append("First line has leading whitespace")

    # --- Rule: Inconsistent function name casing in description ---
    # Find all bracketed references like [funcName]
    bracketed = re.findall(r'\[([^\]]+)\]', plain)
    for ref in bracketed:
        # Skip known symbols
        if ref in known_symbols or ref.startswith('_func_'):
            # Check if the symbol after _func_ matches any known symbol
            inner = ref.replace('_func_', '')
            if inner and inner not in known_symbols:
                # Check for near-match (case-insensitive) against known symbols
                for sym in known_symbols:
                    if sym.lower() == inner.lower() and sym != inner:
                        issues.append(
                            f"Case mismatch in [{ref}]: '{inner}' vs known '{sym}'"
                        )
                        break

    return issues


# ---------------------------------------------------------------------------
# Duplicate Detection
# ---------------------------------------------------------------------------

def check_duplicates(
    all_items: List[Tuple[str, str]],
) -> List[str]:
    """Check for exact duplicate descriptions across LLRs. Input: list of (title, plain_desc)."""
    desc_to_titles: Dict[str, List[str]] = defaultdict(list)
    for title, desc in all_items:
        if desc.strip():
            desc_to_titles[desc.strip()].append(title)

    issues = []
    for desc, titles in desc_to_titles.items():
        if len(titles) > 1:
            issues.append(
                f"Duplicate description shared by: {', '.join(titles)}"
            )
    return issues


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Lint LLR descriptions for a Polarion component.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --component PRJ_CFG1 -f functions.txt
  %(prog)s --component PRJ_CFG1 -f functions.txt -v
  %(prog)s --component PRJ_CFG0 -f functions.txt --project-id Shallowford_BL
        """,
    )
    parser.add_argument(
        '--component', required=True,
        help='Component key (e.g. PRJ_CFG0, PRJ_CFG1)',
    )
    parser.add_argument(
        '-f', '--file', required=True,
        help='Path to file with function names (one per line, DONE - prefix stripped)',
    )
    parser.add_argument(
        '--project-id', default='Shallowford_BSP',
        help='Polarion project ID (default: Shallowford_BSP)',
    )
    parser.add_argument(
        '--verify-ssl', action='store_true', default=False,
        help='Enable SSL certificate verification (disabled by default)',
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Verbose output: show items with no issues too',
    )
    parser.add_argument(
        '-d', '--debug', action='store_true',
        help='Debug output: show queries and raw data',
    )
    parser.add_argument(
        '--report', action='store_true',
        help='Write results to a .lint.txt file in the script directory',
    )
    args = parser.parse_args()

    if args.debug:
        args.verbose = True

    # --- Env vars ---
    base_url = os.environ.get('POLARION_API_BASE')
    pat = os.environ.get('POLARION_PAT')

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
    function_names = load_function_names(args.file)
    if not function_names:
        print("Error: No function names found in file.")
        sys.exit(1)

    # Build set of known symbols (function names used as spellcheck whitelist)
    known_symbols: Set[str] = set(function_names)

    print(f"Component        : {args.component}")
    print(f"Project          : {args.project_id}")
    print(f"Functions to lint: {len(function_names)}")
    print()

    # --- Process ---
    session = make_session(pat, args.verify_ssl)

    total_issues = 0
    total_items = 0
    items_with_issues = 0
    all_descriptions: List[Tuple[str, str]] = []
    report_lines: List[str] = []
    seen_item_ids: Set[str] = set()

    for func_name in function_names:
        items = fetch_llrs(
            session, base_url, args.project_id, args.component,
            func_name, args.verbose, args.debug,
        )

        # First pass: collect bracketed refs across all LLRs for this function
        func_items = []  # (item_id, title, raw_html, plain)
        func_bracketed_refs: Set[str] = set()
        for item in items:
            item_id = item.get('id', '')
            if item_id in seen_item_ids:
                continue
            seen_item_ids.add(item_id)

            title = extract_title(item)
            raw_html = extract_description_raw(item)
            plain = strip_html(raw_html).strip()
            func_items.append((item_id, title, raw_html, plain))

            # Collect all bracketed references from this LLR
            func_bracketed_refs.update(re.findall(r'\[([^\]]+)\]', plain))

        # Second pass: lint each LLR and check bracket consistency across function
        for item_id, title, raw_html, plain in func_items:
            total_items += 1

            # Collect for duplicate check
            all_descriptions.append((title, plain))

            # Lint
            issues = lint_description(title, raw_html, known_symbols, args.verbose)

            # Cross-LLR bracket consistency: if a ref is bracketed in any LLR
            # of this function, it should be bracketed everywhere
            plain_lines = plain.split('\n')
            for ref in func_bracketed_refs:
                for line_num, pline in enumerate(plain_lines, start=1):
                    # Remove bracketed segments from the line, then check
                    line_outside = re.sub(r'\[[^\]]*\]', '', pline)
                    if ref in line_outside:
                        issues.append(
                            f"'{ref}' appears unbracketed on line {line_num}: \"{pline.strip()[:80]}\""
                        )

            if issues:
                items_with_issues += 1
                total_issues += len(issues)
                header = f"[{title}]"
                print(header)
                report_lines.append(header)
                for issue in issues:
                    line = f"  ! {issue}"
                    print(line)
                    report_lines.append(line)
                print()
                report_lines.append('')
            elif args.verbose:
                print(f"[{title}] OK")

    # --- Duplicate check ---
    dup_issues = check_duplicates(all_descriptions)
    if dup_issues:
        print("=" * 60)
        print("Duplicate Descriptions:")
        print("=" * 60)
        report_lines.append("=" * 60)
        report_lines.append("Duplicate Descriptions:")
        report_lines.append("=" * 60)
        for issue in dup_issues:
            total_issues += 1
            line = f"  ! {issue}"
            print(line)
            report_lines.append(line)
        print()
        report_lines.append('')

    # --- Summary ---
    print("=" * 60)
    summary = (
        f"Summary: {total_items} item(s) checked, "
        f"{items_with_issues} with issues, {total_issues} total issue(s)"
    )
    print(summary)
    print("=" * 60)
    report_lines.append("=" * 60)
    report_lines.append(summary)
    report_lines.append("=" * 60)

    # --- Write report ---
    if args.report and report_lines:
        report_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            f'polarion_llr_lint_{args.component}.txt'
        )
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))
        print(f"\nReport written to: {report_path}")


if __name__ == '__main__':
    main()
