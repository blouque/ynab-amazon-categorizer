#!/usr/bin/env python3
"""
ynab_cat.py — Standalone YNAB transaction categorizer with Amazon item
matching and Claude-powered suggestions. Replaces ynab-tui entirely.

Talks directly to the YNAB REST API (stable, documented) and uses the
amazon-orders library directly for Amazon scraping. No ynab Python SDK
dependency, no local state to maintain, no fragile abstractions.

USAGE
    python ynab_cat.py budgets                  # list your budgets, get ID
    python ynab_cat.py test                     # test YNAB + Amazon connectivity
    python ynab_cat.py suggest                  # dry-run report (default, safe)
    python ynab_cat.py suggest --days 14
    python ynab_cat.py suggest --model sonnet
    python ynab_cat.py apply                    # interactive review + writeback
    python ynab_cat.py apply --auto-high        # auto-apply high confidence only

ENVIRONMENT
    YNAB_API_TOKEN        required  (https://app.ynab.com/settings/developer)
    YNAB_BUDGET_ID        required  (run `budgets` to find it)
    ANTHROPIC_API_KEY     required
    AMAZON_USERNAME       optional  (Amazon scraping is optional)
    AMAZON_PASSWORD       optional
    AMAZON_OTP_SECRET     optional  (TOTP secret if 2FA enabled)

INSTALL
    python3.11 -m venv ~/ynab-claude/.venv
    source ~/ynab-claude/.venv/bin/activate
    pip install requests anthropic 'amazon-orders==4.0.*'

DESIGN NOTES
- suggest is dry-run, outputs markdown to stdout. Review in any editor.
- apply reads the SAME generated suggestions and prompts y/n/s for each.
  It never touches YNAB without explicit per-transaction approval unless
  you pass --auto-high.
- Amazon matching: amount within $0.10, date within ±7 days. Orders
  matching multiple candidates are flagged, not guessed.
- Category suggestions are cached on disk so `apply` doesn't re-prompt
  Claude for transactions you already reviewed. Cache at
  ~/.ynab-cat-cache.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    sys.exit("ERROR: pip install requests")
try:
    from anthropic import Anthropic
except ImportError:
    sys.exit("ERROR: pip install anthropic")

# amazon-orders is optional — script works without it, just no Amazon matching
_AMAZON_AVAILABLE = True
try:
    from amazonorders.session import AmazonSession
    from amazonorders.orders import AmazonOrders
except ImportError:
    _AMAZON_AVAILABLE = False


YNAB_BASE = "https://api.ynab.com/v1"
CACHE_PATH = Path.home() / ".ynab-cat-cache.json"

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}


# ──────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Category:
    id: str
    name: str
    group_name: str
    hidden: bool = False
    deleted: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.group_name}: {self.name}"


@dataclass
class Transaction:
    id: str
    date: str
    payee: str
    amount_milli: int  # YNAB milliunits, signed
    memo: str
    account: str
    # YNAB's auto-filled category (if any). The transaction is in our list
    # because it's UNAPPROVED — meaning the user hasn't confirmed this yet.
    current_category_name: str = ""
    current_category_id: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)  # Amazon items
    matched_order_id: str = ""  # for diagnostics; not written to memo
    match_was_ambiguous: bool = False
    # When True, items list is a SUPERSET — the actual shipment contained
    # only some of these items. Claude must NOT generate a split in this
    # case (per-item prices won't sum to the transaction amount).
    is_partial_shipment: bool = False
    # When non-empty, these are the items confidently identified by
    # subset-sum as being in THIS shipment specifically.
    shipment_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def amount(self) -> float:
        return self.amount_milli / 1000.0

    @property
    def abs_amount(self) -> float:
        return abs(self.amount)

    @property
    def is_amazon(self) -> bool:
        p = (self.payee or "").lower()
        return "amazon" in p or "amzn" in p

    @property
    def is_ambiguous_payee(self) -> bool:
        """Payees that, like Amazon, can fund essentially anything and
        cannot be reliably categorized from the payee alone. The script
        will only suggest categories for these when there's strong context
        (matched items, or memo content)."""
        p = (self.payee or "").lower()
        for pattern in (
            "venmo", "paypal", "cash app", "cashapp", "zelle",
            "square", "*sq ",  # Square subscription billing prefix
            "apple cash", "google pay",
        ):
            if pattern in p:
                return True
        return False


# ──────────────────────────────────────────────────────────────────────────
# YNAB REST client (no SDK, just requests)
# ──────────────────────────────────────────────────────────────────────────
class YNAB:
    def __init__(self, token: str, budget_id: str | None = None):
        self.token = token
        self.budget_id = budget_id
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}"})

    def _get(self, path: str) -> Any:
        r = self.s.get(f"{YNAB_BASE}{path}", timeout=30)
        if r.status_code == 429:
            raise RuntimeError("YNAB rate limit hit (200/hr). Wait and retry.")
        r.raise_for_status()
        return r.json()["data"]

    def _patch(self, path: str, body: dict[str, Any]) -> Any:
        r = self.s.patch(f"{YNAB_BASE}{path}", json=body, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"YNAB {r.status_code}: {r.text}")
        return r.json()["data"]

    def list_budgets(self) -> list[dict[str, Any]]:
        return self._get("/budgets")["budgets"]

    def list_categories(self) -> list[Category]:
        assert self.budget_id
        data = self._get(f"/budgets/{self.budget_id}/categories")
        out: list[Category] = []
        for group in data["category_groups"]:
            if group.get("deleted") or group.get("hidden"):
                continue
            for c in group["categories"]:
                if c.get("deleted") or c.get("hidden"):
                    continue
                out.append(
                    Category(
                        id=c["id"],
                        name=c["name"],
                        group_name=group["name"],
                    )
                )
        return out

    def list_unapproved_transactions(
        self, days: int | None = None
    ) -> list[Transaction]:
        """Return transactions YNAB has not yet been approved by the user.

        This includes both truly uncategorized transactions AND transactions
        that YNAB auto-categorized from a payee's last category — because
        auto-fill is unreliable, especially for Amazon.
        """
        assert self.budget_id
        params = "?type=unapproved"
        if days:
            since = (date.today() - timedelta(days=days)).isoformat()
            params += f"&since_date={since}"
        data = self._get(f"/budgets/{self.budget_id}/transactions{params}")
        txns: list[Transaction] = []
        for t in data["transactions"]:
            if t.get("deleted"):
                continue
            # Skip transfers (YNAB handles those internally)
            if t.get("transfer_account_id"):
                continue
            # Skip "Starting Balance" and similar
            if (t.get("payee_name") or "").lower() == "starting balance":
                continue
            txns.append(
                Transaction(
                    id=t["id"],
                    date=t["date"],
                    payee=t.get("payee_name") or "",
                    amount_milli=t["amount"],
                    memo=t.get("memo") or "",
                    account=t.get("account_name") or "",
                    current_category_name=t.get("category_name") or "",
                    current_category_id=t.get("category_id") or "",
                )
            )
        return txns

    # Back-compat alias for any callers that still reference the old name.
    list_uncategorized_transactions = list_unapproved_transactions

    def list_recent_categorized(self, limit: int = 300) -> list[tuple[str, str]]:
        """Return (payee, category_name) pairs from recent approved txns."""
        assert self.budget_id
        since = (date.today() - timedelta(days=180)).isoformat()
        data = self._get(
            f"/budgets/{self.budget_id}/transactions?since_date={since}"
        )
        out: list[tuple[str, str]] = []
        for t in data["transactions"]:
            if t.get("deleted") or not t.get("category_name"):
                continue
            if t.get("transfer_account_id"):
                continue
            payee = t.get("payee_name")
            cat = t["category_name"]
            if not payee or cat == "Uncategorized":
                continue
            out.append((payee, cat))
        # Dedupe preserving order, most recent first
        seen: set[tuple[str, str]] = set()
        deduped: list[tuple[str, str]] = []
        for pair in out:
            if pair in seen:
                continue
            seen.add(pair)
            deduped.append(pair)
            if len(deduped) >= limit:
                break
        return deduped

    def update_category(
        self,
        txn_id: str,
        category_id: str,
        memo: str | None = None,
        approve: bool = True,
    ) -> None:
        assert self.budget_id
        body: dict[str, Any] = {"category_id": category_id, "approved": approve}
        if memo is not None:
            body["memo"] = memo
        self._patch(
            f"/budgets/{self.budget_id}/transactions/{txn_id}",
            {"transaction": body},
        )

    def approve_only(self, txn_id: str, memo: str | None = None) -> None:
        """Approve a transaction without changing its category. Used when
        YNAB's auto-filled category is correct as-is."""
        assert self.budget_id
        body: dict[str, Any] = {"approved": True}
        if memo is not None:
            body["memo"] = memo
        self._patch(
            f"/budgets/{self.budget_id}/transactions/{txn_id}",
            {"transaction": body},
        )

    def update_memo_only(self, txn_id: str, memo: str) -> None:
        """Set memo without changing category or approval status."""
        assert self.budget_id
        self._patch(
            f"/budgets/{self.budget_id}/transactions/{txn_id}",
            {"transaction": {"memo": memo}},
        )

    def split_transaction(
        self,
        txn_id: str,
        subs: list[tuple[int, str]],
        memo: str | None = None,
        approve: bool = True,
    ) -> None:
        """subs: list of (amount_milli, category_id) tuples, signed to match parent."""
        assert self.budget_id
        body: dict[str, Any] = {
            "category_id": None,
            "approved": approve,
            "subtransactions": [
                {"amount": a, "category_id": c} for a, c in subs
            ],
        }
        if memo is not None:
            body["memo"] = memo
        self._patch(
            f"/budgets/{self.budget_id}/transactions/{txn_id}",
            {"transaction": body},
        )


