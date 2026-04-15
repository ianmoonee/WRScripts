import requests, json, os

url = "https://ccn-codecolab.wrs.com:8443/services/json/v1"
login = os.environ["CCN_LOGIN"]
pwd = os.environ["CCN_PASSWORD"]

# Get login ticket
r = requests.post(url, json=[{"command": "SessionService.getLoginTicket", "args": {"login": login, "password": pwd}}], verify=False)
ticket = r.json()[0]["result"]["loginTicket"]

review_id = 28435

# 1) Save full getReviewSummary response to file for inspection
print("=== Fetching full getReviewSummary ===")
req = [
    {"command": "SessionService.authenticate", "args": {"login": login, "ticket": ticket}},
    {"command": "ReviewService.getReviewSummary", "args": {"reviewId": review_id, "clientBuild": "14401"}}
]
resp = requests.post(url, json=req, verify=False)
result = resp.json()[1]
with open("review_summary.json", "w") as f:
    json.dump(result, f, indent=2, default=str)
print("  Saved to review_summary.json")

# 2) Try getReviewMaterials on different reviews
print("\n=== Testing getReviewMaterials on different review IDs ===")
test_reviews = [28435, 31859, 31000, 30000]
for rid in test_reviews:
    req = [
        {"command": "SessionService.authenticate", "args": {"login": login, "ticket": ticket}},
        {"command": "ReviewService.getReviewMaterials", "args": {"reviewId": rid, "clientBuild": "14401"}}
    ]
    resp = requests.post(url, json=req, verify=False)
    result = resp.json()[1]
    has_error = "errors" in result
    short = json.dumps(result, default=str)[:200]
    print(f"  review={rid} -> {'ERROR' if has_error else 'OK'}: {short}")

# 3) Fetch the server manual page for API reference
print("\n=== Fetching server manual (getReviewMaterials section) ===")
manual = requests.get("https://ccn-codecolab.wrs.com:8443/manual", verify=False)
# Search for getReviewMaterials in the manual text
text = manual.text
idx = text.lower().find("getreviewmaterials")
if idx >= 0:
    snippet = text[max(0, idx-200):idx+2000]
    print(snippet)
else:
    print("  getReviewMaterials not found in manual page")
