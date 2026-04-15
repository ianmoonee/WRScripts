# vipConfigureEditor.py - Bulk-edit vipConfigure.sh files across BOOT_APP0* test case directories.
#
# Adds a revision history entry and inserts a shell command before 'set +x'.
#
# Usage:
#   python vipConfigureEditor.py <native_dir> --revision "<entry>" --command "<cmd>" [--dry-run]
#
# Examples:
#
#   1) Preview changes without modifying any files (dry run):
#      python vipConfigureEditor.py C:\wassp\vpx1708-bl-cert-tests\testcases\native --revision "14apr26,pse   Added new boot check." --command "echo 'boot check enabled'" --dry-run
#
#   2) Add a revision entry and a new command to all matching files:
#      python vipConfigureEditor.py C:\wassp\vpx1708-bl-cert-tests\testcases\native --revision "14apr26,pse   Added new boot check." --command "echo 'boot check enabled'"
#
#   3) Multi-word command with pipes:
#      python vipConfigureEditor.py /home/user/testcases/native --revision "14apr26,pse   Enable verbose logging." --command "sed -i 's/quiet/verbose/g' ./gold/bl/prjConfig_gold.c"
#
#   4) Revision entry without '# ' prefix (auto-prepended by the script):
#      python vipConfigureEditor.py ./testcases/native --revision "14apr26,pse   Fix reboot issue." --command "/usr/bin/python \${WASSP_TESTCASE_BASE}/common/configurator.py config.json"
#
# Arguments:
#   path          Top-level directory containing BOOT_APP0* subdirectories (e.g. testcases/native)
#   --revision    Full revision history line (e.g. '14apr26,pse   Description here.')
#   --command     Shell command to insert on the line before 'set +x'
#   --dry-run     Show what would change without writing to disk

import os
import sys
import argparse


def modify_vip_configure(filepath, revision, command, dry_run=False):
    """Modify a single vipConfigure.sh file: insert revision entry and command."""
    with open(filepath, "r") as f:
        lines = f.readlines()

    # --- Insert revision entry after the "# ---" separator in modification history ---
    revision_line = revision if revision.startswith("# ") else f"# {revision}"
    if not revision_line.endswith("\n"):
        revision_line += "\n"

    history_inserted = False
    dash_line_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("# ---") and dash_line_idx is None:
            # Look backwards to confirm we're in a modification history block
            for j in range(i - 1, max(i - 5, -1), -1):
                if "modification history" in lines[j].lower():
                    dash_line_idx = i
                    break

    if dash_line_idx is not None:
        # Insert right after the dashes line (newest-first)
        lines.insert(dash_line_idx + 1, revision_line)
        history_inserted = True

    # --- Insert command before the last "set +x" ---
    command_line = command if command.endswith("\n") else command + "\n"
    command_inserted = False
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "set +x":
            lines.insert(i, command_line)
            command_inserted = True
            break

    if not history_inserted:
        print(f"  WARNING: Could not find modification history block in {filepath}")
    if not command_inserted:
        print(f"  WARNING: Could not find 'set +x' in {filepath}")

    if dry_run:
        print(f"[DRY RUN] Would modify: {filepath}")
        if history_inserted:
            print(f"  + revision: {revision_line.strip()}")
        if command_inserted:
            print(f"  + command before 'set +x': {command.strip()}")
    else:
        with open(filepath, "w") as f:
            f.writelines(lines)
        print(f"Modified: {filepath}")
        if history_inserted:
            print(f"  + revision: {revision_line.strip()}")
        if command_inserted:
            print(f"  + command: {command.strip()}")

    return history_inserted or command_inserted


def process_vm_folder(vm_dir, revision, command, dry_run=False):
    """Process a single vm folder containing vipConfigure.sh."""
    sh_file = os.path.join(vm_dir, "vipConfigure.sh")
    if not os.path.isfile(sh_file):
        return False

    print(f"\n{'='*60}")
    print(f"Processing: {vm_dir}")
    print(f"{'='*60}")

    return modify_vip_configure(sh_file, revision, command, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(
        description="Bulk-edit vipConfigure.sh files: add revision history entry and insert a command before 'set +x'."
    )
    parser.add_argument("path", help="Top-level directory containing BOOT_APP0* subdirectories (e.g. testcases/native)")
    parser.add_argument("--revision", required=True,
                        help="Revision history entry text (e.g. '14apr26,pse   Added new boot check.')")
    parser.add_argument("--command", required=True,
                        help="Shell command to insert before 'set +x'")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without modifying files")
    args = parser.parse_args()

    path = os.path.abspath(args.path)

    if not os.path.isdir(path):
        print(f"Error: directory not found: {path}")
        sys.exit(1)

    modified = 0
    found = 0
    for entry in sorted(os.listdir(path)):
        if "BOOT_APP0" not in entry:
            continue
        vm_dir = os.path.join(path, entry, "vm")
        if os.path.isdir(vm_dir) and os.path.isfile(os.path.join(vm_dir, "vipConfigure.sh")):
            found += 1
            if process_vm_folder(vm_dir, args.revision, args.command, dry_run=args.dry_run):
                modified += 1

    if found == 0:
        print(f"\nNo vm/vipConfigure.sh found in any BOOT_APP0* subdirectory of {path}")
    else:
        action = "Would modify" if args.dry_run else "Modified"
        print(f"\n{action} {modified}/{found} vipConfigure.sh files.")


if __name__ == "__main__":
    main()
