#!/usr/bin/env python3
"""
Get Highest TR Revision

Queries Polarion for all Test Result (TR) work items belonging to a component
and prints the one with the highest revision number.

Environment Variables Required:
- POLARION_API_BASE: Base URL for Polarion API
- POLARION_PAT: Personal Access Token for authentication
- POLARION_PROJECT_ID: Project ID in Polarion (can also be provided via --project-id)

Usage:
    python getHighestTRRevision.py --component SSD_NVME0
    python getHighestTRRevision.py --component PLD_DIO0 --project-id Shallowford_BSP
"""

import os
import sys
import argparse
from typing import List, Optional, Tuple

import requests
import urllib3


def create_session(base_url: str, pat: str, verify_ssl: bool = False) -> requests.Session:
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


def query_tr_ids_paginated(
    session: requests.Session,
    base_url: str,
    project_id: str,
    component: str,
    verify_ssl: bool,
    verbose: bool = False,
) -> List[str]:
    """Return all TR work item IDs for the given component, handling the 100-item API cap."""
    url = f"{base_url}/projects/{project_id}/workitems"
    base_query = (
        f"type:wi_testResult AND NOT status:deleted AND NOT HAS_VALUE:resolution"
        f" AND fld_component.KEY:comp_{component}"
    )
    wi_ids: set = set()

    def query_by_prefix(prefix: str) -> None:
        params = {
            "query": f"{base_query} AND title:{prefix}*",
            "fields[workitems]": "id",
        }
        resp = session.get(url, params=params, verify=verify_ssl)
        if resp.status_code != 200:
            if verbose:
                print(f"  [VERBOSE] GET {resp.status_code} for prefix '{prefix}'")
            return
        items = resp.json().get("data", [])
        ids = [item["id"] for item in items if isinstance(item, dict) and "id" in item]
        if len(ids) >= 100:
            if verbose:
                print(f"  [VERBOSE] title:{prefix}* → {len(ids)} (capped), splitting deeper...")
            for c in "abcdefghijklmnopqrstuvwxyz0123456789_":
                query_by_prefix(f"{prefix}{c}")
        else:
            wi_ids.update(ids)

    for c in "abcdefghijklmnopqrstuvwxyz0123456789_":
        query_by_prefix(c)

    return list(wi_ids)


def fetch_revision(
    session: requests.Session,
    base_url: str,
    project_id: str,
    wi_id: str,
    verify_ssl: bool,
) -> Optional[Tuple[str, str, int]]:
    """
    Fetch title and revision for a single work item.
    Returns (short_id, title, revision_int) or None on failure.
    """
    short_id = wi_id.split("/")[-1] if "/" in wi_id else wi_id
    url = f"{base_url}/projects/{project_id}/workitems/{short_id}"
    params = {"fields[workitems]": "title,revision"}
    resp = session.get(url, params=params, verify=verify_ssl)
    if resp.status_code != 200:
        return None
    attrs = resp.json().get("data", {}).get("attributes", {})
    title = attrs.get("title", "")
    revision_str = attrs.get("revision", "")
    try:
        revision_int = int(revision_str)
    except (ValueError, TypeError):
        revision_int = -1
    return (short_id, title, revision_int)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find the TR with the highest revision number for a given component.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --component SSD_NVME0
  %(prog)s --component PLD_DIO0 --project-id Shallowford_BSP --verbose
        """,
    )
    parser.add_argument("--component", required=True, help="Component name (e.g. SSD_NVME0)")
    parser.add_argument("--project-id", help="Polarion project ID (overrides POLARION_PROJECT_ID)")
    parser.add_argument("--verify-ssl", action="store_true", default=False)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    base_url = os.environ.get("POLARION_API_BASE", "").rstrip("/")
    pat = os.environ.get("POLARION_PAT", "")
    project_id = args.project_id or os.environ.get("POLARION_PROJECT_ID", "")

    missing = []
    if not base_url:
        missing.append("POLARION_API_BASE")
    if not pat:
        missing.append("POLARION_PAT")
    if not project_id:
        missing.append("POLARION_PROJECT_ID (or use --project-id)")
    if missing:
        print("Error: Missing required environment variables:")
        for var in missing:
            print(f"  - {var}")
        sys.exit(1)

    session = create_session(base_url, pat, verify_ssl=args.verify_ssl)

    print(f"Component : {args.component}")
    print(f"Project   : {project_id}")
    print()

    print("Fetching TR work item IDs...")
    wi_ids = query_tr_ids_paginated(
        session, base_url, project_id, args.component,
        verify_ssl=args.verify_ssl, verbose=args.verbose,
    )
    print(f"  Found {len(wi_ids)} TR(s)")

    if not wi_ids:
        print("No TRs found for this component.")
        sys.exit(0)

    print("Fetching revisions...")
    best: Optional[Tuple[str, str, int]] = None
    failed = 0
    first = True
    for wi_id in wi_ids:
        result = fetch_revision(session, base_url, project_id, wi_id, args.verify_ssl)
        if result is None:
            failed += 1
            continue
        short_id, title, rev = result
        if first and args.verbose:
            # Dump full JSON of the first TR to help identify the correct revision field name
            import json as _json
            _short = wi_id.split("/")[-1] if "/" in wi_id else wi_id
            _url = f"{base_url}/projects/{project_id}/workitems/{_short}"
            _params = {
                "fields[workitems]": "@all",
                "fields[categories]": "@all",
                "fields[linkedworkitems]": "@all",
            }
            _resp = session.get(_url, params=_params, verify=args.verify_ssl)
            print(f"\n--- Full JSON for first TR ({_short}) ---")
            print(_json.dumps(_resp.json(), indent=2))
            print("--- End JSON ---\n")
            first = False
        if args.verbose:
            print(f"  {short_id}: revision={rev}  ({title})")
        if best is None or rev > best[2]:
            best = result

    if failed:
        print(f"  Warning: could not fetch {failed} work item(s)")

    print()
    if best:
        print(f"Highest revision TR:")
        print(f"  ID       : {best[0]}")
        print(f"  Title    : {best[1]}")
        print(f"  Revision : {best[2]}")
    else:
        print("No valid revision found.")


if __name__ == "__main__":
    main()
