# WRScripts

Automation scripts for VxWorks certification workflows - CodeCollaborator updates, Polarion management, VIP configuration, Jenkins builds, and target provisioning.

## Script Glossary

| Script | Folder | Description |
|--------|--------|-------------|
| `ccn_updater.py` | ccn-api | Updates CodeCollaborator review hashes (Artifact IDs, Starting/Ending Versions) from git history. Supports BSP/BL modes. |
| `brute.py` | ccn-api | CCN API exploration/debugging tool. Dumps review data to JSON files. |
| `launch_buildwassp.py` | jenkins | Triggers and monitors Jenkins BuildWassp jobs from a JSON config. Supports parallel builds with console streaming. |
| `polarionTestProcedureManager.py` | polarion-updater | Scans repo for tp_*.c files, creates/updates matching Polarion test procedure work items with GitLab links. |
| `polarionSourceLinkUpdater.py` | polarion-updater | Bulk-edits Polarion work item hyperlinks - fixes source URLs, clears suspect flags, converts descriptions. |
| `polarionSameAsSearch.py` | polarion-updater | Finds Polarion test cases with "same as" references and reports unresolved ones. |
| `validate_tc_coverage.py` | polarion-updater | Validates test case coverage by comparing Polarion TCs against test procedure log files. |
| `validate_tc_links.py` | polarion-updater | Validates that Polarion test cases have the required link types (verifies, is implemented by, contains). |
| `test_create_workitem.py` | polarion-updater | Experimental script for testing Polarion work item creation payloads. |
| `imageDeleter.py` | vip-path-operations | Deletes unreferenced image files from VM directories based on vipConfigure.sh references. |
| `vipConfigureEditor.py` | vip-path-operations | Bulk-edits vipConfigure.sh scripts across BOOT_APP directories - adds revision entries and shell commands. |
| `vxworks_telnet.py` | vxWorks-target-operations | Connects to VxWorks targets via telnet, reboots, boots, and runs provisioning commands. Handles target reservation. |
| `vxworks_telnet.sh` | vxWorks-target-operations | Bash/Expect equivalent of vxworks_telnet.py for environments without Python. |

## Folder Structure

| Folder | Purpose |
|--------|---------|
| [ccn-api](ccn-api/) | CodeCollaborator review field automation |
| [jenkins](jenkins/) | Jenkins build job launcher |
| [polarion-updater](polarion-updater/) | Polarion work item queries, validation, and updates |
| [vip-path-operations](vip-path-operations/) | VIP/VM configuration file editing and cleanup |
| [vxWorks-target-operations](vxWorks-target-operations/) | VxWorks target provisioning via telnet |

## Common Environment Variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `CCN_LOGIN` | ccn-api | CodeCollaborator username |
| `CCN_PASSWORD` | ccn-api | CodeCollaborator password |
| `WASSP_PATH` | ccn-api | Path to local wassp git repo |
| `POLARION_API_BASE` | polarion-updater | Polarion REST API base URL |
| `POLARION_PAT` | polarion-updater | Polarion personal access token |
| `POLARION_PROJECT_ID` | polarion-updater | Default Polarion project ID |
| `VXWORKS_USER` | vxWorks-target-operations | VxWorks target login (optional) |
| `VXWORKS_PASSWORD` | vxWorks-target-operations | VxWorks target password (optional) |
