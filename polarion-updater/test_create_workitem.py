#!/usr/bin/env python3
"""
Quick test script to experiment with Polarion work item creation payloads.
Tries creating a single test procedure WI and prints the full response.

Usage:
    python test_create_workitem.py
    python test_create_workitem.py --delete   # delete the created WI after

Environment Variables:
    POLARION_API_BASE, POLARION_PAT, POLARION_PROJECT_ID
"""

import os
import sys
import json
import requests
import argparse
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def main():
    parser = argparse.ArgumentParser(description="Test Polarion work item creation")
    parser.add_argument("--delete", action="store_true", help="Delete the created WI after inspecting")
    parser.add_argument("--verify-ssl", action="store_true", default=False)
    parser.add_argument("--project-id", default=None)
    args = parser.parse_args()

    base_url = os.environ.get("POLARION_API_BASE")
    pat = os.environ.get("POLARION_PAT")
    project_id = args.project_id or os.environ.get("POLARION_PROJECT_ID")

    if not all([base_url, pat, project_id]):
        print("Set POLARION_API_BASE, POLARION_PAT, POLARION_PROJECT_ID")
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })

    verify = args.verify_ssl
    url = f"{base_url}/projects/{project_id}/workitems"

    # --- PAYLOAD TO TEST ---
    # Modify this payload to experiment with different field formats
    payload = {
        "data": [
            {
                "type": "workitems",
                "attributes": {
                    "type": "wi_testProcedure",
                    "title": "TEST_CREATE",
                    "status": "rework",
                    "executionType": "Automated",
                    "hyperlinks": [
                        {"role": "ref_ext", "uri": "https://example.com/test"}
                    ],
                },
                "relationships": {
                    "categories": {
                        "data": [
                            {
                                "type": "categories",
                                "id": f"{project_id}/VXBVIP",
                            }
                        ]
                    },
                    "components": {
                        "data": [
                            {
                                "type": "components",
                                "id": f"{project_id}/BOOT_APP0",
                            }
                        ]
                    },
                },
            }
        ]
    }

    print("=" * 60)
    print("POST", url)
    print("=" * 60)
    print("Request payload:")
    print(json.dumps(payload, indent=2))
    print()

    resp = session.post(url, json=payload, verify=verify)
    print(f"Status: {resp.status_code}")
    print("Response headers:")
    for k, v in resp.headers.items():
        if k.lower() in ("content-type", "location"):
            print(f"  {k}: {v}")
    print()

    try:
        resp_json = resp.json()
        print("Response body:")
        print(json.dumps(resp_json, indent=2))
    except Exception:
        print("Response body (raw):")
        print(resp.text[:2000])

    if resp.status_code not in (200, 201):
        print("\nCreation FAILED, stopping.")
        return

    # Extract created ID
    resp_data = resp_json.get("data", [])
    if isinstance(resp_data, list) and resp_data:
        created = resp_data[0]
    else:
        created = resp_data
    created_id = created.get("id", "")
    short_id = created_id.split("/")[-1] if "/" in created_id else created_id
    print(f"\nCreated work item: {short_id}")

    # Fetch it back to see what fields were actually set
    print("\n" + "=" * 60)
    print(f"GET - Fetching created WI to verify fields")
    print("=" * 60)
    get_url = f"{base_url}/projects/{project_id}/workitems/{short_id}"
    params = {"fields[workitems]": "title,type,status,category,component,executionType,hyperlinks"}
    resp2 = session.get(get_url, params=params, verify=verify)
    print(f"Status: {resp2.status_code}")
    try:
        print(json.dumps(resp2.json(), indent=2))
    except Exception:
        print(resp2.text[:2000])

    # Optionally delete
    if args.delete:
        print("\n" + "=" * 60)
        print(f"DELETE - Cleaning up {short_id}")
        print("=" * 60)
        # Set to deleted status (Polarion usually requires status change)
        del_payload = {
            "data": {
                "type": "workitems",
                "id": created_id,
                "attributes": {"status": "deleted"}
            }
        }
        resp3 = session.patch(get_url, json=del_payload, verify=verify)
        print(f"Status: {resp3.status_code}")
        try:
            print(resp3.json())
        except Exception:
            print(resp3.text[:500])


if __name__ == "__main__":
    main()
