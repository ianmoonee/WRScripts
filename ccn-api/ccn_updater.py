"""
CCN CodeCollaborator Review Updater
====================================

Updates custom fields on a Collaborator review via the JSON API (v1)
and lists all files attached to the review.  The fields to update are
specified in a JSON config file — only the fields present in the file
will be modified.

Modification History
--------------------
05mar26,pse  Initial version

Prerequisites:
    - Set environment variables before running:
        export CCN_LOGIN="your_username"        (Linux)
        export CCN_PASSWORD="your_password"      (Linux)
        $env:CCN_LOGIN = "your_username"        (PowerShell)
        $env:CCN_PASSWORD = "your_password"      (PowerShell)

Usage:
    python ccn_updater.py --review-id 31859 --config fields.json
    python ccn_updater.py --review-id 31859 --config fields.json --dry-run
    python ccn_updater.py --review-id 31859 --config fields.json --debug

Arguments:
    --review-id    The numeric ID of the review to update.
    --config       Path to a JSON file with custom field values to set.
    --dry-run      Validate only, do not apply changes.
    --debug        Enable verbose debug output.

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
import argparse
import subprocess
import urllib3

# Suppress SSL/TLS InsecureRequestWarning messages
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Parse command-line arguments ---
parser = argparse.ArgumentParser(description="Update custom fields on a Collaborator review.")
parser.add_argument("--review-id", type=int, required=True, help="The numeric ID of the review to update.")
parser.add_argument("--config", type=str, required=True, help="Path to JSON file with custom field values.")
parser.add_argument("--dry-run", action="store_true", help="Validate the review without applying changes.")
parser.add_argument("--debug", action="store_true", help="Enable verbose debug output.")
args = parser.parse_args()

DRY_RUN = args.dry_run
DEBUG = args.debug
REVIEW_ID = args.review_id

# --- Load custom fields from JSON config file ---
config_path = args.config
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

session = requests.Session()

# =========================================================================
# STEP 0 — Fetch latest remote references
# Ensures local tracking refs are up to date before any git log lookups.
# =========================================================================
try:
    subprocess.check_output(["git", "fetch", "--all", "--prune"], stderr=subprocess.PIPE, text=True)
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

if DEBUG:
    print("[DEBUG] LoginTicket response:", json.dumps(data, indent=4))

login_ticket = data[0]["result"]["loginTicket"]
if DEBUG:
    print("[DEBUG] Extracted loginTicket:", login_ticket)

# =========================================================================
# STEP 2 — Authenticate and validate the review
# Authenticates the session using the login ticket, then looks up the
# review by ID.  Displays current vs new values for each custom field
# that will be updated.
# =========================================================================
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
    print("Authentication failed:", validate_data[0]["errors"])
    exit(1)
if DEBUG:
    print("[DEBUG] Authentication successful")

# Verify the review was found
if "errors" in validate_data[1]:
    print("Review #{} not found: {}".format(REVIEW_ID, validate_data[1]["errors"]))
    exit(1)

review = validate_data[1].get("result", {})

if not review:
    print("Review #{} not found".format(REVIEW_ID))
    exit(1)

print("Review #{} found: {}".format(REVIEW_ID, review.get("title", "N/A")))

# Display current vs new values for each field being updated
current_fields = {f["name"]: f.get("value", ["N/A"]) for f in review.get("customFields", [])}
for entry in CUSTOM_FIELDS:
    name = entry["name"]
    current = current_fields.get(name, ["N/A"])
    new_val = entry["value"]
    print("  {}: {} -> {}".format(name, current, new_val))
if DEBUG:
    print("[DEBUG] Full review data:", json.dumps(review, indent=4, default=str))

print()

# =========================================================================
# STEP 2b — Fetch the list of files attached to the review
# Uses ReviewService.getReviewSummary to extract file paths from
# scmMaterials[*].consolidatedChangelist.reviewSummaryFiles[*].path.
# Results are stored in REVIEW_FILES (list of path strings).
# =========================================================================
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

REVIEW_FILES = []
BRANCH_NAME = None
if "errors" in summary_data[1]:
    print("WARNING: Failed to fetch review files:", summary_data[1].get("errors"))
else:
    summary = summary_data[1].get("result", {})
    for mat in summary.get("scmMaterials", []):
        changelist = mat.get("consolidatedChangelist", {})
        for f in changelist.get("reviewSummaryFiles", []):
            path = f.get("path", "")
            if path:
                REVIEW_FILES.append(path)

    # Extract branch name from mergeMessage (text between the first pair of single quotes)
    pull_request_merges = summary.get("pullRequestMerges", [])
    merge_message = pull_request_merges[0].get("mergeMessage", "") if pull_request_merges else ""
    if merge_message:
        parts = merge_message.split("'")
        if len(parts) >= 2:
            BRANCH_NAME = parts[1]
    if DEBUG:
        print("[DEBUG] mergeMessage: {!r}".format(merge_message))
        print("[DEBUG] Extracted branch name: {}".format(BRANCH_NAME))

REVIEW_FILES.sort()

# =========================================================================
# STEP 2c — Look up first and last commit hashes for each file
# For each file, queries git log on the review branch to find:
#   - last commit  (newest): git log origin/BRANCH --no-merges -n 1
#   - first commit (oldest): git log origin/BRANCH --no-merges --reverse
# =========================================================================
file_hashes = []
for fp in REVIEW_FILES:
    first_hash = "N/A"
    last_hash = "N/A"
    if BRANCH_NAME:
        ref = "origin/" + BRANCH_NAME
        # Last (newest) commit
        cmd_last = ["git", "log", ref, "--no-merges", "-n", "1",
                    "--pretty=format:%h", "--", fp]
        if DEBUG:
            print("[DEBUG] cmd (last): {}".format(" ".join(cmd_last)))
        try:
            out = subprocess.check_output(cmd_last, text=True, stderr=subprocess.PIPE).strip()
            if DEBUG:
                print("[DEBUG] stdout (last): {!r}".format(out))
            if out:
                last_hash = out
        except subprocess.CalledProcessError as e:
            if DEBUG:
                print("[DEBUG] git log (last) failed: {}".format(e))

        # First (oldest) commit
        cmd_first = ["git", "log", ref, "--no-merges", "--reverse",
                     "--pretty=format:%h", "--", fp]
        if DEBUG:
            print("[DEBUG] cmd (first): {}".format(" ".join(cmd_first)))
        try:
            out = subprocess.check_output(cmd_first, text=True, stderr=subprocess.PIPE).strip()
            if DEBUG:
                print("[DEBUG] stdout (first): {!r}".format(out))
            if out:
                first_hash = out.split("\n")[0]
        except subprocess.CalledProcessError as e:
            if DEBUG:
                print("[DEBUG] git log (first) failed: {}".format(e))
    elif DEBUG:
        print("[DEBUG] No branch name for review #{}, skipping git log".format(REVIEW_ID))

    file_hashes.append({"path": fp, "first": first_hash, "last": last_hash})

# --- Print the three lists ---
print("Files in review #{} ({} files):".format(REVIEW_ID, len(REVIEW_FILES)))
for fp in REVIEW_FILES:
    print("  - {}".format(fp))

print()
print("Files with first commit hash:")
for fh in file_hashes:
    print("  - {} - {}".format(fh["path"], fh["first"]))

print()
print("Files with last commit hash:")
for fh in file_hashes:
    print("  - {} - {}".format(fh["path"], fh["last"]))

print()

# =========================================================================
# STEP 3 — Update the custom fields via ReviewService.editReview
# Re-authenticates and sends the editReview command with the custom
# fields specified in the config file.  Skipped when --dry-run is set.
# =========================================================================
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
            "customFields": CUSTOM_FIELDS
        }
    }
]

if DEBUG:
    print("[DEBUG] Update request:", json.dumps(update_req, indent=4))

if DRY_RUN:
    field_summary = ", ".join("'{}' -> {}".format(f["name"], f["value"]) for f in CUSTOM_FIELDS)
    print("[DRY RUN] Would update review #{}: {}".format(REVIEW_ID, field_summary))
else:
    resp4 = session.post(BASE_URL, json=update_req, verify=False)
    update_data = resp4.json()
    if "errors" in update_data[1]:
        print("Update failed:", update_data[1]["errors"])
    else:
        print("Review #{} updated successfully.".format(REVIEW_ID))
        if DEBUG:
            print("[DEBUG] Update response:", json.dumps(update_data[1], indent=4))