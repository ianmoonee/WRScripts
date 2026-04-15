"""
CCN CodeCollaborator Review Updater
====================================

Updates custom fields on a Collaborator review via the JSON API (v1)
and lists all files attached to the review.  The fields to update are
specified in a JSON config file — only the fields present in the file
will be modified.

Modification History
--------------------
15apr26,tal  Added --bsp/--bl modes, WASSP_PATH env var, 4000-char field guard. Change --update-first-only to --update-most-recent for clarity. 
09mar26,tal  Added fixed path for --config file
06mar26,tal  General improvements:added --update-first-only option; added debug output; improved error handling and messages.
05mar26,tal  Added support for multiple review IDs, with branch name and file list with hash for each.
05mar26,pse  Initial version

Prerequisites:
    - Set environment variables before running:
        export CCN_LOGIN="your_username"        (Linux)
        export CCN_PASSWORD="your_password"      (Linux)
        export WASSP_PATH="/path/to/wassp"      (Linux)
        $env:CCN_LOGIN = "your_username"        (PowerShell)
        $env:CCN_PASSWORD = "your_password"      (PowerShell)
        $env:WASSP_PATH = "C:\\path\\to\\wassp"    (PowerShell)

Usage:
    python ccn_updater.py --help
    python ccn_updater.py --review-id 31859 --bsp
    python ccn_updater.py --review-id 31859 --bl
    python ccn_updater.py --review-id 31859 --bsp --config fields.json
    python ccn_updater.py --review-id 31859 31280 31100 --bsp
    python ccn_updater.py --review-id 31859 31280 --bsp --update-most-recent
    python ccn_updater.py --review-id 31859 --bl --dry-run
    python ccn_updater.py --review-id 31859 --bsp --debug

Arguments:
    --help               Show usage information and exit.
    --review-id          One or more numeric IDs of the reviews to update.
    --bsp                BSP mode: use shortened file paths grouped by directory.
    --bl                 BL mode: use full file paths in a flat sorted list.
    --config             Path to a JSON file with custom field values to set.
                         (optional — defaults to fields.json in the script directory)
    --update-most-recent Only update the newest (first) review; use the rest
                         only for previous-hash lookups.
    --dry-run            Validate only, do not apply changes.
    --debug              Enable verbose debug output.

Config file format (include only the fields you want to update):
    {
        "Polarion PR number": "12345",
        "Artifact ID(s)": "doc1, doc2",
        "Starting Version(s)": "0.10",
        "Ending Version(s)": "0.15",
        "Related document ID(s) and Version(s)": "REQ-42 v2"
    }

API Reference:
    Server version: 14.4.14401
    Server manual:  https://ccn-codecolab.wrs.com:8443/manual
    JSON API endpoint: https://ccn-codecolab.wrs.com:8443/services/json/v1
"""

import requests
import json
import os
import re
import argparse
import subprocess
import urllib3

# Suppress SSL/TLS InsecureRequestWarning messages
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def shorten_path(path):
    """Shorten a file path based on known directory prefixes.

    - helix/guests/vxworks-7/pkgs_v2/test/shallowford-cert-tests/* -> remainder after that prefix
    - ldra/* -> ldra/*
    - Otherwise -> last two directories plus filename
    """
    normalized = path.replace("\\", "/")

    cert_tests_prefix = "helix/guests/vxworks-7/pkgs_v2/test/shallowford-cert-tests/"
    if cert_tests_prefix in normalized:
        idx = normalized.index(cert_tests_prefix)
        return normalized[idx + len(cert_tests_prefix):]

    ldra_prefix = "ldra/"
    if ldra_prefix in normalized:
        idx = normalized.index(ldra_prefix)
        return normalized[idx:]

    parts = normalized.split("/")
    if len(parts) <= 3:
        return "/".join(parts)
    return "/".join(parts[-3:])


def group_by_directory(entries):
    """Group (shortened_path, value) pairs by directory.

    Returns a string with files grouped under their directory header,
    separated by blank lines.  Each entry is "filename - value".
    """
    from collections import OrderedDict
    groups = OrderedDict()
    for short_path, val in entries:
        parts = short_path.replace("\\", "/").rsplit("/", 1)
        if len(parts) == 2:
            directory, filename = parts
        else:
            directory, filename = "", parts[0]
        groups.setdefault(directory, []).append("{} - {}".format(filename, val))

    blocks = []
    for directory, lines in groups.items():
        block = directory + "\n" + "\n".join(lines) if directory else "\n".join(lines)
        blocks.append(block)
    return "\n\n".join(blocks)


