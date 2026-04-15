#!/usr/bin/env bash
#
# VxWorks Target Operations via Telnet (Bash + Expect)
#
# Connects to a VxWorks target's serial console over telnet,
# reboots it via vlmTool, stops autoboot, boots VxWorks,
# and runs a configured set of commands.
#
# Dependencies: expect, jq, telnet, ss/lsof
#
# Exit codes:
#   0 — all targets completed successfully
#   1 — one or more targets failed
#   2 — bad usage / missing dependencies / invalid config
#   130 — interrupted by user (Ctrl+C / SIGINT)
#   143 — terminated by SIGTERM
#

set -euo pipefail

# Source user's shell profile so non-interactive shells find tools like vlmTool
for rc in "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.profile"; do
    [[ -f "$rc" ]] && source "$rc" && break
done

# ── Defaults ──────────────────────────────────────────────
DEFAULT_TIMEOUT=10
DEFAULT_REBOOT_TIMEOUT=180
DEFAULT_PROVISIONER_PROMPT="-> "
DEFAULT_VXWORKS_PROMPT="=>"
DEFAULT_AUTOBOOT_PATTERN="Hit any key to stop autoboot"
DEFAULT_BOOT_CMD="run bootprov"
REBOOT_CMD_TEMPLATE="vlmTool reboot -t %s"
SETTLE_DELAY=5

# ── State ─────────────────────────────────────────────────
CURRENT_TARGET=""
CURRENT_HOST=""
CURRENT_PORT=""
EXPECT_PID=""
INTERRUPTED=0
TARGETS_PASSED=0
TARGETS_FAILED=0
TARGETS_SKIPPED=0
FAILED_LIST=""

# ── Logging ───────────────────────────────────────────────
log() {
    local level="$1"; shift
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$level] $*"
}

log_info()    { log "INFO"    "$@"; }
log_warning() { log "WARNING" "$@"; }
log_error()   { log "ERROR"   "$@"; }

# ── Signal handling / cleanup ─────────────────────────────
cleanup() {
    local exit_code=$?

    # Prevent re-entrancy
    trap '' INT TERM EXIT

    if [[ $INTERRUPTED -eq 1 ]]; then
        echo "" # newline after ^C
        log_warning "Interrupted by user."
    fi

    # Kill the expect subprocess if still running
    if [[ -n "$EXPECT_PID" ]]; then
        if kill -0 "$EXPECT_PID" 2>/dev/null; then
            log_info "Terminating expect session (PID $EXPECT_PID)..."
            kill "$EXPECT_PID" 2>/dev/null || true
            # Give it a moment, then force-kill
            sleep 1
            kill -0 "$EXPECT_PID" 2>/dev/null && kill -9 "$EXPECT_PID" 2>/dev/null || true
        fi
        EXPECT_PID=""
    fi

    # Clean up stale connections for the current target
    if [[ -n "$CURRENT_HOST" && -n "$CURRENT_PORT" ]]; then
        log_info "Cleaning up connections to $CURRENT_HOST:$CURRENT_PORT for target $CURRENT_TARGET..."
        kill_stale_connections "$CURRENT_HOST" "$CURRENT_PORT"
    fi

    # Print run summary
    echo ""
    log_info "=================================================="
    log_info "                   RUN SUMMARY                    "
    log_info "=================================================="
    log_info "  Passed:      $TARGETS_PASSED"
    log_info "  Failed:      $TARGETS_FAILED"
    log_info "  Skipped:     $TARGETS_SKIPPED"
    if [[ -n "$FAILED_LIST" ]]; then
        log_info "  Failed targets: $FAILED_LIST"
    fi
    if [[ $INTERRUPTED -eq 1 ]]; then
        log_info "  Status:      INTERRUPTED"
    elif [[ $TARGETS_FAILED -gt 0 ]]; then
        log_info "  Status:      FAILURE"
    else
        log_info "  Status:      SUCCESS"
    fi
    log_info "=================================================="

    exit "$exit_code"
}

handle_sigint() {
    INTERRUPTED=1
    exit 130
}

handle_sigterm() {
    INTERRUPTED=1
    exit 143
}

