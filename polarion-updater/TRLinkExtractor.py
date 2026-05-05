"""
TRLinkExtractor.py

Scans a folder tree for recently-committed target_*.log files located under
BOOT_APP0 directories, generates GitLab blob links for each, extracts
referenced test case names, and optionally adds those links as ref_src
hyperlinks on matching Polarion Test Result (TR) work items.

Usage:
    python TRLinkExtractor.py <folder_path> [--since "N days ago"]
    python TRLinkExtractor.py <folder_path> --update-polarion [--execute]

Environment variables (required for --update-polarion):
    POLARION_API_BASE    - Base URL of the Polarion REST API
    POLARION_PAT         - Personal Access Token for Polarion
    POLARION_PROJECT_ID  - Target Polarion project ID
"""

import subprocess
import os
import sys
import re
import argparse
import json
import requests
import urllib3

# Suppress insecure-request warnings (Polarion may use self-signed certs)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def get_git_root(folder):
    """Return the absolute path to the top-level directory of the git repo containing *folder*."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=folder, capture_output=True, text=True
    )
    return result.stdout.strip()


def get_remote_url(git_root):
    """Return the HTTPS URL of the 'origin' remote (converts SSH remotes automatically)."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=git_root, capture_output=True, text=True
    )
    url = result.stdout.strip()
    # Convert SSH to HTTPS: git@gitlab.com:group/project.git -> https://gitlab.com/group/project
    ssh_match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
    if ssh_match:
        return f"https://{ssh_match.group(1)}/{ssh_match.group(2)}"
    # Strip trailing .git from HTTPS URLs
    return re.sub(r"\.git$", "", url)


def has_recent_commits(filepath, git_root, since="1 day ago"):
    """Return True if *filepath* has any commits newer than *since* (git log --since format)."""
    result = subprocess.run(
        ["git", "log", f"--since={since}", "--oneline", "--", filepath],
        cwd=git_root, capture_output=True, text=True
    )
    return bool(result.stdout.strip())


def extract_test_cases(filepath):
    """Parse a log file for 'Implements Test Case: <name>' lines; return de-duplicated list."""
    tc_pattern = re.compile(r"Implements Test Case:\s*(.+)")
    test_cases = []
    with open(filepath, "r", errors="ignore") as f:
        for line in f:
            match = tc_pattern.search(line)
            if match:
                tc = match.group(1).strip()
                if tc not in test_cases:
                    test_cases.append(tc)
    return test_cases


# ---------------------------------------------------------------------------
# Polarion integration
# ---------------------------------------------------------------------------

def create_polarion_session():
    """
    Create an authenticated requests.Session for the Polarion REST API.

    Reads POLARION_API_BASE, POLARION_PAT, and POLARION_PROJECT_ID from env.
    Exits with an error message if any are missing.

    Returns:
        (session, base_url, project_id)
    """
    base_url = os.environ.get("POLARION_API_BASE")
    pat = os.environ.get("POLARION_PAT")
    project_id = os.environ.get("POLARION_PROJECT_ID")
    if not all([base_url, pat, project_id]):
        print("Error: POLARION_API_BASE, POLARION_PAT, and POLARION_PROJECT_ID env vars are required")
        sys.exit(1)
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    session.verify = False
    return session, base_url.rstrip("/"), project_id


def query_tr_by_function_name(session, base_url, project_id, func_name):
    """Query Polarion for TRs matching e.g. applyWriteProtect_TR_*"""
    query = f"NOT HAS_VALUE:resolution AND NOT status:deleted AND type:wi_testResult AND title:{func_name}_TR_*"
    url = f"{base_url}/projects/{project_id}/workitems"
    params = {
        "query": query,
        "fields[workitems]": "id,title,hyperlinks,status",
    }
    resp = session.get(url, params=params)
    if resp.status_code != 200:
        print(f"    Error querying Polarion: {resp.status_code}")
        return []
    data = resp.json().get("data", [])
    results = []
    for item in data:
        wi_id = item.get("id", "")
        attrs = item.get("attributes", {})
        results.append({
            "id": wi_id,
            "short_id": wi_id.split("/")[-1] if "/" in wi_id else wi_id,
            "title": attrs.get("title", ""),
            "status": attrs.get("status", ""),
            "hyperlinks": attrs.get("hyperlinks", []),
        })
    return results