def format_path(path, mode):
    """Return a display path based on the active mode.

    - "bsp": shortened path via shorten_path()
    - "bl":  full path with normalized forward slashes
    """
    if mode == "bsp":
        return shorten_path(path)
    return path.replace("\\", "/")


def build_help_epilog():
    """Return the epilog text for --help output."""
    return """\
prerequisites:
  Set environment variables before running:
    export CCN_LOGIN="your_username"         (Linux)
    export CCN_PASSWORD="your_password"      (Linux)
    export WASSP_PATH="/path/to/wassp"       (Linux)
    $env:CCN_LOGIN = "your_username"         (PowerShell)
    $env:CCN_PASSWORD = "your_password"      (PowerShell)
    $env:WASSP_PATH = "C:\\path\\to\\wassp"     (PowerShell)

examples:
  python ccn_updater.py --review-id 31859 --bsp
  python ccn_updater.py --review-id 31859 --bl
  python ccn_updater.py --review-id 31859 --bsp --config fields.json
  python ccn_updater.py --review-id 31859 31280 31100 --bsp
  python ccn_updater.py --review-id 31859 31280 --bsp --update-most-recent
  python ccn_updater.py --review-id 31859 --bl --dry-run
  python ccn_updater.py --review-id 31859 --bsp --debug

config file format (include only the fields you want to update):
  {
      "Polarion PR number": "12345",
      "Artifact ID(s)": "doc1, doc2",
      "Starting Version(s)": "0.10",
      "Ending Version(s)": "0.15",
      "Related document ID(s) and Version(s)": "REQ-42 v2"
  }
"""


# --- Parse command-line arguments ---
parser = argparse.ArgumentParser(
    description="Update custom fields on a Collaborator review.",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=build_help_epilog())
parser.add_argument("--review-id", type=int, nargs="+", required=True, help="One or more numeric IDs of the reviews to update.")
mode_group = parser.add_mutually_exclusive_group(required=True)
mode_group.add_argument("--bsp", action="store_true", help="BSP mode: use shortened file paths grouped by directory.")
mode_group.add_argument("--bl", action="store_true", help="BL mode: use full file paths in a flat sorted list.")
parser.add_argument("--config", type=str, default=None, help="Path to JSON file with custom field values (default: fields.json in the script directory).")
parser.add_argument("--dry-run", action="store_true", help="Validate the review without applying changes.")
parser.add_argument("--debug", action="store_true", help="Enable verbose debug output.")
parser.add_argument("--update-most-recent", action="store_true", help="Only update the newest (most recent) review; use the rest only for previous-hash lookups.")
args = parser.parse_args()

DRY_RUN = args.dry_run
DEBUG = args.debug
UPDATE_MOST_RECENT = args.update_most_recent
MODE = "bsp" if args.bsp else "bl"
REVIEW_IDS = sorted(args.review_id, reverse=True)

# --- Resolve config file path ---
if args.config:
    config_path = args.config
else:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "fields.json")
if not os.path.isfile(config_path):
    print("ERROR: Config file not found: {}".format(config_path))
    exit(1)

with open(config_path, "r") as f:
    field_values = json.load(f)

if not isinstance(field_values, dict) or not field_values:
    print("ERROR: Config file must be a non-empty JSON object, e.g.: ")
    print('  { "Ending Version(s)": "0.15" }')
    exit(1)

# Build the customFields payload from the config file
CUSTOM_FIELDS = [
    {"name": name, "value": [val]}
    for name, val in field_values.items()
]

# --- API configuration and credential validation ---
BASE_URL = "https://ccn-codecolab.wrs.com:8443/services/json/v1"
CCN_LOGIN = os.environ.get("CCN_LOGIN")
CCN_PASSWORD = os.environ.get("CCN_PASSWORD")

if not CCN_LOGIN:
    raise ValueError("CCN_LOGIN environment variable is not set, set it with 'export CCN_LOGIN=\"your_username\"' \
                     (Linux) or '$env:CCN_LOGIN = \"your_username\"' (PowerShell)")
if not CCN_PASSWORD:
    raise ValueError("CCN_PASSWORD environment variable is not set, set it with 'export CCN_PASSWORD=\"your_password\"' \
                     (Linux) or '$env:CCN_PASSWORD = \"your_password\"' (PowerShell)")