trap cleanup EXIT
trap handle_sigint INT
trap handle_sigterm TERM

# ── Helpers ───────────────────────────────────────────────
expand_env_vars() {
    # Replace ${VAR} placeholders with environment variable values
    local text="$1"
    while [[ "$text" =~ \$\{([A-Za-z_][A-Za-z0-9_]*)\} ]]; do
        local var="${BASH_REMATCH[1]}"
        local val="${!var:-}"
        if [[ -z "$val" ]]; then
            log_error "Environment variable '$var' is not set"
            return 1
        fi
        text="${text/\$\{$var\}/$val}"
    done
    echo "$text"
}

kill_stale_connections() {
    local host="$1" port="$2"
    local my_pid=$$
    local pids=()

    # Try ss
    if command -v ss &>/dev/null; then
        while IFS= read -r line; do
            if [[ "$line" == *"pid="* ]]; then
                local pid
                pid=$(echo "$line" | grep -oP 'pid=\K[0-9]+' || true)
                if [[ -n "$pid" && "$pid" != "$my_pid" ]]; then
                    pids+=("$pid")
                fi
            fi
        done < <(ss -tnp dst "$host:$port" 2>/dev/null || true)
    fi

    # Try lsof as fallback
    if command -v lsof &>/dev/null; then
        while IFS= read -r pid; do
            pid=$(echo "$pid" | tr -d '[:space:]')
            if [[ -n "$pid" && "$pid" != "$my_pid" ]]; then
                pids+=("$pid")
            fi
        done < <(lsof -i "@$host:$port" -t 2>/dev/null || true)
    fi

    # Deduplicate and kill
    local seen=()
    for pid in "${pids[@]+"${pids[@]}"}"; do
        local already=0
        for s in "${seen[@]+"${seen[@]}"}"; do
            [[ "$s" == "$pid" ]] && already=1 && break
        done
        if [[ $already -eq 0 ]]; then
            seen+=("$pid")
            log_info "Killing stale process (PID $pid) connected to $host:$port"
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
}

