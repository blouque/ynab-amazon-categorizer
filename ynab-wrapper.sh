#!/usr/bin/env bash
# ynab — one-shot YNAB categorization workflow.
#
# What this does:
#   1. Looks for AZAD CSV exports in ~/Downloads/ (handles name collisions)
#   2. Inspects each CSV's header to identify type (items/orders/transactions)
#   3. Moves them to ~/.config/ynab-claude/ with stable names
#   4. Activates the venv and runs ynab_cat.py apply with all the right flags
#
# Usage:
#   ynab                          # default — last 30 days, auto-approve
#   ynab --days 60                # custom day window
#   ynab --no-approve             # leave transactions unapproved for review
#   ynab --auto-high              # auto-fire high-confidence suggestions
#   ynab --                       # any args after -- pass straight to ynab_cat.py
#
# Anything you'd pass to ynab_cat.py apply works.

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────
# Paths — edit these if you moved things
# ──────────────────────────────────────────────────────────────────────────
YNAB_HOME="${YNAB_HOME:-$HOME/ynab-claude}"
YNAB_CONFIG="${YNAB_CONFIG:-$HOME/.config/ynab-claude}"
YNAB_DOWNLOADS="${YNAB_DOWNLOADS:-$HOME/Downloads}"
YNAB_SCRIPT="$YNAB_HOME/ynab_cat.py"
YNAB_VENV="$YNAB_HOME/.venv"

# How recent a CSV must be in ~/Downloads to be considered. AZAD writes the
# same filename every time (amazon_order_history.csv), so we only pick up
# files modified in the last hour by default — tune via env if needed.
YNAB_RECENT_MINUTES="${YNAB_RECENT_MINUTES:-120}"

ITEMS_CSV="$YNAB_CONFIG/amazon_items.csv"
ORDERS_CSV="$YNAB_CONFIG/amazon_orders.csv"
TX_CSV="$YNAB_CONFIG/amazon_transactions.csv"

# ──────────────────────────────────────────────────────────────────────────
# Sanity checks
# ──────────────────────────────────────────────────────────────────────────
if [ ! -d "$YNAB_HOME" ]; then
  echo "ERROR: $YNAB_HOME not found. Set YNAB_HOME or move the project." >&2
  exit 1
fi
if [ ! -x "$YNAB_VENV/bin/python3" ]; then
  echo "ERROR: venv not found at $YNAB_VENV. Did you set up the project?" >&2
  exit 1
fi
if [ ! -f "$YNAB_SCRIPT" ]; then
  echo "ERROR: $YNAB_SCRIPT not found." >&2
  exit 1
fi
mkdir -p "$YNAB_CONFIG"

# ──────────────────────────────────────────────────────────────────────────
# Step 1: Identify and import any new AZAD CSVs from ~/Downloads
# ──────────────────────────────────────────────────────────────────────────
# Each AZAD report has a deterministic header. We sniff the first line of
# every recent CSV in Downloads and route it accordingly.

identify_csv() {
  # Read the first line, lowercase it, return one of: items|orders|transactions|unknown
  local file="$1"
  local header
  header="$(head -n 1 "$file" 2>/dev/null | tr -d '\r' | tr '[:upper:]' '[:lower:]')"
  case "$header" in
    *"order id"*"description"*"price"*"asin"*)         echo items ;;
    *"order id"*"items"*"date"*"total"*"payments"*)    echo orders ;;
    *"date"*"order ids"*"vendor"*"card_details"*)      echo transactions ;;
    *) echo unknown ;;
  esac
}

# Build a list of candidate CSVs from Downloads, ordered newest-first
# Use find to be portable; -mmin requires GNU/BSD compat — works on macOS.
candidates=()
while IFS= read -r line; do
  [ -n "$line" ] && candidates+=("$line")
done < <(
  find "$YNAB_DOWNLOADS" -maxdepth 1 -type f -name "*.csv" \
    -mmin "-$YNAB_RECENT_MINUTES" 2>/dev/null \
    | xargs -I{} stat -f "%m %N" "{}" 2>/dev/null \
    | sort -rn \
    | awk '{ $1=""; sub(/^ /, ""); print }'
)

# Track the most recent file of each type (bash 3.2 compat — no associative arrays)
latest_items=""
latest_orders=""
latest_transactions=""

for f in "${candidates[@]}"; do
  type="$(identify_csv "$f")"
  case "$type" in
    items)
      [ -z "$latest_items" ] && latest_items="$f"
      ;;
    orders)
      [ -z "$latest_orders" ] && latest_orders="$f"
      ;;
    transactions)
      [ -z "$latest_transactions" ] && latest_transactions="$f"
      ;;
  esac
done

imported_any=0
for pair in "items:$latest_items:$ITEMS_CSV" \
            "orders:$latest_orders:$ORDERS_CSV" \
            "transactions:$latest_transactions:$TX_CSV"; do
  IFS=: read -r type src dest <<< "$pair"
  if [ -n "$src" ]; then
    cp "$src" "$dest"
    rm "$src"
    echo "  ✓ imported $type CSV: $(basename "$src") → $(basename "$dest")"
    imported_any=1
  fi
done

if [ "$imported_any" -eq 0 ]; then
  echo "  · no new AZAD CSVs in $YNAB_DOWNLOADS (last $YNAB_RECENT_MINUTES min)"
fi

# ──────────────────────────────────────────────────────────────────────────
# Step 2: Verify we have at least one CSV to work with (warn but allow run)
# ──────────────────────────────────────────────────────────────────────────
csv_args=()
if [ -f "$ITEMS_CSV" ]; then
  csv_args+=("--amazon-items-csv" "$ITEMS_CSV")
fi
if [ -f "$ORDERS_CSV" ]; then
  csv_args+=("--amazon-orders-csv" "$ORDERS_CSV")
fi
if [ -f "$TX_CSV" ]; then
  csv_args+=("--amazon-transactions-csv" "$TX_CSV")
fi

if [ ${#csv_args[@]} -eq 0 ]; then
  echo "  ⚠️  no Amazon CSVs available — Amazon transactions will fall back to scraper or skip"
fi

# Show CSV ages so you know if they're stale
for f in "$ITEMS_CSV" "$ORDERS_CSV" "$TX_CSV"; do
  if [ -f "$f" ]; then
    age_days=$(( ( $(date +%s) - $(stat -f %m "$f") ) / 86400 ))
    if [ "$age_days" -ge 7 ]; then
      echo "  ⚠️  $(basename "$f") is ${age_days}d old — consider re-exporting from AZAD"
    fi
  fi
done

# ──────────────────────────────────────────────────────────────────────────
# Step 3: Default args + pass-through
# ──────────────────────────────────────────────────────────────────────────
# Default to last 30 days unless caller overrode it. We detect --days in $@.
extra_args=()
has_days=0
for a in "$@"; do
  if [ "$a" = "--days" ] || [[ "$a" == --days=* ]]; then
    has_days=1
    break
  fi
done
if [ "$has_days" -eq 0 ]; then
  extra_args+=("--days" "30")
fi

# ──────────────────────────────────────────────────────────────────────────
# Step 4: Run
# ──────────────────────────────────────────────────────────────────────────
exec "$YNAB_VENV/bin/python3" "$YNAB_SCRIPT" apply \
  "${extra_args[@]}" \
  "${csv_args[@]}" \
  "$@"
