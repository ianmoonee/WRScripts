"""
CCN Diagnostic Tool
====================

Tests each step of the CCR access chain to identify where access fails
for completed or inaccessible reviews.  Useful for debugging before
running ccn_updater.py.

Usage:
    python ccn_diagnostic.py --review-id 31859
    python ccn_diagnostic.py --review-id 31859 31600 31280

Prerequisites:
    Same as ccn_updater.py:
        export CCN_LOGIN="your_username"
        export CCN_PASSWORD="your_password"
"""

import argparse
import json
import os
import re

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://ccn-codecolab.wrs.com:8443/services/json/v1"
REPORT_BASE_URL = "https://ccn-codecolab.wrs.com:8443"

parser = argparse.ArgumentParser(description="Diagnose CCR access for one or more review IDs.")
parser.add_argument("--review-id", type=int, nargs="+", required=True, help="Review IDs to test.")
args = parser.parse_args()

CCN_LOGIN = os.environ.get("CCN_LOGIN")
CCN_PASSWORD = os.environ.get("CCN_PASSWORD")
if not CCN_LOGIN or not CCN_PASSWORD:
    print("ERROR: CCN_LOGIN and CCN_PASSWORD environment variables must be set.")
    exit(1)

session = requests.Session()

# --- Step 1: Get login ticket ---
print("=" * 70)
print("Step 1: Obtaining login ticket...")
login_req = [
    {
        "command": "SessionService.getLoginTicket",
        "args": {"login": CCN_LOGIN, "password": CCN_PASSWORD},
    }
]
resp = session.post(BASE_URL, json=login_req, verify=False)
data = resp.json()
if "errors" in data[0]:
    print("  FAIL: Could not get login ticket: {}".format(data[0]["errors"]))
    exit(1)
login_ticket = data[0]["result"]["loginTicket"]
print("  OK: Login ticket obtained.")
print()

# --- Results table ---
results = []