# ── Expect-based telnet session ───────────────────────────
#
# Runs the full sequence: connect → wait for autoboot →
# stop autoboot → boot → wait for prompt → run commands.
#
# Returns: 0 on success, non-zero on failure.
#
run_expect_session() {
    local host="$1"
    local port="$2"
    local timeout="$3"
    local reboot_timeout="$4"
    local provisioner_prompt="$5"
    local vxworks_prompt="$6"
    local autoboot_pattern="$7"
    local boot_cmd="$8"
    shift 8
    # Remaining args are command entries: "cmd|wait_for|timeout" ...
    local commands=("$@")

    # Build the expect command list
    local expect_cmds=""
    for entry in "${commands[@]}"; do
        local cmd wait_for cmd_timeout
        cmd=$(echo "$entry" | cut -d'|' -f1)
        wait_for=$(echo "$entry" | cut -d'|' -f2)
        cmd_timeout=$(echo "$entry" | cut -d'|' -f3)
        [[ -z "$cmd_timeout" || "$cmd_timeout" == "null" ]] && cmd_timeout="$timeout"

        expect_cmds+="
        set timeout $cmd_timeout
        log_user 1
        send -- \"$cmd\r\"
        puts \"\n\\[INFO\\] >> $cmd\"
        "

        if [[ -n "$wait_for" && "$wait_for" != "null" ]]; then
            expect_cmds+="
        expect {
            -re {$wait_for} {
                puts \"\n\\[INFO\\] Pattern matched for: $cmd\"
            }
            timeout {
                puts \"\n\\[ERROR\\] Timed out waiting for pattern '$wait_for' after command: $cmd\"
                exit 1
            }
            eof {
                puts \"\n\\[ERROR\\] Connection closed unexpectedly after command: $cmd\"
                exit 1
            }
        }
        "
        else
            # Wait for the provisioner prompt
            expect_cmds+="
        expect {
            -- \"$provisioner_prompt\" {
                puts \"\n\\[INFO\\] Prompt received after: $cmd\"
            }
            timeout {
                puts \"\n\\[ERROR\\] Timed out waiting for prompt after command: $cmd\"
                exit 1
            }
            eof {
                puts \"\n\\[ERROR\\] Connection closed unexpectedly after command: $cmd\"
                exit 1
            }
        }
        "
        fi
    done

    # Run expect via heredoc piped through process substitution
    local expect_exit=0
    expect <(cat <<EXPECT_SCRIPT
log_user 1
set timeout $reboot_timeout

puts "\[INFO\] Connecting to $host:$port ..."

# Retry connection
set max_retries 12
set retry_delay 10
for {set attempt 1} {\$attempt <= \$max_retries} {incr attempt} {
    if {[catch {spawn telnet $host $port} err]} {
        if {\$attempt < \$max_retries} {
            puts "\[WARNING\] Connection attempt \$attempt/\$max_retries failed, retrying in \${retry_delay}s..."
            sleep \$retry_delay
        } else {
            puts "\[ERROR\] Failed to connect after \$max_retries attempts"
            exit 1
        }
    } else {
        break
    }
}

puts "\[INFO\] Console connected to $host:$port"

# Wait for autoboot pattern
puts "\[INFO\] Waiting for autoboot pattern '$autoboot_pattern'..."
expect {
    -- "$autoboot_pattern" {
        puts "\n\[INFO\] Autoboot detected, sending Enter."
    }
    timeout {
        puts "\n\[ERROR\] Timed out waiting for autoboot pattern."
        exit 1
    }
    eof {
        puts "\n\[ERROR\] Connection closed before autoboot pattern."
        exit 1
    }
}

send "\r\n"

# Wait for VxWorks prompt
expect {
    -- "$vxworks_prompt" {
        puts "\n\[INFO\] VxWorks prompt '$vxworks_prompt' received."
    }
    timeout {
        puts "\n\[ERROR\] Timed out waiting for VxWorks prompt."
        exit 1
    }
    eof {
        puts "\n\[ERROR\] Connection closed before VxWorks prompt."
        exit 1
    }
}

# Send boot command
puts "\[INFO\] >> $boot_cmd"
send "$boot_cmd\r\n"

# Consume the command echo (the prompt VPX3-xxxx=> is re-displayed in the echo,
# which would falsely match the vxworks_prompt pattern)
set timeout 5
expect -re "bootprov"
set timeout $reboot_timeout

# Wait for provisioner prompt (or detect boot failure via VxWorks prompt)
expect {
    -- "$provisioner_prompt" {
        puts "\n\[INFO\] Provisioner prompt '$provisioner_prompt' received. Waiting for boot to settle..."
    }
    -- "$vxworks_prompt" {
        puts "\n\[ERROR\] Boot failed — returned to VxWorks prompt."
        puts "\n\[ERROR\] Boot output:"
        puts \$expect_out(buffer)
        exit 1
    }
    timeout {
        puts "\n\[ERROR\] Timed out after boot command."
        exit 1
    }
    eof {
        puts "\n\[ERROR\] Connection closed after boot command."
        exit 1
    }
}

sleep $SETTLE_DELAY
send "\r\n"
set timeout $timeout
expect {
    -- "$provisioner_prompt" {}
    timeout {
        puts "\n\[ERROR\] Timed out waiting for clean prompt after settle."
        exit 1
    }
}

puts "\[INFO\] Target is ready."

# ── Run commands ──
$expect_cmds

puts "\n\[INFO\] All commands completed."
send "exit\r"
expect eof
EXPECT_SCRIPT
) 2>&1 &
    EXPECT_PID=$!
    wait "$EXPECT_PID" || expect_exit=$?
    EXPECT_PID=""

    return "$expect_exit"
}

# ── Main ──────────────────────────────────────────────────
usage() {
    echo "Usage: $0 <commands.json> <targets.json> <component> [target1 target2 ...]"
    exit 2
}