def add_source_link_to_tr(session, base_url, project_id, tr, log_link, dry_run=True):
    """
    Add a ref_src hyperlink to a TR work item if not already present.

    Workflow when dry_run=False:
      1. Re-fetch current hyperlinks (avoids stale data / duplicates).
      2. If the link already exists, skip.
      3. Transition status to 'rework' (required to edit fields).
      4. PATCH the hyperlinks list with the new entry.
      5. Transition status back to 'in_review'.
    """
    # Re-fetch current hyperlinks to avoid duplicates on re-runs
    url = f"{base_url}/projects/{project_id}/workitems/{tr['short_id']}"
    params = {"fields[workitems]": "hyperlinks,status"}
    resp = session.get(url, params=params)
    if resp.status_code == 200:
        attrs = resp.json().get("data", {}).get("attributes", {})
        tr["hyperlinks"] = attrs.get("hyperlinks", [])
        tr["status"] = attrs.get("status", tr["status"])

    existing_uris = {link.get("uri", "") for link in tr["hyperlinks"]}
    if log_link in existing_uris:
        print(f"      Link already exists on {tr['short_id']}, skipping")
        return True

    updated_hyperlinks = list(tr["hyperlinks"]) + [{"role": "ref_src", "uri": log_link}]
    # Clean hyperlinks for PATCH
    cleaned = []
    for link in updated_hyperlinks:
        cleaned.append({"role": link["role"], "uri": link["uri"]})

    if dry_run:
        print(f"      [DRY RUN] Would add ref_src link to {tr['short_id']} ({tr['title']})")
        print(f"        + {log_link}")
        return True

    # Set status to rework if needed
    if tr["status"] != "rework":
        payload = {"data": {"type": "workitems", "id": tr["id"], "attributes": {"status": "rework"}}}
        resp = session.patch(f"{base_url}/projects/{project_id}/workitems/{tr['short_id']}", json=payload)
        if resp.status_code not in (200, 204):
            print(f"      Error setting rework status on {tr['short_id']}: {resp.status_code}")
            return False

    # Update hyperlinks
    payload = {"data": {"type": "workitems", "id": tr["id"], "attributes": {"hyperlinks": cleaned}}}
    resp = session.patch(f"{base_url}/projects/{project_id}/workitems/{tr['short_id']}", json=payload)
    if resp.status_code not in (200, 204):
        print(f"      Error updating hyperlinks on {tr['short_id']}: {resp.status_code}")
        return False

    # Set status back to in_review
    payload = {"data": {"type": "workitems", "id": tr["id"], "attributes": {"status": "in_review"}}}
    session.patch(f"{base_url}/projects/{project_id}/workitems/{tr['short_id']}", json=payload)

    print(f"      ✓ Added ref_src link to {tr['short_id']} ({tr['title']})")
    return True


def tc_to_func_name(tc_name):
    """Extract function name from TC: applyWriteProtect_HLTC_13 -> applyWriteProtect"""
    # Strip trailing annotations like "(also covered by this TP)"
    tc_name = re.sub(r"\s*\(.*\)\s*$", "", tc_name)
    match = re.match(r"(.+)_(HLTC|LLTC)_\d+$", tc_name)
    if not match:
        return None
    return match.group(1)


def update_polarion_for_log(session, base_url, project_id, log_link, test_cases, dry_run=True):
    """
    For each TC in a log file, find matching TRs and add the log link as ref_src.

    Groups test cases by function name to avoid redundant Polarion queries and
    ensures each TR is only updated once per log file.
    """
    # Group TCs by function name to avoid duplicate queries
    queried = {}       # func_name -> list of TRs returned by Polarion
    updated_trs = set()  # TR IDs already updated for this log (prevents double-updates)
    for tc in test_cases:
        func_name = tc_to_func_name(tc)
        if not func_name:
            print(f"    Skipping '{tc}' (could not parse TC name)")
            continue

        if func_name not in queried:
            trs = query_tr_by_function_name(session, base_url, project_id, func_name)
            queried[func_name] = trs
            if not trs:
                print(f"    No TRs found for {func_name}_TR_*")

        for tr in queried[func_name]:
            if tr["id"] in updated_trs:
                continue
            updated_trs.add(tr["id"])
            add_source_link_to_tr(session, base_url, project_id, tr, log_link, dry_run)


def main():
    parser = argparse.ArgumentParser(description="Extract GitLab links for recent log files and optionally update Polarion TPs")
    parser.add_argument("folder_path", help="Path to the folder to scan")
    parser.add_argument("--update-polarion", action="store_true", help="Add log file links as source references on Polarion TPs")
    parser.add_argument("--execute", action="store_true", help="Actually execute Polarion updates (default is dry run)")
    parser.add_argument("--since", default="1 day ago", help="Git log --since filter (default: '1 day ago')")
    args = parser.parse_args()

    folder = os.path.abspath(args.folder_path)
    git_root = get_git_root(folder)
    remote_url = get_remote_url(git_root)

    print(f"Remote: {remote_url}")
    print(f"Scanning: {folder}\n")

    polarion_session = None
    if args.update_polarion:
        polarion_session, base_url, project_id = create_polarion_session()
        dry_run = not args.execute
        if dry_run:
            print("[DRY RUN MODE] Use --execute to apply changes\n")
        else:
            print("[EXECUTE MODE] Changes will be applied to Polarion\n")

    # Walk the directory tree looking for recently-committed log files
    for root, _, files in os.walk(folder):
        # Only process directories that contain "BOOT_APP0" in the path
        if "BOOT_APP0" not in root:
            continue
        for fname in files:
            # Only consider files matching target_*.log
            if not fname.startswith("target_") or not fname.endswith(".log"):
                continue
            filepath = os.path.join(root, fname)
            if has_recent_commits(filepath, git_root, args.since):
                rel_path = os.path.relpath(filepath, git_root).replace("\\", "/")
                link = f"{remote_url}/-/blob/main/{rel_path}"
                test_cases = extract_test_cases(filepath)
                print(link)
                if test_cases:
                    for tc in test_cases:
                        print(f"  - {tc}")
                else:
                    print("  (no test cases found)")
                print()

                if polarion_session and test_cases:
                    print(f"  Updating Polarion TRs...")
                    update_polarion_for_log(polarion_session, base_url, project_id, link, test_cases, dry_run)
                    print()


if __name__ == "__main__":
    main()