WASSP_PATH = os.environ.get("WASSP_PATH")
if not WASSP_PATH:
    raise ValueError("WASSP_PATH environment variable is not set, set it with 'export WASSP_PATH=\"/path/to/wassp\"' \
                     (Linux) or '$env:WASSP_PATH = \"C:\\path\\to\\wassp\"' (PowerShell)")
if not os.path.isdir(WASSP_PATH):
    raise ValueError("WASSP_PATH '{}' is not a valid directory".format(WASSP_PATH))

session = requests.Session()

# =========================================================================
# STEP 0 — Fetch latest remote references
# Ensures local tracking refs are up to date before any git log lookups.
# =========================================================================
try:
    subprocess.check_output(["git", "fetch", "--all", "--prune"], stderr=subprocess.PIPE, text=True, cwd=WASSP_PATH)
    if DEBUG:
        print("[DEBUG] git fetch --all --prune succeeded")
except subprocess.CalledProcessError as e:
    print("WARNING: git fetch failed: {}".format(e))

# =========================================================================
# STEP 1 — Obtain a login ticket
# Requests a one-time login ticket from SessionService using credentials.
# The ticket is then used for authentication in subsequent requests.
# =========================================================================
login_req = [
    {
        "command": "SessionService.getLoginTicket",
        "args": {
            "login": CCN_LOGIN,
            "password": CCN_PASSWORD
        }
    }
]

resp = session.post(BASE_URL, json=login_req, verify=False)
data = resp.json()

#if DEBUG:
#    print("[DEBUG] LoginTicket response:", json.dumps(data, indent=4))

login_ticket = data[0]["result"]["loginTicket"]
#if DEBUG:
#    print("[DEBUG] Extracted loginTicket:", login_ticket)

# =========================================================================
# FIRST PASS — Collect branch names and file lists for all reviews
# Loops through every review ID, authenticates, fetches the review
# summary, and stores the branch name and file list for later use.
# =========================================================================
ccr_data = []  # ordered list matching REVIEW_IDS (descending / newest first)

for REVIEW_ID in REVIEW_IDS:

    # --- Authenticate and validate the review ---
    validate_req = [
        {
            "command": "SessionService.authenticate",
            "args": {
                "login": CCN_LOGIN,
                "ticket": login_ticket
            }
        },
        {
            "command": "ReviewService.findReviewById",
            "args": {
                "reviewId": REVIEW_ID
            }
        }
    ]

    resp2 = session.post(BASE_URL, json=validate_req, verify=False)
    validate_data = resp2.json()

    # Verify authentication succeeded
    if "errors" in validate_data[0]:
        #print("Authentication failed:", validate_data[0]["errors"])
        exit(1)
    #if DEBUG:
    #    print("[DEBUG] Authentication successful")

    # Verify the review was found
    if "errors" in validate_data[1]:
        #print("Review #{} not found: {}".format(REVIEW_ID, validate_data[1]["errors"]))
        ccr_data.append({"review_id": REVIEW_ID, "branch": None, "files": []})
        continue

    review = validate_data[1].get("result", {})

    if not review:
        #print("Review #{} not found".format(REVIEW_ID))
        ccr_data.append({"review_id": REVIEW_ID, "branch": None, "files": []})
        continue

    #print("Review #{} found: {}".format(REVIEW_ID, review.get("title", "N/A")))

    # Display current vs new values for each field being updated
    #current_fields = {f["name"]: f.get("value", ["N/A"]) for f in review.get("customFields", [])}
    #for entry in CUSTOM_FIELDS:
    #    name = entry["name"]
    #    current = current_fields.get(name, ["N/A"])
    #    new_val = entry["value"]
    #    print("  {}: {} -> {}".format(name, current, new_val))
    #if DEBUG:
    #    print("[DEBUG] Full review data:", json.dumps(review, indent=4, default=str))

    #print()

    # --- Fetch the review summary (files + branch name) ---
    summary_req = [
        {
            "command": "SessionService.authenticate",
            "args": {
                "login": CCN_LOGIN,
                "ticket": login_ticket
            }
        },
        {
            "command": "ReviewService.getReviewSummary",
            "args": {
                "reviewId": REVIEW_ID,
                "clientBuild": "14401"
            }
        }
    ]
    resp_sum = session.post(BASE_URL, json=summary_req, verify=False)
    summary_data = resp_sum.json()

    review_files = []
    if "errors" in summary_data[1]:
        #print("WARNING: Failed to fetch review files:", summary_data[1].get("errors"))
        pass
    else:
        summary = summary_data[1].get("result", {})
        for mat in summary.get("scmMaterials", []):
            changelist = mat.get("consolidatedChangelist", {})
            for f in changelist.get("reviewSummaryFiles", []):
                path = f.get("path", "")
                if path:
                    review_files.append(path)

    # Extract branch name from mergeMessage (text between the first pair of single quotes)
    # Path: result -> pullRequestMerges -> [0] -> mergeMessage
    pull_request_merges = summary.get("pullRequestMerges", [])
    merge_message = pull_request_merges[0].get("mergeMessage", "") if pull_request_merges else ""
    branch_name = None
    if merge_message:
        parts = merge_message.split("'")
        if len(parts) >= 2:
            branch_name = parts[1]

    ccr_data.append({
        "review_id": REVIEW_ID,
        "branch": branch_name,
        "files": review_files
    })