# ──────────────────────────────────────────────────────────────────────────
# Amazon order matching
# ──────────────────────────────────────────────────────────────────────────
def load_amazon_csv(path: Path) -> list[dict[str, Any]]:
    """Load Amazon orders from a CSV produced by gmail_amazon.py or similar.

    Expected columns: order_number, date (YYYY-MM-DD), total, items_json.
    items_json is a JSON-encoded list of {"title": str, "price": float|null}.
    """
    import csv as _csv
    from datetime import date as _date
    out: list[dict[str, Any]] = []
    with open(path, newline="") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            try:
                d_parts = row["date"].split("-")
                if len(d_parts) != 3:
                    continue
                order_date = _date(int(d_parts[0]), int(d_parts[1]), int(d_parts[2]))
            except Exception:
                continue
            try:
                total = float(row["total"])
            except (ValueError, KeyError):
                continue
            try:
                items = json.loads(row.get("items_json", "[]") or "[]")
                if not isinstance(items, list):
                    items = []
            except Exception:
                items = []
            out.append({
                "order_id": row.get("order_number", ""),
                "date": order_date,
                "total": total,
                "items": items,
                "charges": [],  # Gmail data has no charge breakdown
            })
    return out


# Pattern to extract dated charges from the AZAD payments column.
# Examples:
#   "Prime Visa ending in 5295: 2026-04-16: $12.23"
#   "2026-04-06: $6.65"
_AZAD_PAYMENT_RE = re.compile(
    r"(?:([^;:]+?):\s*)?(\d{4}-\d{2}-\d{2})\s*:\s*\$?([\d,]+\.\d{2})"
)


def _parse_azad_payments(s: str) -> list[dict[str, Any]]:
    """Parse the 'payments' column from azad's Orders CSV. Returns list of
    {date: date, amount: float, card: str}."""
    from datetime import date as _date
    out = []
    if not s:
        return out
    for card, dt_str, amt_str in _AZAD_PAYMENT_RE.findall(s):
        try:
            y, m, d = dt_str.split("-")
            charge_date = _date(int(y), int(m), int(d))
            amount = float(amt_str.replace(",", ""))
        except (ValueError, IndexError):
            continue
        out.append({
            "date": charge_date,
            "amount": amount,
            "card": (card or "").strip(),
        })
    return out


