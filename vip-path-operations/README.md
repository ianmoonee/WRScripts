# vip-path-operations

Scripts for bulk editing and cleaning up VIP/VM configuration files in native directories.

## Scripts

### imageDeleter.py

Deletes unreferenced image files from VM directories. Parses `vipConfigure.sh` scripts to build a list of referenced files, then removes everything not in that list. Core files (`vipConfigure.sh`, `uVxWorks`, `.dtb`/`.dts` files) are always protected.

**Usage:**
```
python imageDeleter.py /path/to/native --dry-run
python imageDeleter.py /path/to/native
python imageDeleter.py /path/to/vipConfigure.sh --dry-run
```

**Key arguments:** `path` (native directory or single `.sh` file), `--dry-run`

### vipConfigureEditor.py

Bulk-edits `vipConfigure.sh` scripts across `BOOT_APP0*` directories. Adds a revision history entry and inserts shell commands before `set +x`. Processes all matching directories under the given native path.

**Usage:**
```
python vipConfigureEditor.py /path/to/native --revision "16apr26,tal  Added NTP config" --command 'echo "ntp_server=10.0.0.1"' --dry-run
python vipConfigureEditor.py /path/to/native --revision "16apr26,tal  Added NTP config" --command 'echo "ntp_server=10.0.0.1"'
```

**Key arguments:** `path` (native directory), `--revision` (required), `--command` (required), `--dry-run`
