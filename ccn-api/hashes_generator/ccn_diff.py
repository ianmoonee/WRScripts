"""
CCN CodeCollaborator Review Field Diff
=======================================

Computes what ccn_updater.py *would* write to the dynamic CCR fields
(Artifact ID(s), Starting Version(s), Ending Version(s)), fetches their
current values from the review, and displays a colored unified diff.

Usage:
    python ccn_diff.py --review-id 31859 --bsp
    python ccn_diff.py --review-id 31859 --bl
    python ccn_diff.py --review-id 31859 31280 --bsp --update-most-recent
    python ccn_diff.py --review-id 31859 --bsp --no-color
    python ccn_diff.py --review-id 31859 --bsp --debug

Prerequisites (same as ccn_updater.py):
    export CCN_LOGIN="your_username"
    export CCN_PASSWORD="your_password"
    export WASSP_PATH="/path/to/wassp"
"""

import argparse
import difflib
import html as html_module
import json
import os
import subprocess
import sys

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

class _Colors:
    RED = "\033[91m"
    GREEN = "\033[92m"
    CYAN = "\033[96m"
    RESET = "\033[0m"


NO_COLOR = False  # set by CLI
HTML_LINES = None  # set to a list when --html is used


def _colorize(text, color):
    if NO_COLOR:
        return text
    return "{}{}{}".format(color, text, _Colors.RESET)


# ---------------------------------------------------------------------------
# HTML output helpers
# ---------------------------------------------------------------------------

_HTML_COLOR_MAP = {
    _Colors.RED: "#e06c75",
    _Colors.GREEN: "#98c379",
    _Colors.CYAN: "#56b6c2",
}


def _html_span(text, color):
    """Wrap text in a colored <span> for HTML output."""
    escaped = html_module.escape(text)
    hex_color = _HTML_COLOR_MAP.get(color, "#abb2bf")
    return '<span style="color:{}">{}</span>'.format(hex_color, escaped)


def _html_line(text, color=None):
    """Append a line to the HTML buffer."""
    if HTML_LINES is None:
        return
    if color:
        HTML_LINES.append(_html_span(text, color))
    else:
        HTML_LINES.append(html_module.escape(text))


def _write_html_file(filepath, title):
    """Write the accumulated HTML_LINES to a file."""
    body = "<br>\n".join(HTML_LINES)
    content = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ background: #282c34; color: #abb2bf; font-family: monospace; font-size: 14px; padding: 20px; white-space: pre-wrap; }}
