#!/usr/bin/env python3
"""
Polarion Test Procedure Work Item Manager

Scans a local git repo for tp_*.c files in SBL_BOOT_APP0* folders' HLTP/LLTP
directories, matches them against existing Polarion test procedure work items
by title pattern, updates existing items' hyperlinks, and creates new work items
for unmatched files.

Environment Variables Required:
- POLARION_API_BASE: Base URL for Polarion API
- POLARION_PAT: Personal Access Token for authentication
- POLARION_PROJECT_ID: Project ID in Polarion (can also be provided via --project-id)
- CCN_LOGIN: CodeCollaborator username (required when --ccr-id is provided)
- CCN_PASSWORD: CodeCollaborator password (required when --ccr-id is provided)

Usage:
    python polarionTestProcedureManager.py --repo-path /path/to/wassp --ccr-id 28264 [--dry-run|--execute]
"""

import os
import sys
import argparse
import glob
import re
import json
import subprocess
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class PolarionSourceLinkUpdater:
    """Minimal Polarion REST API client for work item operations."""

    def __init__(self, base_url: str, pat: str, project_id: str,
                 verify_ssl: bool = False, verbose: bool = False):
        self.base_url = base_url.rstrip('/')
        self.pat = pat
        self.project_id = project_id
        self.verify_ssl = verify_ssl
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {pat}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })
        self.session.verify = verify_ssl

    def query_work_items(self, query: str) -> List[str]:
        """Query Polarion for work items matching the query. Returns list of work item IDs."""
        print(f"Querying Polarion with: {query}")
        url = f"{self.base_url}/projects/{self.project_id}/workitems"
        params = {
            'query': query,
            'fields[workitems]': 'id,type,hyperlinks,title,status',
        }
        response = self.session.get(url, params=params, verify=self.verify_ssl)
        if response.status_code != 200:
            print(f"Error querying Polarion: {response.status_code}")
            print(f"Response: {response.text[:500]}")
            return []
        data = response.json()
        work_item_ids = []
        if isinstance(data, dict) and 'data' in data:
            items = data['data']
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and 'id' in item:
                        work_item_ids.append(item['id'])
        print(f"Found {len(work_item_ids)} work items matching query")
        return work_item_ids

    @staticmethod
    def _extract_short_id(work_item_id: str) -> str:
        """Extract short ID from full work item ID (e.g. 'project/ITEM-123' -> 'ITEM-123')."""
        if '/' in work_item_id:
            return work_item_id.split('/')[-1]
        return work_item_id

    def update_work_item_attributes(self, work_item_id: str, attributes: Dict[str, Any],
                                     dry_run: bool = True) -> bool:
        """Update any work item attributes via PATCH."""
        if dry_run:
            return True
        short_id = self._extract_short_id(work_item_id)
        url = f"{self.base_url}/projects/{self.project_id}/workitems/{short_id}"
        payload = {
            'data': {
                'type': 'workitems',
                'id': work_item_id,
                'attributes': attributes,
            }
        }
        response = self.session.patch(url, json=payload, verify=self.verify_ssl)
        if response.status_code in (200, 204):
            return True
        print(f"  ✗ Error updating work item: {response.status_code}")
        print(f"    Response: {response.text[:500]}")
        return False

    def update_work_item_status(self, work_item_id: str, new_status: str, dry_run: bool = True) -> bool:
        """Update work item status."""
        return self.update_work_item_attributes(work_item_id, {'status': new_status}, dry_run=dry_run)

    def update_work_item_hyperlinks(self, work_item_id: str, updated_hyperlinks: List[Dict],
                                    dry_run: bool = True) -> bool:
        """Update all hyperlinks for a work item."""
        cleaned = [{'role': l['role'], 'uri': l['uri']} for l in updated_hyperlinks]
        return self.update_work_item_attributes(work_item_id, {'hyperlinks': cleaned}, dry_run=dry_run)

RELATIVE_TEST_PATH = os.path.join(
    "helix", "guests", "vxworks-7", "pkgs_v2", "test",
    "shallowford-cert-tests", "SFORD_POS"
)

DEFAULT_GITLAB_BASE = (
    "https://ccn-gitlab.wrs.com/shallowford/project/wassp/-/blob/wassp-jenkins-nth"
)

CCR_URL_TEMPLATE = "https://ccn-codeocolab.wrs.com:8443/ui#reviewid={ccr_id}"


@dataclass
class TpFileInfo:
    """Represents a discovered tp_*.c file and its context."""
    tp_filename: str          # e.g. tp_SBL_BOOT_APP0_applyWriteProtect.c
    tl_filename: Optional[str]  # e.g. tl_SBL_BOOT_APP0.c (may be None if missing)
    boot_app_variant: str     # e.g. SBL_BOOT_APP0
    test_type: str            # HLTP or LLTP
    test_name: str            # e.g. applyWriteProtect
    dir_path: str             # absolute path to the HLTP/LLTP directory
    # GitLab-relative path from repo root (forward slashes)
    rel_dir: str              # e.g. helix/guests/.../native/SBL_BOOT_APP0/HLTP
    # Test case names extracted from tp file comments
    tc_names: List[str] = field(default_factory=list)

    # Optional override for the Polarion component name
    component_override: Optional[str] = None

    @property
    def component(self) -> str:
        """Derive Polarion component from variant (strip SBL_ prefix), or use override."""
        if self.component_override:
            return self.component_override
        if self.boot_app_variant.startswith("SBL_"):
            return self.boot_app_variant[4:]
        return self.boot_app_variant

    @property
    def group_key(self) -> str:
        """Key for grouping: testName + testType."""
        return f"{self.test_name}_{self.test_type}"

    @property
    def sort_key(self) -> Tuple[int, str]:
        """Sort key: base variant (SBL_BOOT_APP0) first, then alphabetical."""
        is_base = 0 if self.boot_app_variant == "SBL_BOOT_APP0" else 1
        return (is_base, self.boot_app_variant)