# =========================================================================
# SECOND PASS — Look up git hashes and update reviews
# For each review (newest first), prints the branch name and file list.
# For each file:
#   - current_hash : last commit on this CCR's branch
#   - prev_hash    : last commit on the previous (older) CCR's branch,
#                    walking backwards through older CCRs until found.
# Then applies the field update (Step 3).
# =========================================================================
for idx, entry in enumerate(ccr_data):
    REVIEW_ID = entry["review_id"]
    BRANCH_NAME = entry["branch"]
    REVIEW_FILES = entry["files"]

    # Collect hashes for all files first
    if DEBUG:
        print("[DEBUG] Review #{}: branch='{}', {} files".format(REVIEW_ID, BRANCH_NAME, len(REVIEW_FILES)))
    file_hashes = []
    for fp in REVIEW_FILES:
        # Current hash: last commit on this CCR's branch
        current_hash = "N/A"
        if BRANCH_NAME:
            ref = "origin/" + BRANCH_NAME
            cmd = ["git", "log", ref, "--no-merges", "-n", "1",
                     "--pretty=format:%h", "--", fp]
            if DEBUG:
                print("[DEBUG] cmd: {}".format(" ".join(cmd)))
            try:
                out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE, cwd=WASSP_PATH).strip()
                if DEBUG:
                    print("[DEBUG] stdout: {!r}".format(out))
                if out:
                    current_hash = out
            except subprocess.CalledProcessError as e:
                if DEBUG:
                    print("[DEBUG] git log failed: {}".format(e))
        elif DEBUG:
            print("[DEBUG] No branch name for review #{}, skipping git log".format(REVIEW_ID))

        # Previous hash: walk backwards through older CCRs' branches
        prev_hash = "N/A"
        for j in range(idx + 1, len(ccr_data)):
            older_branch = ccr_data[j]["branch"]
            if not older_branch:
                continue
            ref = "origin/" + older_branch
            cmd = ["git", "log", ref, "--no-merges", "-n", "1",
                     "--pretty=format:%h", "--", fp]
            try:
                out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE, cwd=WASSP_PATH).strip()
                if out:
                    prev_hash = out
                    break
            except subprocess.CalledProcessError:
                continue

        file_hashes.append({"path": fp, "current": current_hash, "prev": prev_hash})

    # Print grouped output: header, latest change list, then previous change list
    #print("===== CCR #{} - {} =====".format(REVIEW_ID, BRANCH_NAME if BRANCH_NAME else "N/A"))
    #
    #print("latest change")
    #for fh in file_hashes:
    #    print("  {} - {}".format(fh["path"], fh["current"]))
    #
    #if len(ccr_data) > 1:
    #    print("")
    #    print("previous change")
    #    for fh in file_hashes:
    #        print("  {} - {}".format(fh["path"], fh["prev"]))
    #
    #print("")

    # =========================================================================
    # STEP 3 — Update the custom fields via ReviewService.editReview
    # Re-authenticates and sends the editReview command with the custom
    # fields specified in the config file.  The "Starting Version(s)" and
    # "Ending Version(s)" fields are dynamically built from the file hashes.
    # Skipped when --dry-run is set.
    # =========================================================================

    # Build "Ending Version(s)" from latest-change hashes (skip N/A)
    ending_entries = []
    for fh in file_hashes:
        if fh["current"] != "N/A":
            display = format_path(fh["path"], MODE)
            if DEBUG:
                print("[DEBUG] Ending: '{}' -> '{}'".format(fh["path"], display))
            ending_entries.append((display, fh["current"]))
    if ending_entries:
        if MODE == "bsp":
            ending_value = group_by_directory(ending_entries)
        else:
            ending_value = "\n".join("{} - {}".format(p, h) for p, h in sorted(ending_entries))
    else:
        ending_value = "N/A"
    if DEBUG:
        print("[DEBUG] Ending Version(s) value:\n{}".format(ending_value))

    # Build "Starting Version(s)" from previous-change hashes (skip N/A)
    starting_entries = []
    for fh in file_hashes:
        if fh["prev"] != "N/A":
            display = format_path(fh["path"], MODE)
            if DEBUG:
                print("[DEBUG] Starting: '{}' -> '{}'".format(fh["path"], display))
            starting_entries.append((display, fh["prev"]))
    if starting_entries:
        if MODE == "bsp":
            starting_value = group_by_directory(starting_entries)
        else:
            starting_value = "\n".join("{} - {}".format(p, h) for p, h in sorted(starting_entries))
    else:
        starting_value = "N/A"
    if DEBUG:
        print("[DEBUG] Starting Version(s) value:\n{}".format(starting_value))

    # Build "Artifact ID(s)" from file paths
    artifact_lines = []
    for fh in file_hashes:
        display = format_path(fh["path"], MODE)
        if DEBUG:
            print("[DEBUG] Artifact: '{}' -> '{}'".format(fh["path"], display))
        artifact_lines.append(display)
    if artifact_lines:
        artifact_value = "\n".join(sorted(artifact_lines) if MODE == "bl" else artifact_lines)
    else:
        artifact_value = "N/A"

    # Merge config fields with the dynamically generated fields
    dynamic_overrides = {
        "Artifact ID(s)": artifact_value,
        "Starting Version(s)": starting_value,
        "Ending Version(s)": ending_value,
    }
    merged_fields = []
    for cf in CUSTOM_FIELDS:
        if cf["name"] in dynamic_overrides:
            merged_fields.append({"name": cf["name"], "value": [dynamic_overrides.pop(cf["name"])]})
        else:
            merged_fields.append(cf)
    # Add any dynamic fields not already in the config
    for name, val in dynamic_overrides.items():
        merged_fields.append({"name": name, "value": [val]})

    update_req = [
        {
            "command": "SessionService.authenticate",
            "args": {
                "login": CCN_LOGIN,
                "ticket": login_ticket
            }
        },
        {
            "command": "ReviewService.editReview",
            "args": {
                "reviewId": REVIEW_ID,
                "customFields": merged_fields
            }
        }
    ]

    # Skip update for non-first reviews when --update-most-recent is set
    if UPDATE_MOST_RECENT and idx > 0:
        if DEBUG:
            print("[DEBUG] Skipping update for review #{} (--update-most-recent)".format(REVIEW_ID))
        continue

    # --- 4000-char field guard ---
    oversized = [(mf["name"], len(mf["value"][0])) for mf in merged_fields if len(mf["value"][0]) > 4000]
    if oversized:
        results_dir = os.path.join(".", "{}_results".format(REVIEW_ID))
        os.makedirs(results_dir, exist_ok=True)
        for mf in merged_fields:
            safe_name = re.sub(r'[^\w]+', '_', mf["name"]).strip('_')
            out_path = os.path.join(results_dir, "{}_{}.txt".format(REVIEW_ID, safe_name))
            with open(out_path, "w") as fout:
                fout.write(mf["value"][0])
        for fname, flen in oversized:
            print("WARNING: Field '{}' exceeds 4000 characters ({} chars).".format(fname, flen))
        print("Skipping update for review #{}. Field values written to {}".format(REVIEW_ID, results_dir))
        print()
        continue

    if DRY_RUN:
        print("[DRY RUN] Would update review #{}:".format(REVIEW_ID))
        for mf in merged_fields:
            print("  {} -> {}".format(mf["name"], mf["value"][0]))
    else:
        resp4 = session.post(BASE_URL, json=update_req, verify=False)
        update_data = resp4.json()
        if "errors" in update_data[1]:
            print("Update failed:", update_data[1]["errors"])
        else:
            print("Review #{} updated successfully.".format(REVIEW_ID))

    print()  # blank line between reviews