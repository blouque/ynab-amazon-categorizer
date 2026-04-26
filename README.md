# YNAB Amazon Categorizer

Automate YNAB transaction categorization using Claude AI, with special handling for Amazon purchases. Turns hours of manual categorization into minutes.

## What This Does

- **Categorizes YNAB transactions** using Claude AI with full context from your budget history
- **Matches Amazon orders to bank charges** using the Amazon Order History Reporter Chrome extension
- **Handles split shipments** automatically using subset-sum matching
- **Writes human-readable memos** for every Amazon transaction (e.g., "USB hub, hole saw, desk grommet")
- **Auto-approves by default** — one keystroke per transaction

## Features

- **Near-100% Amazon match rate** when using the premium Amazon Order History Reporter extension
- **Smart split detection** — figures out which items were in each shipment when Amazon splits an order
- **Item-level categorization** — splits multi-category Amazon orders across the right budget categories
- **Persistent memory** — learns from your categorization patterns over time
- **One-command workflow** — downloads CSVs, routes them, runs categorization automatically

## Prerequisites

1. **YNAB account** with API access
2. **Anthropic API key** (Claude)
3. **Python 3.11+**
4. **Chrome** with the [Amazon Order History Reporter](https://chrome.google.com/webstore/detail/amazon-order-history-repo/mgkilgclilajckgnedgjgnfdokkgnibi) extension
   - Premium subscription required for full functionality (provides per-shipment charge data)
5. **macOS or Linux** (tested on macOS; Linux should work with minor path adjustments)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/ynab-amazon-categorizer.git
cd ynab-amazon-categorizer
```

### 2. Set up Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure credentials

Store your API keys in macOS Keychain (recommended) or environment variables:

```bash
# Add to Keychain (macOS)
security add-generic-password -a "$USER" -s ynab-api-token -w 'YOUR_YNAB_TOKEN'
security add-generic-password -a "$USER" -s anthropic-api-key -w 'YOUR_ANTHROPIC_KEY'

# Add to ~/.zshrc or ~/.bashrc
export YNAB_API_TOKEN=$(security find-generic-password -a "$USER" -s ynab-api-token -w 2>/dev/null)
export YNAB_BUDGET_ID="YOUR_BUDGET_ID"  # Get this from: python ynab_cat.py budgets
export ANTHROPIC_API_KEY=$(security find-generic-password -a "$USER" -s anthropic-api-key -w 2>/dev/null)
```

**Finding your YNAB Budget ID:**

```bash
source .venv/bin/activate
python ynab_cat.py budgets
```

### 4. Install the wrapper script

```bash
chmod +x install-ynab-wrapper.sh ynab-wrapper.sh
./install-ynab-wrapper.sh ynab-wrapper.sh
source ~/.zshrc  # or ~/.bashrc
```

### 5. Verify installation

```bash
ynab --help
```

## Usage

### Daily Workflow

1. **Export Amazon data** (do this weekly or whenever you remember):
   - Open the Amazon Order History Reporter extension in Chrome
   - Click "Items report" → Save (lands in ~/Downloads as `amazon_order_history.csv`)
   - Click "Orders report" → Save (Chrome renames to `amazon_order_history (1).csv`)
   - Click "Transactions report" → Save (Chrome renames to `amazon_order_history (2).csv`)

2. **Run the categorizer**:
   ```bash
   ynab
   ```

3. **Review suggestions**:
   - Press `y` to apply the suggestion and approve
   - Press `e` to manually pick a different category
   - Press `s` to skip (memo still written for Amazon transactions)
   - Press `q` to quit

That's it. The script will:
- Automatically detect and import your Amazon CSVs from ~/Downloads
- Match Amazon charges to orders with near-100% accuracy
- Generate human-readable memos
- Apply categories and approve transactions

### Command Options

```bash
ynab                    # Default: last 30 days, auto-approve
ynab --days 60          # Process last 60 days
ynab --no-approve       # Leave transactions unapproved for manual review in YNAB
ynab --auto-high        # Auto-apply high-confidence suggestions without prompting
ynab --limit 10         # Process only 10 transactions (for testing)
```

### Advanced Usage

Run the Python script directly for more control:

```bash
source .venv/bin/activate

# List your budgets
python ynab_cat.py budgets

# Test API connectivity
python ynab_cat.py test

# Dry-run report (no writes)
python ynab_cat.py suggest --days 30

# Interactive categorization
python ynab_cat.py apply --days 30 \
  --amazon-items-csv ~/.config/ynab-claude/amazon_items.csv \
  --amazon-orders-csv ~/.config/ynab-claude/amazon_orders.csv \
  --amazon-transactions-csv ~/.config/ynab-claude/amazon_transactions.csv
```

## How It Works

### Amazon Matching Pipeline

1. **CSV Import**: Reads Items, Orders, and Transactions CSVs from the Amazon Order History Reporter extension
2. **Exact Charge Matching**: Matches YNAB transactions to Amazon charges by exact amount + date (±2 days)
3. **Subset-Sum Resolution**: For split shipments, determines which items were in each shipment by finding subsets of items whose prices (+ tax) match the charge amount
4. **Partial Shipment Flagging**: When subset-sum can't find a unique solution, flags the transaction and shows all order items for context
5. **Claude Categorization**: Sends item titles + your budget categories + past categorization examples to Claude for intelligent category suggestions
6. **Memo Generation**: Creates concise, human-readable memos like "Vitamin D3 K2 Gummies" or "USB hub, hole saw, desk grommet"

### Smart Features

- **Split-shipment detection**: Recognizes when Amazon charged you twice for one order
- **Mixed-payment handling**: Filters out gift card/rewards charges (they don't appear in your bank)
- **Tax estimation**: Tests 0-12% tax rates to find the best item-to-charge match
- **Defensive validation**: Rejects splits where amounts don't sum to the transaction
- **Cache persistence**: Remembers suggestions across sessions so you can quit and resume

## File Locations

```
~/ynab-claude/                        # Main project directory
  ├── ynab_cat.py                     # Core Python script
  ├── .venv/                          # Python virtual environment
  └── ...

~/.config/ynab-claude/                # Data directory
  ├── amazon_items.csv                # Imported from AZAD extension
  ├── amazon_orders.csv               # Imported from AZAD extension
  ├── amazon_transactions.csv         # Imported from AZAD extension (premium only)
  └── gmail_credentials.json          # (Optional) Gmail OAuth for email-based matching

~/.ynab-cat-cache.json                # Suggestion cache (safe to delete)

~/bin/ynab                            # Wrapper script (added to PATH)
```

## Troubleshooting

### "No Amazon CSVs available"

The wrapper looks for CSVs in `~/Downloads/` modified in the last 2 hours. If you exported earlier:

```bash
# Increase the time window
YNAB_RECENT_MINUTES=480 ynab    # 8 hours
```

Or manually copy CSVs:

```bash
cp ~/Downloads/amazon_order_history.csv ~/.config/ynab-claude/amazon_items.csv
cp ~/Downloads/amazon_order_history\ \(1\).csv ~/.config/ynab-claude/amazon_orders.csv
cp ~/Downloads/amazon_order_history\ \(2\).csv ~/.config/ynab-claude/amazon_transactions.csv
```

### Low Amazon match rate

- **Verify you have all three CSVs**: Items + Orders + Transactions (premium feature)
- **Check CSV freshness**: Export fresh data if your CSVs are >7 days old
- **Look for mixed-payment orders**: Orders paid with gift cards + credit cards may not match cleanly

### "YNAB API rate limit"

YNAB limits to 200 requests/hour. If you hit this:

```bash
ynab --limit 50    # Process fewer transactions per run
```

### API key issues

```bash
# Verify credentials are set
echo $YNAB_API_TOKEN
echo $YNAB_BUDGET_ID
echo $ANTHROPIC_API_KEY

# Test connectivity
python ynab_cat.py test
```

## Cost

- **Claude API (Haiku 4.5)**: ~$0.10-$0.25/month for ~100 Amazon transactions
- **Amazon Order History Reporter Premium**: Check extension pricing
- **YNAB API**: Free (included with YNAB subscription)

## Privacy & Security

- **API keys stored in macOS Keychain** (not plaintext)
- **No data sent to third parties** except:
  - YNAB API (your transaction data)
  - Claude API (transaction amounts, payees, item titles — no account numbers)
- **Amazon data stays local** (CSVs in `~/.config/ynab-claude/`)
- **Cache file is local** (`~/.ynab-cat-cache.json`)

## Contributing

Contributions welcome! Areas for improvement:

- [ ] Windows/WSL support
- [ ] Gmail-based Amazon matching (no extension required)
- [ ] Other e-commerce sites (Target, Walmart, etc.)
- [ ] Recurring transaction detection
- [ ] Budget goal tracking integration
- [ ] Web UI

## License

MIT License - see LICENSE file for details.

## Acknowledgments

- Built with [Anthropic's Claude API](https://www.anthropic.com/)
- Integrates with [YNAB](https://www.ynab.com/)
- Amazon data sourced via [Amazon Order History Reporter](https://chrome.google.com/webstore/detail/amazon-order-history-repo/mgkilgclilajckgnedgjgnfdokkgnibi)

## Author

Created by Brien Louque ([@atersolitas](https://github.com/atersolitas))

---

**Disclaimer**: This is an unofficial tool and is not affiliated with YNAB, Amazon, or Anthropic.
