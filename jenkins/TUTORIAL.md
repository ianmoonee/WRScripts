# launch_buildwassp.py — Tutorial

A command-line tool to trigger and monitor one or more **BuildWassp** jobs on a Jenkins server.

---

## Table of Contents

1. [Requirements](#requirements)
2. [Quick Start](#quick-start)
3. [JSON Configuration Format](#json-configuration-format)
4. [Usage](#usage)
   - [Single Build](#single-build)
   - [Multiple Builds](#multiple-builds)
   - [Follow Console Output](#follow-console-output)
5. [How It Works](#how-it-works)
6. [Logs](#logs)
7. [Jenkins Parameters Reference](#jenkins-parameters-reference)
8. [Troubleshooting](#troubleshooting)

---

## Requirements

- **Python 3.6+**
- **requests** library:
  ```bash
  pip install requests
  ```

No Jenkins authentication token is needed — the script uses anonymous access and automatically fetches a CSRF crumb before triggering builds.

---

## Quick Start

```bash
# 1. Install the dependency
pip install requests

# 2. Create a JSON config file (see format below)

# 3. Launch builds
python launch_buildwassp.py -c runs_example.json
```

---

## JSON Configuration Format

The config file is a **JSON array** of objects. The structure follows a **defaults + overrides** pattern:

| Position | Role | Description |
|----------|------|-------------|
| 1st object | **Defaults** | Contains all shared parameters for every build. |
| 2nd+ objects | **Runs** | Each one overrides specific fields from the defaults. |

### Example — `runs_example.json`

```json
[
    {
        "Branch": "cert_code_cleaning",
        "BranchWassp": "wassp-jenkins",
        "matrix": "vx7GOS.xls",
        "testNameDir": "vpx1708-gos-cert-tests"
    },
    {
        "colrow": "H:421"
    },
    {
        "colrow": "H:422"
    }
]
```

**What this produces:**

| Build | Branch | BranchWassp | matrix | testNameDir | colrow |
|-------|--------|-------------|--------|-------------|--------|
| Run 1 | cert_code_cleaning | wassp-jenkins | vx7GOS.xls | vpx1708-gos-cert-tests | H:421 |
| Run 2 | cert_code_cleaning | wassp-jenkins | vx7GOS.xls | vpx1708-gos-cert-tests | H:422 |

Each run inherits all defaults and only specifies the field(s) that differ (in this case, `colrow`).

### Single-Build Config

If the JSON array contains **only one object**, that object is used directly as a single build:

```json
[
    {
        "Branch": "cert_code_cleaning",
        "BranchWassp": "wassp-jenkins",
        "matrix": "vx7GOS.xls",
        "testNameDir": "vpx1708-gos-cert-tests",
        "colrow": "H:421"
    }
]
```

---

## Usage

### Single Build

```bash
python launch_buildwassp.py -c config.json
```

Triggers one Jenkins build using the parameters in the first (and only) JSON object.

### Multiple Builds

```bash
python launch_buildwassp.py -c runs_example.json
```

Triggers **N** builds in parallel, one per override object (2nd, 3rd, etc.). Each build merges its fields on top of the defaults.

### Follow Console Output

Add the `-f` / `--follow` flag to stream each build's console output in real time:

```bash
python launch_buildwassp.py -c runs_example.json -f
```

The script will poll the Jenkins console log every 3 seconds and print new output until all builds finish. When complete, it reports the final result (`SUCCESS`, `FAILURE`, etc.) and total duration.

### CLI Reference

| Flag | Long Form | Required | Description |
|------|-----------|----------|-------------|
| `-c` | `--config` | Yes | Path to the JSON configuration file. |
| `-f` | `--follow` | No | Follow console output until builds finish. |

---

## How It Works

```
                          ┌────────────────────┐
                          │  Read JSON config   │
                          └────────┬───────────┘
                                   │
                     ┌─────────────┴─────────────┐
                     │                           │
              1 object only               2+ objects
                     │                           │
              Single build            Merge defaults + run
                     │                   for each override
                     │                           │
                     ▼                           ▼
              ┌─────────────┐        ┌──────────────────┐
              │ Fetch CSRF  │        │ Spawn threads    │
              │   crumb     │        │ (one per build)  │
              └──────┬──────┘        └────────┬─────────┘
                     │                        │
                     ▼                        ▼
              ┌─────────────┐        ┌──────────────────┐
              │   POST      │        │  Each thread:    │
              │ buildWith   │        │  - Fetch crumb   │
              │ Parameters  │        │  - POST build    │
              └──────┬──────┘        │  - Poll queue    │
                     │               │  - (follow log)  │
                     ▼               └────────┬─────────┘
              ┌─────────────┐                 │
              │ Poll queue  │                 ▼
              │ for build # │        ┌──────────────────┐
              └──────┬──────┘        │ Join all threads  │
                     │               └──────────────────┘
                     ▼
              ┌─────────────┐
              │ (Follow log)│
              └─────────────┘
```

**Step-by-step:**

1. **Parse CLI args** — reads `--config` and optional `--follow`.
2. **Load JSON** — first object becomes defaults; remaining objects become run overrides.
3. **Merge parameters** — each run's fields are merged on top of defaults.
4. **Fetch CSRF crumb** — GETs `/crumbIssuer/api/json` and includes the crumb header in subsequent POSTs.
5. **Trigger build** — POSTs to `/job/BuildWassp/buildWithParameters` with the merged parameters.
6. **Resolve build URL** — polls the Jenkins queue item until a build number is assigned (up to ~60 s).
7. **Follow output** *(optional)* — streams `/logText/progressiveText` every 3 s until the build completes.
8. **Report result** — prints `BUILD SUCCESS` or `BUILD FAILURE` with total duration.

For multiple builds, steps 4–8 run in **parallel threads** (one per build, staggered 1 s apart).

---

## Logs

All console output is written to log files in a `logs/` directory created next to your config file.

| Mode | Log filename pattern | Example |
|------|---------------------|---------|
| Single build | `<colrow>.log` | `H_421.log` |
| Multi build | `run-<N>_<colrow>.log` | `run-1_H_421.log`, `run-2_H_422.log` |

Special characters in the tag are replaced with underscores.

---

## Jenkins Parameters Reference

| Parameter | Description | Example |
|-----------|-------------|---------|
| `Branch` | Git branch to build | `cert_code_cleaning` |
| `BranchWassp` | WASSP branch name | `wassp-jenkins` |
| `matrix` | Test matrix spreadsheet filename | `vx7GOS.xls` |
| `colrow` | Column:row reference into the matrix | `H:421` |
| `testNameDir` | Platform type / test directory name | `vpx1708-gos-cert-tests` |

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| `Could not fetch crumb` warning | Jenkins crumb issuer may be disabled or unreachable | The script will try without it — usually still works. Check network connectivity. |
| `Failed to trigger build. HTTP 403` | Permission issue or crumb mismatch | Verify the Jenkins URL is correct and anonymous build access is enabled. |
| Build URL not resolved | Queue item timed out (~60 s) | Jenkins may be overloaded. Check the Jenkins UI manually. |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Should not occur — SSL verification is disabled | Ensure `urllib3` is installed (comes with `requests`). |
| `Error: JSON config must be a list` | Config file is not a JSON array | Wrap your config objects in `[ ... ]`. |