</style>
</head>
<body>
{body}
</body>
</html>
""".format(title=html_module.escape(title), body=body)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print("HTML diff written to: {}".format(filepath))


# ---------------------------------------------------------------------------
# Path helpers (copied from ccn_updater.py)
# ---------------------------------------------------------------------------

def shorten_path(path):
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


def _has_code_files(filenames):
    for name in filenames:
        base = name.rsplit("/", 1)[-1]
        if base.startswith("tl_") or base.startswith("tp_"):
            return True
    return False


def _classify_dirs(dir_to_filenames):
    code_dirs = set()
    for d, filenames in dir_to_filenames.items():
        if _has_code_files(filenames):
            code_dirs.add(d)
    for d in list(dir_to_filenames.keys()):
        if d in code_dirs:
            continue
        prefix = d + "/" if d else ""
        for cd in list(code_dirs):
            if prefix and cd.startswith(prefix):
                code_dirs.add(d)
                break

    aux = sorted(d for d in dir_to_filenames if d not in code_dirs)
    code = sorted(d for d in dir_to_filenames if d in code_dirs)
    return aux, code


def group_by_directory(entries):
    from collections import OrderedDict

    groups = OrderedDict()
    filenames_by_dir = {}
    for short_path, val in entries:
        normalized = short_path.replace("\\", "/")
        parts = normalized.rsplit("/", 1)
        if len(parts) == 2:
            directory, filename = parts
        else:
            directory, filename = "", parts[0]
        groups.setdefault(directory, []).append("{} - {}".format(normalized, val))
        filenames_by_dir.setdefault(directory, []).append(filename)

    aux_dirs, code_dirs = _classify_dirs(filenames_by_dir)

    blocks = []
    for directory in aux_dirs + code_dirs:
        lines = sorted(groups[directory])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def group_paths_by_directory(paths):
    from collections import OrderedDict

    groups = OrderedDict()
    filenames_by_dir = {}
    for short_path in paths:
        normalized = short_path.replace("\\", "/")
        parts = normalized.rsplit("/", 1)
        if len(parts) == 2:
            directory, filename = parts
        else:
            directory, filename = "", parts[0]
        groups.setdefault(directory, []).append(normalized)
        filenames_by_dir.setdefault(directory, []).append(filename)

    aux_dirs, code_dirs = _classify_dirs(filenames_by_dir)

    blocks = []
    for directory in aux_dirs + code_dirs:
        lines = sorted(groups[directory])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_path(path, mode):
    if mode == "bsp":
        return shorten_path(path)
    return path.replace("\\", "/")


# ---------------------------------------------------------------------------
# Diff display
# ---------------------------------------------------------------------------

def _normalize_lines(text):
    """Sort non-blank lines and strip blank separators for order-independent comparison."""
    lines = [l for l in text.splitlines() if l.strip()]
    lines.sort()
    return lines


def print_field_diff(field_name, current_value, new_value):
    """Print a colored unified diff for a single field (order-independent). Returns (adds, removes)."""
    current_lines = _normalize_lines(current_value)
    new_lines = _normalize_lines(new_value)

    diff = list(difflib.unified_diff(
        current_lines, new_lines,
        fromfile="CCR current: {}".format(field_name),
        tofile="Computed new: {}".format(field_name),
        lineterm="",
    ))

    to_terminal = HTML_LINES is None

    if not diff:
        msg = "  {} \u2014 no differences".format(field_name)
        if to_terminal:
            print("  {} \u2014 {}".format(field_name, _colorize("no differences", _Colors.CYAN)))
        _html_line(msg, _Colors.CYAN)
        return 0, 0

    adds = 0
    removes = 0
    if to_terminal:
        print()
    _html_line("")
    for line in diff:
        stripped = line.rstrip("\n")
        if stripped.startswith("---"):
            if to_terminal:
                print("  " + _colorize(stripped, _Colors.RED))
            _html_line("  " + stripped, _Colors.RED)
        elif stripped.startswith("+++"):
            if to_terminal:
                print("  " + _colorize(stripped, _Colors.GREEN))
            _html_line("  " + stripped, _Colors.GREEN)
        elif stripped.startswith("@@"):
            if to_terminal:
                print("  " + _colorize(stripped, _Colors.CYAN))
            _html_line("  " + stripped, _Colors.CYAN)
        elif stripped.startswith("-"):
            removes += 1
            if to_terminal:
                print("  " + _colorize(stripped, _Colors.RED))
            _html_line("  " + stripped, _Colors.RED)
        elif stripped.startswith("+"):
            adds += 1
            if to_terminal:
                print("  " + _colorize(stripped, _Colors.GREEN))
            _html_line("  " + stripped, _Colors.GREEN)
        else:
            if to_terminal:
                print("  " + stripped)
            _html_line("  " + stripped)
    if to_terminal:
        print()
    _html_line("")
    return adds, removes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global NO_COLOR
    global HTML_LINES

    parser = argparse.ArgumentParser(
        description="Show differences between current CCR field values and what ccn_updater would compute.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--review-id", type=int, nargs="+", required=True,
                        help="One or more numeric IDs of the reviews to diff.")
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--bsp", action="store_true",
                            help="BSP mode: use shortened file paths grouped by directory.")
    mode_group.add_argument("--bl", action="store_true",
                            help="BL mode: use full file paths in a flat sorted list.")
    parser.add_argument("--config", type=str, default=None,
                        help="Path to JSON config file (default: fields.json in script dir).")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug output.")
    parser.add_argument("--update-most-recent", action="store_true",
                        help="Only diff the newest review; use the rest for previous-hash lookups.")
    parser.add_argument("--component", type=str, default=None,
                        help="Component name (for informational display only in diff mode).")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable colored output.")
    parser.add_argument("--html", type=str, default=None, metavar="FILE",
                        help="Write colored diff output to an HTML file.")
    args = parser.parse_args()

    # Color setup
    NO_COLOR = args.no_color or not sys.stdout.isatty()

    # HTML setup
    if args.html:
        HTML_LINES = []

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
        sys.exit(1)

    with open(config_path, "r") as f:
        field_values = json.load(f)

    if not isinstance(field_values, dict) or not field_values:
        print("ERROR: Config file must be a non-empty JSON object.")
        sys.exit(1)

    CUSTOM_FIELDS = [{"name": name, "value": [val]} for name, val in field_values.items()]

    # --- API configuration and credential validation ---
    BASE_URL = "https://ccn-codecolab.wrs.com:8443/services/json/v1"
    CCN_LOGIN = os.environ.get("CCN_LOGIN")
    CCN_PASSWORD = os.environ.get("CCN_PASSWORD")

    if not CCN_LOGIN:
        print("ERROR: CCN_LOGIN environment variable is not set.")
        sys.exit(1)
    if not CCN_PASSWORD:
        print("ERROR: CCN_PASSWORD environment variable is not set.")
        sys.exit(1)

    WASSP_PATH = os.environ.get("WASSP_PATH")
    if not WASSP_PATH:
        print("ERROR: WASSP_PATH environment variable is not set.")
        sys.exit(1)
    if not os.path.isdir(WASSP_PATH):
        print("ERROR: WASSP_PATH '{}' is not a valid directory.".format(WASSP_PATH))
        sys.exit(1)

    session = requests.Session()

    # =========================================================================
    # STEP 0 — Fetch latest remote references
    # =========================================================================
    try:
        subprocess.check_output(
            ["git", "fetch", "--all", "--prune"], stderr=subprocess.PIPE, text=True, cwd=WASSP_PATH
        )
        if DEBUG:
            print("[DEBUG] git fetch --all --prune succeeded")
    except subprocess.CalledProcessError as e:
        print("WARNING: git fetch failed: {}".format(e))

    # =========================================================================
    # STEP 1 — Obtain a login ticket
    # =========================================================================
    login_req = [
        {
            "command": "SessionService.getLoginTicket",
            "args": {"login": CCN_LOGIN, "password": CCN_PASSWORD},
        }
    ]

    resp = session.post(BASE_URL, json=login_req, verify=False)
    data = resp.json()
    login_ticket = data[0]["result"]["loginTicket"]

    # =========================================================================
    # FIRST PASS — Collect branch names, file lists, and current field values
    # =========================================================================
    ccr_data = []

    for REVIEW_ID in REVIEW_IDS:
        validate_req = [
            {
                "command": "SessionService.authenticate",
                "args": {"login": CCN_LOGIN, "ticket": login_ticket},
            },
            {"command": "ReviewService.findReviewById", "args": {"reviewId": REVIEW_ID}},
        ]

        resp2 = session.post(BASE_URL, json=validate_req, verify=False)
        validate_data = resp2.json()

        if "errors" in validate_data[0]:
            print("ERROR: Authentication failed for review #{}".format(REVIEW_ID))
            sys.exit(1)

        if "errors" in validate_data[1]:
            ccr_data.append({"review_id": REVIEW_ID, "branch": None, "files": [], "current_fields": {}})
            continue

        review = validate_data[1].get("result", {})
        if not review:
            ccr_data.append({"review_id": REVIEW_ID, "branch": None, "files": [], "current_fields": {}})
            continue

        # Extract current custom field values
        current_fields = {}
        for f in review.get("customFields", []):
            val_list = f.get("value", [""])
            current_fields[f["name"]] = val_list[0] if val_list else ""

        # Fetch the review summary (files + branch name)
        summary_req = [
            {
                "command": "SessionService.authenticate",
                "args": {"login": CCN_LOGIN, "ticket": login_ticket},
            },
            {
                "command": "ReviewService.getReviewSummary",
                "args": {"reviewId": REVIEW_ID, "clientBuild": "14401"},
            },
        ]
        resp_sum = session.post(BASE_URL, json=summary_req, verify=False)
        summary_data = resp_sum.json()

        review_files = []
        branch_name = None

        if "errors" not in summary_data[1]:
            summary = summary_data[1].get("result", {})
            if DEBUG:
                print("[DEBUG] Review #{} summary top-level keys: {}".format(
                    REVIEW_ID, list(summary.keys())))
                for i, mat in enumerate(summary.get("scmMaterials", [])):
                    print("[DEBUG]   scmMaterials[{}] keys: {}".format(i, list(mat.keys())))
                    changelist = mat.get("consolidatedChangelist", {})
                    print("[DEBUG]     consolidatedChangelist keys: {}".format(list(changelist.keys())))
                    for j, f in enumerate(changelist.get("reviewSummaryFiles", [])):
                        print("[DEBUG]       reviewSummaryFiles[{}] keys: {} -> {}".format(
                            j, list(f.keys()), json.dumps(f, indent=8, default=str)))
                        if j >= 2:
                            print("[DEBUG]       ... ({} more files)".format(
                                len(changelist.get("reviewSummaryFiles", [])) - 3))
                            break

            for mat in summary.get("scmMaterials", []):
                changelist = mat.get("consolidatedChangelist", {})
                for f in changelist.get("reviewSummaryFiles", []):
                    path = f.get("path", "")
                    change_type = str(f.get("changeType", "")).upper()
                    if change_type == "REVERTED":
                        if DEBUG:
                            print("[DEBUG]       Skipping REVERTED file: {}".format(path))
                        continue
                    if path:
                        review_files.append(path)

            # Extract branch name from mergeMessage
            pull_request_merges = summary.get("pullRequestMerges", [])
            merge_message = pull_request_merges[0].get("mergeMessage", "") if pull_request_merges else ""
            if merge_message:
                parts = merge_message.split("'")
                if len(parts) >= 2:
                    branch_name = parts[1]

        ccr_data.append({
            "review_id": REVIEW_ID,
            "branch": branch_name,
            "files": review_files,
            "current_fields": current_fields,
        })

    # =========================================================================
    # SECOND PASS — Compute hashes and display diffs
    # =========================================================================
    for idx, entry in enumerate(ccr_data):
        REVIEW_ID = entry["review_id"]
        BRANCH_NAME = entry["branch"]
        REVIEW_FILES = entry["files"]
        current_fields = entry["current_fields"]

        # Skip non-first reviews when --update-most-recent
        if UPDATE_MOST_RECENT and idx > 0:
            if DEBUG:
                print("[DEBUG] Skipping diff for review #{} (--update-most-recent)".format(REVIEW_ID))
            continue

        if DEBUG:
            print("[DEBUG] Review #{}: branch='{}', {} files".format(
                REVIEW_ID, BRANCH_NAME, len(REVIEW_FILES)))

        # --- Compute hashes for all files ---
        file_hashes = []
        for fp in REVIEW_FILES:
            current_hash = "N/A"
            if BRANCH_NAME:
                ref = "origin/" + BRANCH_NAME
                cmd = ["git", "log", ref, "--no-merges", "-n", "1", "--pretty=format:%h", "--", fp]
                if DEBUG:
                    print("[DEBUG] cmd: {}".format(" ".join(cmd)))
                try:
                    out = subprocess.check_output(
                        cmd, text=True, stderr=subprocess.PIPE, cwd=WASSP_PATH
                    ).strip()
                    if DEBUG:
                        print("[DEBUG] stdout: {!r}".format(out))
                    if out:
                        current_hash = out
                except subprocess.CalledProcessError as e:
                    if DEBUG:
                        print("[DEBUG] git log failed: {}".format(e))
            elif DEBUG:
                print("[DEBUG] No branch name for review #{}, skipping git log".format(REVIEW_ID))

            prev_hash = "N/A"
            for j in range(idx + 1, len(ccr_data)):
                older_branch = ccr_data[j]["branch"]
                if not older_branch:
                    continue
                ref = "origin/" + older_branch
                cmd = ["git", "log", ref, "--no-merges", "-n", "1", "--pretty=format:%h", "--", fp]
                try:
                    out = subprocess.check_output(
                        cmd, text=True, stderr=subprocess.PIPE, cwd=WASSP_PATH
                    ).strip()
                    if out:
                        prev_hash = out
                        break
                except subprocess.CalledProcessError:
                    continue

            # Fallback: oldest commit on current branch
            if prev_hash == "N/A" and BRANCH_NAME:
                ref = "origin/" + BRANCH_NAME
                cmd = ["git", "log", ref, "--no-merges", "--pretty=format:%h", "--", fp]
                if DEBUG:
                    print("[DEBUG] prev_hash fallback cmd: {}".format(" ".join(cmd)))
                try:
                    out = subprocess.check_output(
                        cmd, text=True, stderr=subprocess.PIPE, cwd=WASSP_PATH
                    ).strip()
                    if out:
                        prev_hash = out.splitlines()[-1]
                        if DEBUG:
                            print("[DEBUG] prev_hash fallback result: {!r}".format(prev_hash))
                except subprocess.CalledProcessError as e:
                    if DEBUG:
                        print("[DEBUG] prev_hash fallback failed: {}".format(e))

            file_hashes.append({"path": fp, "current": current_hash, "prev": prev_hash})

        # --- Build computed field values ---
        ending_entries = []
        for fh in file_hashes:
            display = format_path(fh["path"], MODE)
            ending_entries.append((display, fh["current"]))
        if ending_entries:
            if MODE == "bsp":
                ending_value = group_by_directory(ending_entries)
            else:
                ending_value = "\n".join("{} - {}".format(p, h) for p, h in sorted(ending_entries))
        else:
            ending_value = ""

        starting_entries = []
        for fh in file_hashes:
            display = format_path(fh["path"], MODE)
            starting_entries.append((display, fh["prev"]))
        if starting_entries:
            if MODE == "bsp":
                starting_value = group_by_directory(starting_entries)
            else:
                starting_value = "\n".join("{} - {}".format(p, h) for p, h in sorted(starting_entries))
        else:
            starting_value = ""

        artifact_lines = []
        for fh in file_hashes:
            display = format_path(fh["path"], MODE)
            artifact_lines.append(display)
        if artifact_lines:
            if MODE == "bsp":
                artifact_value = group_paths_by_directory(artifact_lines)
            else:
                artifact_value = "\n".join(sorted(artifact_lines))
        else:
            artifact_value = ""

        # --- Get current values from CCR ---
        current_artifact = current_fields.get("Artifact ID(s)", "")
        current_starting = current_fields.get("Starting Version(s)", "")
        current_ending = current_fields.get("Ending Version(s)", "")

        # --- Display diffs ---
        to_terminal = HTML_LINES is None
        header = "============================== CCR #{} — {} ==============================".format(
            REVIEW_ID, BRANCH_NAME if BRANCH_NAME else "N/A")
        if to_terminal:
            print(_colorize(header, _Colors.CYAN))
            print()
        else:
            print("Processing CCR #{}...".format(REVIEW_ID))
        _html_line(header, _Colors.CYAN)
        _html_line("")

        total_adds = 0
        total_removes = 0

        # Dynamic fields (computed from git hashes)
        for field_name, current_val, new_val in [
            ("Artifact ID(s)", current_artifact, artifact_value),
            ("Starting Version(s)", current_starting, starting_value),
            ("Ending Version(s)", current_ending, ending_value),
        ]:
            adds, removes = print_field_diff(field_name, current_val, new_val)
            total_adds += adds
            total_removes += removes

        # Static fields from config (e.g. "Related document ID(s) and Version(s)")
        _DYNAMIC_FIELDS = {"Artifact ID(s)", "Starting Version(s)", "Ending Version(s)"}
        for cf in CUSTOM_FIELDS:
            fname = cf["name"]
            if fname in _DYNAMIC_FIELDS:
                continue
            expected_val = cf["value"][0]
            current_val = current_fields.get(fname, "")
            adds, removes = print_field_diff(fname, current_val, expected_val)
            total_adds += adds
            total_removes += removes

        # Summary
        if total_adds == 0 and total_removes == 0:
            msg = "  All fields are identical — nothing to update."
            if to_terminal:
                print(_colorize(msg, _Colors.GREEN))
            else:
                print("  CCR #{}: no differences".format(REVIEW_ID))
            _html_line(msg, _Colors.GREEN)
        else:
            summary = "  Summary: {} addition(s), {} removal(s)".format(total_adds, total_removes)
            if to_terminal:
                print(_colorize(summary, _Colors.CYAN))
            else:
                print("  CCR #{}: {} addition(s), {} removal(s)".format(REVIEW_ID, total_adds, total_removes))
            _html_line(summary, _Colors.CYAN)
        if to_terminal:
            print()
        _html_line("")

    # Write HTML file if requested
    if args.html and HTML_LINES is not None:
        _write_html_file(args.html, "CCN Diff Report")


if __name__ == "__main__":
    main()