@dataclass
class MatchResult:
    """Result of matching tp files to existing Polarion work items."""
    # Matched: (existing_work_item_id, existing_title, tp_file)
    updates: List[Tuple[str, str, TpFileInfo]] = field(default_factory=list)
    # Unmatched tp files needing new work items, with their assigned number
    creates: List[Tuple[int, TpFileInfo]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 1: File Discovery
# ---------------------------------------------------------------------------

# Regex to match __TP_DESC_FLAGS__(Subtest_N, tcName, ...)
_TP_DESC_FLAGS_PATTERN = re.compile(
    r'__TP_DESC_FLAGS__\s*\(\s*\w+\s*,\s*(\w+)',
)


def parse_tc_names_from_file(tp_path: str, include_srvc: bool = False, verbose: bool = False) -> List[str]:
    """
    Read a tp .c file and extract test case names from __TP_DESC_FLAGS__ entries
    within testCases_Init arrays (and optionally testCases_Srvc arrays), e.g.:
    LOCAL TEST_CASE testCases_Init[] = {
        __TP_DESC_FLAGS__(Subtest_1, bootAppInit_HLTC_1, 2, 0)
    };
    The TC name is the second argument.
    """
    target_arrays = {'testCases_Init'}
    if include_srvc:
        target_arrays.add('testCases_Srvc')

    tc_names = []
    seen = set()
    in_target_array = False
    try:
        with open(tp_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                stripped = line.strip()
                # Detect start of a target array
                if any(arr in line for arr in target_arrays) and '=' in line:
                    in_target_array = True
                    continue
                # Detect start of any other TEST_CASE array (stop capturing)
                if in_target_array and re.search(r'LOCAL\s+TEST_CASE\s+\w+', line):
                    in_target_array = False
                    continue
                # Detect end of array
                if in_target_array and stripped.startswith('};'):
                    in_target_array = False
                    continue
                # Only parse __TP_DESC_FLAGS__ inside target arrays
                if in_target_array:
                    m = _TP_DESC_FLAGS_PATTERN.search(line)
                    if m:
                        tc_name = m.group(1)
                        if tc_name not in seen:
                            seen.add(tc_name)
                            tc_names.append(tc_name)
    except OSError as e:
        if verbose:
            print(f"  [VERBOSE] Could not read {tp_path}: {e}")
    return tc_names


def discover_tp_files(repo_path: str, include_srvc: bool = False, verbose: bool = False,
                      component_glob: str = "SBL_BOOT_APP0*",
                      component_override: Optional[str] = None) -> List[TpFileInfo]:
    """
    Scan the repo for tp_*.c files in <component_glob>/HLTP and <component_glob>/LLTP.
    """
    native_dir = os.path.join(repo_path, RELATIVE_TEST_PATH)
    if not os.path.isdir(native_dir):
        print(f"Error: native test directory not found: {native_dir}")
        sys.exit(1)

    results: List[TpFileInfo] = []

    # Find all matching component directories
    pattern = os.path.join(native_dir, component_glob)
    boot_app_dirs = sorted(glob.glob(pattern))

    if not boot_app_dirs:
        print(f"Warning: No directories matching '{component_glob}' found in {native_dir}")
        return results

    for boot_app_dir in boot_app_dirs:
        variant = os.path.basename(boot_app_dir)

        for test_type in ("HLTP", "LLTP"):
            type_dir = os.path.join(boot_app_dir, test_type)
            if not os.path.isdir(type_dir):
                if verbose:
                    print(f"  [VERBOSE] No {test_type} directory in {variant}")
                continue

            # Collect all directories to search: the type_dir itself + any subdirectories
            # LLTPs often have tp files inside subdirectories (e.g., LLTP/bootElfLib/tp_*.c)
            search_dirs = [type_dir]
            for entry in os.scandir(type_dir):
                if entry.is_dir():
                    search_dirs.append(entry.path)

            for search_dir in search_dirs:
                # Track if we're in a subdirectory (e.g., LLTP/bootElfLib/)
                subdir_name = None
                if search_dir != type_dir:
                    subdir_name = os.path.basename(search_dir)

                # Find the tl file
                # In subdirectories, tl may be named tl_{variant}_{subdir}.c
                tl_filename = None
                if subdir_name:
                    tl_pattern_sub = os.path.join(search_dir, f"tl_{variant}_{subdir_name}.c")
                    tl_matches = glob.glob(tl_pattern_sub)
                    if tl_matches:
                        tl_filename = os.path.basename(tl_matches[0])
                if not tl_filename:
                    tl_pattern = os.path.join(search_dir, f"tl_{variant}.c")
                    tl_matches = glob.glob(tl_pattern)
                    if tl_matches:
                        tl_filename = os.path.basename(tl_matches[0])
                if not tl_filename:
                    # Fallback: any tl_*.c file in this directory
                    tl_any = sorted(glob.glob(os.path.join(search_dir, "tl_*.c")))
                    if tl_any:
                        tl_filename = os.path.basename(tl_any[0])

                # Find all tp files
                tp_pattern = os.path.join(search_dir, f"tp_{variant}_*.c")
                tp_files_found = sorted(glob.glob(tp_pattern))

                # In subdirectories, tp files may also be named tp_{subdir}_*.c
                # e.g. LLTP/bootMmu/tp_bootMmu_bootAppMmuInit.c
                if subdir_name:
                    tp_pattern_sub = os.path.join(search_dir, f"tp_{subdir_name}_*.c")
                    tp_sub_found = sorted(glob.glob(tp_pattern_sub))
                    # Add only files not already matched by the variant pattern
                    existing_paths = set(tp_files_found)
                    for p in tp_sub_found:
                        if p not in existing_paths:
                            tp_files_found.append(p)
                    tp_files_found.sort()

                for tp_path in tp_files_found:
                    tp_basename = os.path.basename(tp_path)
                    # Extract test name: strip tp_{variant}_ or tp_{subdir}_ prefix and .c suffix
                    prefix = f"tp_{variant}_"
                    subdir_prefix = f"tp_{subdir_name}_" if subdir_name else None
                    if tp_basename.startswith(prefix) and tp_basename.endswith(".c"):
                        test_name = tp_basename[len(prefix):-2]
                    elif subdir_prefix and tp_basename.startswith(subdir_prefix) and tp_basename.endswith(".c"):
                        test_name = tp_basename[len(subdir_prefix):-2]
                    else:
                        if verbose:
                            print(f"  [VERBOSE] Skipping file with unexpected name: {tp_basename}")
                        continue

                    # For files in subdirectories (e.g., LLTP/bootElfLib/), strip the
                    # subdirectory name prefix from the test name:
                    # bootElfLib_bootElfModule -> bootElfModule
                    if subdir_name and test_name.startswith(f"{subdir_name}_"):
                        test_name = test_name[len(subdir_name) + 1:]

                    # Build relative dir path (forward slashes for GitLab URLs)
                    rel_dir = os.path.relpath(search_dir, repo_path).replace("\\", "/")

                    # Parse TC names from tp file comments
                    tc_names = parse_tc_names_from_file(tp_path, include_srvc, verbose)

                    info = TpFileInfo(
                        tp_filename=tp_basename,
                        tl_filename=tl_filename,
                        boot_app_variant=variant,
                        test_type=test_type,
                        test_name=test_name,
                        dir_path=search_dir,
                        rel_dir=rel_dir,
                        tc_names=tc_names,
                        component_override=component_override,
                    )
                    results.append(info)

                    if verbose:
                        print(f"  [VERBOSE] Found: {variant}/{test_type}/{os.path.relpath(tp_path, type_dir).replace(os.sep, '/')} -> test={test_name}")

    return results


# ---------------------------------------------------------------------------
# Phase 2: Polarion Query & Matching
# ---------------------------------------------------------------------------

def query_existing_work_items(
    updater: PolarionSourceLinkUpdater,
    group_key: str,
    verbose: bool = False,
) -> List[Tuple[str, str, int]]:
    """
    Query Polarion for existing test procedure work items matching a group key.

    Returns list of (work_item_id, title, number_suffix) sorted by number.
    """
    test_name, test_type = group_key.rsplit("_", 1)
    # Strip leading underscores — Polarion Lucene cannot handle them in title queries
    query_test_name = test_name.lstrip("_")
    # Lucene query: type + title wildcard (no quotes — wildcards are literal inside quotes)
    query = f'NOT HAS_VALUE:resolution AND NOT status:deleted AND type:wi_testProcedure AND title:{query_test_name}_{test_type}_*'

    if verbose:
        print(f"  [VERBOSE] Querying: {query}")

    work_item_ids = updater.query_work_items(query)
    if not work_item_ids:
        return []

    results = []
    for wi_id in work_item_ids:
        short_id = updater._extract_short_id(wi_id)
        url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{short_id}"
        params = {"fields[workitems]": "title,status,hyperlinks"}
        resp = updater.session.get(url, params=params, verify=updater.verify_ssl)
        if resp.status_code != 200:
            continue
        data = resp.json()
        title = data.get("data", {}).get("attributes", {}).get("title", "")

        # Extract the trailing number from e.g. applyWriteProtect_HLTP_1
        match = re.search(rf"^{re.escape(test_name)}_{re.escape(test_type)}_(\d+)$", title)
        if match:
            num = int(match.group(1))
            results.append((wi_id, title, num))
            print(f"      -> {short_id} - {title}")

    results.sort(key=lambda x: x[2])
    return results


def match_files_to_work_items(
    tp_files: List[TpFileInfo],
    updater: PolarionSourceLinkUpdater,
    verbose: bool = False,
) -> MatchResult:
    """
    Group tp files by (testName, testType), query Polarion for existing WIs,
    and produce update/create lists.
    """
    # Group by testName_testType
    groups: Dict[str, List[TpFileInfo]] = {}
    for f in tp_files:
        groups.setdefault(f.group_key, []).append(f)

    result = MatchResult()

    for group_key in sorted(groups.keys()):
        files = sorted(groups[group_key], key=lambda f: f.sort_key)
        existing = query_existing_work_items(updater, group_key, verbose)

        if verbose:
            print(f"  [VERBOSE] Group '{group_key}': {len(files)} file(s), {len(existing)} existing WI(s)")

        # Match 1:1 by position
        for i, tp_file in enumerate(files):
            if i < len(existing):
                wi_id, wi_title, _ = existing[i]
                result.updates.append((wi_id, wi_title, tp_file))
            else:
                # Determine next available number
                if existing:
                    max_num = max(e[2] for e in existing)
                else:
                    max_num = 0
                # Account for already-queued creates in this group
                already_queued = sum(
                    1 for _, c in result.creates if c.group_key == group_key
                )
                next_num = max_num + 1 + already_queued
                result.creates.append((next_num, tp_file))

    return result


# ---------------------------------------------------------------------------
# Phase 3 & 4: Update / Create Work Items
# ---------------------------------------------------------------------------

def build_gitlab_urls(
    tp_file: TpFileInfo, gitlab_base: str
) -> Tuple[Optional[str], str]:
    """Build the tl and tp GitLab URLs for a tp file."""
    tl_url = None
    if tp_file.tl_filename:
        tl_url = f"{gitlab_base}/{tp_file.rel_dir}/{tp_file.tl_filename}"
    tp_url = f"{gitlab_base}/{tp_file.rel_dir}/{tp_file.tp_filename}"
    return tl_url, tp_url


def build_ccr_url(ccr_id: str) -> str:
    return CCR_URL_TEMPLATE.format(ccr_id=ccr_id)


def _is_source_reference(link: dict) -> bool:
    """Check if a hyperlink has a source-reference role."""
    role = link.get("role", {})
    role_id = role.get("id", "") if isinstance(role, dict) else str(role)
    return "ref_src" in role_id.lower() or "source" in role_id.lower()


def update_existing_work_item(
    updater: PolarionSourceLinkUpdater,
    wi_id: str,
    wi_title: str,
    tp_file: TpFileInfo,
    gitlab_base: str,
    ccr_id: str,
    component: str,
    category: str,
    dry_run: bool = True,
    verbose: bool = False,
) -> bool:
    """Update an existing work item's hyperlinks, component, and category."""
    short_id = updater._extract_short_id(wi_id)
    print(f"\n  Updating: {short_id} - {wi_title}")
    print(f"    Matched to: {tp_file.boot_app_variant}/{tp_file.test_type}/{tp_file.tp_filename}")

    # Fetch current hyperlinks
    url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{short_id}"
    params = {"fields[workitems]": "hyperlinks,status,fld_component",
              "fields[categories]": "@all"}
    resp = updater.session.get(url, params=params, verify=updater.verify_ssl)
    if resp.status_code != 200:
        print(f"    Error fetching work item: {resp.status_code}")
        return False

    data = resp.json()
    attrs = data.get("data", {}).get("attributes", {})
    current_links = attrs.get("hyperlinks", [])
    current_status = attrs.get("status", "")
    current_component = attrs.get("fld_component", "")

    # Build new source reference links
    tl_url, tp_url = build_gitlab_urls(tp_file, gitlab_base)
    new_source_links = []
    if tl_url:
        new_source_links.append({"role": "ref_src", "uri": tl_url})
    new_source_links.append({"role": "ref_src", "uri": tp_url})

    # Build new CCR link
    if ccr_id:
        ccr_url = build_ccr_url(ccr_id)
        new_ccr_link = {"role": "ref_int", "uri": ccr_url}
    else:
        ccr_url = None
        new_ccr_link = None

    # Preserve non-source-reference links that are not .c file links, add new source refs + CCR
    preserved = [link for link in current_links
                 if not _is_source_reference(link) and not link.get("uri", "").endswith(".c")]
    updated_hyperlinks = preserved + new_source_links

    # Add CCR link only if not already present
    existing_uris = {link.get("uri", "") for link in preserved}
    if ccr_url and ccr_url not in existing_uris:
        updated_hyperlinks.append(new_ccr_link)

    # Report changes
    old_source = [link for link in current_links if _is_source_reference(link)]
    old_c_links = [link for link in current_links
                   if not _is_source_reference(link) and link.get("uri", "").endswith(".c")]
    if dry_run:
        if current_status != "rework":
            print(f"    [DRY RUN] Would change status from '{current_status}' to 'rework'")
        print(f"    [DRY RUN] Would remove {len(old_source)} old source reference link(s):")
        for link in old_source:
            print(f"      - {link.get('uri', '?')}")
        if old_c_links:
            print(f"    [DRY RUN] Would remove {len(old_c_links)} additional .c hyperlink(s):")
            for link in old_c_links:
                print(f"      - {link.get('uri', '?')}")
        print(f"    [DRY RUN] Would add {len(new_source_links)} new source reference link(s):")
        for link in new_source_links:
            print(f"      + {link['uri']}")
        if ccr_url and ccr_url not in existing_uris:
            print(f"    [DRY RUN] Would add CCR internal reference:")
            print(f"      + {ccr_url}")
        elif ccr_url:
            print(f"    [DRY RUN] CCR link already exists, skipping")
        # Component
        desired_component = f"comp_{component}"
        if current_component != desired_component:
            print(f"    [DRY RUN] Would update fld_component from '{current_component}' to '{desired_component}'")
        # Category
        print(f"    [DRY RUN] Would set fld_category to '{category}'")
        return True

    # Execute: status -> rework
    if current_status != "rework":
        print(f"    Changing status to 'rework'...")
        if not updater.update_work_item_status(wi_id, "rework", dry_run=False):
            print(f"    ✗ Failed to change status to 'rework', skipping")
            return False
        print(f"    ✓ Status changed to 'rework'")

    # Execute: update hyperlinks
    if updater.update_work_item_hyperlinks(wi_id, updated_hyperlinks, dry_run=False):
        print(f"    ✓ Hyperlinks updated")
    else:
        print(f"    ✗ Failed to update hyperlinks")
        return False

    # Execute: update fld_component
    desired_component = f"comp_{component}"
    if current_component != desired_component:
        if updater.update_work_item_attributes(wi_id, {"fld_component": desired_component}, dry_run=False):
            print(f"    ✓ Component updated to '{desired_component}'")
        else:
            print(f"    ✗ Failed to update component")

    # Execute: update fld_category via work item PATCH with relationships
    cat_url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{short_id}"
    cat_payload = {
        "data": {
            "type": "workitems",
            "id": wi_id,
            "relationships": {
                "fld_category": {
                    "data": {
                        "type": "categories",
                        "id": f"{updater.project_id}/cat_{category}",
                    }
                }
            },
        }
    }
    cat_resp = updater.session.patch(cat_url, json=cat_payload, verify=updater.verify_ssl)
    if cat_resp.status_code in (200, 204):
        print(f"    ✓ Category updated to '{category}'")
    else:
        print(f"    ✗ Failed to update category: {cat_resp.status_code}")
        print(f"      Response: {cat_resp.text[:500]}")

    # Note: status -> in_review is done after TC linking in the main flow
    return True


def create_new_work_item(
    updater: PolarionSourceLinkUpdater,
    number: int,
    tp_file: TpFileInfo,
    gitlab_base: str,
    ccr_id: str,
    component: str,
    category: str,
    author: Optional[str] = None,
    dry_run: bool = True,
    verbose: bool = False,
) -> Optional[str]:
    """Create a new test procedure work item. Returns the created WI ID, or None on failure."""
    title = f"{tp_file.test_name}_{tp_file.test_type}_{number}"

    # Build hyperlinks
    tl_url, tp_url = build_gitlab_urls(tp_file, gitlab_base)
    hyperlinks = []
    if tl_url:
        hyperlinks.append({"role": "ref_src", "uri": tl_url})
    hyperlinks.append({"role": "ref_src", "uri": tp_url})
    if ccr_id:
        ccr_url = build_ccr_url(ccr_id)
        hyperlinks.append({"role": "ref_int", "uri": ccr_url})

    print(f"\n  Creating: {title}")
    print(f"    Component: {component}")
    print(f"    Source: {tp_file.boot_app_variant}/{tp_file.test_type}/{tp_file.tp_filename}")
    print(f"    Hyperlinks:")
    for link in hyperlinks:
        role_label = "source reference" if link["role"] == "ref_src" else "internal reference"
        print(f"      [{role_label}] {link['uri']}")

    if dry_run:
        print(f"    [DRY RUN] Would create work item with above details")
        return "DRY_RUN"

    # POST to create the work item
    url = f"{updater.base_url}/projects/{updater.project_id}/workitems"
    payload = {
        "data": [
            {
                "type": "workitems",
                "attributes": {
                    "type": "wi_testProcedure",
                    "title": title,
                    "status": "rework",
                    "executionType": "Automated",
                    "fld_executionType": "automated",
                    "fld_component": f"comp_{component}",
                    "hyperlinks": hyperlinks,
                },
                "relationships": {
                    "fld_category": {
                        "data": {
                            "type": "categories",
                            "id": f"{updater.project_id}/cat_{category}",
                        }
                    },
                    **({
                        "author": {
                            "data": {
                                "type": "users",
                                "id": author,
                            }
                        }
                    } if author else {}),
                },
            }
        ]
    }

    if verbose:
        print(f"    [VERBOSE] POST {url}")
        print(f"    [VERBOSE] Payload: {json.dumps(payload, indent=2)}")

    resp = updater.session.post(url, json=payload, verify=updater.verify_ssl)
    if resp.status_code in (200, 201):
        resp_data = resp.json().get("data", [])
        if isinstance(resp_data, list) and resp_data:
            created_data = resp_data[0]
        else:
            created_data = resp_data if isinstance(resp_data, dict) else {}
        created_id = created_data.get("id", "?")
        short_id = updater._extract_short_id(created_id)
        print(f"    ✓ Created: {short_id}")
        # Note: status -> in_review is done after TC linking in the main flow
        return created_id
    else:
        print(f"    ✗ Error creating work item: {resp.status_code}")
        print(f"      Response: {resp.text[:500]}")
        return None


# ---------------------------------------------------------------------------
# Phase 5: Link Test Cases to Test Procedures
# ---------------------------------------------------------------------------

def find_tc_work_item(
    updater: PolarionSourceLinkUpdater,
    tc_name: str,
    tc_cache: Dict[str, Optional[str]],
    verbose: bool = False,
) -> Optional[str]:
    """
    Find a test case work item ID by title. Returns full WI ID or None.
    Uses tc_cache to avoid duplicate queries.
    """
    if tc_name in tc_cache:
        return tc_cache[tc_name]

    query = f'NOT HAS_VALUE:resolution AND NOT status:deleted AND type:wi_testCase AND title:{tc_name}'
    if verbose:
        print(f"      [VERBOSE] TC query: {query}")

    wi_ids = updater.query_work_items(query)
    if not wi_ids:
        tc_cache[tc_name] = None
        return None

    # Verify exact title match (Lucene may return partial matches)
    for wi_id in wi_ids:
        short_id = updater._extract_short_id(wi_id)
        url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{short_id}"
        params = {"fields[workitems]": "title"}
        resp = updater.session.get(url, params=params, verify=updater.verify_ssl)
        if resp.status_code == 200:
            title = resp.json().get("data", {}).get("attributes", {}).get("title", "")
            if title == tc_name:
                tc_cache[tc_name] = wi_id
                return wi_id

    if verbose:
        print(f"      [VERBOSE] No exact title match for TC '{tc_name}'")
    tc_cache[tc_name] = None
    return None


def _get_existing_tc_links(
    updater: PolarionSourceLinkUpdater,
    tp_wi_id: str,
    verbose: bool = False,
) -> List[Dict]:
    """Get all linked work items on a TP except TR links ('is derived by')."""
    short_id = updater._extract_short_id(tp_wi_id)
    url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{short_id}/linkedworkitems"
    params = {"fields[linkedworkitems]": "@all"}
    resp = updater.session.get(url, params=params, verify=updater.verify_ssl)
    if resp.status_code != 200:
        return []

    linked = resp.json().get("data", [])
    tc_links = []
    for item in linked:
        attrs = item.get("attributes", {})
        role = str(attrs.get("role", "")).lower()
        # Skip TR links (is derived by / derived)
        if "derived" in role:
            continue
        tc_links.append(item)
    return tc_links


def _delete_linked_work_item(
    updater: PolarionSourceLinkUpdater,
    tp_wi_id: str,
    link_item: Dict,
    verbose: bool = False,
) -> bool:
    """Delete an existing linked work item relationship."""
    short_id = updater._extract_short_id(tp_wi_id)
    full_link_id = link_item.get("id", "")
    link_id_parts = full_link_id.split("/")
    if len(link_id_parts) >= 4:
        link_suffix = "/".join(link_id_parts[2:])
    else:
        link_suffix = full_link_id

    url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{short_id}/linkedworkitems/{link_suffix}"
    if verbose:
        print(f"      [VERBOSE] DELETE {url}")

    resp = updater.session.delete(url, verify=updater.verify_ssl)
    return resp.status_code in (200, 204)


def _create_linked_work_item(
    updater: PolarionSourceLinkUpdater,
    tp_wi_id: str,
    tc_wi_id: str,
    verbose: bool = False,
) -> bool:
    """Create an 'implements' link FROM TP TO TC via POST."""
    tp_short = updater._extract_short_id(tp_wi_id)
    tc_short = updater._extract_short_id(tc_wi_id)

    url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{tp_short}/linkedworkitems"

    payload = {
        "data": [
            {
                "type": "linkedworkitems",
                "attributes": {
                    "role": "implements",
                    "suspect": False,
                },
                "relationships": {
                    "workItem": {
                        "data": {
                            "type": "workitems",
                            "id": f"{updater.project_id}/{tc_short}",
                        }
                    }
                },
            }
        ]
    }

    if verbose:
        print(f"      [VERBOSE] POST {url}")
        print(f"      [VERBOSE] Payload: {json.dumps(payload, indent=2)}")

    resp = updater.session.post(url, json=payload, verify=updater.verify_ssl)
    if resp.status_code not in (200, 201, 204):
        print(f"      [ERROR] POST {resp.status_code}: {resp.text[:300]}")
    return resp.status_code in (200, 201, 204)


def link_test_cases_to_tp(
    updater: PolarionSourceLinkUpdater,
    tp_wi_id: str,
    tp_file: TpFileInfo,
    tc_cache: Dict[str, Optional[str]],
    dry_run: bool = True,
    verbose: bool = False,
) -> int:
    """
    Link test cases found in the tp file to the given TP work item.
    Returns count of TCs successfully linked.
    """
    if not tp_file.tc_names:
        return 0

    tp_short = updater._extract_short_id(tp_wi_id)
    print(f"    TC linking for {tp_short}:")
    print(f"      Found {len(tp_file.tc_names)} TC reference(s) in file")

    linked_count = 0

    # Remove all existing TC links from TP first (preserves TR links)
    if not dry_run:
        existing = _get_existing_tc_links(updater, tp_wi_id, verbose)
        if existing and verbose:
            print(f"      Deleting {len(existing)} existing TC link(s) from TP {tp_short}:")
            for link in existing:
                link_id = link.get("id", "?")
                role = link.get("attributes", {}).get("role", "?")
                print(f"        - {link_id} (role: {role})")
        for link in existing:
            _delete_linked_work_item(updater, tp_wi_id, link, verbose)
    elif dry_run:
        print(f"      [DRY RUN] Would remove existing TC links from TP {tp_short} (preserving TR links)")

    for tc_name in tp_file.tc_names:
        tc_wi_id = find_tc_work_item(updater, tc_name, tc_cache, verbose)
        if not tc_wi_id:
            print(f"      ⚠ TC '{tc_name}' not found in Polarion, skipping")
            continue

        tc_short = updater._extract_short_id(tc_wi_id)

        if dry_run:
            print(f"      [DRY RUN] Would link TP {tp_short} → implements → TC {tc_short} ({tc_name})")
            linked_count += 1
            continue

        # Create new link: TP implements TC
        if _create_linked_work_item(updater, tp_wi_id, tc_wi_id, verbose):
            print(f"      ✓ Linked TP {tp_short} → implements → TC {tc_short} ({tc_name})")
            linked_count += 1
        else:
            print(f"      ✗ Failed to link TP {tp_short} → TC {tc_short} ({tc_name})")

    return linked_count


# ---------------------------------------------------------------------------
# CCR Branch Resolution & Git Checkout
# ---------------------------------------------------------------------------

CCN_API_BASE = "https://ccn-codecolab.wrs.com:8443/services/json/v1"


def resolve_ccr_branch(ccr_id: str, verbose: bool = False) -> str:
    """
    Query the CCN CodeCollaborator API to resolve a CCR review ID
    to its WASSP branch name.

    Requires CCN_LOGIN and CCN_PASSWORD environment variables.
    Returns the branch name string, or exits on failure.
    """
    ccn_login = os.environ.get("CCN_LOGIN")
    ccn_password = os.environ.get("CCN_PASSWORD")

    if not ccn_login:
        print("Error: CCN_LOGIN environment variable is not set (required when --ccr-id is provided)")
        sys.exit(1)
    if not ccn_password:
        print("Error: CCN_PASSWORD environment variable is not set (required when --ccr-id is provided)")
        sys.exit(1)

    session = requests.Session()

    # Get login ticket
    login_req = [
        {
            "command": "SessionService.getLoginTicket",
            "args": {
                "login": ccn_login,
                "password": ccn_password,
            },
        }
    ]
    resp = session.post(CCN_API_BASE, json=login_req, verify=False)
    data = resp.json()
    if "errors" in data[0]:
        print(f"Error: CCN login failed: {data[0]['errors']}")
        sys.exit(1)
    login_ticket = data[0]["result"]["loginTicket"]

    # Authenticate and find the review
    validate_req = [
        {
            "command": "SessionService.authenticate",
            "args": {"login": ccn_login, "ticket": login_ticket},
        },
        {
            "command": "ReviewService.findReviewById",
            "args": {"reviewId": int(ccr_id)},
        },
    ]
    resp2 = session.post(CCN_API_BASE, json=validate_req, verify=False)
    validate_data = resp2.json()
    if "errors" in validate_data[0]:
        print(f"Error: CCN authentication failed")
        sys.exit(1)
    if "errors" in validate_data[1]:
        print(f"Error: CCR #{ccr_id} not found: {validate_data[1]['errors']}")
        sys.exit(1)

    # Get review summary to extract branch name
    summary_req = [
        {
            "command": "SessionService.authenticate",
            "args": {"login": ccn_login, "ticket": login_ticket},
        },
        {
            "command": "ReviewService.getReviewSummary",
            "args": {"reviewId": int(ccr_id), "clientBuild": "14401"},
        },
    ]
    resp_sum = session.post(CCN_API_BASE, json=summary_req, verify=False)
    summary_data = resp_sum.json()
    if "errors" in summary_data[1]:
        print(f"Error: Failed to fetch review summary for CCR #{ccr_id}")
        sys.exit(1)

    summary = summary_data[1].get("result", {})
    pull_request_merges = summary.get("pullRequestMerges", [])
    merge_message = pull_request_merges[0].get("mergeMessage", "") if pull_request_merges else ""

    branch_name = None
    if merge_message:
        parts = merge_message.split("'")
        if len(parts) >= 2:
            branch_name = parts[1]

    if not branch_name:
        print(f"Error: Could not extract branch name from CCR #{ccr_id}")
        sys.exit(1)

    if verbose:
        print(f"  [VERBOSE] CCR #{ccr_id} -> branch '{branch_name}'")

    return branch_name


def checkout_wassp_branch(repo_path: str, branch_name: str, verbose: bool = False) -> None:
    """
    Fetch all remotes and checkout the given branch in the wassp repo.
    Exits on failure.
    """
    try:
        subprocess.check_output(
            ["git", "fetch", "--all", "--prune"],
            stderr=subprocess.PIPE, text=True, cwd=repo_path,
        )
        if verbose:
            print(f"  [VERBOSE] git fetch --all --prune succeeded")
    except subprocess.CalledProcessError as e:
        print(f"Warning: git fetch failed: {e}")

    try:
        subprocess.check_output(
            ["git", "checkout", branch_name],
            stderr=subprocess.PIPE, text=True, cwd=repo_path,
        )
        print(f"  Checked out branch '{branch_name}' in {repo_path}")
    except subprocess.CalledProcessError:
        # Branch may not exist locally yet — try tracking the remote
        try:
            subprocess.check_output(
                ["git", "checkout", "-b", branch_name, f"origin/{branch_name}"],
                stderr=subprocess.PIPE, text=True, cwd=repo_path,
            )
            print(f"  Checked out new tracking branch '{branch_name}' in {repo_path}")
        except subprocess.CalledProcessError as e:
            print(f"Error: Failed to checkout branch '{branch_name}': {e}")
            sys.exit(1)

    # Pull latest changes
    try:
        subprocess.check_output(
            ["git", "pull"],
            stderr=subprocess.PIPE, text=True, cwd=repo_path,
        )
        if verbose:
            print(f"  [VERBOSE] git pull succeeded on branch '{branch_name}'")
    except subprocess.CalledProcessError as e:
        print(f"Error: git pull failed on branch '{branch_name}': {e}")
        sys.exit(1)

    print(f"  Changed wassp branch to CCR branch: {branch_name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Manage Polarion test procedure work items for BOOT_APP tp/tl files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --repo-path /path/to/wassp --ccr-id 28264 --dry-run
  %(prog)s --repo-path /path/to/wassp --ccr-id 28264 --execute --limit 1
  %(prog)s --repo-path /path/to/wassp --ccr-id 28264 --project-id Shallowford_BL
        """,
    )

    parser.add_argument(
        "--repo-path",
        required=True,
        help="Path to the local git repo root (wassp)",
    )
    parser.add_argument(
        "--ccr-id",
        default=None,
        help="CCR review ID for internal reference hyperlinks (optional, skipped if not provided)",
    )
    parser.add_argument(
        "--gitlab-base",
        default=DEFAULT_GITLAB_BASE,
        help=f"GitLab base URL (default: {DEFAULT_GITLAB_BASE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Show what would be changed without making actual changes (default)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually execute the changes (overrides --dry-run)",
    )
    parser.add_argument(
        "--project-id",
        help="Polarion project ID (overrides POLARION_PROJECT_ID env var)",
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        default=False,
        help="Enable SSL certificate verification",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output for debugging",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of work items to process (updates + creates). 0 = no limit (default)",
    )
    parser.add_argument(
        "--skip-updates",
        action="store_true",
        help="Skip updating existing work items, only create new ones",
    )
    parser.add_argument(
        "--skip-creates",
        action="store_true",
        help="Skip creating new work items, only update existing ones",
    )
    parser.add_argument(
        "--include-srvc",
        action="store_true",
        help="Also parse TC names from testCases_Srvc arrays (by default only testCases_Init is parsed)",
    )
    parser.add_argument(
        "--component-glob",
        default="SBL_BOOT_APP0*",
        help="Glob pattern for component directories under native/ (default: SBL_BOOT_APP0*)",
    )
    parser.add_argument(
        "--component",
        required=True,
        help="Polarion component name (e.g. SSD_NVME0). Will be prefixed with comp_ automatically.",
    )
    parser.add_argument(
        "--category",
        default="BSP_POS",
        help="Polarion category ID (default: BSP_POS). Will be prefixed with cat_ automatically.",
    )
    parser.add_argument(
        "--author",
        default=None,
        help="Polarion user ID to set as author on newly created work items (optional)",
    )

    args = parser.parse_args()

    # Validate repo path
    repo_path = os.path.abspath(args.repo_path)
    if not os.path.isdir(repo_path):
        print(f"Error: repo path does not exist: {repo_path}")
        sys.exit(1)

    # Environment variables
    base_url = os.environ.get("POLARION_API_BASE")
    pat = os.environ.get("POLARION_PAT")
    project_id = args.project_id or os.environ.get("POLARION_PROJECT_ID")

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

    dry_run = not args.execute

    print("=" * 60)
    if dry_run:
        print("DRY RUN MODE - No changes will be made")
    else:
        print("EXECUTE MODE - Changes will be applied!")
    print("=" * 60)

    # Phase 0: Resolve CCR branch and checkout
    if args.ccr_id:
        print(f"\nPhase 0: Resolving CCR #{args.ccr_id} branch and checking out...")
        branch_name = resolve_ccr_branch(args.ccr_id, verbose=args.verbose)
        checkout_wassp_branch(repo_path, branch_name, verbose=args.verbose)

    # Phase 1: discover files
    print(f"\nPhase 1: Discovering tp files in {repo_path}")
    tp_files = discover_tp_files(repo_path, include_srvc=args.include_srvc, verbose=args.verbose,
                                 component_glob=args.component_glob,
                                 component_override=args.component)
    if not tp_files:
        print("No tp files found. Nothing to do.")
        return

    # Print discovery summary
    by_variant: Dict[str, List[TpFileInfo]] = {}
    for f in tp_files:
        by_variant.setdefault(f"{f.boot_app_variant}/{f.test_type}", []).append(f)

    print(f"\nDiscovered {len(tp_files)} tp file(s) across {len(by_variant)} folder(s):")
    for key in sorted(by_variant.keys()):
        files = by_variant[key]
        print(f"  {key}: {len(files)} file(s)")
        for f in files:
            tc_count = len(f.tc_names)
            tc_info = f" [{tc_count} TC(s)]" if tc_count else ""
            print(f"    - {f.tp_filename} (test: {f.test_name}){tc_info}")

    # Phase 2: match against Polarion
    print(f"\nPhase 2: Querying Polarion for existing work items...")
    updater = PolarionSourceLinkUpdater(
        base_url, pat, project_id,
        verify_ssl=args.verify_ssl,
        verbose=args.verbose,
    )
    match_result = match_files_to_work_items(tp_files, updater, verbose=args.verbose)

    print(f"\nMatching results:")
    print(f"  Work items to update: {len(match_result.updates)}")
    print(f"  Work items to create: {len(match_result.creates)}")

    # --- Change log (grouped by function) ---
    entries_by_group: Dict[str, List[str]] = {}
    for wi_id, wi_title, tp_file in match_result.updates:
        short_id = updater._extract_short_id(wi_id)
        tl_url, tp_url = build_gitlab_urls(tp_file, args.gitlab_base)
        lines = [f"  [WOULD UPDATE] {short_id} - {wi_title}"]
        for tc_name in tp_file.tc_names:
            lines.append(f"      [implements] {tc_name}")
        lines.append("")
        if tl_url:
            lines.append(f"      [source reference] {tl_url}")
        lines.append(f"      [source reference] {tp_url}")
        entries_by_group.setdefault(tp_file.group_key, []).append("\n".join(lines))

    for number, tp_file in match_result.creates:
        title = f"{tp_file.test_name}_{tp_file.test_type}_{number}"
        tl_url, tp_url = build_gitlab_urls(tp_file, args.gitlab_base)
        lines = [f"  [WOULD CREATE] {title}"]
        for tc_name in tp_file.tc_names:
            lines.append(f"      [implements] {tc_name}")
        lines.append("")
        if tl_url:
            lines.append(f"      [source reference] {tl_url}")
        lines.append(f"      [source reference] {tp_url}")
        entries_by_group.setdefault(tp_file.group_key, []).append("\n".join(lines))

    if entries_by_group:
        print(f"\nChange log:")
        first_group = True
        for group_key in sorted(entries_by_group.keys()):
            if not first_group:
                print("  " + "-" * 40)
            first_group = False
            group_entries = entries_by_group[group_key]
            for i, entry in enumerate(group_entries):
                print(entry)
                if i < len(group_entries) - 1:
                    print()

    # Phase 3: update existing work items
    updated_ok = 0
    updated_fail = 0
    items_processed = 0
    tc_linked_total = 0
    tc_cache: Dict[str, Optional[str]] = {}
    limit = args.limit
    if match_result.updates and not args.skip_updates:
        print(f"\nPhase 3: Updating existing work items...")
        for wi_id, wi_title, tp_file in match_result.updates:
            if limit and items_processed >= limit:
                print(f"  Limit of {limit} reached, stopping.")
                break
            if update_existing_work_item(
                updater, wi_id, wi_title, tp_file,
                args.gitlab_base, args.ccr_id,
                component=args.component, category=args.category,
                dry_run=dry_run, verbose=args.verbose,
            ):
                updated_ok += 1
                # Link TCs to this TP (TP is still in rework)
                tc_linked_total += link_test_cases_to_tp(
                    updater, wi_id, tp_file, tc_cache,
                    dry_run=dry_run, verbose=args.verbose,
                )
                # Now set TP to in_review
                if not dry_run:
                    print(f"    Changing TP status to 'in_review'...")
                    if updater.update_work_item_status(wi_id, "in_review", dry_run=False):
                        print(f"    ✓ TP status changed to 'in_review'")
                    else:
                        print(f"    ⚠ Failed to change TP status to 'in_review'")
                else:
                    print(f"    [DRY RUN] Would change TP status to 'in_review'")
            else:
                updated_fail += 1
            items_processed += 1

    # Phase 4: create new work items
    created_ok = 0
    created_fail = 0
    if match_result.creates and not args.skip_creates:
        print(f"\nPhase 4: Creating new work items...")
        for number, tp_file in match_result.creates:
            if limit and items_processed >= limit:
                print(f"  Limit of {limit} reached, stopping.")
                break
            created_id = create_new_work_item(
                updater, number, tp_file,
                args.gitlab_base, args.ccr_id,
                component=args.component, category=args.category,
                author=args.author,
                dry_run=dry_run, verbose=args.verbose,
            )
            if created_id:
                created_ok += 1
                # Link TCs to this TP (TP is still in rework)
                tc_linked_total += link_test_cases_to_tp(
                    updater, created_id, tp_file, tc_cache,
                    dry_run=dry_run, verbose=args.verbose,
                )
                # Now set TP to in_review
                if not dry_run and created_id != "DRY_RUN":
                    print(f"    Changing TP status to 'in_review'...")
                    if updater.update_work_item_status(created_id, "in_review", dry_run=False):
                        print(f"    ✓ TP status changed to 'in_review'")
                    else:
                        print(f"    ⚠ Failed to change TP status to 'in_review'")
                elif dry_run:
                    print(f"    [DRY RUN] Would change TP status to 'in_review'")
            else:
                created_fail += 1
            items_processed += 1

    # Summary
    print(f"\n{'=' * 60}")
    print("Summary:")
    print(f"  Files discovered: {len(tp_files)}")
    print(f"  Existing WIs matched: {len(match_result.updates)}")
    if dry_run:
        print(f"    Would update: {updated_ok}")
        print(f"  New WIs to create: {len(match_result.creates)}")
        print(f"    Would create: {created_ok}")
        print(f"  TC links: {tc_linked_total}")
        print(f"\nThis was a DRY RUN. Use --execute to apply changes.")
    else:
        print(f"    Updated OK: {updated_ok}, Failed: {updated_fail}")
        print(f"  New WIs created: {created_ok}, Failed: {created_fail}")
        print(f"  TC links created: {tc_linked_total}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