for review_id in args.review_id:
    print("=" * 70)
    print("Testing CCR #{}".format(review_id))
    print("=" * 70)

    result = {
        "review_id": review_id,
        "api_find": False,
        "api_summary": False,
        "branch_found": False,
        "branch_name": None,
        "file_count": 0,
        "title_mr_number": None,
        "report_access": False,
        "report_mr_number": None,
    }

    # --- Step 2: findReviewById ---
    print("\n  Step 2: ReviewService.findReviewById...")
    validate_req = [
        {
            "command": "SessionService.authenticate",
            "args": {"login": CCN_LOGIN, "ticket": login_ticket},
        },
        {"command": "ReviewService.findReviewById", "args": {"reviewId": review_id}},
    ]
    resp2 = session.post(BASE_URL, json=validate_req, verify=False)
    validate_data = resp2.json()

    if "errors" in validate_data[0]:
        print("    FAIL: Authentication failed: {}".format(validate_data[0]["errors"]))
        results.append(result)
        continue

    if "errors" in validate_data[1]:
        print("    FAIL: {}".format(validate_data[1]["errors"]))
    else:
        review = validate_data[1].get("result", {})
        if review:
            result["api_find"] = True
            title = review.get("title", "N/A")
            print("    OK: Review found - '{}'".format(title))
            print("    Phase: {}".format(review.get("phase", {}).get("phaseName", "N/A")))
            # Extract MR number from review title
            for pattern in [
                r'[Mm]erge\s+[Rr]equest\s*[#!](\d+)',
                r'MR\s*[#!](\d+)',
                r'#(\d+)',
            ]:
                match = re.search(pattern, title)
                if match:
                    result["title_mr_number"] = match.group(1)
                    print("    MR from title: !{}".format(match.group(1)))
                    break
            if not result["title_mr_number"]:
                print("    WARN: No MR number found in title")
        else:
            print("    FAIL: Empty result")

    # --- Step 3: getReviewSummary ---
    print("\n  Step 3: ReviewService.getReviewSummary...")
    summary_req = [
        {
            "command": "SessionService.authenticate",
            "args": {"login": CCN_LOGIN, "ticket": login_ticket},
        },
        {
            "command": "ReviewService.getReviewSummary",
            "args": {"reviewId": review_id, "clientBuild": "14401"},
        },
    ]
    resp_sum = session.post(BASE_URL, json=summary_req, verify=False)
    summary_data = resp_sum.json()

    if "errors" in summary_data[1]:
        print("    FAIL: {}".format(summary_data[1]["errors"]))
    else:
        summary = summary_data[1].get("result", {})
        result["api_summary"] = True
        print("    OK: Summary retrieved (keys: {})".format(list(summary.keys())))

        # Files
        file_count = 0
        for mat in summary.get("scmMaterials", []):
            changelist = mat.get("consolidatedChangelist", {})
            files = changelist.get("reviewSummaryFiles", [])
            file_count += len(files)
        result["file_count"] = file_count
        print("    Files found: {}".format(file_count))

        # Branch from pullRequestMerges
        pull_request_merges = summary.get("pullRequestMerges", [])
        print("    pullRequestMerges count: {}".format(len(pull_request_merges)))
        if pull_request_merges:
            merge_msg = pull_request_merges[0].get("mergeMessage", "")
            print("    mergeMessage: '{}'".format(merge_msg[:200]))
            parts = merge_msg.split("'")
            if len(parts) >= 2:
                result["branch_found"] = True
                result["branch_name"] = parts[1]
                print("    Branch extracted: '{}'".format(parts[1]))
            else:
                print("    WARN: Could not extract branch from mergeMessage")
        else:
            print("    WARN: No pullRequestMerges in summary")

    # --- Step 4: Report URL access ---
    print("\n  Step 4: ReviewDetailReport (web URL)...")
    report_url = (
        "{}/go?formSubmittedreportConfig=1"
        "&pv_component=ErrorsAndMessages"
        "&page=ReviewDetailReport"
        "&pv_ErrorsAndMessages_fingerPrint=861101"
        "&offsetX=0&offsetY=0"
        "&data-format=report_html"
        "&reviewId={}"
        "&defectsFormat=0"
        "&materialsFormat=0"
        "&checklistHistory=NO"
        "&commentsFormat=0"
        "&fileActivity=NO"
        "&buttonSubmit=Run"
    ).format(REPORT_BASE_URL, review_id)
    try:
        resp_report = session.get(report_url, verify=False, timeout=30)
        print("    HTTP status: {}".format(resp_report.status_code))
        if resp_report.status_code == 200:
            result["report_access"] = True
            html = resp_report.text
            print("    Response length: {} chars".format(len(html)))

            # Try to find MR number patterns in the HTML
            # Look for patterns like "Merge Request !123" or "MR !123" or "#123"
            mr_patterns = [
                r'[Mm]erge\s+[Rr]equest\s*!(\d+)',
                r'MR\s*!(\d+)',
                r'!\s*(\d+)',
            ]
            for pattern in mr_patterns:
                matches = re.findall(pattern, html)
                if matches:
                    result["report_mr_number"] = matches[0]
                    print("    MR number found (pattern '{}'): !{}".format(pattern, matches[0]))
                    break

            if not result["report_mr_number"]:
                print("    WARN: No MR number found in report HTML")
                # Dump first 2000 chars for inspection
                print("    --- HTML preview (first 2000 chars) ---")
                print(html[:2000])
                print("    --- end preview ---")
        else:
            print("    FAIL: HTTP {}".format(resp_report.status_code))
    except requests.RequestException as e:
        print("    FAIL: Request error: {}".format(e))

    results.append(result)
    print()

# --- Summary table ---
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
header = "{:<12} {:<10} {:<10} {:<10} {:<30} {:<7} {:<10} {:<10} {:<8}".format(
    "Review ID", "API Find", "API Sum.", "Branch", "Branch Name", "Files", "Title MR", "Report", "MR #"
)
print(header)
print("-" * len(header))
for r in results:
    print(
        "{:<12} {:<10} {:<10} {:<10} {:<30} {:<7} {:<10} {:<10} {:<8}".format(
            r["review_id"],
            "OK" if r["api_find"] else "FAIL",
            "OK" if r["api_summary"] else "FAIL",
            "OK" if r["branch_found"] else "FAIL",
            r["branch_name"] or "N/A",
            r["file_count"],
            "!{}".format(r["title_mr_number"]) if r["title_mr_number"] else "N/A",
            "OK" if r["report_access"] else "FAIL",
            "!{}".format(r["report_mr_number"]) if r["report_mr_number"] else "N/A",
        )
    )
print()
