import os
import re
import sys
import argparse


def get_referenced_files(sh_path):
    """Parse the .sh file and extract destination filenames from cp commands."""
    referenced = set()
    with open(sh_path, "r") as f:
        for line in f:
            line = line.strip()
            # Match lines like: cp <source> <dest_dir>/<filename>
            match = re.match(r"^\s*cp\s+\S+\s+\S+/(\S+)", line)
            if match:
                referenced.add(match.group(1))
    return referenced


def delete_unlisted_files(target_dir, referenced_files, dry_run=False):
    """Delete files in target_dir whose names are not in referenced_files."""
    deleted = []
    kept = []
    for filename in os.listdir(target_dir):
        filepath = os.path.join(target_dir, filename)
        if not os.path.isfile(filepath):
            continue
        if filename not in referenced_files:
            if dry_run:
                print(f"[DRY RUN] Would delete: {filepath}")
            else:
                os.remove(filepath)
                print(f"Deleted: {filepath}")
            deleted.append(filename)
        else:
            kept.append(filename)
    print(f"\nKept {len(kept)} files, deleted {len(deleted)} files.")


def process_vm_folder(vm_dir, dry_run=False):
    """Process a single vm folder: find vipConfigure.sh and clean up."""
    sh_file = os.path.join(vm_dir, "vipConfigure.sh")
    if not os.path.isfile(sh_file):
        return

    print(f"\n{'='*60}")
    print(f"Processing: {vm_dir}")
    print(f"{'='*60}")

    referenced = get_referenced_files(sh_file)
    # Always keep the .sh file itself and protected files
    referenced.add("vipConfigure.sh")
    referenced.add("uVxWorks")
    referenced.add("cw1708.dtb")
    referenced.add("cw1708.dts")
    print(f"Found {len(referenced)} referenced files in vipConfigure.sh")
    delete_unlisted_files(vm_dir, referenced, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(
        description="Delete files not referenced in vipConfigure.sh across native subdirectories."
    )
    parser.add_argument("path", help="Path to the native directory or a single vipConfigure.sh file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting",
    )
    args = parser.parse_args()

    path = os.path.abspath(args.path)

    if os.path.isfile(path):
        # Single .sh file mode
        target_dir = os.path.dirname(path)
        process_vm_folder(target_dir, dry_run=args.dry_run)
    elif os.path.isdir(path):
        # Walk into each subfolder looking for vm/vipConfigure.sh
        found = False
        for entry in sorted(os.listdir(path)):
            if "BOOT_APP0" not in entry:
                continue
            vm_dir = os.path.join(path, entry, "vm")
            if os.path.isdir(vm_dir) and os.path.isfile(os.path.join(vm_dir, "vipConfigure.sh")):
                found = True
                process_vm_folder(vm_dir, dry_run=args.dry_run)
        if not found:
            print(f"No vm/vipConfigure.sh found in any subdirectory of {path}")
    else:
        print(f"Error: path not found: {path}")
        sys.exit(1)


if __name__ == "__main__":
    main()