[[ $# -lt 3 ]] && usage

COMMANDS_PATH="$1"
TARGETS_PATH="$2"
COMPONENT="$3"
shift 3
SELECTED_TARGETS=("${@}")

log_info "Starting VxWorks target operations."

# ── Validate dependencies ────────────────────────────────
missing_deps=()
for dep in expect jq telnet; do
    if ! command -v "$dep" &>/dev/null; then
        missing_deps+=("$dep")
    fi
done
if [[ ${#missing_deps[@]} -gt 0 ]]; then
    log_error "Missing required dependencies: ${missing_deps[*]}"
    log_error "Install them before running this script."
    exit 2
fi

# ── Validate input files ─────────────────────────────────
for f in "$COMMANDS_PATH" "$TARGETS_PATH"; do
    if [[ ! -f "$f" ]]; then
        log_error "File not found: $f"
        exit 2
    fi
    if ! jq empty "$f" 2>/dev/null; then
        log_error "Invalid JSON in: $f"
        exit 2
    fi
done

# ── Load configuration ───────────────────────────────────
if ! jq -e ".components.\"$COMPONENT\"" "$COMMANDS_PATH" &>/dev/null; then
    available=$(jq -r '.components | keys | join(", ")' "$COMMANDS_PATH")
    log_error "Component '$COMPONENT' not found. Available: $available"
    exit 2
fi

if ! jq -e ".components.\"$COMPONENT\".targets" "$COMMANDS_PATH" &>/dev/null; then
    log_error "Component '$COMPONENT' has no 'targets' array."
    exit 2
fi

log_info "Running component: $COMPONENT"

# Iterate over target entries in the component
num_entries=$(jq ".components.\"$COMPONENT\".targets | length" "$COMMANDS_PATH")

if [[ "$num_entries" -eq 0 ]]; then
    log_warning "Component '$COMPONENT' has an empty targets list. Nothing to do."
    exit 0
fi

for (( i=0; i<num_entries; i++ )); do
    # "target" can be a string or array — flatten to list
    target_type=$(jq -r ".components.\"$COMPONENT\".targets[$i].target | type" "$COMMANDS_PATH")
    if [[ "$target_type" == "array" ]]; then
        mapfile -t target_names < <(jq -r ".components.\"$COMPONENT\".targets[$i].target[]" "$COMMANDS_PATH")
    elif [[ "$target_type" == "string" ]]; then
        target_names=("$(jq -r ".components.\"$COMPONENT\".targets[$i].target" "$COMMANDS_PATH")")
    else
        log_error "Entry $i: 'target' must be a string or array, got '$target_type'"
        TARGETS_FAILED=$((TARGETS_FAILED + 1))
        continue
    fi

    if [[ ${#target_names[@]} -eq 0 ]]; then
        log_warning "Entry $i: empty target list, skipping."
        continue
    fi

    # Read commands for this entry
    num_cmds=$(jq ".components.\"$COMPONENT\".targets[$i].commands | length" "$COMMANDS_PATH")
    if [[ "$num_cmds" -eq 0 ]]; then
        log_warning "Entry $i: no commands defined, skipping."
        TARGETS_SKIPPED=$((TARGETS_SKIPPED + ${#target_names[@]}))
        continue
    fi

    cmd_entries=()
    env_error=0
    for (( j=0; j<num_cmds; j++ )); do
        raw_cmd=$(jq -r ".components.\"$COMPONENT\".targets[$i].commands[$j].cmd" "$COMMANDS_PATH")
        wait_for=$(jq -r ".components.\"$COMPONENT\".targets[$i].commands[$j].wait_for // \"null\"" "$COMMANDS_PATH")
        cmd_timeout=$(jq -r ".components.\"$COMPONENT\".targets[$i].commands[$j].timeout // \"null\"" "$COMMANDS_PATH")

        if ! cmd=$(expand_env_vars "$raw_cmd"); then
            env_error=1
            break
        fi
        cmd_entries+=("${cmd}|${wait_for}|${cmd_timeout}")
    done

    if [[ $env_error -eq 1 ]]; then
        log_error "Skipping targets ${target_names[*]} due to missing environment variable."
        TARGETS_FAILED=$((TARGETS_FAILED + ${#target_names[@]}))
        for n in "${target_names[@]}"; do
            FAILED_LIST="${FAILED_LIST:+$FAILED_LIST, }$n"
        done
        continue
    fi

    for name in "${target_names[@]}"; do
        # Bail early on interrupt
        [[ $INTERRUPTED -eq 1 ]] && break 2

        # Filter by selected targets if any were given
        if [[ ${#SELECTED_TARGETS[@]} -gt 0 ]]; then
            local_match=0
            for sel in "${SELECTED_TARGETS[@]}"; do
                [[ "$sel" == "$name" ]] && local_match=1 && break
            done
            if [[ $local_match -eq 0 ]]; then
                TARGETS_SKIPPED=$((TARGETS_SKIPPED + 1))
                continue
            fi
        fi

        # Look up target in targets.json
        if ! jq -e ".targets.\"$name\"" "$TARGETS_PATH" &>/dev/null; then
            log_error "Target '$name' not found in $TARGETS_PATH"
            TARGETS_FAILED=$((TARGETS_FAILED + 1))
            FAILED_LIST="${FAILED_LIST:+$FAILED_LIST, }$name"
            continue
        fi

        host=$(jq -r ".targets.\"$name\".host" "$TARGETS_PATH")
        port=$(jq -r ".targets.\"$name\".port" "$TARGETS_PATH")
        t_timeout=$(jq -r ".targets.\"$name\".timeout // \"$DEFAULT_TIMEOUT\"" "$TARGETS_PATH")
        t_prov_prompt=$(jq -r ".targets.\"$name\".provisioner_prompt // \"$DEFAULT_PROVISIONER_PROMPT\"" "$TARGETS_PATH")

        # Validate host/port
        if [[ -z "$host" || "$host" == "null" ]]; then
            log_error "Target '$name': missing 'host' in $TARGETS_PATH"
            TARGETS_FAILED=$((TARGETS_FAILED + 1))
            FAILED_LIST="${FAILED_LIST:+$FAILED_LIST, }$name"
            continue
        fi
        if [[ -z "$port" || "$port" == "null" ]]; then
            log_error "Target '$name': missing 'port' in $TARGETS_PATH"
            TARGETS_FAILED=$((TARGETS_FAILED + 1))
            FAILED_LIST="${FAILED_LIST:+$FAILED_LIST, }$name"
            continue
        fi

        # Track active target for cleanup
        CURRENT_TARGET="$name"
        CURRENT_HOST="$host"
        CURRENT_PORT="$port"

        log_info "=================================================="
        log_info "Target: $name ($host:$port)"
        log_info "=================================================="

        # Reboot the target (local command)
        reboot_cmd=$(printf "$REBOOT_CMD_TEMPLATE" "$name")
        log_info "Running local command: $reboot_cmd"
        if reboot_out=$($reboot_cmd 2>&1); then
            log_info "Reboot command output: $reboot_out"
        else
            log_warning "Reboot command exited with error: $reboot_out"
        fi

        # Kill stale connections before connecting
        kill_stale_connections "$host" "$port"

        # Run expect session — capture exit code without set -e killing us
        session_exit=0
        run_expect_session \
            "$host" "$port" \
            "$t_timeout" "$DEFAULT_REBOOT_TIMEOUT" \
            "$t_prov_prompt" "$DEFAULT_VXWORKS_PROMPT" \
            "$DEFAULT_AUTOBOOT_PATTERN" "$DEFAULT_BOOT_CMD" \
            "${cmd_entries[@]}" || session_exit=$?

        # Clean up connections regardless of outcome
        kill_stale_connections "$host" "$port"

        if [[ $session_exit -ne 0 ]]; then
            log_error "FAILED on target $name (expect exited with code $session_exit)"
            TARGETS_FAILED=$((TARGETS_FAILED + 1))
            FAILED_LIST="${FAILED_LIST:+$FAILED_LIST, }$name"
        else
            log_info "=== $name: All commands completed ==="
            TARGETS_PASSED=$((TARGETS_PASSED + 1))
        fi

        # Clear active-target state
        CURRENT_TARGET=""
        CURRENT_HOST=""
        CURRENT_PORT=""

        sleep 5
    done
done

# Exit with failure if any target failed
if [[ $TARGETS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
