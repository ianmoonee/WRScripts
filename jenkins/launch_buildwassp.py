#!/usr/bin/env python3
"""
launch_buildwassp.py - Trigger and monitor Jenkins BuildWassp jobs.

This script launches one or more BuildWassp jobs on the Jenkins server at
https://ccn-sford.wrs.com. All build parameters are read from a JSON config
file (no hardcoded defaults).

JSON config format:
    [
        { <defaults - full set of parameters> },
        { <run 1 - only fields to override> },
        { <run 2 - only fields to override> },
        ...
    ]

    - The 1st object contains the default parameters shared by all runs.
      If it is the only object, a single build is triggered with those params.
    - Each subsequent object defines a run. Its fields are merged on top of
      the defaults, so you only need to specify what differs.

Supported Jenkins parameters:
    Branch       - Git branch to build.
    BranchWassp  - WASSP branch name.
    matrix       - Test matrix spreadsheet filename (e.g. vx7GOS.xls).
    colrow       - Column:row reference into the matrix (e.g. H:421).
    testNameDir  - Platform type / test directory name.

Usage:
    # Single build (JSON has 1 object):
    python launch_buildwassp.py -c config.json

    # Multiple builds (JSON has 2+ objects):
    python launch_buildwassp.py -c runs.json

    # Follow console output in real time:
    python launch_buildwassp.py -c runs.json -f

Requirements:
    pip install requests

Notes:
    - No authentication token is needed (anonymous access).
    - The script automatically fetches a Jenkins CSRF crumb before triggering.
    - SSL verification is disabled for self-signed certificates.
    - Multiple builds run in parallel threads; output is tagged per run.
"""

import argparse
import json
import os
import re
import requests
import urllib3
import sys
import time
import threading

# Suppress InsecureRequestWarning for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

JENKINS_URL = "https://ccn-sford.wrs.com"
JOB_NAME = "BuildWassp"


def get_crumb(session):
    """Fetch a CSRF crumb from the Jenkins crumbIssuer API.

    Jenkins requires a crumb (CSRF token) to be included as a header in
    POST requests. This function GETs /crumbIssuer/api/json and returns
    a dict suitable for passing as ``headers=`` to requests.post().

    Args:
        session: A requests.Session used to share cookies with the build request.

    Returns:
        A dict like {"Jenkins-Crumb": "<token>"}, or {} if the crumb
        could not be obtained.
    """
    crumb_url = f"{JENKINS_URL}/crumbIssuer/api/json"
    resp = session.get(crumb_url, verify=False)
    if resp.status_code != 200:
        print(f"Warning: Could not fetch crumb (HTTP {resp.status_code}). Trying without it.")
        return {}
    data = resp.json()
    print(f"Got crumb: {data['crumbRequestField']}")
    return {data["crumbRequestField"]: data["crumb"]}


# Lock for thread-safe printing
_print_lock = threading.Lock()


def tprint(tag, msg, log_file=None, **kwargs):
    """Thread-safe print with a tag prefix.

    Wraps each message as ``[tag] msg`` and uses a lock so output from
    concurrent threads doesn't interleave mid-line.  When *log_file* is
    provided the line is written to that file (flushed immediately) instead
    of stdout.

    Args:
        tag:      Short label identifying the build (e.g. "run-1 H:421").
        msg:      The message text to print.
        log_file: An open file object to write to.  If None, prints to stdout.
    """
    with _print_lock:
        line = f"[{tag}] {msg}"
        if log_file:
            log_file.write(line + "\n")
            log_file.flush()
        else:
            print(line, **kwargs)


def launch_build(params, follow=False, tag=None, log_file=None):
    """Trigger a single Jenkins build and optionally follow its output.

    Sends a POST to /job/BuildWassp/buildWithParameters with the given
    params. After triggering, polls the queue to resolve the actual build
    URL. If ``follow`` is True, streams the console log until completion.

    Args:
        params:   Dict of Jenkins build parameters.
        follow:   If True, stream console output after triggering.
        tag:      Label for log lines (defaults to the colrow value).
        log_file: Open file object for writing logs.  If None, prints to stdout.

    Returns:
        The build URL string, or None if the trigger failed.
    """
    if tag is None:
        tag = params.get("colrow", "build")
    url = f"{JENKINS_URL}/job/{JOB_NAME}/buildWithParameters"
    tprint(tag, f"Triggering {JOB_NAME} at {url}", log_file=log_file)
    tprint(tag, "Parameters:", log_file=log_file)
    for k, v in params.items():
        tprint(tag, f"  {k}: {v}", log_file=log_file)

    session = requests.Session()
    crumb_header = get_crumb(session)

    resp = session.post(url, data=params, headers=crumb_header, verify=False)

    if resp.status_code in (200, 201):
        tprint(tag, f"Build triggered successfully (HTTP {resp.status_code}).", log_file=log_file)
        build_url = None
        if "Location" in resp.headers:
            queue_url = resp.headers["Location"]
            build_url = get_build_url(session, queue_url)
            if build_url:
                tprint(tag, f"Build URL: {build_url}", log_file=log_file)
            else:
                tprint(tag, f"Queue URL: {queue_url}", log_file=log_file)
        if follow and build_url:
            follow_build(session, build_url, tag=tag, log_file=log_file)
        return build_url
    else:
        tprint(tag, f"Failed to trigger build. HTTP {resp.status_code}", log_file=log_file)
        tprint(tag, resp.text, log_file=log_file)
        return None


