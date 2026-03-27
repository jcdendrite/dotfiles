#!/usr/bin/env bash

input=$(cat)

# Extract fields from JSON input
model=$(echo "$input" | jq -r '.model.display_name // "Unknown Model"')
cwd=$(echo "$input" | jq -r '.workspace.current_dir // .cwd // ""')
used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
total_cost=$(echo "$input" | jq -r '.cost.total_cost_usd // 0')

# ANSI color codes (dim-friendly)
RESET='\033[0m'
BOLD='\033[1m'
DIM='\033[2m'
CYAN='\033[36m'
GREEN='\033[32m'
YELLOW='\033[33m'
BLUE='\033[34m'
MAGENTA='\033[35m'
RED='\033[31m'

# --- Context progress bar ---
build_bar() {
    local pct="${1:-0}"
    local width=20
    local filled=$(( pct * width / 100 ))
    local empty=$(( width - filled ))
    local bar=""
    for i in $(seq 1 $filled); do bar="${bar}#"; done
    for i in $(seq 1 $empty); do bar="${bar}-"; done
    echo "$bar"
}

if [ -n "$used_pct" ]; then
    used_int=$(printf "%.0f" "$used_pct")
    bar=$(build_bar "$used_int")
    # Color the bar based on usage
    if [ "$used_int" -ge 85 ]; then
        bar_color="$RED"
    elif [ "$used_int" -ge 60 ]; then
        bar_color="$YELLOW"
    else
        bar_color="$GREEN"
    fi
    ctx_display=$(printf "${bar_color}[${bar}]${RESET} ${used_int}%%")
else
    ctx_display=$(printf "${DIM}[--------------------] --%${RESET}")
fi

# --- Session cost ---
cost_display=$(printf "\$%.4f" "$total_cost")

# --- Git branch ---
git_branch=""
if [ -n "$cwd" ] && [ -d "$cwd" ]; then
    branch=$(git -C "$cwd" --no-optional-locks symbolic-ref --short HEAD 2>/dev/null)
    if [ -n "$branch" ]; then
        git_branch=$(printf " ${MAGENTA}(%s)${RESET}" "$branch")
    fi
fi

# --- Working directory (shorten home) ---
home_dir="$HOME"
short_cwd="${cwd/#$home_dir/~}"

# --- Assemble status line ---
echo -e "${CYAN}${model}${RESET}  ${ctx_display}  ${YELLOW}${cost_display}${RESET}  ${BLUE}${short_cwd}${RESET}${git_branch}"
