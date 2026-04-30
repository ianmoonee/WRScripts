# ccn-api

Scripts for interacting with the CodeCollaborator (CCN) JSON API.

## Scripts

### ccn_updater.py

Updates custom fields on one or more CodeCollaborator reviews via the JSON API. Automatically generates **Artifact ID(s)**, **Starting Version(s)**, and **Ending Version(s)** fields from git commit history across multiple CCR branches.

**Features:**
- Multiple review IDs — cross-references branches across CCRs for previous-hash lookups
- `--bsp` mode: shortened file paths grouped by directory
- `--bl` mode: full file paths in a flat sorted list
- 4000-character field guard — writes field values to text files when too large for the API
- `--update-most-recent` — only updates the newest review, uses older ones for hash context
- `--dry-run` — validates without pushing changes

**Prerequisites:**
```
export CCN_LOGIN="your_username"
export CCN_PASSWORD="your_password"
export WASSP_PATH="/path/to/wassp"
```

**Usage:**
```
python ccn_updater.py --review-id 31859 --bsp
python ccn_updater.py --review-id 31859 31280 --bl --update-most-recent
python ccn_updater.py --review-id 31859 --bsp --dry-run
```

### ccn_updater_5.py

Earlier version of ccn_updater.py. Kept for reference — use `ccn_updater.py` instead.

### brute.py

API exploration/debugging script that fetches review summaries and tests the `getReviewMaterials` endpoint. Saves raw JSON responses to files for inspection.

**Prerequisites:** `CCN_LOGIN`, `CCN_PASSWORD` environment variables.

## Configuration

### fields.json

Default config file for ccn_updater.py. Specifies which custom fields to update on a review. Only fields present in the file are modified.

```json
{
    "Artifact ID(s)": "",
    "Starting Version(s)": "",
    "Ending Version(s)": ""
}
```
