#!/usr/bin/env python3
"""
Polarion Test Procedure Work Item Updater

Scans a git repo for tp_*.c files in component folders' HLTP/LLTP directories,
matches them against existing Polarion test procedure work items by title pattern,
updates existing items' hyperlinks, and creates new work items for unmatched files.

Supports two modes:
  - Local mode (--repo-path): checks out the CCR branch locally and scans the filesystem.
  - Remote mode (no --repo-path): browses the branch via GitLab API (requires GITLAB_TOKEN). Branch is resolved from --ccr-id or provided directly with --branch.

Environment Variables Required:
- POLARION_API_BASE: Base URL for Polarion API
- POLARION_PAT: Personal Access Token for authentication
- POLARION_PROJECT_ID: Project ID in Polarion (can also be provided via --project-id)
- CCN_LOGIN: CodeCollaborator username
- CCN_PASSWORD: CodeCollaborator password
- GITLAB_TOKEN: GitLab personal access token (required for remote mode)

Usage:
    python PolarionTPUpdater.py --gitlab-base https://ccn-gitlab.wrs.com/shallowford/project/wassp/-/blob/wassp-jenkins --ccr-id 31421 --component-glob "POSBSP_SSD_NVME0_BATCH4*" --component SSD_NVME0
    python PolarionTPUpdater.py --gitlab-base https://ccn-gitlab.wrs.com/shallowford/project/wassp/-/blob/wassp-jenkins-nth --ccr-id 28264 --component-glob "SBL_BOOT_APP0*" --component BOOT_APP0 --execute
    python PolarionTPUpdater.py --gitlab-base https://ccn-gitlab.wrs.com/shallowford/project/wassp/-/blob/wassp-jenkins --branch my-feature-branch --component SSD_NVME0 --component-glob "POSBSP_SSD_NVME0*"
"""

import os
import sys
import argparse
import fnmatch
import glob
import re
import json
import subprocess
import io
import contextlib
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any
from urllib.parse import quote as url_quote, urlparse

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
        if self.verbose:
            print(f"  [VERBOSE] Querying Polarion with: {query}")
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
        if self.verbose:
            print(f"  [VERBOSE] Found {len(work_item_ids)} work items matching query")
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Path to the test directory relative to the repo root.
# OS-native separators (used for local filesystem operations).
RELATIVE_TEST_BASE = os.path.join(
    "helix", "guests", "vxworks-7", "pkgs_v2", "test",
    "shallowford-cert-tests"
)
# Same path with forward slashes (used for GitLab API calls and URLs).
RELATIVE_TEST_BASE_POSIX = "helix/guests/vxworks-7/pkgs_v2/test/shallowford-cert-tests"

DEFAULT_GITLAB_BASE = (
    "https://ccn-gitlab.wrs.com/shallowford/project/wassp/-/blob/wassp-jenkins"
)

CCR_URL_TEMPLATE = "https://ccn-codecolab.wrs.com:8443/ui#review:id={ccr_id}"


class GitLabRepoClient:
    """GitLab API client for browsing repository contents remotely."""

    def __init__(self, gitlab_base_url: str, token: str, verify_ssl: bool = False):
        # Parse host and project from a gitlab blob URL like:
        # https://ccn-gitlab.wrs.com/shallowford/project/wassp/-/blob/branch
        self.verify_ssl = verify_ssl
        parts = gitlab_base_url.split("/-/")
        if len(parts) < 2:
            print(f"Error: Cannot parse GitLab URL: {gitlab_base_url}")
            sys.exit(1)
        repo_url = parts[0]  # https://ccn-gitlab.wrs.com/shallowford/project/wassp
        parsed = urlparse(repo_url)
        self.host = f"{parsed.scheme}://{parsed.netloc}"
        self.project_path = parsed.path.strip("/")
        self.project_id = url_quote(self.project_path, safe="")
        self.api_base = f"{self.host}/api/v4"
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})
        self.session.verify = verify_ssl

    def list_tree(self, path: str, ref: str) -> List[Dict]:
        """List directory contents at path on the given ref/branch."""
        url = f"{self.api_base}/projects/{self.project_id}/repository/tree"
        params = {"path": path, "ref": ref, "per_page": 100}
        all_items: List[Dict] = []
        page = 1
        while True:
            params["page"] = page
            resp = self.session.get(url, params=params, verify=self.verify_ssl)
            if resp.status_code != 200:
                if page == 1:
                    # Directory doesn't exist or error
                    return []
                break
            items = resp.json()
            if not items:
                break
            all_items.extend(items)
            page += 1
        return all_items

    def get_file_content(self, file_path: str, ref: str) -> Optional[str]:
        """Get raw file content from the repo."""
        encoded_path = url_quote(file_path, safe="")
        url = f"{self.api_base}/projects/{self.project_id}/repository/files/{encoded_path}/raw"
        params = {"ref": ref}
        resp = self.session.get(url, params=params, verify=self.verify_ssl)
        if resp.status_code == 200:
            return resp.text
        return None


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class TpFileInfo:
    """Represents a discovered tp_*.c file and its context."""
    tp_filename: str          # e.g. tp_POSBSP_SSD_NVME0_nvmeInit.c
    tl_filename: Optional[str]  # e.g. tl_POSBSP_SSD_NVME0.c (may be None if missing)
    variant: str              # e.g. POSBSP_SSD_NVME0 or SBL_BOOT_APP0
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
    def group_key(self) -> str:
        """Key for grouping: testName + testType."""
        return f"{self.test_name}_{self.test_type}"

    @property
    def sort_key(self) -> str:
        """Sort key: alphabetical by variant."""
        return self.variant


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

# Regex to extract the TC name (second argument) from __TP_DESC_FLAGS__(Subtest_N, tcName, ...)
_TP_DESC_FLAGS_PATTERN = re.compile(
    r'__TP_DESC_FLAGS__\s*\(\s*\w+\s*,\s*(\w+)',
)

# Mapping from gate macros to the TEST_CASE arrays they enable
_MACRO_TO_ARRAY = {
    'INIT_MODE_NEEDED': 'testCases_Init',
    'SRVC_MODE_NEEDED': 'testCases_Srvc',
    'PRE_STAGE_NEEDED': 'testCasesPre',
    'POST_STAGE_NEEDED': 'testCasesPost',
}


def _strip_comments(line: str, in_block: bool) -> Tuple[str, bool]:
    """Strip C-style comments from a line, tracking block comment state.

    Returns (effective_text, still_in_block_comment).
    """
    effective = ''
    i = 0
    while i < len(line):
        if in_block:
            end = line.find('*/', i)
            if end == -1:
                break
            in_block = False
            i = end + 2
        elif line[i:i+2] == '/*':
            end = line.find('*/', i + 2)
            if end == -1:
                in_block = True
                break
            i = end + 2
        elif line[i:i+2] == '//':
            break
        else:
            effective += line[i]
            i += 1
    return effective, in_block


def _detect_active_arrays(content: str) -> set:
    """Detect which TEST_CASE arrays are active based on uncommented #define macros.

    Scans for #define lines containing gate macros (e.g. __INIT_MODE_NEEDED__),
    ignoring lines that are inside // or /* */ comments.
    """
    active = set()
    in_block = False
    for line in content.splitlines():
        effective, in_block = _strip_comments(line, in_block)
        effective = effective.strip()
        if effective.startswith('#') and 'define' in effective:
            for macro, array in _MACRO_TO_ARRAY.items():
                if macro in effective:
                    active.add(array)
    return active


