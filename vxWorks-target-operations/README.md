# VxWorks Target Operations

Automates rebooting VxWorks targets, connecting to their serial consoles via telnet, and running a set of provisioning commands. The script handles target reservation/unreservation via `vlmTool` automatically, grouping targets by backplane to minimize reserve operations.

## Prerequisites

- Python 3.6+
- `vlmTool` available on your PATH (used to reboot targets)
- Network access to the target serial console hosts
- FTP credentials (for commands that copy files over FTP)

## Project Structure

```
├── vxworks_telnet.py   # Main script
├── commands.json       # Command definitions per component
├── targets.json        # Target board definitions (host, port)
└── README.md           # This file
```

## Configuration

### targets.json

Defines each target board by name, with its telnet host, port, and backplane:

```json
{
    "targets": {
        "42441": {
            "host": "128.224.118.40",
            "port": 2007,
            "backplane": "2"
        }
    }
}
```

Targets sharing the same `backplane` value are grouped together — the script reserves the backplane once, processes all targets on it, then unreserves once.

If `backplane` is omitted, the target is treated as its own independent backplane.

You can optionally add `"timeout"` and `"provisioner_prompt"` per target to override the defaults.

### commands.json

Groups commands under **components** (e.g. `boot_app`, `nvme`). Each component lists one or more target entries with the commands to run:

```json
{
    "components": {
        "my_component": {
            "targets": [
                {
                    "target": "42441",
                    "commands": [
                        {
                            "cmd": "myCommand()",
                            "wait_for": "value = 0 = 0x0",
                            "timeout": 10
                        }
                    ]
                }
            ]
        }
    }
}
```

Each command entry has:

| Field      | Required | Description                                          |
|------------|----------|------------------------------------------------------|
| `cmd`      | Yes      | The VxWorks shell command to send                    |
| `wait_for` | No       | A string or regex pattern to wait for in the output  |
| `timeout`  | No       | Seconds to wait before timing out (default: 10)      |

A `target` field can be a single string or a list of target names. When it's a list, the same commands run on each target in that group.

## Environment Variables

Commands in `commands.json` support `${VAR_NAME}` placeholders that are expanded from environment variables at runtime. This keeps credentials out of the JSON file.

Currently used variables:

| Variable           | Description                                       |
|--------------------|---------------------------------------------------|
| `VXWORKS_USER`     | FTP / remote login username                       |
| `VXWORKS_PASSWORD` | FTP / remote login password                       |
| `FORCE_RESERVE`    | If set (any value), force-reserves targets with `-f` immediately. If not set, the script tries to reserve normally up to 10 times (5s apart), then falls back to force reserve. |

Set them before running the script:

```bash
# Linux / macOS
export VXWORKS_USER="myuser"
export VXWORKS_PASSWORD="mypassword"
```

```powershell
# Windows PowerShell
$env:VXWORKS_USER = "myuser"
$env:VXWORKS_PASSWORD = "mypassword"
```

If a referenced variable is not set, the script will exit with an error telling you which one is missing.

## Usage

```bash
python vxworks_telnet.py <commands.json> <targets.json> <component> [target1 target2 ...]
```

### Arguments

| Argument         | Description                                                        |
|------------------|--------------------------------------------------------------------|
| `commands.json`  | Path to the commands file                                          |
| `targets.json`   | Path to the targets file                                           |
| `component`      | Which component to run (e.g. `boot_app`, `nvme`)                   |
| `target1 ...`    | *(Optional)* Run only on these targets instead of all in the group |

### Examples

Run the `boot_app` component on all its configured targets:

```bash
export VXWORKS_USER="myuser"
export VXWORKS_PASSWORD="mypassword"

python vxworks_telnet.py commands.json targets.json boot_app
```

Run only on target `42441`:

```bash
python vxworks_telnet.py commands.json targets.json boot_app 42441
```

Run the `nvme` component:

```bash
python vxworks_telnet.py commands.json targets.json nvme
```

## What the Script Does

For each backplane group, the script:

1. **Reserves** the backplane via `vlmTool reserve -t <target>` (with retry logic, see Environment Variables)
2. **Sets a reserve note** via `vlmTool reserveNote`
3. For each target on that backplane:
   1. **Reboots** the board using `vlmTool reboot -t <target>`
   2. **Connects** to the serial console via telnet (retries up to 2 minutes)
   3. **Intercepts autoboot** — waits for "Hit any key to stop autoboot" and stops it
   4. **Boots VxWorks** — sends `run bootprov` and waits for the provisioner prompt (`-> `)
   5. **Runs commands** — sends each command from `commands.json` and waits for the expected output
   6. **Disconnects** and moves to the next target
4. **Unreserves** the backplane via `vlmTool unreserve -t <target>`

On failure or interruption (Ctrl+C / SIGTERM), all currently reserved backplanes are unreserved before exiting.

## Logging

All output is printed to the console with timestamps:

```
2026-04-01 10:33:02 [INFO] Running local command: vlmTool reserve -t 42441
```

## Troubleshooting

| Problem                           | Fix                                                                 |
|-----------------------------------|---------------------------------------------------------------------|
| `Environment variable 'X' is not set` | Export the missing variable before running the script           |
| `Connection refused` after reboot | The board may take longer to come up — the script retries for ~2 min |
| `Timed out waiting for '...'`     | Increase the `timeout` for that command in `commands.json`          |
| `Boot failed — returned to VxWorks prompt` | The boot command failed on the target; check board state     |
| `Component 'X' not found`        | Check `commands.json` for available component names                 |
