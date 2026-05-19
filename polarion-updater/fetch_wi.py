#!/usr/bin/env python3
"""
Fetch a single Polarion work item and print its full JSON.
Usage: python fetch_wi.py <WORK_ITEM_SHORT_ID>
Example: python fetch_wi.py POSBSP_SSD_NVME0_nvme_ctrlr_TR_1
"""
import os
import sys
import json
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

base_url   = os.environ["POLARION_API_BASE"].rstrip("/")
pat        = os.environ["POLARION_PAT"]
project_id = os.environ["POLARION_PROJECT_ID"]

if len(sys.argv) < 2:
    print("Usage: python fetch_wi.py <WORK_ITEM_SHORT_ID>")
    sys.exit(1)

wi_id = sys.argv[1]
url   = f"{base_url}/projects/{project_id}/workitems/{wi_id}"

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {pat}", "Accept": "application/json"})

params = {
    "fields[workitems]": "@all",
    "fields[categories]": "@all",
    "fields[linkedworkitems]": "@all",
    "fields[plans]": "@all",
    "fields[projects]": "@all",
    "fields[users]": "@all",
    "fields[workitem_attachments]": "@all",
    "fields[workitem_comments]": "@all",
}

resp = session.get(url, params=params, verify=False)
print(f"Status: {resp.status_code}")
print(json.dumps(resp.json(), indent=2))