def parse_tc_names_from_content(content: str) -> List[str]:
    """
    Extract test case names from __TP_DESC_FLAGS__ entries within active
    TEST_CASE arrays in a tp .c file's content.

    Determines which arrays are active by scanning for uncommented #define
    gate macros:
      - __INIT_MODE_NEEDED__  -> testCases_Init
      - __SRVC_MODE_NEEDED__  -> testCases_Srvc
      - __PRE_STAGE_NEEDED__  -> testCasesPre
      - __POST_STAGE_NEEDED__ -> testCasesPost

    Commented-out defines (// or /* */) are ignored.
    """
    target_arrays = _detect_active_arrays(content)

    tc_names = []
    seen = set()
    in_target_array = False
    for line in content.splitlines():
        stripped = line.strip()
        if any(arr in line for arr in target_arrays) and '=' in line:
            in_target_array = True
            continue
        if in_target_array and re.search(r'LOCAL\s+TEST_CASE\s+\w+', line):
            in_target_array = False
            continue
        if in_target_array and stripped.startswith('};'):
            in_target_array = False
            continue
        if in_target_array:
            m = _TP_DESC_FLAGS_PATTERN.search(line)
            if m:
                tc_name = m.group(1)
                if tc_name not in seen:
                    seen.add(tc_name)
                    tc_names.append(tc_name)
    return tc_names


