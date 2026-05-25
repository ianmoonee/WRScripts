#!/bin/bash

LOG_FILE="$(pwd)/target_reservations_log.txt"
TEMP_FILE="$(pwd)/target_reservations_log.tmp"
HISTORY_FILE="$(pwd)/reservation_history.txt"
targets=(34340 35049 42441 90932 84926 93870 90138 61630 63281 45183 69788)

# Colors
RED="\033[31m"
GREEN="\033[32m"
YELLOW="\033[33m"
MAGENTA="\033[35m"
CYAN="\033[36m"
RESET="\033[0m"

declare -A previous_elapsed
declare -A previous_owner
declare -A previous_note

# Restore previous log to rebuild elapsed info
restore_previous() {
    if [[ -f "$LOG_FILE" ]]; then
        while IFS= read -r line; do
            if [[ "$line" =~ Target\ ([0-9]+).*RESERVED\ by\ ([^[:space:]]+).*Elapsed:\ ([0-9]+)h\ ([0-9]+)m ]]; then
                target="${BASH_REMATCH[1]}"
                owner="${BASH_REMATCH[2]}"
                hours="${BASH_REMATCH[3]}"
                minutes="${BASH_REMATCH[4]}"
                previous_elapsed[$target]=$((hours * 3600 + minutes * 60))
                previous_owner[$target]="$owner"
            fi
        done < "$LOG_FILE"
    fi
}

# Log reservation history when reservation ends or owner/note changes
log_history() {
    local target=$1
    local owner=$2
    local elapsed=$3
    local note=$4
    local end_epoch=$(date +%s)
    local start_epoch=$((end_epoch - elapsed))
    local start_time=$(date -d "@$start_epoch" '+%Y-%m-%d %H:%M:%S')
    local end_time=$(date -d "@$end_epoch" '+%Y-%m-%d %H:%M:%S')
    local hours=$((elapsed / 3600))
    local minutes=$(( (elapsed % 3600) / 60 ))

    echo "Target $target was reserved by user \"$owner\" from $start_time to $end_time TOTAL TIME = ${hours}h ${minutes}m (Note: \"$note\")" >> "$HISTORY_FILE"
}

while true; do
    restore_previous

    # Write everything to temporary file first
    > "$TEMP_FILE"
    header="${MAGENTA}===== Latest Check: $(date '+%Y-%m-%d %H:%M:%S') =====${RESET}"
    echo -e "$header" >> "$TEMP_FILE"

    reserved_count=0
    free_count=0

    for target in "${targets[@]}"; do
        output=$(vlmTool getAttr -t "$target" all)
        reserver=$(echo "$output" | grep -Po 'By\s*:\s*\K\S+')
        note=$(echo "$output" | grep -Po 'Note\s*:\s*\K.*')
        reserver="${reserver%@*}"

        if [[ -n "$reserver" ]]; then
            if [[ "${previous_owner[$target]}" != "$reserver" || "${previous_note[$target]}" != "$note" ]]; then
                if [[ -n "${previous_owner[$target]}" ]]; then
                    log_history "$target" "${previous_owner[$target]}" "${previous_elapsed[$target]}" "${previous_note[$target]}"
                fi
                elapsed=60
            else
                elapsed=$((previous_elapsed[$target] + 60))
            fi

            previous_elapsed[$target]=$elapsed
            previous_owner[$target]="$reserver"
            previous_note[$target]="$note"

            hours=$((elapsed / 3600))
            minutes=$(( (elapsed % 3600) / 60 ))

            printf "%s ${RED}|${RESET} Target ${GREEN}%5s${RESET} ${YELLOW}|${RESET} RESERVED by ${CYAN}%-8s${RESET} ${MAGENTA}|${RESET} Elapsed: %dh%3dm ${MAGENTA}|${RESET} Note: ${CYAN}%s${RESET}\n" \
                "$(date '+%Y-%m-%d %H:%M:%S')" "$target" "$reserver" "$hours" "$minutes" "$note" >> "$TEMP_FILE"
            ((reserved_count++))
        else
            if [[ -n "${previous_owner[$target]}" ]]; then
                log_history "$target" "${previous_owner[$target]}" "${previous_elapsed[$target]}" "${previous_note[$target]}"
                unset previous_owner[$target]
                unset previous_elapsed[$target]
                unset previous_note[$target]
            fi

            line="$(date '+%Y-%m-%d %H:%M:%S') ${RED}|${RESET} Target ${GREEN}$target${RESET} ${YELLOW}|${RESET} FREE"
            echo -e "$line" >> "$TEMP_FILE"
            ((free_count++))
        fi
    done

    summary="${CYAN}Reserved:${RESET} $reserved_count ${MAGENTA}|${RESET} ${CYAN}Free:${RESET} $free_count"
    echo -e "$summary" >> "$TEMP_FILE"

    # ✅ Atomic update: replace main log with temp file
    mv "$TEMP_FILE" "$LOG_FILE"
    chmod 644 "$LOG_FILE"

    sleep 60
done
