# Contributing to YNAB Amazon Categorizer

Thanks for your interest in contributing! This project automates YNAB categorization using Claude AI, with special handling for Amazon purchases.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/ynab-amazon-categorizer.git`
3. Set up the development environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

## Development Workflow

1. Create a feature branch: `git checkout -b feature/your-feature-name`
2. Make your changes
3. Test thoroughly with your own YNAB account
4. Commit with clear messages: `git commit -m "Add: feature description"`
5. Push to your fork: `git push origin feature/your-feature-name`
6. Open a pull request

## Code Style

- Follow PEP 8 for Python code
- Use type hints where helpful
- Keep functions focused and well-documented
- Add comments for complex logic (especially the Amazon matching algorithms)

## Testing

Before submitting:

1. Test with real YNAB data (use a test budget if possible)
2. Verify Amazon matching with various scenarios:
   - Single-item orders
   - Multi-item orders
   - Split shipments
   - Mixed payment methods (gift card + credit card)
3. Test error handling (missing CSVs, API failures, etc.)

## Areas for Contribution

### High Priority

- **Windows/WSL support**: Adapt file paths and shell scripts
- **Gmail-based matching**: Parse Amazon confirmation emails as CSV alternative
- **Better error messages**: More helpful diagnostics when matching fails

### Medium Priority

- **Other retailers**: Target, Walmart, etc.
- **Recurring transaction handling**: Auto-categorize known recurring charges
- **Budget goal integration**: Suggest categories based on budget goal progress

### Lower Priority

- **Web UI**: Browser-based interface for non-technical users
- **Batch reporting**: Summary of categorization changes made
- **Category learning**: Improve Claude's suggestions based on user corrections

## Pull Request Guidelines

- **One feature per PR**: Keep changes focused
- **Update documentation**: README, comments, docstrings
- **Test edge cases**: Don't just test the happy path
- **Explain your changes**: Why is this improvement needed?

## Code Architecture

### Key Files

- **ynab_cat.py**: Main Python script
  - `YNAB` class: API wrapper for YNAB REST API
  - `load_azad_csvs()`: Imports and merges Amazon data
  - `match_amazon()`: Core matching algorithm (Pass 1: exact, Pass 2: heuristic)
  - `_find_item_subset()`: Subset-sum solver for split shipments
  - `cmd_apply()`: Interactive categorization loop

- **ynab-wrapper.sh**: Shell wrapper that routes CSVs and launches Python
  - `identify_csv()`: Detects CSV type from headers
  - Auto-imports from Downloads, manages file lifecycle

### Amazon Matching Algorithm

1. **Build charge index**: Extract `(amount, date, order)` tuples from Transactions CSV
2. **Pass 1 - Exact match**: Match YNAB charges to Amazon charges by amount (±$0.05) and date (±2 days)
3. **Subset-sum for split shipments**: When charge ≠ order total, find which items sum to charge (testing 0-12% tax)
4. **Pass 2 - Heuristic**: For unmatched transactions, try matching by order total + date window
5. **Prompt construction**: Send matched items to Claude with category list and past examples

### API Usage

- **YNAB API**: Read-only except for `update_category()` and `split_transaction()` writes
- **Claude API**: One request per uncached transaction, using Haiku 4.5 for cost efficiency
- **Rate limits**: YNAB allows 200 req/hr; Claude has generous per-account limits

## Sensitive Data Handling

**Never commit**:
- API keys or tokens
- YNAB budget data
- Amazon CSVs
- Personal transaction information
- Cache files with real transaction data

**Always**:
- Use example/dummy data in documentation
- Store credentials in environment variables or Keychain
- Add sensitive patterns to .gitignore

## Questions?

Open an issue or discussion on GitHub. For security issues, email directly rather than opening a public issue.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
