# polarion-updater

Scripts for querying and updating Polarion work items via the REST API.

All scripts share common prerequisites:
```
export POLARION_API_BASE="https://your-polarion-server/api"
export POLARION_PAT="your_personal_access_token"
export POLARION_PROJECT_ID="your_project_id"
```

## Scripts

### polarionTestProcedureManager.py

Scans a local git repo for `tp_*.c` test procedure files, matches them against existing Polarion test procedure work items, updates hyperlinks with GitLab URLs, and creates new work items for unmatched files. Automatically extracts test case names from tp file comments and links them.

**Usage:**
```
python polarionTestProcedureManager.py --repo-path /path/to/repo --ccr-id 31859 --dry-run
python polarionTestProcedureManager.py --repo-path /path/to/repo --ccr-id 31859 --execute
python polarionTestProcedureManager.py --repo-path /path/to/repo --ccr-id 31859 --execute --skip-creates
```

**Key arguments:** `--repo-path`, `--ccr-id`, `--gitlab-base`, `--dry-run`, `--execute`, `--limit`, `--skip-updates`, `--skip-creates`, `--include-srvc`, `--author`

### polarionSourceLinkUpdater.py

Manages Polarion work item hyperlinks and attributes. Performs bulk operations on source links: replaces "native" with "SFORD_POS", converts "/raw/" to "/blob/" in URLs, clears suspect flags, converts HTML descriptions to plain text, and replaces "wassp-jenkins" with "wassp-jenkins-nth".

**Usage:**
```
python polarionSourceLinkUpdater.py query --dry-run
python polarionSourceLinkUpdater.py query --execute --clear-suspects
python polarionSourceLinkUpdater.py --ids WI-1234 WI-5678 --execute --jenkins-nth
```

**Key arguments:** `query`, `--ids`, `--ids-file`, `--dry-run`, `--execute`, `--clear-suspects`, `--convert-descriptions`, `--jenkins-nth`

### polarionSameAsSearch.py

Queries Polarion for test case work items containing "same as &lt;name&gt;" references. Groups results by test name and identifies unresolved references. Supports output as a full report, TDK_CERT_TC_LOG lines, or C arrays.

**Usage:**
```
python polarionSameAsSearch.py --component COMPONENT_NAME
python polarionSameAsSearch.py --component COMPONENT_NAME --c-array
python polarionSameAsSearch.py --component COMPONENT_NAME --clear
```

**Key arguments:** `--component`, `--project-id`, `--pattern`, `--clear`, `--c-array`

### validate_tc_coverage.py

Validates test case coverage by comparing Polarion TC titles against test procedure log files. Checks whether each TC appears in at least one `.log` file and generates a coverage report. Excludes unimplemented TCs.

**Usage:**
```
python validate_tc_coverage.py --log-dir /path/to/logs --component COMPONENT_NAME
python validate_tc_coverage.py --log-dir /path/to/logs --query "lucene query"
```

**Key arguments:** `--log-dir`, `--project-id`, `--component`, `--title-filter`, `--query`, `--limit`

### validate_tc_links.py

Validates linked work items on Polarion test cases. Ensures each TC has the expected link types: "verifies" (to requirements), "is implemented by" (to TP), and "contains" (to Checklist). Detects reverse links via Lucene queries.

**Usage:**
```
python validate_tc_links.py --component COMPONENT_NAME
python validate_tc_links.py --query "lucene query" --limit 50
```

**Key arguments:** `--project-id`, `--component`, `--title-filter`, `--query`, `--limit`

### test_create_workitem.py

Test/experimental script for validating Polarion work item creation payloads. Creates a single test procedure work item, verifies its fields, and optionally deletes it.

**Usage:**
```
python test_create_workitem.py
python test_create_workitem.py --delete
```