def get_build_url(session, queue_url):
    """Resolve a queue item URL to the actual build URL.

    After Jenkins accepts a build, it returns a queue item URL. This
    function polls that queue item (up to ~60 seconds) until Jenkins
    assigns a build number and the ``executable`` field appears.

    Args:
        session:    A requests.Session (shares cookies/crumb state).
        queue_url:  The Location header URL returned when the build was queued.

    Returns:
        The build URL (e.g. https://.../job/BuildWassp/26781/), or None
        if it could not be resolved within the timeout.
    """
    api_url = queue_url.rstrip("/") + "/api/json"
    for _ in range(30):
        time.sleep(2)
        resp = session.get(api_url, verify=False)
        if resp.status_code != 200:
            continue
        data = resp.json()
        if "executable" in data and data["executable"]:
            return data["executable"]["url"]
    return None


def follow_build(session, build_url, tag="build", log_file=None):
    """Stream the Jenkins console log in real time until the build finishes.

    Uses the /logText/progressiveText API, which returns new log chunks
    since the last byte offset. Polls every 3 seconds. When the build
    completes, prints the final result (SUCCESS / FAILURE / etc.) and
    total duration.

    Args:
        session:    A requests.Session.
        build_url:  The build URL (e.g. https://.../job/BuildWassp/26781/).
        tag:        Label prefix for each printed line.
        log_file:   Open file object for writing logs.  If None, prints to stdout.
    """
    log_url = build_url.rstrip("/") + "/logText/progressiveText"
    api_url = build_url.rstrip("/") + "/api/json"
    start = 0
    tprint(tag, "\n" + "=" * 60, log_file=log_file)
    tprint(tag, "CONSOLE OUTPUT", log_file=log_file)
    tprint(tag, "=" * 60, log_file=log_file)

    while True:
        resp = session.get(log_url, params={"start": start}, verify=False)
        if resp.status_code == 200:
            text = resp.text
            if text:
                for line in text.splitlines():
                    tprint(tag, line, log_file=log_file)
            new_start = resp.headers.get("X-Text-Size")
            if new_start:
                start = int(new_start)
            more_data = resp.headers.get("X-More-Data", "false")
            if more_data.lower() != "true":
                break
        time.sleep(3)

    # Final status
    resp = session.get(api_url, verify=False)
    if resp.status_code == 200:
        data = resp.json()
        result = data.get("result", "UNKNOWN")
        duration_ms = data.get("duration", 0)
        duration_s = duration_ms // 1000
        minutes, seconds = divmod(duration_s, 60)
        tprint(tag, "\n" + "=" * 60, log_file=log_file)
        tprint(tag, f"BUILD {result}  (duration: {minutes}m {seconds}s)", log_file=log_file)
        tprint(tag, "=" * 60, log_file=log_file)


def main():
    parser = argparse.ArgumentParser(
        description="Launch one or more BuildWassp Jenkins jobs.",
        epilog="""JSON format:
  1st object = defaults (also used as-is for single build)
  2nd+ objects = runs (override specific fields)

Examples:
  Single build:   python launch_buildwassp.py -c config.json
  Multi build:    python launch_buildwassp.py -c config.json
  Multi + follow: python launch_buildwassp.py -c config.json -f""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", "-c", type=str, required=True,
                        help="JSON file: 1st object = defaults, 2nd+ = runs. If only 1 object, it runs as a single build.")
    parser.add_argument("--follow", "-f", action="store_true",
                        help="Follow console output until builds finish")

    args = parser.parse_args()

    with open(args.config, "r") as f:
        entries = json.load(f)
    if not isinstance(entries, list) or len(entries) < 1:
        print("Error: JSON config must be a list with at least 1 object.")
        sys.exit(1)

    defaults = entries[0]
    runs = entries[1:]

    # Build a log directory next to the config file
    config_dir = os.path.dirname(os.path.abspath(args.config))
    log_dir = os.path.join(config_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    if not runs:
        # Single-build mode: just use the defaults object
        log_name = re.sub(r'[^\w.-]', '_', defaults.get('colrow', 'build')) + ".log"
        log_path = os.path.join(log_dir, log_name)
        print(f"Single build from {args.config}")
        print(f"Logging to {log_path}")
        with open(log_path, "w") as lf:
            launch_build(defaults, follow=args.follow, log_file=lf)
    else:
        # Multi-build mode
        print(f"Defaults: {defaults}")
        print(f"Launching {len(runs)} builds from {args.config}...\n")
        threads = []
        log_files = []
        for i, run_params in enumerate(runs):
            merged = dict(defaults)
            merged.update(run_params)
            tag = f"run-{i+1} {merged.get('colrow', '')}"
            log_name = re.sub(r'[^\w.-]', '_', tag) + ".log"
            log_path = os.path.join(log_dir, log_name)
            print(f"  {tag} -> {log_path}")
            lf = open(log_path, "w")
            log_files.append(lf)
            t = threading.Thread(
                target=launch_build,
                args=(merged,),
                kwargs={"follow": args.follow, "tag": tag, "log_file": lf},
            )
            threads.append(t)
            t.start()
            time.sleep(1)
        for t in threads:
            t.join()
        for lf in log_files:
            lf.close()


if __name__ == "__main__":
    main()