def _parse_money(s: str) -> float:
    """Parse '$22.49' or '22.49' or '' → float."""
    if not s:
        return 0.0
    s = s.replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def load_azad_csvs(
    items_path: Path | None,
    orders_path: Path | None,
    transactions_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Load Amazon Order History Reporter (azad) CSVs and merge into our
    standard order shape. Any path may be None — we use whatever's available.

    Items CSV (per-item rows): order id, order date, description, price, ASIN
    Orders CSV (per-order rows): order id, date, total, payments
    Transactions CSV (per-charge rows): date, order ids, card_details, amount
        ↑ This is the gold-standard signal. One row per actual charge,
        showing exactly which payment method was hit and for how much.
        Premium-only feature in azad.

    When all three are available, matching against the YNAB Chase ledger
    becomes near-perfect because we know the exact Visa charge per shipment.
    """
    import csv as _csv
    from datetime import date as _date

    # Step 1: Items keyed by order_id → list of items
    items_by_order: dict[str, list[dict[str, Any]]] = {}
    item_dates: dict[str, _date] = {}
    if items_path and items_path.exists():
        with open(items_path, newline="", encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                oid = (row.get("order id") or row.get("order_id") or "").strip()
                if not oid:
                    continue
                title = (row.get("description") or "").strip()
                if not title:
                    continue
                price = _parse_money(row.get("price", ""))
                items_by_order.setdefault(oid, []).append({
                    "title": title,
                    "price": price if price > 0 else None,
                    "asin": (row.get("ASIN") or "").strip() or None,
                })
                if oid not in item_dates:
                    try:
                        d_str = (row.get("order date") or "").strip()
                        if d_str:
                            y, m, d = d_str.split("-")
                            item_dates[oid] = _date(int(y), int(m), int(d))
                    except (ValueError, IndexError):
                        pass

    # Step 2: Orders + payments (legacy data path; transactions CSV is better)
    orders_meta: dict[str, dict[str, Any]] = {}
    if orders_path and orders_path.exists():
        with open(orders_path, newline="", encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                oid = (row.get("order id") or row.get("order_id") or "").strip()
                if not oid:
                    continue
                try:
                    d_str = (row.get("date") or "").strip()
                    y, m, d = d_str.split("-")
                    order_date = _date(int(y), int(m), int(d))
                except (ValueError, IndexError):
                    order_date = item_dates.get(oid)
                    if not order_date:
                        continue
                total = _parse_money(row.get("total", ""))
                refund = _parse_money(row.get("refund", ""))
                charges = _parse_azad_payments(row.get("payments", ""))
                items = items_by_order.get(oid, [])
                if not items:
                    raw_items = row.get("items", "")
                    if raw_items:
                        for piece in raw_items.split(";"):
                            t = piece.strip()
                            if t:
                                items.append({"title": t, "price": None, "asin": None})
                orders_meta[oid] = {
                    "order_id": oid,
                    "date": order_date,
                    "total": total,
                    "items": items,
                    "charges": charges,
                    "refund": refund,
                }

    # Step 3: Transactions CSV — this is the high-fidelity charge data.
    # Each row is one actual charge to a specific payment method on a date.
    # We OVERLAY these onto orders_meta, replacing any inferior charges
    # parsed from the orders CSV's `payments` column.
    if transactions_path and transactions_path.exists():
        # Group transactions by order_id
        tx_by_order: dict[str, list[dict[str, Any]]] = {}
        with open(transactions_path, newline="", encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                # `order ids` may be comma-separated for combined transactions
                oid_field = (row.get("order ids") or "").strip()
                if not oid_field or oid_field == "??":
                    continue
                try:
                    d_str = (row.get("date") or "").strip()
                    y, m, d = d_str.split("-")
                    tx_date = _date(int(y), int(m), int(d))
                except (ValueError, IndexError):
                    continue
                # The amount is signed (negative for outflows). We keep the
                # absolute value for matching against YNAB's abs_amount.
                try:
                    amount = abs(float(str(row.get("amount", "0")).strip()))
                except ValueError:
                    continue
                if amount <= 0:
                    continue
                card = (row.get("card_details") or "").strip()
                # Only include charges that hit a real payment method we'd
                # see in YNAB. Gift cards and "Rewards points" don't appear
                # in any bank ledger and would create false-match noise.
                if not card:
                    continue
                if "gift card" in card.lower():
                    continue
                if "rewards" in card.lower() or "points" in card.lower():
                    continue
                # Split combined order_ids; same charge applies to all
                for oid in oid_field.split(","):
                    oid = oid.strip()
                    if oid:
                        tx_by_order.setdefault(oid, []).append({
                            "date": tx_date,
                            "amount": amount,
                            "card": card,
                        })

        # Overlay onto orders_meta
        for oid, txs in tx_by_order.items():
            if oid in orders_meta:
                # Replace charges with the high-fidelity ones
                orders_meta[oid]["charges"] = txs
            else:
                # We have a charge for an order we haven't seen in items/orders.
                # Create a stub entry so the matcher can still find it.
                # Date defaults to first charge date; total = sum of charges
                items = items_by_order.get(oid, [])
                first_date = min((t["date"] for t in txs), default=None)
                total = sum(t["amount"] for t in txs)
                if first_date:
                    orders_meta[oid] = {
                        "order_id": oid,
                        "date": item_dates.get(oid, first_date),
                        "total": total,
                        "items": items,
                        "charges": txs,
                        "refund": 0.0,
                    }

    # Step 4: Add any orders that exist only in items CSV
    for oid, items in items_by_order.items():
        if oid in orders_meta:
            continue
        order_date = item_dates.get(oid)
        if not order_date:
            continue
        total = sum((i["price"] or 0) for i in items)
        orders_meta[oid] = {
            "order_id": oid,
            "date": order_date,
            "total": total,
            "items": items,
            "charges": [],
            "refund": 0.0,
        }

    return list(orders_meta.values())


def fetch_amazon_orders(days: int) -> list[dict[str, Any]]:
    """Return list of {order_id, date, total, items:[{title, price}]} for
    orders placed within the last `days` days. Returns [] if creds missing."""
    if not _AMAZON_AVAILABLE:
        print("  amazon-orders not installed, skipping Amazon enrichment",
              file=sys.stderr)
        return []
    user = os.environ.get("AMAZON_USERNAME")
    pw = os.environ.get("AMAZON_PASSWORD")
    if not (user and pw):
        print("  AMAZON_USERNAME/PASSWORD not set, skipping Amazon enrichment",
              file=sys.stderr)
        return []

    session = AmazonSession(user, pw)
    try:
        session.login()
    except Exception as e:
        print(f"  Amazon login failed: {e}", file=sys.stderr)
        print("  If 2FA: run `amazon-orders login` interactively once to establish session.",
              file=sys.stderr)
        return []

    ao = AmazonOrders(session)
    # Pick appropriate time filter
    if days <= 30:
        orders = ao.get_order_history(time_filter="last30", full_details=True)
    elif days <= 90:
        orders = ao.get_order_history(time_filter="months-3", full_details=True)
    else:
        # Fall back to year-based query for longer windows
        orders = ao.get_order_history(year=date.today().year, full_details=True)

    out = []
    cutoff = date.today() - timedelta(days=days)
    for o in orders:
        try:
            order_date = o.order_placed_date
            if isinstance(order_date, str):
                order_date = datetime.fromisoformat(order_date).date()
            if order_date < cutoff:
                continue
            items = []
            for shipment in getattr(o, "shipments", []) or []:
                for it in getattr(shipment, "items", []) or []:
                    items.append({
                        "title": str(getattr(it, "title", "") or ""),
                        "price": float(getattr(it, "price", 0) or 0),
                    })
            # Older amazon-orders structure: items directly on order
            if not items and hasattr(o, "items"):
                for it in o.items or []:
                    items.append({
                        "title": str(getattr(it, "title", "") or ""),
                        "price": float(getattr(it, "price", 0) or 0),
                    })
            out.append({
                "order_id": str(getattr(o, "order_number", "")),
                "date": order_date,
                "total": float(getattr(o, "grand_total", 0) or 0),
                "items": items,
            })
        except Exception as e:
            print(f"  skipping malformed order: {e}", file=sys.stderr)
            continue
    return out


def find_nearby_orders(
    txn: Transaction,
    orders: list[dict[str, Any]],
    days_window: int = 14,
    max_orders: int = 5,
) -> list[dict[str, Any]]:
    """For an unmatched Amazon transaction, return orders placed within
    `days_window` days of the txn date, sorted nearest-first. Used to give
    Claude possible context when an order didn't match by amount."""
    if not orders:
        return []
    try:
        txn_date = datetime.fromisoformat(txn.date).date()
    except Exception:
        return []
    candidates = []
    for o in orders:
        diff = abs((o["date"] - txn_date).days)
        if diff > days_window:
            continue
        if not o.get("items"):
            continue
        candidates.append((diff, o))
    candidates.sort(key=lambda c: c[0])
    return [o for _, o in candidates[:max_orders]]


def _find_item_subset(
    items: list[dict[str, Any]],
    target_amount: float,
    tax_rate_max: float = 0.12,
) -> list[dict[str, Any]] | None:
    """Find a subset of items whose prices sum to target_amount, optionally
    plus tax up to tax_rate_max. Returns the subset, or None if no unique
    solution exists.

    Uses simple recursive subset-sum since item lists are small (typically <10).
    Returns None if:
    - No subset matches within tolerance
    - Multiple distinct subsets match (ambiguous)
    - Any item has missing price data
    """
    priced = [it for it in items if it.get("price") is not None and it["price"] > 0]
    if len(priced) != len(items):
        # Some items are missing prices — can't reliably do subset-sum
        return None
    if len(priced) > 14:
        # 2^14 = 16k subsets; bigger gets slow and unreliable
        return None
    if not priced:
        return None

    found_subsets: list[list[dict[str, Any]]] = []
    # Try each tax rate from 0 (no tax) up to max in 0.5% steps
    # Use the lowest matching rate so we prefer simpler explanations
    for tax_pct in [0, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.12]:
        target_pretax = target_amount / (1 + tax_pct)
        n = len(priced)
        # Iterate all non-empty subsets via bitmask
        for mask in range(1, 1 << n):
            subset_total = 0.0
            subset = []
            for i in range(n):
                if mask & (1 << i):
                    subset_total += priced[i]["price"]
                    subset.append(priced[i])
            if abs(subset_total - target_pretax) < 0.05:
                # Match within 5 cents of pretax target
                found_subsets.append(subset)
        if found_subsets:
            break  # Use this tax rate's results

    if not found_subsets:
        return None
    if len(found_subsets) > 1:
        # Check whether they're all the same set of items (different orderings)
        signatures = {tuple(sorted(id(it) for it in s)) for s in found_subsets}
        if len(signatures) > 1:
            return None  # Genuine ambiguity
    return found_subsets[0]


def match_amazon(
    txns: list[Transaction],
    orders: list[dict[str, Any]],
    debug: bool = False,
) -> dict[str, int]:
    """Mutate txns in place: attach .items to matched Amazon transactions.

    Two-pass match:
      1. EXACT match on parsed charge_date + charge_amount from the azad
         Orders CSV `payments` column. This is the gold-standard signal —
         direct from Amazon's records.
      2. HEURISTIC match on order_total + order_date for orders without
         dated charge data (legacy Gmail-parsed orders, or older azad rows).

    Match window: -2 to +21 days from order date for the heuristic pass.
    Asymmetric because the charge always comes on or after the order email,
    but timezone slop can make it appear 1-2 days earlier.
    """
    amazon_txns = [t for t in txns if t.is_amazon and not t.items]
    if not amazon_txns or not orders:
        return {"matched": 0, "exact": 0, "heuristic": 0,
                "ambiguous": 0, "unmatched": len(amazon_txns)}

    counts = {"matched": 0, "exact": 0, "heuristic": 0,
              "ambiguous": 0, "unmatched": 0}

    AMOUNT_TOL_EXACT = 0.01      # charges should match to the cent
    AMOUNT_TOL_HEURISTIC = 0.10
    DAYS_BEFORE = 2
    DAYS_AFTER = 21

    # ─── Pass 1: exact charge match ─────────────────────────────────────
    # Build an index of every charge from every order so we can match
    # multiple shipments that share an order_id.
    charge_index: list[tuple[float, "date", dict[str, Any]]] = []
    from datetime import date as _date  # noqa: F401 (used implicitly)
    for o in orders:
        for ch in o.get("charges", []) or []:
            charge_index.append((ch["amount"], ch["date"], o))

    matched_ids: set[str] = set()

    for t in amazon_txns:
        try:
            txn_date = datetime.fromisoformat(t.date).date()
        except Exception:
            continue
        target_amt = t.abs_amount
        for amount, charge_date, order in charge_index:
            if abs(amount - target_amt) > AMOUNT_TOL_EXACT:
                continue
            if abs((txn_date - charge_date).days) > 2:
                # Allow a couple days of slop between Amazon's record of the
                # charge and Chase posting it. Usually 0-1 days.
                continue
            # Decide whether this charge represents the FULL order or only
            # part of it (split shipment).
            order_total = order.get("total", 0.0) or 0.0
            charge_total_for_order = sum(
                (c.get("amount") or 0)
                for c in (order.get("charges") or [])
            )
            n_charges = len(order.get("charges") or [])
            is_partial = (
                n_charges > 1 and abs(target_amt - order_total) > 0.50
            )

            t.matched_order_id = order["order_id"]

            if is_partial:
                # Try to figure out which items were in THIS shipment.
                subset = _find_item_subset(order["items"], target_amt)
                if subset is not None:
                    # Confidently identified — use only those items
                    t.items = subset
                    t.shipment_items = subset
                    t.is_partial_shipment = False  # we resolved it
                else:
                    # Couldn't determine — give Claude the full list but
                    # flag it as a partial so the prompt knows not to split.
                    t.items = order["items"]
                    t.is_partial_shipment = True
            else:
                t.items = order["items"]

            matched_ids.add(t.id)
            counts["matched"] += 1
            counts["exact"] += 1
            break

    # ─── Pass 2: heuristic by order total + date ────────────────────────
    for t in amazon_txns:
        if t.id in matched_ids:
            continue
        try:
            txn_date = datetime.fromisoformat(t.date).date()
        except Exception:
            counts["unmatched"] += 1
            continue
        target_amt = t.abs_amount
        candidates = []
        for o in orders:
            if abs(o["total"] - target_amt) > AMOUNT_TOL_HEURISTIC:
                continue
            day_diff = (txn_date - o["date"]).days  # signed
            if day_diff < -DAYS_BEFORE or day_diff > DAYS_AFTER:
                continue
            candidates.append((abs(day_diff), o))
        if len(candidates) == 1:
            t.items = candidates[0][1]["items"]
            t.matched_order_id = candidates[0][1]["order_id"]
            counts["matched"] += 1
            counts["heuristic"] += 1
        elif len(candidates) > 1:
            candidates.sort(key=lambda c: c[0])
            t.items = candidates[0][1]["items"]
            t.matched_order_id = candidates[0][1]["order_id"]
            t.match_was_ambiguous = True
            counts["ambiguous"] += 1
            counts["matched"] += 1
            counts["heuristic"] += 1
        else:
            counts["unmatched"] += 1
            if debug:
                amt_close = sorted(
                    orders, key=lambda o: abs(o["total"] - target_amt)
                )[:3]
                print(
                    f"  no match: {t.date} {t.payee} ${target_amt:.2f}",
                    file=sys.stderr,
                )
                for o in amt_close:
                    if abs(o["total"] - target_amt) < 5.00:
                        print(
                            f"    nearest by amount: {o['date']} "
                            f"${o['total']:.2f} (diff ${abs(o['total'] - target_amt):.2f}, "
                            f"{(txn_date - o['date']).days}d)",
                            file=sys.stderr,
                        )
                if charge_index:
                    nearest_charges = sorted(
                        charge_index, key=lambda c: abs(c[0] - target_amt)
                    )[:3]
                    for amt, dt, _o in nearest_charges:
                        if abs(amt - target_amt) < 5.00:
                            print(
                                f"    nearest charge:    {dt} ${amt:.2f} "
                                f"(diff ${abs(amt - target_amt):.2f}, "
                                f"{(txn_date - dt).days}d)",
                                file=sys.stderr,
                            )

    return counts


# ──────────────────────────────────────────────────────────────────────────
# Claude suggestions
# ──────────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are a YNAB categorization assistant. Given a transaction and the \
user's available categories, suggest the best category. For Amazon \
orders with multiple items that clearly belong to different categories, \
suggest a split with per-item amounts.

Respond with STRICT JSON only — no prose, no markdown fences:

{
  "action": "single" | "split" | "skip",
  "suggestions": [
    { "category": "Group: Category", "amount": 12.34, "reason": "..." }
  ],
  "confidence": "high" | "medium" | "low",
  "memo": "short human-readable description (~30-60 chars), or empty string",
  "notes": "optional brief note"
}

Rules:
- action="single": one category for the whole transaction, suggestions has one entry with amount=full absolute amount
- action="split": multiple categories, amounts must sum to the total (absolute value)
- action="skip": you cannot determine a good category; explain in notes
- amounts are positive dollars (ignore sign)
- use ONLY categories from the provided list — never invent new ones
- match user's existing payee→category patterns from the examples when reasonable
- for Amazon orders with a single obvious category (all items same type), use "single"
- for Amazon orders spanning multiple clear categories, use "split"

AMBIGUOUS PAYEE RULE (Venmo, PayPal, Cash App, Zelle, Square, etc.):
- These are payment platforms, not merchants — the actual purpose is unknown
- DEFAULT to action="skip" with notes explaining why
- Only suggest a category if the memo or transaction context makes the
  purpose unambiguous (e.g., memo="piano lesson" → activities)
- Past examples like "Venmo → Estate Sale" reflect ONE specific transaction,
  not a pattern that should be applied to other Venmo transactions
- Confidence should be "low" even when suggesting; the user knows context you don't

UNMATCHED AMAZON RULE:
- If the transaction is Amazon and no items are listed, the order data
  was unavailable. Set action="skip" — do NOT guess from the most-recent
  Amazon category. The notes should say "no order data available" so the
  user knows to look it up manually.
- The exception: if "POSSIBLE NEARBY ORDERS" are listed (see below),
  reason about whether one of those plausibly explains this charge, and
  suggest a category if so with confidence="medium" or lower.

MEMO field rules (very important):
- The memo summarizes WHAT was purchased, in plain English, ~30-60 chars max
- Strip marketing fluff, model numbers, sizes, redundant brand repetitions
- For a single Amazon item: brand + product type → "Aveeno Body Cream", "Dell Battery", "Charmin Toilet Paper"
- For multi-item Amazon orders: comma-separated short descriptors → "USB hub, hole saw, desk grommet"
- For non-Amazon transactions: leave empty string "" (the payee is already informative)
- Don't include prices, quantities, or order numbers in memo
- Don't include the word "Amazon" — it's redundant given the payee
- If action="skip" but Amazon items are visible: STILL fill the memo with item descriptors. The user wants a memo describing the purchase even if you can't categorize it.
"""


def build_user_prompt(
    txn: Transaction,
    categories: list[Category],
    examples: list[tuple[str, str]],
    nearby_orders: list[dict[str, Any]] | None = None,
) -> str:
    parts = ["AVAILABLE CATEGORIES:"]
    parts.extend(f"  - {c.full_name}" for c in categories)
    parts.append("")

    if examples:
        parts.append("RECENT CATEGORIZATION EXAMPLES (payee → category):")
        parts.extend(f"  {p} → {c}" for p, c in examples[:30])
        parts.append("")

    parts.append("TRANSACTION TO CATEGORIZE:")
    parts.append(f"  Date:    {txn.date}")
    parts.append(f"  Payee:   {txn.payee}")
    parts.append(f"  Amount:  ${txn.abs_amount:.2f}")
    parts.append(f"  Account: {txn.account}")
    if txn.memo:
        parts.append(f"  Memo:    {txn.memo}")
    if txn.current_category_name:
        parts.append(
            f"  Auto-filled category (UNAPPROVED — YNAB guessed this from "
            f"the previous transaction with the same payee; verify it's actually "
            f"correct): {txn.current_category_name}"
        )

    if txn.items:
        parts.append("")
        if txn.is_partial_shipment:
            parts.append(
                f"PARTIAL SHIPMENT — order was charged across multiple "
                f"shipments. The transaction amount (${txn.abs_amount:.2f}) "
                f"is for ONLY SOME items below; we couldn't determine which. "
                f"DO NOT generate a split — items don't sum to the charge. "
                f"Use action='single' and pick a category that fits the order's "
                f"overall theme. Items in the order:"
            )
        else:
            parts.append(f"AMAZON ORDER ITEMS ({len(txn.items)}):")
        for it in txn.items:
            p = it.get("price")
            p_str = f" — ${float(p):.2f}" if p else ""
            parts.append(f"  - {it['title']}{p_str}")
    elif nearby_orders:
        # Unmatched Amazon transaction — give Claude possibly-related orders
        # so it can reason rather than blindly guessing.
        parts.append("")
        parts.append(
            f"POSSIBLE NEARBY ORDERS (this charge did NOT match any order "
            f"by exact charge or order total — it may be a partial shipment "
            f"or split charge from one of these orders, or unrelated):"
        )
        for o in nearby_orders:
            parts.append(
                f"  Order {o['order_id']} on {o['date']} — total ${o['total']:.2f}"
            )
            for it in o["items"][:6]:
                p = it.get("price")
                p_str = f" (${float(p):.2f})" if p else ""
                parts.append(f"    • {it['title'][:80]}{p_str}")

    parts.append("")
    parts.append("Respond with JSON only.")
    return "\n".join(parts)


def call_claude(
    client: Anthropic, model: str, txn: Transaction,
    categories: list[Category], examples: list[tuple[str, str]],
    nearby_orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    user_prompt = build_user_prompt(txn, categories, examples, nearby_orders)
    resp = client.messages.create(
        model=model, max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        return {
            "action": "skip", "suggestions": [], "confidence": "low",
            "notes": f"Parse error: {e}. Raw: {text[:200]}",
        }


# ──────────────────────────────────────────────────────────────────────────
# Cache (so `apply` can reuse suggestions without re-calling Claude)
# ──────────────────────────────────────────────────────────────────────────
def load_cache() -> dict[str, dict[str, Any]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def save_cache(cache: dict[str, dict[str, Any]]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, indent=2, default=str))


# ──────────────────────────────────────────────────────────────────────────
# Category name → id resolver (handles fuzzy matches from Claude)
# ──────────────────────────────────────────────────────────────────────────
def resolve_category(name: str, categories: list[Category]) -> Category | None:
    name_lower = name.strip().lower()
    # Exact full_name match
    for c in categories:
        if c.full_name.lower() == name_lower:
            return c
    # Match on category name only (no group)
    for c in categories:
        if c.name.lower() == name_lower:
            return c
    # Match "Group: Category" when Claude might have used slightly different group
    if ":" in name_lower:
        _, cat_part = name_lower.split(":", 1)
        cat_part = cat_part.strip()
        for c in categories:
            if c.name.lower() == cat_part:
                return c
    return None


# ──────────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────────
def cmd_budgets(args: argparse.Namespace) -> int:
    token = _require_env("YNAB_API_TOKEN")
    y = YNAB(token)
    budgets = y.list_budgets()
    print(f"Found {len(budgets)} budget(s):\n")
    for b in budgets:
        print(f"  id:    {b['id']}")
        print(f"  name:  {b['name']}")
        print(f"  last:  {b.get('last_modified_on', '')}")
        print()
    if len(budgets) == 1:
        print(f"Export for shell:\n  export YNAB_BUDGET_ID={budgets[0]['id']}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    ok = True
    print("Testing YNAB...", end=" ", flush=True)
    try:
        token = _require_env("YNAB_API_TOKEN")
        budget_id = _require_env("YNAB_BUDGET_ID")
        y = YNAB(token, budget_id)
        cats = y.list_categories()
        print(f"OK ({len(cats)} categories)")
    except Exception as e:
        print(f"FAIL: {e}")
        ok = False

    print("Testing Claude...", end=" ", flush=True)
    try:
        _require_env("ANTHROPIC_API_KEY")
        c = Anthropic()
        c.messages.create(
            model=MODELS["haiku"], max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        print("OK")
    except Exception as e:
        print(f"FAIL: {e}")
        ok = False

    print("Testing Amazon...", end=" ", flush=True)
    if not _AMAZON_AVAILABLE:
        print("SKIP (amazon-orders not installed)")
    elif not (os.environ.get("AMAZON_USERNAME") and os.environ.get("AMAZON_PASSWORD")):
        print("SKIP (credentials not set)")
    else:
        try:
            s = AmazonSession(os.environ["AMAZON_USERNAME"], os.environ["AMAZON_PASSWORD"])
            s.login()
            print("OK")
        except Exception as e:
            print(f"FAIL: {e}")
            ok = False

    return 0 if ok else 1


def cmd_suggest(args: argparse.Namespace) -> int:
    token = _require_env("YNAB_API_TOKEN")
    budget_id = _require_env("YNAB_BUDGET_ID")
    _require_env("ANTHROPIC_API_KEY")

    y = YNAB(token, budget_id)
    client = Anthropic()

    print("Fetching categories...", file=sys.stderr)
    categories = y.list_categories()
    print(f"  {len(categories)} categories", file=sys.stderr)

    print("Fetching recent categorized examples...", file=sys.stderr)
    examples = y.list_recent_categorized()
    print(f"  {len(examples)} unique payee→category pairs", file=sys.stderr)

    print(f"Fetching uncategorized transactions (last {args.days} days)...",
          file=sys.stderr)
    txns = y.list_unapproved_transactions(days=args.days)
    if args.limit:
        txns = txns[:args.limit]
    print(f"  {len(txns)} uncategorized "
          f"({sum(1 for t in txns if t.is_amazon)} Amazon)",
          file=sys.stderr)

    if not txns:
        print("Nothing to categorize.", file=sys.stderr)
        return 0

    amazon_count = sum(1 for t in txns if t.is_amazon)
    orders: list[dict[str, Any]] = []
    if amazon_count:
        # Priority order: AZAD CSVs > Gmail CSV > scraper
        any_azad = (
            args.amazon_items_csv
            or args.amazon_orders_csv
            or args.amazon_transactions_csv
        )
        if any_azad:
            print(
                f"Loading AZAD CSVs (items={args.amazon_items_csv}, "
                f"orders={args.amazon_orders_csv}, "
                f"transactions={args.amazon_transactions_csv})...",
                file=sys.stderr,
            )
            orders = load_azad_csvs(
                args.amazon_items_csv,
                args.amazon_orders_csv,
                args.amazon_transactions_csv,
            )
        elif args.amazon_csv:
            print(f"Loading Amazon orders from {args.amazon_csv}...", file=sys.stderr)
            orders = load_amazon_csv(args.amazon_csv)
        else:
            print(f"Fetching Amazon orders via scraper...", file=sys.stderr)
            orders = fetch_amazon_orders(args.days + 14)
        n_charges = sum(len(o.get("charges", [])) for o in orders)
        print(
            f"  {len(orders)} Amazon orders, {n_charges} dated charges available",
            file=sys.stderr,
        )
        counts = match_amazon(txns, orders, debug=args.debug_amazon)
        print(
            f"  matched={counts['matched']} (exact={counts['exact']}, "
            f"heuristic={counts['heuristic']}, ambiguous={counts['ambiguous']}) "
            f"unmatched={counts['unmatched']} of {amazon_count} Amazon txns",
            file=sys.stderr,
        )

    model = MODELS[args.model]
    print(f"Calling Claude ({model})...", file=sys.stderr)

    cache = load_cache() if args.use_cache else {}
    results: list[tuple[Transaction, dict[str, Any]]] = []
    for i, t in enumerate(txns, 1):
        print(f"  [{i}/{len(txns)}] {t.date} {t.payee[:40]:40} ${t.abs_amount:>8.2f}",
              file=sys.stderr)
        if t.id in cache:
            result = cache[t.id]
        else:
            nearby = []
            if t.is_amazon and not t.items and orders:
                nearby = find_nearby_orders(t, orders)
            try:
                result = call_claude(client, model, t, categories, examples,
                                     nearby_orders=nearby)
            except Exception as e:
                result = {"action": "skip", "suggestions": [],
                          "confidence": "low", "notes": f"API error: {e}"}
            cache[t.id] = result
        results.append((t, result))

    if args.use_cache:
        save_cache(cache)

    print(render_report(results))
    return 0


def _build_memo(suggested: str, existing_memo: str) -> str | None:
    """Build the memo string to write. Returns None if there's nothing to write
    (i.e., no AI suggestion AND no existing memo to preserve)."""
    suggested = (suggested or "").strip()
    if not suggested:
        return None
    tag = "[ai]"
    existing = (existing_memo or "").strip()
    # Strip our own previous tag if re-running so we don't double-prefix
    existing_clean = existing.split("[ai]", 1)[0].strip()
    parts = [p for p in [tag, suggested, existing_clean] if p]
    return " ".join(parts)[:200]


def _maybe_write_memo_only(
    y: "YNAB",
    txn: "Transaction",
    suggested_memo: str,
) -> tuple[bool, str]:
    """When the user takes 'no action' on the category but the txn is an
    Amazon order with item context, write the memo so they keep that
    information for review. Returns (was_written, msg_for_print)."""
    if not txn.is_amazon:
        return False, ""
    memo = _build_memo(suggested_memo, txn.memo)
    if not memo:
        return False, ""
    try:
        y.update_memo_only(txn.id, memo)
        return True, f"  ✎ Memo written: {memo[:60]!r}"
    except Exception as e:
        return False, f"  ✗ Memo write failed: {e}"


def _pick_category_interactive(
    categories: list[Category],
) -> Category | None:
    """Interactive picker: filter by partial name, then pick by index.
    Returns the chosen Category or None if cancelled / invalid."""
    print("\n  Categories (type partial name to filter, blank to list all):")
    try:
        q = input("  Filter: ").strip().lower()
    except EOFError:
        return None
    matches = (
        [c for c in categories if q in c.full_name.lower()]
        if q
        else categories
    )
    if not matches:
        print("  No matches.")
        return None
    for idx, c in enumerate(matches[:30]):
        print(f"    {idx:2}: {c.full_name}")
    if len(matches) > 30:
        print(f"    ... +{len(matches) - 30} more (refine filter to see)")
    try:
        pick = int(input("  #: ").strip())
        return matches[pick]
    except (ValueError, IndexError, EOFError):
        print("  Invalid selection.")
        return None


def cmd_apply(args: argparse.Namespace) -> int:
    token = _require_env("YNAB_API_TOKEN")
    budget_id = _require_env("YNAB_BUDGET_ID")
    _require_env("ANTHROPIC_API_KEY")

    y = YNAB(token, budget_id)
    client = Anthropic()

    categories = y.list_categories()
    examples = y.list_recent_categorized()
    txns = y.list_unapproved_transactions(days=args.days)
    if args.limit:
        txns = txns[:args.limit]

    if not txns:
        print("Nothing to categorize.")
        return 0

    amazon_count = sum(1 for t in txns if t.is_amazon)
    orders: list[dict[str, Any]] = []
    if amazon_count:
        any_azad = (
            args.amazon_items_csv
            or args.amazon_orders_csv
            or args.amazon_transactions_csv
        )
        if any_azad:
            print(
                f"Loading AZAD CSVs (items={args.amazon_items_csv}, "
                f"orders={args.amazon_orders_csv}, "
                f"transactions={args.amazon_transactions_csv})..."
            )
            orders = load_azad_csvs(
                args.amazon_items_csv,
                args.amazon_orders_csv,
                args.amazon_transactions_csv,
            )
        elif args.amazon_csv:
            print(f"Loading Amazon orders from {args.amazon_csv}...")
            orders = load_amazon_csv(args.amazon_csv)
        else:
            print(f"Fetching Amazon orders...")
            orders = fetch_amazon_orders(args.days + 14)
        counts = match_amazon(txns, orders, debug=args.debug_amazon)
        print(
            f"Amazon match: {counts['matched']} matched "
            f"(exact={counts['exact']}, heuristic={counts['heuristic']}, "
            f"ambiguous={counts['ambiguous']}), "
            f"{counts['unmatched']} unmatched (of {amazon_count})"
        )

    cache = load_cache()
    model = MODELS[args.model]

    print(f"\n{len(txns)} transactions to review.")
    if args.approve:
        print(
            "Commands: [y]apply suggestion+approve / [e]dit / "
            "[s]kip / [q]uit / [?]help\n"
        )
    else:
        print(
            "Commands: [y]apply suggestion / [a]pply suggestion+approve / "
            "[e]dit / [s]kip / [q]uit / [?]help\n"
        )

    applied = skipped = failed = 0

    for i, t in enumerate(txns, 1):
        # Get or generate suggestion
        if t.id in cache:
            result = cache[t.id]
        else:
            nearby = []
            if t.is_amazon and not t.items and orders:
                nearby = find_nearby_orders(t, orders)
            try:
                result = call_claude(client, model, t, categories, examples,
                                     nearby_orders=nearby)
            except Exception as e:
                result = {"action": "skip", "suggestions": [],
                          "confidence": "low", "notes": f"API error: {e}"}
            cache[t.id] = result
            save_cache(cache)

        # Display
        print("─" * 70)
        print(f"[{i}/{len(txns)}]  {t.date}  {t.payee}  ${t.abs_amount:.2f}")
        if t.account:
            print(f"  Account: {t.account}")
        if t.current_category_name:
            print(f"  YNAB auto-filled: {t.current_category_name}  (UNAPPROVED)")
        if t.items:
            label = "  Items:"
            if t.is_partial_shipment:
                label += (
                    f"  ⚠️  PARTIAL SHIPMENT — charge is for SOME of these "
                    f"items only (couldn't determine which)"
                )
            elif t.shipment_items:
                label += (
                    f"  (order {t.matched_order_id} — items resolved by "
                    f"subset-sum from a partial shipment)"
                )
            elif t.match_was_ambiguous:
                label += "  ⚠️  ambiguous match — multiple orders fit; verify items below"
            elif t.matched_order_id:
                label += f"  (order {t.matched_order_id})"
            print(label)
            for it in t.items:
                p = it.get("price")
                p_str = f"  (${float(p):.2f})" if p else ""
                print(f"    • {it['title']}{p_str}")
        elif t.is_amazon and orders:
            # Unmatched Amazon — show nearby orders so user can sanity-check
            # whether one of them might be the source of this charge.
            nearby_for_display = find_nearby_orders(t, orders)
            if nearby_for_display:
                print(f"  No exact match. Nearby orders ({len(nearby_for_display)}):")
                for o in nearby_for_display:
                    diff_days = (
                        datetime.fromisoformat(t.date).date() - o["date"]
                    ).days
                    print(
                        f"    {o['date']} ({diff_days:+d}d)  ${o['total']:>7.2f}  "
                        f"{o['order_id']}"
                    )
                    for it in o["items"][:3]:
                        title = it["title"][:60]
                        p = it.get("price")
                        p_str = f" (${float(p):.2f})" if p else ""
                        print(f"      • {title}{p_str}")
                    if len(o["items"]) > 3:
                        print(f"      ... +{len(o['items']) - 3} more")

        action = result.get("action", "skip")
        conf = result.get("confidence", "low")
        sugg = result.get("suggestions", [])
        suggested_memo = (result.get("memo") or "").strip()
        memo_to_write = _build_memo(suggested_memo, t.memo)

        if action == "skip":
            print(f"  Suggestion: SKIP — {result.get('notes', '')}")
            if suggested_memo:
                print(f"    memo: {suggested_memo!r}")
            try:
                choice = input(
                    "  [e=edit (pick category manually) / s=skip / q=quit] "
                ).strip().lower()
            except EOFError:
                choice = "q"
            if choice in ("q", "quit"):
                print("\nStopped by user.")
                break
            if choice == "e":
                cat = _pick_category_interactive(categories)
                if cat is None:
                    skipped += 1
                    continue
                try:
                    y.update_category(
                        t.id, cat.id,
                        memo=memo_to_write,
                        approve=args.approve,
                    )
                    msg = f"  ✓ Applied: {cat.full_name}"
                    if memo_to_write:
                        msg += f"  memo={memo_to_write[:60]!r}"
                    if not args.approve:
                        msg += "  [left UNAPPROVED]"
                    print(msg)
                    applied += 1
                except Exception as e:
                    print(f"  ✗ Failed: {e}")
                    failed += 1
                continue
            # n, s, blank, anything else → still write memo for Amazon if we have one
            written, msg = _maybe_write_memo_only(y, t, suggested_memo)
            if msg:
                print(msg)
            skipped += 1
            continue

        print(f"  Suggestion ({conf}):")
        if action == "split":
            for s in sugg:
                print(f"    ${float(s['amount']):>7.2f} → {s['category']}  ({s.get('reason', '')})")
        else:
            s = sugg[0] if sugg else {}
            print(f"    → {s.get('category', '?')}  ({s.get('reason', '')})")

        if suggested_memo:
            print(f"    memo: {suggested_memo!r}")

        # Prompt
        auto_apply = args.auto_high and conf == "high"
        if auto_apply:
            print(f"  [auto-applying high confidence]")
            # In auto-approve mode, --auto-high triggers y; otherwise a.
            choice = "y" if args.approve else "a"
        else:
            if args.approve:
                prompt = "  [y=apply+approve / e=edit / s=skip / q=quit / ?] "
            else:
                prompt = "  [y=apply / a=apply+approve / e=edit / s=skip / q=quit / ?] "
            try:
                choice = input(prompt).strip().lower()
            except EOFError:
                choice = "q"

        if choice in ("q", "quit"):
            print("\nStopped by user.")
            break
        if choice == "?":
            if args.approve:
                print("  y=apply suggestion + memo, mark APPROVED in YNAB")
            else:
                print("  y=apply suggestion + memo (leave UNAPPROVED in YNAB)")
                print("  a=apply suggestion + memo, mark APPROVED in YNAB")
            print("  e=edit (pick category manually) + memo")
            print("  s=skip (no category change; memo still written for Amazon)")
            print("  q=quit (no further changes)")
            keys = "[y/e/s/q]" if args.approve else "[y/a/e/s/q]"
            try:
                choice = input(f"  {keys} ").strip().lower()
            except EOFError:
                choice = "q"
        if choice in ("n", "s", ""):
            # Even though the user took no action on category, write the memo
            # for Amazon transactions so the item info is preserved in YNAB.
            written, msg = _maybe_write_memo_only(y, t, suggested_memo)
            if msg:
                print(msg)
            skipped += 1
            continue

        if choice == "e":
            cat = _pick_category_interactive(categories)
            if cat is None:
                # User cancelled the picker. Still write memo for Amazon.
                written, msg = _maybe_write_memo_only(y, t, suggested_memo)
                if msg:
                    print(msg)
                skipped += 1
                continue
            try:
                y.update_category(
                    t.id, cat.id,
                    memo=memo_to_write,
                    approve=args.approve,
                )
                msg = f"  ✓ Applied: {cat.full_name}"
                if memo_to_write:
                    msg += f"  memo={memo_to_write[:60]!r}"
                if not args.approve:
                    msg += "  [left UNAPPROVED]"
                print(msg)
                applied += 1
            except Exception as e:
                print(f"  ✗ Failed: {e}")
                failed += 1
            continue

        # 'y' = apply Claude's suggestion, leave unapproved (per --approve)
        # 'a' = apply Claude's suggestion AND force-approve regardless of --approve
        if choice not in ("y", "a"):
            # Unrecognized — treat as skip but still capture the memo
            written, msg = _maybe_write_memo_only(y, t, suggested_memo)
            if msg:
                print(msg)
            skipped += 1
            continue
        effective_approve = True if choice == "a" else args.approve

        try:
            if action == "split":
                # Defensive: validate split amounts before writing.
                claimed_total = sum(float(s.get("amount", 0)) for s in sugg)
                txn_total = abs(t.amount_milli) / 1000.0
                if abs(claimed_total - txn_total) > 0.50:
                    print(
                        f"  ✗ REJECTED split: amounts sum to "
                        f"${claimed_total:.2f}, transaction is ${txn_total:.2f}. "
                        f"Use 'e' to pick a single category instead."
                    )
                    failed += 1
                    continue
                subs = []
                sign = 1 if t.amount_milli >= 0 else -1
                for s in sugg:
                    cat = resolve_category(s["category"], categories)
                    if not cat:
                        raise ValueError(f"unknown category: {s['category']}")
                    amt_milli = int(round(float(s["amount"]) * 1000)) * sign
                    subs.append((amt_milli, cat.id))
                total = sum(a for a, _ in subs)
                diff = t.amount_milli - total
                if diff and subs:
                    subs[-1] = (subs[-1][0] + diff, subs[-1][1])
                y.split_transaction(
                    t.id, subs,
                    memo=memo_to_write,
                    approve=effective_approve,
                )
                msg = f"  ✓ Split applied ({len(subs)} categories)"
                if memo_to_write:
                    msg += f"  memo={memo_to_write[:60]!r}"
                msg += "  [APPROVED]" if effective_approve else "  [left UNAPPROVED]"
                print(msg)
            else:
                s = sugg[0]
                cat = resolve_category(s["category"], categories)
                if not cat:
                    raise ValueError(f"unknown category: {s['category']}")
                y.update_category(
                    t.id, cat.id,
                    memo=memo_to_write,
                    approve=effective_approve,
                )
                msg = f"  ✓ Applied: {cat.full_name}"
                if memo_to_write:
                    msg += f"  memo={memo_to_write[:60]!r}"
                msg += "  [APPROVED]" if effective_approve else "  [left UNAPPROVED]"
                print(msg)
            applied += 1
        except Exception as e:
            print(f"  ✗ Failed: {e}")
            failed += 1

    save_cache(cache)
    print("\n" + "=" * 70)
    print(f"Done: {applied} applied, {skipped} skipped, {failed} failed")
    return 0


# ──────────────────────────────────────────────────────────────────────────
# Reporting (used by `suggest`)
# ──────────────────────────────────────────────────────────────────────────
def render_report(results: list[tuple[Transaction, dict[str, Any]]]) -> str:
    lines = ["# YNAB categorization suggestions\n"]
    n = len(results)
    n_split = sum(1 for _, r in results if r.get("action") == "split")
    n_high = sum(1 for _, r in results if r.get("confidence") == "high")
    lines.append(f"**{n} transactions** · {n_split} splits · {n_high} high-confidence\n")
    lines.append("---\n")

    for t, r in results:
        action = r.get("action", "skip")
        conf = r.get("confidence", "low")
        sugg = r.get("suggestions", [])
        notes = r.get("notes", "")

        lines.append(f"## {t.date} — {t.payee} — ${t.abs_amount:.2f}")
        if t.account:
            lines.append(f"_{t.account}_\n")
        if t.current_category_name:
            lines.append(f"**YNAB auto-filled (unapproved):** {t.current_category_name}")
            lines.append("")
        if t.items:
            lines.append("**Items:**")
            for it in t.items:
                p = it.get("price")
                p_str = f" (${float(p):.2f})" if p else ""
                lines.append(f"- {it['title']}{p_str}")
            lines.append("")

        if action == "skip":
            lines.append(f"⚠️  **skip** — {notes}\n")
        elif action == "split":
            lines.append(f"**SPLIT** (confidence: {conf})")
            for s in sugg:
                amt = float(s.get("amount", 0))
                lines.append(f"- ${amt:>7.2f} → {s.get('category', '?')}  _{s.get('reason', '')}_")
            lines.append("")
        else:
            s = sugg[0] if sugg else {}
            lines.append(f"**{s.get('category', '?')}** (confidence: {conf})")
            if s.get("reason"):
                lines.append(f"_{s['reason']}_")
            lines.append("")

        lines.append("---\n")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────
def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"ERROR: {name} not set in environment.")
    return v


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("budgets", help="list your YNAB budgets and IDs")
    sub.add_parser("test", help="test YNAB, Claude, and Amazon connections")

    s = sub.add_parser("suggest", help="dry-run: emit markdown suggestion report")
    s.add_argument("--days", type=int, default=30, help="only txns from last N days")
    s.add_argument("--limit", type=int, default=None, help="cap number of txns")
    s.add_argument("--model", choices=list(MODELS), default="haiku")
    s.add_argument("--use-cache", action="store_true", default=True,
                   help="cache Claude suggestions (default on)")
    s.add_argument("--no-cache", dest="use_cache", action="store_false")
    s.add_argument("--amazon-csv", type=Path, default=None,
                   help="path to CSV from gmail_amazon.py (skips scraper)")
    s.add_argument("--amazon-items-csv", type=Path, default=None,
                   help="path to Items CSV from Amazon Order History Reporter Chrome ext")
    s.add_argument("--amazon-orders-csv", type=Path, default=None,
                   help="path to Orders CSV from Amazon Order History Reporter")
    s.add_argument("--amazon-transactions-csv", type=Path, default=None,
                   help="path to Transactions CSV from azad premium "
                        "(per-charge data — gold standard for matching)")
    s.add_argument("--debug-amazon", action="store_true",
                   help="print why each Amazon transaction failed to match")

    a = sub.add_parser("apply", help="interactive review + writeback to YNAB")
    a.add_argument("--days", type=int, default=30)
    a.add_argument("--limit", type=int, default=None)
    a.add_argument("--model", choices=list(MODELS), default="haiku")
    a.add_argument("--auto-high", action="store_true",
                   help="auto-apply high-confidence suggestions without prompting")
    a.add_argument(
        "--no-approve",
        dest="approve",
        action="store_false",
        default=True,
        help="opt out of auto-approve. With this flag, [y] writes the "
             "category but leaves the transaction UNAPPROVED in YNAB; "
             "an [a] keystroke becomes available to apply+approve. "
             "Default is auto-approve (y always approves).",
    )
    a.add_argument("--amazon-csv", type=Path, default=None,
                   help="path to CSV from gmail_amazon.py (skips scraper)")
    a.add_argument("--amazon-items-csv", type=Path, default=None,
                   help="path to Items CSV from Amazon Order History Reporter Chrome ext")
    a.add_argument("--amazon-orders-csv", type=Path, default=None,
                   help="path to Orders CSV from Amazon Order History Reporter")
    a.add_argument("--amazon-transactions-csv", type=Path, default=None,
                   help="path to Transactions CSV from azad premium "
                        "(per-charge data — gold standard for matching)")
    a.add_argument("--debug-amazon", action="store_true",
                   help="print why each Amazon transaction failed to match")

    args = ap.parse_args()
    return {
        "budgets": cmd_budgets,
        "test": cmd_test,
        "suggest": cmd_suggest,
        "apply": cmd_apply,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