def parse_tc_names_from_file(tp_path: str, verbose: bool = False) -> List[str]:
    """
    Read a tp .c file from disk and extract test case names from __TP_DESC_FLAGS__
    entries within active TEST_CASE arrays. See ``parse_tc_names_from_content`` for
    the array-detection rules (Init / Srvc / Pre / Post gate macros).
    """
    try:
        with open(tp_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except OSError as e:
        if verbose:
            print(f"  [VERBOSE] Could not read {tp_path}: {e}")
        return []
    return parse_tc_names_from_content(content)


def _extract_test_name(
    tp_basename: str, variant: str, subdir_name: Optional[str]
) -> Optional[str]:
    """
    Extract the test name from a tp_*.c filename.

    Handles two naming conventions:
      - tp_{variant}_{testName}.c  (standard)
      - tp_{subdir}_{testName}.c   (inside LLTP subdirectories)

    Returns the test name string, or None if the filename is unrecognized.
    """
    # Try standard prefix, then ei_ variant, then subdir prefix
    prefix = f"tp_{variant}_"
    ei_prefix = f"tp_ei_{variant}_"
    subdir_prefix = f"tp_{subdir_name}_" if subdir_name else None
    if tp_basename.startswith(prefix) and tp_basename.endswith(".c"):
        test_name = tp_basename[len(prefix):-2]
    elif tp_basename.startswith(ei_prefix) and tp_basename.endswith(".c"):
        test_name = tp_basename[len(ei_prefix):-2]
    elif subdir_prefix and tp_basename.startswith(subdir_prefix) and tp_basename.endswith(".c"):
        test_name = tp_basename[len(subdir_prefix):-2]
    else:
        return None
    # Strip subdirectory name prefix (e.g. bootElfLib_bootElfModule -> bootElfModule)
    if subdir_name and test_name.startswith(f"{subdir_name}_"):
        test_name = test_name[len(subdir_name) + 1:]
    return test_name


def discover_tp_files(repo_path: str, verbose: bool = False,
                      component_glob: str = "",
                      component_override: Optional[str] = None) -> List[TpFileInfo]:
    """
    Scan the local repo for tp_*.c files in <component_glob>/HLTP and <component_glob>/LLTP.
    """
    # If component_glob starts with POSBSP, look under SFORD_POS; otherwise under native
    subdir = "SFORD_POS" if component_glob.startswith("POSBSP") else "native"
    native_dir = os.path.join(repo_path, RELATIVE_TEST_BASE, subdir)
    if not os.path.isdir(native_dir):
        print(f"Error: test directory not found: {native_dir}")
        sys.exit(1)

    results: List[TpFileInfo] = []

    # Find all matching component directories
    pattern = os.path.join(native_dir, component_glob)
    component_dirs = sorted(glob.glob(pattern))

    if not component_dirs:
        print(f"Warning: No directories matching '{component_glob}' found in {native_dir}")
        return results

    for component_dir in component_dirs:
        variant = os.path.basename(component_dir)

        for test_type in ("HLTP", "LLTP"):
            type_dir = os.path.join(component_dir, test_type)
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
                    if not tl_matches:
                        tl_pattern_sub = os.path.join(search_dir, f"tl_ei_{variant}_{subdir_name}.c")
                        tl_matches = glob.glob(tl_pattern_sub)
                    if tl_matches:
                        tl_filename = os.path.basename(tl_matches[0])
                if not tl_filename:
                    tl_pattern = os.path.join(search_dir, f"tl_{variant}.c")
                    tl_matches = glob.glob(tl_pattern)
                    if not tl_matches:
                        tl_pattern = os.path.join(search_dir, f"tl_ei_{variant}.c")
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

                # Also match tp_ei_{variant}_*.c (alternate naming convention)
                tp_ei_pattern = os.path.join(search_dir, f"tp_ei_{variant}_*.c")
                for p in sorted(glob.glob(tp_ei_pattern)):
                    if p not in set(tp_files_found):
                        tp_files_found.append(p)
                tp_files_found.sort()

                # In subdirectories, tp files may also be named tp_{subdir}_*.c
                # e.g. LLTP/bootMmu/tp_bootMmu_bootAppMmuInit.c
                if subdir_name:
                    existing_paths = set(tp_files_found)
                    for sub_pref in [f"tp_{subdir_name}_*.c", f"tp_ei_{subdir_name}_*.c"]:
                        tp_pattern_sub = os.path.join(search_dir, sub_pref)
                        for p in sorted(glob.glob(tp_pattern_sub)):
                            if p not in existing_paths:
                                tp_files_found.append(p)
                                existing_paths.add(p)
                    tp_files_found.sort()

                for tp_path in tp_files_found:
                    tp_basename = os.path.basename(tp_path)
                    test_name = _extract_test_name(tp_basename, variant, subdir_name)
                    if test_name is None:
                        if verbose:
                            print(f"  [VERBOSE] Skipping file with unexpected name: {tp_basename}")
                        continue

                    # Build relative dir path (forward slashes for GitLab URLs)
                    rel_dir = os.path.relpath(search_dir, repo_path).replace("\\", "/")

                    # Parse TC names from tp file comments
                    tc_names = parse_tc_names_from_file(tp_path, verbose)

                    info = TpFileInfo(
                        tp_filename=tp_basename,
                        tl_filename=tl_filename,
                        variant=variant,
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


def discover_tp_files_remote(
    gitlab_client: GitLabRepoClient,
    ref: str,
    verbose: bool = False,
    component_glob: str = "",
    component_override: Optional[str] = None,
) -> List[TpFileInfo]:
    """
    Discover tp_*.c files by browsing the GitLab repository via API.
    Same logic as discover_tp_files() but uses GitLab API instead of local filesystem.
    """
    subdir = "SFORD_POS" if component_glob.startswith("POSBSP") else "native"
    base_path = f"{RELATIVE_TEST_BASE_POSIX}/{subdir}"

    # List component directories matching the glob
    entries = gitlab_client.list_tree(base_path, ref)
    dirs = [e for e in entries if e.get("type") == "tree"]
    matching_dirs = [d for d in dirs if fnmatch.fnmatch(d["name"], component_glob)]

    if not matching_dirs:
        print(f"Warning: No directories matching '{component_glob}' found in {base_path} on ref '{ref}'")
        return []

    results: List[TpFileInfo] = []

    for variant_entry in sorted(matching_dirs, key=lambda d: d["name"]):
        variant = variant_entry["name"]

        for test_type in ("HLTP", "LLTP"):
            type_path = f"{base_path}/{variant}/{test_type}"
            type_entries = gitlab_client.list_tree(type_path, ref)
            if not type_entries:
                if verbose:
                    print(f"  [VERBOSE] No {test_type} directory in {variant}")
                continue

            # Collect search dirs: the type_path itself + any subdirectories
            sub_dirs_to_search = [(type_path, None)]  # (path, subdir_name)
            for entry in type_entries:
                if entry.get("type") == "tree":
                    sub_dirs_to_search.append((f"{type_path}/{entry['name']}", entry["name"]))

            for search_path, subdir_name in sub_dirs_to_search:
                if subdir_name:
                    dir_entries = gitlab_client.list_tree(search_path, ref)
                else:
                    dir_entries = type_entries

                file_names = sorted(
                    e["name"] for e in dir_entries if e.get("type") == "blob"
                )

                # Find tl file
                tl_filename = None
                if subdir_name:
                    tl_cand = f"tl_{variant}_{subdir_name}.c"
                    if tl_cand not in file_names:
                        tl_cand = f"tl_ei_{variant}_{subdir_name}.c"
                    if tl_cand in file_names:
                        tl_filename = tl_cand
                if not tl_filename:
                    tl_cand = f"tl_{variant}.c"
                    if tl_cand not in file_names:
                        tl_cand = f"tl_ei_{variant}.c"
                    if tl_cand in file_names:
                        tl_filename = tl_cand
                if not tl_filename:
                    tl_any = [f for f in file_names if f.startswith("tl_") and f.endswith(".c")]
                    if tl_any:
                        tl_filename = tl_any[0]

                # Find tp files matching variant pattern (including tp_ei_ variant)
                tp_prefix = f"tp_{variant}_"
                tp_ei_prefix = f"tp_ei_{variant}_"
                tp_files_found = sorted(
                    f for f in file_names
                    if (f.startswith(tp_prefix) or f.startswith(tp_ei_prefix)) and f.endswith(".c")
                )

                # In subdirectories, also match tp_{subdir}_*.c
                if subdir_name:
                    existing_set = set(tp_files_found)
                    for sub_pref in [f"tp_{subdir_name}_", f"tp_ei_{subdir_name}_"]:
                        for f in sorted(file_names):
                            if f.startswith(sub_pref) and f.endswith(".c") and f not in existing_set:
                                tp_files_found.append(f)
                                existing_set.add(f)
                    tp_files_found.sort()

                for tp_basename in tp_files_found:
                    test_name = _extract_test_name(tp_basename, variant, subdir_name)
                    if test_name is None:
                        if verbose:
                            print(f"  [VERBOSE] Skipping file with unexpected name: {tp_basename}")
                        continue

                    rel_dir = search_path  # already forward-slash POSIX path

                    # Fetch file content from GitLab and parse TC names
                    tp_file_path = f"{search_path}/{tp_basename}"
                    content = gitlab_client.get_file_content(tp_file_path, ref)
                    tc_names = parse_tc_names_from_content(content) if content else []

                    info = TpFileInfo(
                        tp_filename=tp_basename,
                        tl_filename=tl_filename,
                        variant=variant,
                        test_type=test_type,
                        test_name=test_name,
                        dir_path=search_path,
                        rel_dir=rel_dir,
                        tc_names=tc_names,
                        component_override=component_override,
                    )
                    results.append(info)

                    if verbose:
                        display = tp_basename if not subdir_name else f"{subdir_name}/{tp_basename}"
                        print(f"  [VERBOSE] Found: {variant}/{test_type}/{display} -> test={test_name}")

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
            if verbose:
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

        if len(files) > 1 and len(existing) > 1:
            # Smart matching: use TC overlap from Polarion to pair WIs with files
            test_name = group_key.rsplit("_", 1)[0]
            prefix = f"{test_name}_"

            print(f"  Smart matching '{group_key}' ({len(files)} files, {len(existing)} WIs) — resolving TC links...")

            # Fetch same-function TC titles for each existing WI
            wi_tc_map: Dict[str, set] = {}
            for wi_id, wi_title, num in existing:
                all_titles = _fetch_linked_tc_titles(updater, wi_id)
                same_func = {t for t in all_titles if t.startswith(prefix)}
                wi_tc_map[wi_id] = same_func
                if verbose:
                    short = updater._extract_short_id(wi_id)
                    print(f"    [VERBOSE] {short} ({wi_title}): {len(same_func)} same-function TC(s)")

            # Build overlap scores for all (WI, file) pairs
            overlaps = []
            for wi_idx, (wi_id, wi_title, num) in enumerate(existing):
                for file_idx, tp_file in enumerate(files):
                    file_tc_set = set(tp_file.tc_names)
                    overlap = len(wi_tc_map[wi_id] & file_tc_set)
                    if overlap > 0:
                        overlaps.append((overlap, wi_idx, file_idx))

            # Greedy match: highest overlap first
            overlaps.sort(key=lambda x: x[0], reverse=True)
            matched_files: set = set()
            matched_wis: set = set()
            pairs: List[Tuple[int, int]] = []

            for overlap, wi_idx, file_idx in overlaps:
                if wi_idx not in matched_wis and file_idx not in matched_files:
                    pairs.append((wi_idx, file_idx))
                    matched_wis.add(wi_idx)
                    matched_files.add(file_idx)
                    if verbose:
                        short = updater._extract_short_id(existing[wi_idx][0])
                        print(f"    [VERBOSE] Matched {short} ({existing[wi_idx][1]}) ↔ {files[file_idx].variant} (overlap: {overlap})")

            # Positional fallback for unmatched WIs/files
            remaining_wis = [i for i in range(len(existing)) if i not in matched_wis]
            remaining_files = [i for i in range(len(files)) if i not in matched_files]
            for wi_idx, file_idx in zip(remaining_wis, remaining_files):
                pairs.append((wi_idx, file_idx))
                matched_files.add(file_idx)
                if verbose:
                    short = updater._extract_short_id(existing[wi_idx][0])
                    print(f"    [VERBOSE] Fallback match {short} ({existing[wi_idx][1]}) ↔ {files[file_idx].variant}")

            # Record updates
            for wi_idx, file_idx in pairs:
                wi_id, wi_title, _ = existing[wi_idx]
                result.updates.append((wi_id, wi_title, files[file_idx]))

            # Remaining files with no WI → creates
            for file_idx in range(len(files)):
                if file_idx not in matched_files:
                    if existing:
                        max_num = max(e[2] for e in existing)
                    else:
                        max_num = 0
                    already_queued = sum(
                        1 for _, c in result.creates if c.group_key == group_key
                    )
                    next_num = max_num + 1 + already_queued
                    result.creates.append((next_num, files[file_idx]))
        else:
            # Simple 1:1 positional matching (single file or single WI)
            for i, tp_file in enumerate(files):
                if i < len(existing):
                    wi_id, wi_title, _ = existing[i]
                    result.updates.append((wi_id, wi_title, tp_file))
                else:
                    if existing:
                        max_num = max(e[2] for e in existing)
                    else:
                        max_num = 0
                    already_queued = sum(
                        1 for _, c in result.creates if c.group_key == group_key
                    )
                    next_num = max_num + 1 + already_queued
                    result.creates.append((next_num, tp_file))

    return result


# ---------------------------------------------------------------------------
# Phase 3: Update Existing Work Items  /  Phase 4: Create New Work Items
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
    """Build the CodeCollaborator review URL for the given CCR ID."""
    return CCR_URL_TEMPLATE.format(ccr_id=ccr_id)


def _is_ccr_url_for(uri: str, ccr_id: str) -> bool:
    """Return True if the URI references the given CCR, matching both the
    standard ccn-codecolab and the alternative ccn-p1codecolab01 hosts."""
    if not uri or not ccr_id:
        return False
    host_ok = "ccn-codecolab.wrs.com" in uri or "ccn-p1codecolab01.wrs.com" in uri
    if not host_ok:
        return False
    # Match either  review:id=<id>  or  review/<id>  style URLs
    return (f"review:id={ccr_id}" in uri) or (f"review/{ccr_id}" in uri)


def _has_ccr_link(uris, ccr_id: str) -> bool:
    """Return True if any URI in the iterable is a CCR link for ccr_id."""
    return any(_is_ccr_url_for(u, ccr_id) for u in uris)


def _is_source_reference(link: dict) -> bool:
    """Check if a hyperlink has a source-reference role."""
    role = link.get("role", {})
    role_id = role.get("id", "") if isinstance(role, dict) else str(role)
    return "ref_src" in role_id.lower() or "source" in role_id.lower()


def _link_role_id(link: dict) -> str:
    """Extract a comparable role string from a hyperlink (handles dict or scalar role)."""
    role = link.get("role", "")
    if isinstance(role, dict):
        return role.get("id", "")
    return str(role)


def _normalize_hyperlinks(links: List[Dict]) -> set:
    """Normalize hyperlinks to a set of (role_id, uri) tuples for comparison."""
    return {(_link_role_id(link), link.get("uri", "")) for link in links}


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
) -> Tuple[bool, bool, set, str, str]:
    """Update an existing work item's hyperlinks, component, and category.

    Returns (success, changes_needed, current_uris, current_component, current_category_id).
    """
    short_id = updater._extract_short_id(wi_id)
    print(f"\n  Updating: {short_id} - {wi_title}")
    print(f"    Matched to: {tp_file.variant}/{tp_file.test_type}/{tp_file.tp_filename}")

    # Fetch current hyperlinks
    url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{short_id}"
    params = {"fields[workitems]": "hyperlinks,status,fld_component,fld_category",
              "fields[categories]": "@all"}
    resp = updater.session.get(url, params=params, verify=updater.verify_ssl)
    if resp.status_code != 200:
        print(f"    Error fetching work item: {resp.status_code}")
        return False, False, set(), "", ""

    data = resp.json()
    attrs = data.get("data", {}).get("attributes", {})
    current_links = attrs.get("hyperlinks", [])
    current_status = attrs.get("status", "")
    current_component = attrs.get("fld_component", "")
    _category_rel = (
        data.get("data", {})
            .get("relationships", {})
            .get("fld_category", {})
            .get("data") or {}
    )
    current_category_id = _category_rel.get("id", "") if isinstance(_category_rel, dict) else ""

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

    # Add CCR link only if not already present (any p1 or non-p1 variant counts)
    existing_uris = {link.get("uri", "") for link in preserved}
    if ccr_url and not _has_ccr_link(existing_uris, ccr_id):
        updated_hyperlinks.append(new_ccr_link)

    # --- Alignment check ---
    # Compare (role, uri) pairs so a URL attached with the wrong role
    # (e.g. a .c file as ref_int instead of ref_src) still registers as a diff.
    # current_uris (URI-only) is kept for downstream change-log formatting.
    current_uris = {link.get("uri", "") for link in current_links}
    current_pairs = _normalize_hyperlinks(current_links)
    desired_pairs = _normalize_hyperlinks(updated_hyperlinks)
    hyperlinks_aligned = current_pairs == desired_pairs

    desired_component = f"comp_{component}"
    component_aligned = current_component == desired_component

    desired_category_id = f"{updater.project_id}/cat_{category}"
    category_aligned = current_category_id == desired_category_id

    if verbose:
        if not hyperlinks_aligned:
            extra = current_pairs - desired_pairs
            missing = desired_pairs - current_pairs
            if extra:
                print(f"    [VERBOSE] Hyperlinks to remove (role, uri): {extra}")
            if missing:
                print(f"    [VERBOSE] Hyperlinks to add    (role, uri): {missing}")
        if not component_aligned:
            print(f"    [VERBOSE] Component: '{current_component}' → '{desired_component}'")
        if not category_aligned:
            print(f"    [VERBOSE] Category:  '{current_category_id}' → '{desired_category_id}'")

    if hyperlinks_aligned and component_aligned and category_aligned:
        print(f"    ✓ Hyperlinks, component and category already aligned")
        return True, False, current_uris, current_component, current_category_id

    # Changes needed — report details
    old_source = [link for link in current_links if _is_source_reference(link)]
    # Compute actual differences (role+URI pairs being removed vs added)
    pairs_to_remove = current_pairs - desired_pairs
    pairs_to_add = desired_pairs - current_pairs
    if dry_run:
        if current_status != "rework":
            print(f"    [DRY RUN] Would change status from '{current_status}' to 'rework'")
        if not hyperlinks_aligned:
            if pairs_to_remove:
                print(f"    [DRY RUN] Would remove {len(pairs_to_remove)} hyperlink(s):")
                for role, uri in sorted(pairs_to_remove):
                    print(f"      - [{role}] {uri}")
            if pairs_to_add:
                print(f"    [DRY RUN] Would add {len(pairs_to_add)} hyperlink(s):")
                for role, uri in sorted(pairs_to_add):
                    print(f"      + [{role}] {uri}")
            # Show what stays unchanged
            unchanged_src = [link for link in old_source
                             if (_link_role_id(link), link.get("uri", "")) in desired_pairs]
            if unchanged_src:
                print(f"    ✓ {len(unchanged_src)} source reference link(s) already aligned")
        else:
            print(f"    ✓ Hyperlinks already aligned")
        if not component_aligned:
            print(f"    [DRY RUN] Would update fld_component from '{current_component}' to '{desired_component}'")
        else:
            print(f"    ✓ Component already aligned")
        # Category
        if not category_aligned:
            print(f"    [DRY RUN] Would set fld_category to '{category}'")
        else:
            print(f"    ✓ Category already aligned")
        return True, True, current_uris, current_component, current_category_id

    # Execute: status -> rework
    if current_status != "rework":
        print(f"    Changing status to 'rework'...")
        if not updater.update_work_item_status(wi_id, "rework", dry_run=False):
            print(f"    ✗ Failed to change status to 'rework', skipping")
            return False, True, current_uris, current_component, current_category_id
        print(f"    ✓ Status changed to 'rework'")

    # Execute: update hyperlinks (only if changed)
    if not hyperlinks_aligned:
        if updater.update_work_item_hyperlinks(wi_id, updated_hyperlinks, dry_run=False):
            print(f"    ✓ Hyperlinks updated")
        else:
            print(f"    ✗ Failed to update hyperlinks")
            return False, True, current_uris, current_component, current_category_id

    # Execute: update fld_component (only if changed)
    if not component_aligned:
        if updater.update_work_item_attributes(wi_id, {"fld_component": desired_component}, dry_run=False):
            print(f"    ✓ Component updated to '{desired_component}'")
        else:
            print(f"    ✗ Failed to update component")

    # Execute: update fld_category via work item PATCH with relationships (only if changed)
    if not category_aligned:
        cat_url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{short_id}"
        cat_payload = {
            "data": {
                "type": "workitems",
                "id": wi_id,
                "relationships": {
                    "fld_category": {
                        "data": {
                            "type": "categories",
                            "id": desired_category_id,
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
    return True, True, current_uris, current_component, current_category_id


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
    print(f"    Source: {tp_file.variant}/{tp_file.test_type}/{tp_file.tp_filename}")
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
# Test Case Linking Helpers  (invoked during Phase 3 and Phase 4)
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


def _fetch_linked_tc_titles(
    updater: PolarionSourceLinkUpdater,
    wi_id: str,
) -> set:
    """Fetch titles of all non-derived linked work items on a WI (quiet, for matching)."""
    existing = _get_existing_tc_links(updater, wi_id)
    titles = set()
    for item in existing:
        _wi_data = (item.get("relationships", {})
                        .get("workItem", {})
                        .get("data") or {})
        rel_id = _wi_data.get("id", "") if isinstance(_wi_data, dict) else ""
        if rel_id:
            target_short_id = updater._extract_short_id(rel_id)
        else:
            parts = item.get("id", "").split("/")
            if len(parts) >= 3:
                target_short_id = parts[-2]
            else:
                continue
        url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{target_short_id}"
        params = {"fields[workitems]": "title"}
        resp = updater.session.get(url, params=params, verify=updater.verify_ssl)
        if resp.status_code == 200:
            title = resp.json().get("data", {}).get("attributes", {}).get("title", "")
            if title:
                titles.add(title)
    return titles


def _resolve_existing_tc_links(
    updater: PolarionSourceLinkUpdater,
    tp_wi_id: str,
    test_name: str,
    verbose: bool = False,
) -> Tuple[set, List[Dict], List[Dict], set]:
    """
    Classify existing TC links on a TP as same-function or foreign.

    A TC is "same-function" if its title starts with '{test_name}_'.
    Everything else is foreign (belongs to a different function's TP).

    Returns (same_function_tc_names, same_function_links, foreign_links, foreign_tc_names).
    """
    existing = _get_existing_tc_links(updater, tp_wi_id, verbose)
    if not existing:
        return set(), [], [], set()

    same_function_names: set = set()
    same_function_links: List[Dict] = []
    foreign_links: List[Dict] = []
    foreign_names: set = set()
    prefix = f"{test_name}_"

    tp_short = updater._extract_short_id(tp_wi_id)
    print(f"      Resolving {len(existing)} existing TC link(s) on {tp_short}...")

    for item in existing:
        link_id = item.get("id", "")

        # Strategy 1: use relationships.workItem.data.id (most reliable)
        _wi_data = (item.get("relationships", {})
                        .get("workItem", {})
                        .get("data") or {})
        rel_id = _wi_data.get("id", "") if isinstance(_wi_data, dict) else ""
        if rel_id:
            target_short_id = updater._extract_short_id(rel_id)
        else:
            # Strategy 2: parse from link ID — target is second-to-last segment
            # (format varies but role is always last, target always before it)
            parts = link_id.split("/")
            if len(parts) >= 3:
                target_short_id = parts[-2]
            else:
                print(f"        ? Could not parse link ID: {link_id}")
                foreign_links.append(item)
                continue

        # Fetch TC title
        url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{target_short_id}"
        params = {"fields[workitems]": "title"}
        resp = updater.session.get(url, params=params, verify=updater.verify_ssl)
        if resp.status_code != 200:
            print(f"        ? Could not fetch {target_short_id} (HTTP {resp.status_code}) — treating as foreign")
            foreign_links.append(item)
            continue

        title = resp.json().get("data", {}).get("attributes", {}).get("title", "")

        if title.startswith(prefix):
            same_function_names.add(title)
            same_function_links.append(item)
            if verbose:
                print(f"        ✓ {target_short_id} - {title}")
        else:
            foreign_links.append(item)
            foreign_names.add(title)
            print(f"        ↳ foreign: {target_short_id} - {title}")

    print(f"      Classified: {len(same_function_links)} same-function, {len(foreign_links)} foreign")

    return same_function_names, same_function_links, foreign_links, foreign_names


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
    _resolved: Optional[Tuple[set, List[Dict], List[Dict], set]] = None,
) -> Tuple[int, bool]:
    """
    Link test cases found in the tp file to the given TP work item.
    Preserves foreign TC links (those belonging to other functions).

    Returns (count of TCs linked, whether TC links were already aligned).
    Pass _resolved to reuse a prior _resolve_existing_tc_links() result.
    """
    if not tp_file.tc_names:
        return 0, True

    tp_short = updater._extract_short_id(tp_wi_id)
    print(f"    TC linking for {tp_short}:")
    print(f"      Found {len(tp_file.tc_names)} TC reference(s) in file")

    # Classify existing TC links as same-function or foreign
    if tp_wi_id == "DRY_RUN":
        # Newly created WI in dry_run mode — no existing links
        same_function_names, same_function_links, foreign_links, foreign_names = set(), [], [], set()
    elif _resolved is not None:
        same_function_names, same_function_links, foreign_links, foreign_names = _resolved
    else:
        same_function_names, same_function_links, foreign_links, foreign_names = _resolve_existing_tc_links(
            updater, tp_wi_id, tp_file.test_name, verbose
        )

    desired_names = set(tp_file.tc_names)

    # Check alignment
    if same_function_names == desired_names:
        print(f"      ✓ TC links already aligned ({len(desired_names)} TCs"
              + (f", {len(foreign_links)} foreign preserved" if foreign_links else "")
              + ")")
        return 0, True

    # Not aligned — show what differs
    to_add = desired_names - same_function_names
    to_remove = same_function_names - desired_names
    if to_add:
        print(f"      TC(s) to add:    {sorted(to_add)}")
    if to_remove:
        print(f"      TC(s) to remove: {sorted(to_remove)}")

    linked_count = 0

    # Delete only same-function TC links (preserve foreign)
    if not dry_run:
        if same_function_links:
            print(f"      Deleting {len(same_function_links)} of the function TC link(s)...")
            for link in same_function_links:
                _delete_linked_work_item(updater, tp_wi_id, link, verbose)
    else:
        print(f"      [DRY RUN] Would remove {len(same_function_links)} of the function TC link(s)"
              + (f", preserve {len(foreign_links)} foreign" if foreign_links else "")
              + ")")

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

    return linked_count, False


# ---------------------------------------------------------------------------
# CCR Branch Resolution & Git Checkout
# ---------------------------------------------------------------------------

# CodeCollaborator JSON API endpoint (used for CCR branch resolution)
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
        description="Manage Polarion test procedure work items for a component's tp/tl files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --gitlab-base https://ccn-gitlab.wrs.com/shallowford/project/wassp/-/blob/wassp-jenkins --ccr-id 31421 --component-glob "POSBSP_SSD_NVME0*" --component SSD_NVME0
  %(prog)s --gitlab-base https://ccn-gitlab.wrs.com/shallowford/project/wassp/-/blob/wassp-jenkins --ccr-id 28264 --component-glob "SBL_BOOT_APP0*" --component BOOT_APP0 --execute --limit 1
  %(prog)s --gitlab-base https://ccn-gitlab.wrs.com/shallowford/project/wassp/-/blob/wassp-jenkins --branch my-feature-branch --component SSD_NVME0 --component-glob "POSBSP_SSD_NVME0*"
        """,
    )

    parser.add_argument(
        "--repo-path",
        default=None,
        help="Path to the local git repo root (wassp). If omitted, files are browsed via GitLab API (requires GITLAB_TOKEN).",
    )
    parser.add_argument(
        "--ccr-id",
        help="CCR review ID — resolves the branch automatically and adds CCR hyperlink",
    )
    parser.add_argument(
        "--branch",
        help="Git branch name to use directly (skips CCR resolution, no CCR hyperlink added)",
    )
    parser.add_argument(
        "--gitlab-base",
        default=DEFAULT_GITLAB_BASE,
        help=f"GitLab base URL (default: {DEFAULT_GITLAB_BASE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without making actual changes (this is the default; flag kept for explicitness)",
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
        "--component-glob",
        required=True,
        help="Glob pattern for component directories under native/ or SFORD_POS/ (e.g. SBL_BOOT_APP0*). "
             "If the glob starts with POSBSP, searches under SFORD_POS/; otherwise under native/.",
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

    if args.ccr_id and args.branch:
        parser.error("The branch must be either provided directly (--branch) or resolved from a CCR ID (--ccr-id), not both.")
    if not args.ccr_id and not args.branch:
        parser.error("Either --ccr-id or --branch must be provided.")

    # Determine mode: local (--repo-path) or remote (GitLab API)
    use_remote = args.repo_path is None
    repo_path = None
    if not use_remote:
        repo_path = os.path.abspath(args.repo_path)
        if not os.path.isdir(repo_path):
            print(f"Error: repo path does not exist: {repo_path}")
            sys.exit(1)

    # Environment variables
    base_url = os.environ.get("POLARION_API_BASE")
    pat = os.environ.get("POLARION_PAT")
    project_id = args.project_id or os.environ.get("POLARION_PROJECT_ID")
    gitlab_token = os.environ.get("GITLAB_TOKEN")

    missing = []
    if not base_url:
        missing.append(("POLARION_API_BASE", "Polarion REST API base URL (e.g. https://ccn-polarion.wrs.com/polarion/rest/v1)"))
    if not pat:
        missing.append(("POLARION_PAT", "Polarion personal access token — generate one from your Polarion profile"))
    if not project_id:
        missing.append(("POLARION_PROJECT_ID", 'Polarion project ID (e.g. "Shallowford_BSP" or "Shallowford_BL"). Can also be passed via --project-id'))
    if use_remote and not gitlab_token:
        missing.append(("GITLAB_TOKEN", "GitLab personal access token (read_api + read_repository scopes) — generate one from your GitLab profile. Required in remote mode (when --repo-path is not provided)"))
    if args.ccr_id:
        if not os.environ.get("CCN_LOGIN"):
            missing.append(("CCN_LOGIN", "CodeCollaborator username — required when using --ccr-id"))
        if not os.environ.get("CCN_PASSWORD"):
            missing.append(("CCN_PASSWORD", "CodeCollaborator password — required when using --ccr-id"))

    if missing:
        print("Error: The following environment variables are missing:\n")
        for var, desc in missing:
            print(f"  {var}")
            print(f"    {desc}\n")
        print("Make sure your ~/.bashrc (or equivalent) contains the following:\n")
        print('  export POLARION_API_BASE="https://ccn-polarion.wrs.com/polarion/rest/v1"')
        print('  export POLARION_PAT="<your personal access token — see your Polarion profile>"')
        print('  export POLARION_PROJECT_ID="e.g.: Shallowford_BSP or Shallowford_BL"')
        print('  export CCN_LOGIN="<your CodeCollaborator username>"')
        print('  export CCN_PASSWORD="<your CodeCollaborator password>"')
        print('  export GITLAB_TOKEN="<your personal access token — see your GitLab profile>"\n')
        print("Don't forget to run  \"source ~/.bashrc\" after updating your environment variables. :p")
        sys.exit(1)

    if args.dry_run and args.execute:
        parser.error("--dry-run and --execute are mutually exclusive.")
        
    # Default is dry-run; --execute flips it. --dry-run is accepted as an explicit opt-in.
    dry_run = not args.execute

    print("=" * 180)
    if dry_run:
        print("DRY RUN MODE - No changes will be made")
    else:
        print("EXECUTE MODE - Changes will be applied!")
    print("=" * 180)
    print("\nLoading...", end="", flush=True)

    # Phase 0: Resolve branch
    ccr_id = args.ccr_id  # may be None if --branch was used
    print("\r" + " " * 30 + "\r", end="", flush=True)
    if args.branch:
        branch_name = args.branch
        print(f"Phase 0: Using branch '{branch_name}' (provided directly)")
    else:
        print(f"Phase 0: Resolving CCR branch...")
        branch_name = resolve_ccr_branch(ccr_id, verbose=args.verbose)

    if use_remote:
        # Remote mode: files are fetched from the CCR/feature branch,
        # but hyperlinks use --gitlab-base as-is (e.g. wassp-jenkins).
        gitlab_base = args.gitlab_base
        if args.verbose:
            print(f"  [VERBOSE] Using remote mode (GitLab API) on branch '{branch_name}'")
            print(f"  [VERBOSE] Source reference base: {gitlab_base}")

        gitlab_client = GitLabRepoClient(
            args.gitlab_base, gitlab_token,
            verify_ssl=args.verify_ssl,
        )
    else:
        # Local mode: checkout the branch
        gitlab_base = args.gitlab_base
        checkout_wassp_branch(repo_path, branch_name, verbose=args.verbose)

    # Phase 1: discover files
    if use_remote:
        tp_files = discover_tp_files_remote(
            gitlab_client, branch_name,
            verbose=args.verbose,
            component_glob=args.component_glob,
            component_override=args.component,
        )
    else:
        tp_files = discover_tp_files(
            repo_path, verbose=args.verbose,
            component_glob=args.component_glob,
            component_override=args.component,
        )
    if not tp_files:
        print("No tp files found. Nothing to do.")
        return

    by_variant: Dict[str, List[TpFileInfo]] = {}
    for f in tp_files:
        by_variant.setdefault(f"{f.variant}/{f.test_type}", []).append(f)

    print(f"Phase 1: Discovered {len(tp_files)} TP file(s) across {len(by_variant)} folder(s)")

    if args.verbose:
        for key in sorted(by_variant.keys()):
            files = by_variant[key]
            print(f"  {key}: {len(files)} file(s)")
            for f in files:
                tc_count = len(f.tc_names)
                tc_info = f" [{tc_count} TC(s)]" if tc_count else ""
                print(f"    - {f.tp_filename} (test: {f.test_name}){tc_info}")

    # Phase 2: match against Polarion
    print(f"Phase 2: Querying Polarion for existing work items...")
    updater = PolarionSourceLinkUpdater(
        base_url, pat, project_id,
        verify_ssl=args.verify_ssl,
        verbose=args.verbose,
    )
    match_result = match_files_to_work_items(tp_files, updater, verbose=args.verbose)

    # Identify multi-file groups (for showing variant in Resolved Mappings and Change Log)
    _group_counts: Dict[str, int] = {}
    for f in tp_files:
        _group_counts[f.group_key] = _group_counts.get(f.group_key, 0) + 1
    multi_file_groups = {k for k, v in _group_counts.items() if v > 1}

    # --- Resolved Mappings and Polarion Work Items (printed immediately after matching) ---
    # Grouped by function name (test_name) so HLTP and LLTP of the same function
    # appear together. Each entry is ((test_type, number), text) for sorting.
    info_by_group: Dict[str, List[Tuple[Tuple[str, int], str]]] = {}
    for wi_id, wi_title, tp_file in match_result.updates:
        short_id = updater._extract_short_id(wi_id)
        tl_url, tp_url = build_gitlab_urls(tp_file, gitlab_base)
        lines = [f"  {short_id} - {wi_title}"]
        if tp_file.group_key in multi_file_groups:
            lines.append(f"          \u21b3 {tp_file.variant}")
        lines.append("")
        # Fetch linked TC titles and current hyperlinks to show alignment
        prefix = f"{tp_file.test_name}_"
        all_tc_titles = _fetch_linked_tc_titles(updater, wi_id)
        foreign_tc_names = sorted(t for t in all_tc_titles if not t.startswith(prefix))
        # Fetch current hyperlink URIs for alignment check
        fetch_url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{short_id}"
        fetch_resp = updater.session.get(
            fetch_url, params={"fields[workitems]": "hyperlinks"},
            verify=updater.verify_ssl,
        )
        current_uris: set = set()
        if fetch_resp.status_code == 200:
            current_links = fetch_resp.json().get("data", {}).get("attributes", {}).get("hyperlinks", [])
            current_uris = {link.get("uri", "") for link in current_links}
        # Same-function TCs that are linked but NOT in the tp file (stale links)
        same_function_titles = sorted(
            t for t in all_tc_titles
            if t.startswith(prefix) and t not in set(tp_file.tc_names)
        )
        if tp_file.tc_names:
            for tc_name in tp_file.tc_names:
                if tc_name in all_tc_titles:
                    lines.append(f"      \u2713 [implements] {tc_name}")
                else:
                    lines.append(f"      \u2717 [implements] {tc_name}")
        for tc_name in same_function_titles:
            lines.append(f"      [-] [implements] {tc_name}  Not in the TP file, would be removed")
        if foreign_tc_names:
            for tc_name in foreign_tc_names:
                lines.append(f"      \u2713 [implements] {tc_name}  (foreign)")
        if tp_file.tc_names or foreign_tc_names or same_function_titles:
            lines.append("")
        if tl_url:
            mark = "\u2713" if tl_url in current_uris else "\u2717"
            lines.append(f"      {mark} [source reference] {tl_url}")
        tp_mark = "\u2713" if tp_url in current_uris else "\u2717"
        lines.append(f"      {tp_mark} [source reference] {tp_url}")
        if ccr_id:
            ccr_mark = "\u2713" if _has_ccr_link(current_uris, ccr_id) else "\u2717"
            lines.append(f"      {ccr_mark} [internal reference] CCR #{ccr_id}")
        _m = re.search(r"_(\d+)$", wi_title)
        _num = int(_m.group(1)) if _m else 0
        info_by_group.setdefault(tp_file.test_name, []).append(
            ((tp_file.test_type, _num), "\n".join(lines))
        )

    for number, tp_file in match_result.creates:
        title = f"{tp_file.test_name}_{tp_file.test_type}_{number}"
        tl_url, tp_url = build_gitlab_urls(tp_file, gitlab_base)
        lines = [f"  (new) {title}"]
        if tp_file.group_key in multi_file_groups:
            lines.append(f"          \u21b3 {tp_file.variant}")
        lines.append("")
        if tp_file.tc_names:
            for tc_name in tp_file.tc_names:
                lines.append(f"      [implements] {tc_name}")
            lines.append("")
        if tl_url:
            lines.append(f"      [source reference] {tl_url}")
        lines.append(f"      [source reference] {tp_url}")
        info_by_group.setdefault(tp_file.test_name, []).append(
            ((tp_file.test_type, number), "\n".join(lines))
        )

    # Build a Polarion query showing all TP work item titles in this run
    all_tp_titles = []
    for wi_id, wi_title, tp_file in match_result.updates:
        all_tp_titles.append(wi_title)
    for number, tp_file in match_result.creates:
        all_tp_titles.append(f"{tp_file.test_name}_{tp_file.test_type}_{number}")
    if all_tp_titles:
        title_query = " OR ".join(f"title:{t}" for t in all_tp_titles)
        polarion_query = f"NOT status:deleted AND type:wi_testProcedure AND ({title_query})"
        query_len = max(len(polarion_query), 80)
        print(f"\n{'*' * query_len}")
        print(f"(!) Paste Query below in the Polarion search to compare with the TPs fetched from CCR more easily :)")
        print(f"{polarion_query}")
        print(f"{'*' * query_len}")

    branch_label = f"CCR #{ccr_id} ({branch_name})" if ccr_id else branch_name
    print(f"\n{'=' * 180}")
    print(f"Resolved Mappings from {branch_label} and Polarion Work Items")
    print("=" * 180)
    first_group = True
    for group_key in sorted(info_by_group.keys()):
        if not first_group:
            print()
            print("  " + "-" * 120)
        first_group = False
        entries = [
            text for _sk, text in sorted(
                info_by_group[group_key], key=lambda x: x[0]
            )
        ]
        for i, entry in enumerate(entries):
            print()
            print(entry)
            if i < len(entries) - 1:
                print()

    # Phase 3: update existing work items
    updated_ok = 0
    updated_fail = 0
    aligned_count = 0
    items_processed = 0
    tc_linked_total = 0
    tc_cache: Dict[str, Optional[str]] = {}
    # Change-log entries grouped by function name (test_name) so HLTP and LLTP
    # of the same function appear together. Each entry is (sort_key, text)
    # where sort_key = (test_type, number) to order HLTP before LLTP and by
    # trailing number ascending.
    change_entries_by_group: Dict[str, List[Tuple[Tuple[str, int], str]]] = {}
    limit = args.limit
    if match_result.updates and not args.skip_updates:
        print(f"\nPhase 3: Resolving needed updates...")
        # In non-verbose mode, suppress the detailed per-WI processing output;
        # the Change Log below summarises everything. contextlib.redirect_stdout
        # guarantees sys.stdout is restored even if an exception is raised.
        _redirect = contextlib.redirect_stdout(io.StringIO()) if not args.verbose else contextlib.nullcontext()
        with _redirect:
            for wi_id, wi_title, tp_file in match_result.updates:
                if limit and items_processed >= limit:
                    print(f"  Limit of {limit} reached, stopping.")
                    break

                short_id = updater._extract_short_id(wi_id)

                # Step 1: Check and apply WI attribute changes
                result = update_existing_work_item(
                    updater, wi_id, wi_title, tp_file,
                    gitlab_base, ccr_id,
                    component=args.component, category=args.category,
                    dry_run=dry_run, verbose=args.verbose,
                )
                success, wi_changes, wi_current_uris, wi_current_component, wi_current_category = (
                    result[0], result[1], result[2], result[3], result[4]
                )

                if not success:
                    updated_fail += 1
                    items_processed += 1
                    continue

                # Step 2: Pre-resolve TC alignment (avoid double fetch)
                if tp_file.tc_names and wi_id != "DRY_RUN":
                    resolved = _resolve_existing_tc_links(
                        updater, wi_id, tp_file.test_name, args.verbose
                    )
                    same_names = resolved[0]
                    tc_will_change = same_names != set(tp_file.tc_names)
                else:
                    resolved = None
                    tc_will_change = bool(tp_file.tc_names)

                # Step 3: If only TC links need changes (WI attrs aligned),
                # ensure rework status before modifying links
                if tc_will_change and not wi_changes and not dry_run:
                    fetch_url = f"{updater.base_url}/projects/{updater.project_id}/workitems/{short_id}"
                    fetch_resp = updater.session.get(
                        fetch_url, params={"fields[workitems]": "status"},
                        verify=updater.verify_ssl,
                    )
                    if fetch_resp.status_code == 200:
                        cur_st = fetch_resp.json().get("data", {}).get("attributes", {}).get("status", "")
                        if cur_st != "rework":
                            print(f"    Setting status to 'rework' for TC link changes...")
                            updater.update_work_item_status(wi_id, "rework", dry_run=False)
                elif tc_will_change and not wi_changes and dry_run:
                    print(f"    [DRY RUN] Would change status to 'rework' for TC link changes")

                # Step 4: Apply TC link changes (passing pre-resolved data)
                tc_count, tc_aligned = link_test_cases_to_tp(
                    updater, wi_id, tp_file, tc_cache,
                    dry_run=dry_run, verbose=args.verbose,
                    _resolved=resolved,
                )
                tc_linked_total += tc_count

                # Determine overall alignment
                fully_aligned = not wi_changes and tc_aligned

                if fully_aligned:
                    aligned_count += 1
                    foreign_count = len(resolved[3]) if resolved and resolved[3] else 0
                    tc_summary = f"{len(tp_file.tc_names)} TCs"
                    if foreign_count:
                        tc_summary += f", {foreign_count} foreign"
                    lines = [f"  [ALREADY ALIGNED] {short_id} - {wi_title}"]
                    if tp_file.group_key in multi_file_groups:
                        lines.append(f"          \u21b3 {tp_file.variant}")
                    lines.append(f"      TCs already aligned ({tc_summary}) \u2713")
                    lines.append(f"      Source references already aligned \u2713")
                    if ccr_id:
                        lines.append(f"      Internal reference already aligned \u2713")
                    lines.append(f"      Component already aligned \u2713")
                    lines.append(f"      Category already aligned \u2713")
                    _m = re.search(r"_(\d+)$", wi_title)
                    _num = int(_m.group(1)) if _m else 0
                    change_entries_by_group.setdefault(tp_file.test_name, []).append(
                        ((tp_file.test_type, _num), "\n".join(lines))
                    )
                else:
                    updated_ok += 1
                    tl_url, tp_url = build_gitlab_urls(tp_file, gitlab_base)
                    tag = "[WOULD UPDATE]" if dry_run else "[UPDATED]"
                    lines = [f"  {tag} {short_id} - {wi_title}"]
                    if tp_file.group_key in multi_file_groups:
                        lines.append(f"          \u21b3 {tp_file.variant}")
                    if tc_aligned and tp_file.tc_names:
                        foreign_count = len(resolved[3]) if resolved and resolved[3] else 0
                        tc_summary = f"{len(tp_file.tc_names)} TCs"
                        if foreign_count:
                            tc_summary += f", {foreign_count} foreign"
                        lines.append(f"      TCs already aligned ({tc_summary}) \u2713")
                    else:
                        same_names = resolved[0] if resolved else set()
                        desired_set = set(tp_file.tc_names)
                        add_suffix = "Currently missing - will be added" if dry_run else "Was missing, added"
                        remove_suffix = "Not in the TP file, would be removed" if dry_run else "Not in the TP file, removed"
                        for tc_name in tp_file.tc_names:
                            if tc_name in same_names:
                                lines.append(f"      \u2713 [implements] {tc_name}")
                            elif tc_cache.get(tc_name) is None:
                                lines.append(f"      \u26a0 [implements] {tc_name}  Work Item not found in Polarion - check TC name in TP file or Polarion")
                            else:
                                lines.append(f"      [+] [implements] {tc_name}  {add_suffix}")
                        # Same-function TCs linked on WI but not in TP file (stale — removed/would be removed)
                        stale_names = sorted(same_names - desired_set)
                        for tc_name in stale_names:
                            lines.append(f"      [-] [implements] {tc_name}  {remove_suffix}")
                        if resolved and resolved[3]:
                            for tc_name in sorted(resolved[3]):
                                lines.append(f"      \u2713 [implements] {tc_name}  (foreign)")
                    lines.append("")
                    src_aligned = (tp_url in wi_current_uris and
                                   (not tl_url or tl_url in wi_current_uris))
                    if src_aligned:
                        lines.append(f"      Source references already aligned \u2713")
                    else:
                        if tl_url:
                            mark = "\u2713" if tl_url in wi_current_uris else "[+]"
                            lines.append(f"      {mark} [source reference] {tl_url}")
                        tp_mark = "\u2713" if tp_url in wi_current_uris else "[+]"
                        lines.append(f"      {tp_mark} [source reference] {tp_url}")
                    if ccr_id:
                        ccr_url = build_ccr_url(ccr_id)
                        if _has_ccr_link(wi_current_uris, ccr_id):
                            lines.append(f"      Internal reference already aligned \u2713")
                        else:
                            lines.append(f"      [+] [internal reference] {ccr_url}")
                    # Component alignment line
                    desired_component_str = f"comp_{args.component}"
                    if wi_current_component == desired_component_str:
                        lines.append(f"      Component already aligned \u2713")
                    else:
                        lines.append(f"      [+] Component: '{wi_current_component or '(none)'}' → '{desired_component_str}'")
                    # Category alignment line
                    desired_category_str = f"{updater.project_id}/cat_{args.category}"
                    if wi_current_category == desired_category_str:
                        lines.append(f"      Category already aligned \u2713")
                    else:
                        lines.append(f"      [+] Category: '{wi_current_category or '(none)'}' → '{desired_category_str}'")
                    _m = re.search(r"_(\d+)$", wi_title)
                    _num = int(_m.group(1)) if _m else 0
                    change_entries_by_group.setdefault(tp_file.test_name, []).append(
                        ((tp_file.test_type, _num), "\n".join(lines))
                    )

                    # Status → in_review (only when changes were/would be made)
                    if not dry_run:
                        print(f"    Changing TP status to 'in_review'...")
                        if updater.update_work_item_status(wi_id, "in_review", dry_run=False):
                            print(f"    ✓ TP status changed to 'in_review'")
                        else:
                            print(f"    ⚠ Failed to change TP status to 'in_review'")
                    else:
                        print(f"    [DRY RUN] Would change TP status to 'in_review'")

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
                gitlab_base, ccr_id,
                component=args.component, category=args.category,
                author=args.author,
                dry_run=dry_run, verbose=args.verbose,
            )
            if created_id:
                created_ok += 1
                # Link TCs to this TP (TP is still in rework)
                tc_count, tc_aligned = link_test_cases_to_tp(
                    updater, created_id, tp_file, tc_cache,
                    dry_run=dry_run, verbose=args.verbose,
                )
                tc_linked_total += tc_count
                # Now set TP to in_review
                if not dry_run and created_id != "DRY_RUN":
                    print(f"    Changing TP status to 'in_review'...")
                    if updater.update_work_item_status(created_id, "in_review", dry_run=False):
                        print(f"    ✓ TP status changed to 'in_review'")
                    else:
                        print(f"    ⚠ Failed to change TP status to 'in_review'")
                elif dry_run:
                    print(f"    [DRY RUN] Would change TP status to 'in_review'")

                # Accumulate change log entry
                title = f"{tp_file.test_name}_{tp_file.test_type}_{number}"
                tl_url, tp_url = build_gitlab_urls(tp_file, gitlab_base)
                tag = "[WOULD CREATE]" if dry_run else "[CREATED]"
                lines = [f"  {tag} {title}"]
                if tp_file.group_key in multi_file_groups:
                    lines.append(f"          \u21b3 {tp_file.variant}")
                for tc_name in tp_file.tc_names:
                    lines.append(f"      [+] [implements] {tc_name}")
                lines.append("")
                if tl_url:
                    lines.append(f"      [+] [source reference] {tl_url}")
                lines.append(f"      [+] [source reference] {tp_url}")
                if ccr_id:
                    ccr_url = build_ccr_url(ccr_id)
                    lines.append(f"      [+] [internal reference] {ccr_url}")
                change_entries_by_group.setdefault(tp_file.test_name, []).append(
                    ((tp_file.test_type, number), "\n".join(lines))
                )
            else:
                created_fail += 1
            items_processed += 1

    # --- Change Log (after all processing, with alignment info) ---
    if change_entries_by_group:
        print(f"\n{'=' * 180}")
        print("Change Log")
        print("=" * 180)
        first_group = True
        for group_key in sorted(change_entries_by_group.keys()):
            if not first_group:
                print("  " + "-" * 120)
            first_group = False
            # Sort entries so HLTPs come before LLTPs and numbers ascend
            group_entries = [
                text for _sk, text in sorted(
                    change_entries_by_group[group_key], key=lambda x: x[0]
                )
            ]
            for i, entry in enumerate(group_entries):
                print(entry)
                if i < len(group_entries) - 1:
                    print()

    # Summary
    print(f"\n{'=' * 180}")
    print("Summary:")
    print(f"  Files discovered: {len(tp_files)}")
    print(f"  Existing WIs matched: {len(match_result.updates)}")
    if dry_run:
        print(f"    Already aligned: {aligned_count}")
        print(f"    Would update: {updated_ok}")
        print(f"  New WIs to create: {len(match_result.creates)}")
        print(f"    Would create: {created_ok}")
        print(f"  TC links: {tc_linked_total}")
        print(f"\nThis was a DRY RUN. Use --execute to apply changes.")
    else:
        print(f"    Already aligned: {aligned_count}")
        print(f"    Updated OK: {updated_ok}, Failed: {updated_fail}")
        print(f"  New WIs created: {created_ok}, Failed: {created_fail}")
        print(f"  TC links created: {tc_linked_total}")
    print(f"{'=' * 180}")

if __name__ == "__main__":
    main()
