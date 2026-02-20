"""
AI Chat blueprint – lets users interact with an AI agent that can
read and manipulate all accounting data (categories, transactions,
assets, depreciation categories, settings, users).

Supported providers (set via env vars):
  AI_PROVIDER   = openai | anthropic | custom   (default: openai)
  AI_API_KEY    = <your key>
  AI_MODEL      = <model name>                   (default: gpt-4o)
  AI_BASE_URL   = <custom base URL>              (only for 'custom')
"""

import json
import os
import traceback
from datetime import date, datetime

from flask import (
    Blueprint, Response, jsonify, render_template, request, stream_with_context,
)
from flask_login import current_user, login_required

from helpers import (
    calculate_tax, calculate_tax_from_net, format_currency,
    get_tax_rate_for_treatment, parse_amount, parse_date,
    TAX_TREATMENT_LABELS,
)
from models import (
    Account, Asset, Category, ChatHistory, DepreciationCategory, Document, SiteSettings, Transaction, User, db,
)
from audit import archive_file
from depreciation import (
    DEPRECIATION_METHODS, get_book_value, get_depreciation_for_year,
    get_depreciation_schedule, get_disposal_result,
)

ai_bp = Blueprint('ai_chat', __name__, template_folder='../templates/admin')

# ---------------------------------------------------------------------------
# Provider helpers
# ---------------------------------------------------------------------------

def _get_ai_config():
    """Return (provider, api_key, model, base_url) from env."""
    provider = os.environ.get('AI_PROVIDER', 'openai').lower()
    api_key = os.environ.get('AI_API_KEY', '')
    model = os.environ.get('AI_MODEL', '')
    base_url = os.environ.get('AI_BASE_URL', '')

    if not model:
        if provider == 'anthropic':
            model = 'claude-sonnet-4-20250514'
        else:
            model = 'gpt-4o'

    return provider, api_key, model, base_url


# ---------------------------------------------------------------------------
# Tool definitions  (OpenAI function-calling schema)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    # ---- Categories ----
    {
        "type": "function",
        "function": {
            "name": "list_categories",
            "description": "List all booking categories. Optionally filter by type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type_filter": {
                        "type": "string",
                        "enum": ["income", "expense", "all"],
                        "description": "Filter categories by type. Default 'all'.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_category",
            "description": "Create a new booking category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Category name"},
                    "type": {"type": "string", "enum": ["income", "expense"]},
                    "description": {"type": "string", "description": "Optional description"},
                    "sort_order": {"type": "integer", "description": "Sort order (default 0)"},
                },
                "required": ["name", "type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_category",
            "description": "Edit an existing booking category by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["income", "expense"]},
                    "description": {"type": "string"},
                    "sort_order": {"type": "integer"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_category",
            "description": "Delete a booking category by ID. Transactions will be unlinked.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    # ---- Transactions ----
    {
        "type": "function",
        "function": {
            "name": "list_transactions",
            "description": "List transactions with optional filters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Filter by year"},
                    "month": {"type": "integer", "description": "Filter by month (1-12)"},
                    "type_filter": {"type": "string", "enum": ["income", "expense", "transfer", "all"]},
                    "category_id": {"type": "integer", "description": "Filter by category ID"},
                    "account_id": {"type": "integer", "description": "Filter by account ID"},
                    "limit": {"type": "integer", "description": "Max results (default 50)"},
                    "search": {"type": "string", "description": "Search in description/notes"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_transaction",
            "description": "Get full details of a single transaction by ID.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_transaction",
            "description": "Create a new transaction (income or expense).",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date YYYY-MM-DD"},
                    "type": {"type": "string", "enum": ["income", "expense"]},
                    "description": {"type": "string"},
                    "amount": {"type": "number", "description": "Gross amount (brutto)"},
                    "category_id": {"type": "integer", "description": "Category ID (optional)"},
                    "account_id": {"type": "integer", "description": "Account ID (required)"},
                    "tax_treatment": {
                        "type": "string",
                        "enum": ["none", "standard", "reduced", "tax_free", "reverse_charge", "intra_eu", "custom"],
                        "description": "Tax treatment (default 'none')",
                    },
                    "custom_tax_rate": {"type": "number", "description": "Custom tax rate if tax_treatment='custom'"},
                    "notes": {"type": "string"},
                },
                "required": ["date", "type", "description", "amount", "account_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_transaction",
            "description": "Edit an existing transaction by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "date": {"type": "string"},
                    "type": {"type": "string", "enum": ["income", "expense"]},
                    "description": {"type": "string"},
                    "amount": {"type": "number"},
                    "category_id": {"type": "integer"},
                    "account_id": {"type": "integer"},
                    "tax_treatment": {"type": "string"},
                    "custom_tax_rate": {"type": "number"},
                    "notes": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_transaction",
            "description": "Delete a transaction by ID.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    # ---- Assets ----
    {
        "type": "function",
        "function": {
            "name": "list_assets",
            "description": "List assets (Anlagegüter) with optional status filter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "disposed", "all"],
                        "description": "Filter by status (default 'all')",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_asset",
            "description": "Get full details of an asset by ID, including depreciation schedule.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_asset",
            "description": "Create a new depreciable asset. Use quantity > 1 to create a bundle of identical items (e.g. 6 lamps). The purchase_price_gross is the TOTAL price; it will be divided by quantity for each item. Each item depreciates individually. You SHOULD pass account_id to create the linked cash outflow transaction (Abgangsbuchung) – this records the payment in the account balance (not counted in EÜR). Only omit account_id if the user explicitly says no payment should be booked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "purchase_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "purchase_price_gross": {"type": "number", "description": "Total gross price for ALL items"},
                    "quantity": {"type": "integer", "description": "Number of identical items (default 1). Creates a bundle when > 1."},
                    "depreciation_method": {
                        "type": "string",
                        "enum": ["sofort", "linear", "sammelposten", "degressive"],
                    },
                    "useful_life_months": {"type": "integer"},
                    "salvage_value": {"type": "number"},
                    "depreciation_category_id": {"type": "integer"},
                    "purchase_tax_treatment": {"type": "string"},
                    "custom_tax_rate": {"type": "number"},
                    "account_id": {"type": "integer", "description": "Account ID to book the cash outflow transaction. Should normally always be provided to record the payment."},
                    "notes": {"type": "string"},
                },
                "required": ["name", "purchase_date", "purchase_price_gross"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_asset",
            "description": "Edit an existing asset by ID. To edit ALL assets in a bundle at once, pass bundle_id instead of id. When editing a bundle: name is the base name (suffixes are auto-generated), purchase_price_gross is the TOTAL price for all items (split equally). You can change the quantity to add or remove items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Single asset ID (use this OR bundle_id)"},
                    "bundle_id": {"type": "string", "description": "Bundle ID to edit all items at once"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "purchase_date": {"type": "string"},
                    "purchase_price_gross": {"type": "number", "description": "For bundles: TOTAL price for all items"},
                    "quantity": {"type": "integer", "description": "New quantity for the bundle (only with bundle_id). Increases add items, decreases remove the last active ones."},
                    "depreciation_method": {"type": "string"},
                    "useful_life_months": {"type": "integer"},
                    "salvage_value": {"type": "number"},
                    "depreciation_category_id": {"type": "integer"},
                    "purchase_tax_treatment": {"type": "string"},
                    "custom_tax_rate": {"type": "number"},
                    "notes": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_asset",
            "description": "Delete an asset by ID, or delete ALL assets in a bundle by passing bundle_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Single asset ID (use this OR bundle_id)"},
                    "bundle_id": {"type": "string", "description": "Bundle ID to delete all items at once"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispose_asset",
            "description": "Record the disposal of one or more assets. For bundles, you can pass multiple IDs to dispose several items at once. The disposal_price_gross is the TOTAL price for all disposed items (split equally).",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Single asset ID to dispose (use this OR ids, not both)"},
                    "ids": {"type": "array", "items": {"type": "integer"}, "description": "Multiple asset IDs to dispose at once (for bundle partial disposal)"},
                    "disposal_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "disposal_price_gross": {"type": "number", "description": "Total sale price gross for all items (0 if scrapped)"},
                    "disposal_reason": {
                        "type": "string",
                        "enum": ["sold", "scrapped", "private_use", "other"],
                    },
                    "disposal_tax_treatment": {"type": "string"},
                    "custom_tax_rate": {"type": "number"},
                },
                "required": ["disposal_date", "disposal_reason"],
            },
        },
    },
    # ---- Accounts ----
    {
        "type": "function",
        "function": {
            "name": "list_accounts",
            "description": "List all accounts (Konten) with their current balances.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_account",
            "description": "Create a new account (e.g. Bargeld, PayPal).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "initial_balance": {"type": "number", "description": "Starting balance (Startsaldo)"},
                    "sort_order": {"type": "integer"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_account",
            "description": "Edit an existing account by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "initial_balance": {"type": "number"},
                    "sort_order": {"type": "integer"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_account",
            "description": "Delete an account by ID. Fails if transactions are still assigned.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_transfer",
            "description": "Create a transfer (Umbuchung) between two accounts. Not counted in EÜR.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date YYYY-MM-DD"},
                    "from_account_id": {"type": "integer", "description": "Source account ID"},
                    "to_account_id": {"type": "integer", "description": "Destination account ID"},
                    "amount": {"type": "number", "description": "Transfer amount"},
                    "description": {"type": "string", "description": "Description (default 'Umbuchung')"},
                    "notes": {"type": "string"},
                },
                "required": ["date", "from_account_id", "to_account_id", "amount"],
            },
        },
    },
    # ---- Depreciation Categories ----
    {
        "type": "function",
        "function": {
            "name": "list_depreciation_categories",
            "description": "List all depreciation categories (AfA-Kategorien).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_depreciation_category",
            "description": "Create a new depreciation category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "useful_life_months": {"type": "integer"},
                    "default_method": {"type": "string", "enum": ["sofort", "linear", "sammelposten", "degressive"]},
                    "description": {"type": "string"},
                    "sort_order": {"type": "integer"},
                },
                "required": ["name", "useful_life_months"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_depreciation_category",
            "description": "Edit a depreciation category by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "useful_life_months": {"type": "integer"},
                    "default_method": {"type": "string"},
                    "description": {"type": "string"},
                    "sort_order": {"type": "integer"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_depreciation_category",
            "description": "Delete a depreciation category by ID.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    # ---- Settings ----
    {
        "type": "function",
        "function": {
            "name": "get_settings",
            "description": "Get current site/business settings.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_settings",
            "description": "Update site/business settings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "business_name": {"type": "string"},
                    "address_lines": {"type": "string"},
                    "contact_lines": {"type": "string"},
                    "bank_lines": {"type": "string"},
                    "tax_number": {"type": "string"},
                    "vat_id": {"type": "string"},
                    "tax_mode": {"type": "string", "enum": ["kleinunternehmer", "regular"]},
                    "tax_rate": {"type": "number"},
                    "tax_rate_reduced": {"type": "number"},
                },
                "required": [],
            },
        },
    },
    # ---- Dashboard / Report ----
    {
        "type": "function",
        "function": {
            "name": "get_dashboard_summary",
            "description": "Get a financial summary for a given year (income, expenses, profit, monthly breakdown).",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Year (default: current year)"},
                },
                "required": [],
            },
        },
    },
    # ---- Users (admin only) ----
    {
        "type": "function",
        "function": {
            "name": "list_users",
            "description": "List all users (admin only).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_user",
            "description": "Create a new user (admin only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "display_name": {"type": "string"},
                    "is_admin": {"type": "boolean"},
                },
                "required": ["username", "password"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_user",
            "description": "Edit user by ID (admin only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "username": {"type": "string"},
                    "display_name": {"type": "string"},
                    "password": {"type": "string"},
                    "is_admin": {"type": "boolean"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_user",
            "description": "Delete user by ID (admin only). Cannot delete yourself.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    # ---- Web access ----
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch the content of a web page by URL. Returns the text content (HTML tags stripped). Useful for looking up current tax rates, exchange rates, legal info, AfA tables, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL to fetch (https://...)"},
                    "max_length": {"type": "integer", "description": "Max characters to return (default 10000, max 50000)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo. Returns a list of results with title, URL, and snippet. Use this to find relevant pages before fetching them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Number of results (default 5, max 10)"},
                },
                "required": ["query"],
            },
        },
    },
    # ---- Python evaluation ----
    {
        "type": "function",
        "function": {
            "name": "python_eval",
            "description": (
                "Execute a Python code snippet and return its output. "
                "Use this for any calculations: arithmetic, percentages, tax computations, "
                "currency conversions, date math, statistics, etc. "
                "The code runs in a restricted sandbox. You have access to the math, decimal, "
                "datetime, statistics, and itertools modules. "
                "Use print() to produce output – whatever is printed will be returned as the result. "
                "If nothing is printed, the repr() of the last expression is returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute. Use print() for output.",
                    }
                },
                "required": ["code"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _cat_to_dict(c):
    return {
        'id': c.id, 'name': c.name, 'type': c.type,
        'description': c.description, 'sort_order': c.sort_order,
    }


def _tx_to_dict(t):
    return {
        'id': t.id,
        'date': t.date.isoformat() if t.date else None,
        'type': t.type,
        'description': t.description,
        'amount': t.amount,
        'net_amount': t.net_amount,
        'tax_amount': t.tax_amount,
        'tax_treatment': t.tax_treatment,
        'tax_rate': t.tax_rate,
        'category_id': t.category_id,
        'category_name': t.category.name if t.category else None,
        'account_id': t.account_id,
        'account_name': t.account.name if t.account else None,
        'transfer_to_account_id': t.transfer_to_account_id,
        'transfer_to_account_name': t.transfer_to_account.name if t.transfer_to_account else None,
        'linked_asset_id': t.linked_asset_id,
        'notes': t.notes,
        'documents': [{'id': d.id, 'filename': d.filename, 'original_filename': d.original_filename}
                       for d in Document.query.filter_by(entity_type='transaction', entity_id=t.id).all()],
        'document_filename': t.document_filename,  # legacy
    }


def _asset_to_dict(a, include_schedule=False):
    d = {
        'id': a.id,
        'name': a.name,
        'description': a.description,
        'bundle_id': a.bundle_id,
        'purchase_date': a.purchase_date.isoformat() if a.purchase_date else None,
        'purchase_price_gross': a.purchase_price_gross,
        'purchase_price_net': a.purchase_price_net,
        'purchase_tax_treatment': a.purchase_tax_treatment,
        'purchase_tax_rate': a.purchase_tax_rate,
        'purchase_tax_amount': a.purchase_tax_amount,
        'depreciation_method': a.depreciation_method,
        'useful_life_months': a.useful_life_months,
        'salvage_value': a.salvage_value,
        'depreciation_category_id': a.depreciation_category_id,
        'depreciation_category_name': a.depreciation_category.name if a.depreciation_category else None,
        'book_value': get_book_value(a),
        'is_active': a.is_active,
        'disposal_date': a.disposal_date.isoformat() if a.disposal_date else None,
        'disposal_price': a.disposal_price,
        'disposal_price_gross': a.disposal_price_gross,
        'disposal_reason': a.disposal_reason,
        'notes': a.notes,
    }
    if include_schedule:
        d['depreciation_schedule'] = get_depreciation_schedule(a)
        d['disposal_result'] = get_disposal_result(a)
    return d


def _dep_cat_to_dict(c):
    return {
        'id': c.id, 'name': c.name,
        'useful_life_months': c.useful_life_months,
        'default_method': c.default_method,
        'description': c.description,
        'sort_order': c.sort_order,
    }


def _account_to_dict(a):
    return {
        'id': a.id, 'name': a.name,
        'description': a.description,
        'initial_balance': a.initial_balance,
        'current_balance': a.get_balance(),
        'sort_order': a.sort_order,
    }


def _apply_tax(amount_gross, tax_treatment, settings, custom_rate=None):
    """Compute net_amount, tax_amount, effective_rate from gross."""
    effective_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)
    if effective_rate > 0:
        net, tax = calculate_tax(amount_gross, effective_rate)
    else:
        net, tax = amount_gross, 0.0
    return net, tax, effective_rate


def execute_tool(name, args):
    """Execute a tool by name with given args, return JSON-serialisable result."""
    settings = SiteSettings.get_settings()

    # ---- Categories ----
    if name == 'list_categories':
        tf = args.get('type_filter', 'all')
        q = Category.query.order_by(Category.sort_order, Category.name)
        if tf in ('income', 'expense'):
            q = q.filter_by(type=tf)
        return [_cat_to_dict(c) for c in q.all()]

    if name == 'create_category':
        c = Category(
            name=args['name'], type=args['type'],
            description=args.get('description'),
            sort_order=args.get('sort_order', 0),
        )
        db.session.add(c)
        db.session.commit()
        return {'status': 'created', 'category': _cat_to_dict(c)}

    if name == 'edit_category':
        c = Category.query.get(args['id'])
        if not c:
            return {'error': f"Category {args['id']} not found"}
        for k in ('name', 'type', 'description', 'sort_order'):
            if k in args:
                setattr(c, k, args[k])
        db.session.commit()
        return {'status': 'updated', 'category': _cat_to_dict(c)}

    if name == 'delete_category':
        c = Category.query.get(args['id'])
        if not c:
            return {'error': f"Category {args['id']} not found"}
        Transaction.query.filter_by(category_id=c.id).update({'category_id': None})
        db.session.delete(c)
        db.session.commit()
        return {'status': 'deleted', 'id': args['id']}

    # ---- Transactions ----
    if name == 'list_transactions':
        q = Transaction.query
        if 'year' in args:
            q = q.filter(db.extract('year', Transaction.date) == args['year'])
        if args.get('month'):
            q = q.filter(db.extract('month', Transaction.date) == args['month'])
        tf = args.get('type_filter', 'all')
        if tf in ('income', 'expense', 'transfer'):
            q = q.filter(Transaction.type == tf)
        if args.get('category_id'):
            q = q.filter(Transaction.category_id == args['category_id'])
        if args.get('account_id'):
            aid = args['account_id']
            q = q.filter(
                (Transaction.account_id == aid) | (Transaction.transfer_to_account_id == aid)
            )
        if args.get('search'):
            search = f"%{args['search']}%"
            q = q.filter(
                db.or_(
                    Transaction.description.ilike(search),
                    Transaction.notes.ilike(search),
                )
            )
        limit = args.get('limit', 50)
        txs = q.order_by(Transaction.date.desc()).limit(limit).all()
        return [_tx_to_dict(t) for t in txs]

    if name == 'get_transaction':
        t = Transaction.query.get(args['id'])
        if not t:
            return {'error': f"Transaction {args['id']} not found"}
        return _tx_to_dict(t)

    if name == 'create_transaction':
        tax_treatment = args.get('tax_treatment', 'none')
        if settings.tax_mode == 'kleinunternehmer':
            tax_treatment = 'none'
        gross = args['amount']
        net, tax, eff_rate = _apply_tax(gross, tax_treatment, settings, args.get('custom_tax_rate'))
        t = Transaction(
            date=parse_date(args['date']),
            type=args['type'],
            description=args['description'],
            amount=gross,
            net_amount=net,
            tax_amount=tax,
            tax_treatment=tax_treatment,
            tax_rate=eff_rate,
            category_id=args.get('category_id'),
            account_id=args.get('account_id'),
            notes=args.get('notes'),
        )
        db.session.add(t)
        db.session.commit()
        return {'status': 'created', 'transaction': _tx_to_dict(t)}

    if name == 'edit_transaction':
        t = Transaction.query.get(args['id'])
        if not t:
            return {'error': f"Transaction {args['id']} not found"}
        if t.linked_asset_id:
            return {'error': 'Cannot edit a linked asset transaction'}
        if t.type == 'transfer':
            return {'error': 'Cannot edit a transfer via edit_transaction. Delete and recreate instead.'}
        if 'date' in args:
            t.date = parse_date(args['date'])
        if 'type' in args:
            t.type = args['type']
        if 'description' in args:
            t.description = args['description']
        if 'category_id' in args:
            t.category_id = args['category_id'] or None
        if 'account_id' in args:
            t.account_id = args['account_id'] or None
        if 'notes' in args:
            t.notes = args['notes'] or None

        # Recalculate tax if amount or treatment changed
        tax_treatment = args.get('tax_treatment', t.tax_treatment or 'none')
        if settings.tax_mode == 'kleinunternehmer':
            tax_treatment = 'none'
        t.tax_treatment = tax_treatment

        gross = args.get('amount', t.amount)
        net, tax, eff_rate = _apply_tax(gross, tax_treatment, settings, args.get('custom_tax_rate'))
        t.amount = gross
        t.net_amount = net
        t.tax_amount = tax
        t.tax_rate = eff_rate

        db.session.commit()
        return {'status': 'updated', 'transaction': _tx_to_dict(t)}

    if name == 'delete_transaction':
        t = Transaction.query.get(args['id'])
        if not t:
            return {'error': f"Transaction {args['id']} not found"}
        if t.linked_asset_id:
            return {'error': 'Cannot delete a linked asset transaction. Delete the asset instead.'}
        # Archive all attached documents
        for doc in Document.query.filter_by(entity_type='transaction', entity_id=t.id).all():
            from flask import current_app
            archive_file(current_app.config['UPLOAD_FOLDER'], doc.filename)
            db.session.delete(doc)
        db.session.delete(t)
        db.session.commit()
        return {'status': 'deleted', 'id': args['id']}

    # ---- Assets ----
    if name == 'list_assets':
        status = args.get('status', 'all')
        q = Asset.query
        if status == 'active':
            q = q.filter(Asset.disposal_date.is_(None))
        elif status == 'disposed':
            q = q.filter(Asset.disposal_date.isnot(None))
        return [_asset_to_dict(a) for a in q.order_by(Asset.purchase_date.desc()).all()]

    if name == 'get_asset':
        a = Asset.query.get(args['id'])
        if not a:
            return {'error': f"Asset {args['id']} not found"}
        return _asset_to_dict(a, include_schedule=True)

    if name == 'create_asset':
        import uuid as _uuid
        tax_treatment = args.get('purchase_tax_treatment', 'none')
        if settings.tax_mode == 'kleinunternehmer':
            tax_treatment = 'none'
        total_gross = args['purchase_price_gross']
        quantity = max(1, args.get('quantity', 1))

        # Compute per-unit prices
        unit_gross = round(total_gross / quantity, 2) if quantity > 1 else total_gross
        unit_net, unit_tax, eff_rate = _apply_tax(unit_gross, tax_treatment, settings, args.get('custom_tax_rate'))

        bundle_id = str(_uuid.uuid4()) if quantity > 1 else None
        base_name = args['name']
        created = []

        for i in range(quantity):
            item_name = f"{base_name} ({i+1}/{quantity})" if quantity > 1 else base_name
            a = Asset(
                name=item_name,
                description=args.get('description'),
                bundle_id=bundle_id,
                purchase_date=parse_date(args['purchase_date']),
                purchase_price_gross=unit_gross,
                purchase_price_net=unit_net,
                purchase_tax_treatment=tax_treatment,
                purchase_tax_rate=eff_rate,
                purchase_tax_amount=unit_tax,
                depreciation_method=args.get('depreciation_method', 'linear'),
                useful_life_months=args.get('useful_life_months'),
                salvage_value=args.get('salvage_value', 0),
                depreciation_category_id=args.get('depreciation_category_id'),
                notes=args.get('notes'),
            )
            db.session.add(a)
            db.session.flush()
            created.append(a)

        # Optional: create linked cash outflow transaction
        outflow_account_id = args.get('account_id')
        if outflow_account_id:
            link_asset_id = created[0].id
            outflow_tx = Transaction(
                date=parse_date(args['purchase_date']),
                type='expense',
                description=f'Anlagekauf: {base_name}',
                amount=total_gross,
                net_amount=unit_net * quantity,
                tax_amount=unit_tax * quantity,
                tax_treatment=tax_treatment,
                tax_rate=eff_rate,
                account_id=outflow_account_id,
                linked_asset_id=link_asset_id,
                notes='Automatisch erstellt bei Anlage von Anlagegut',
            )
            db.session.add(outflow_tx)

        db.session.commit()
        if quantity > 1:
            return {
                'status': 'created',
                'bundle_id': bundle_id,
                'quantity': quantity,
                'per_unit_gross': unit_gross,
                'per_unit_net': unit_net,
                'assets': [_asset_to_dict(a) for a in created],
            }
        return {'status': 'created', 'asset': _asset_to_dict(created[0])}

    if name == 'edit_asset':
        # Bundle edit: edit all items in a bundle at once
        if 'bundle_id' in args:
            items = Asset.query.filter_by(bundle_id=args['bundle_id']).order_by(Asset.id).all()
            if not items:
                return {'error': f"Bundle {args['bundle_id']} not found"}

            # Handle quantity changes
            new_quantity = args.get('quantity')
            if new_quantity is not None:
                old_count = len(items)
                disposed_items = [a for a in items if a.disposal_date is not None]
                active_items = [a for a in items if a.disposal_date is None]
                min_qty = len(disposed_items)

                if new_quantity < 1:
                    return {'error': 'Quantity must be at least 1'}
                if new_quantity < min_qty:
                    return {'error': f'Cannot reduce below {min_qty} (already disposed)'}

                if new_quantity < old_count:
                    # Remove excess active items (last ones first)
                    to_remove = old_count - new_quantity
                    removable = sorted(active_items, key=lambda a: a.id, reverse=True)
                    for a in removable[:to_remove]:
                        db.session.delete(a)
                        items.remove(a)
                elif new_quantity > old_count:
                    rep = items[0]
                    rep_docs = Document.query.filter_by(entity_type='asset', entity_id=rep.id).all()
                    for _ in range(new_quantity - old_count):
                        new_asset = Asset(
                            bundle_id=args['bundle_id'],
                            name='',
                            purchase_date=rep.purchase_date,
                            purchase_price_gross=0,
                            purchase_price_net=0,
                            depreciation_method=rep.depreciation_method,
                            useful_life_months=rep.useful_life_months,
                            salvage_value=rep.salvage_value or 0,
                            depreciation_category_id=rep.depreciation_category_id,
                        )
                        db.session.add(new_asset)
                        items.append(new_asset)
                    db.session.flush()
                    # Copy documents from representative to new items
                    for new_asset in items[-(new_quantity - old_count):]:
                        for rd in rep_docs:
                            doc = Document(filename=rd.filename, original_filename=rd.original_filename,
                                           entity_type='asset', entity_id=new_asset.id)
                            db.session.add(doc)
                    db.session.flush()  # assign IDs to new items

            count = len(items)
            # For bundles, 'name' is the base name
            base_name = args.get('name')
            # Handle price: purchase_price_gross is TOTAL for all items
            if 'purchase_price_gross' in args:
                tax_treatment = args.get('purchase_tax_treatment', items[0].purchase_tax_treatment or 'none')
                if settings.tax_mode == 'kleinunternehmer':
                    tax_treatment = 'none'
                total_gross = args['purchase_price_gross']
                unit_gross = round(total_gross / count, 2)
                unit_net, unit_tax, eff_rate = _apply_tax(unit_gross, tax_treatment, settings, args.get('custom_tax_rate'))
            simple_fields = ['description', 'depreciation_method', 'useful_life_months',
                             'salvage_value', 'depreciation_category_id', 'notes']
            items.sort(key=lambda a: a.id)
            for i, a in enumerate(items, 1):
                if base_name:
                    a.name = f"{base_name} ({i}/{count})"
                elif new_quantity is not None:
                    # Re-number even without a new name
                    old_base = a.name.rsplit(' (', 1)[0] if '(' in a.name else a.name
                    if not old_base:
                        old_base = items[0].name.rsplit(' (', 1)[0] if '(' in items[0].name else items[0].name
                    a.name = f"{old_base} ({i}/{count})"
                for k in simple_fields:
                    if k in args:
                        setattr(a, k, args[k])
                if 'purchase_date' in args:
                    a.purchase_date = parse_date(args['purchase_date'])
                if 'purchase_price_gross' in args:
                    a.purchase_price_gross = unit_gross
                    a.purchase_price_net = unit_net
                    a.purchase_tax_treatment = tax_treatment
                    a.purchase_tax_rate = eff_rate
                    a.purchase_tax_amount = unit_tax
            db.session.commit()
            return {'status': 'updated', 'count': count, 'bundle_id': args['bundle_id'],
                    'assets': [_asset_to_dict(a) for a in items]}

        # Single asset edit
        if 'id' not in args:
            return {'error': 'Either id or bundle_id is required'}
        a = Asset.query.get(args['id'])
        if not a:
            return {'error': f"Asset {args['id']} not found"}
        simple = ['name', 'description', 'depreciation_method', 'useful_life_months',
                  'salvage_value', 'depreciation_category_id', 'notes']
        for k in simple:
            if k in args:
                setattr(a, k, args[k])
        if 'purchase_date' in args:
            a.purchase_date = parse_date(args['purchase_date'])
        if 'purchase_price_gross' in args:
            tax_treatment = args.get('purchase_tax_treatment', a.purchase_tax_treatment or 'none')
            if settings.tax_mode == 'kleinunternehmer':
                tax_treatment = 'none'
            gross = args['purchase_price_gross']
            net, tax, eff_rate = _apply_tax(gross, tax_treatment, settings, args.get('custom_tax_rate'))
            a.purchase_price_gross = gross
            a.purchase_price_net = net
            a.purchase_tax_treatment = tax_treatment
            a.purchase_tax_rate = eff_rate
            a.purchase_tax_amount = tax
        db.session.commit()
        return {'status': 'updated', 'asset': _asset_to_dict(a)}

    if name == 'delete_asset':
        # Bundle delete: delete all items in a bundle
        if 'bundle_id' in args:
            items = Asset.query.filter_by(bundle_id=args['bundle_id']).all()
            if not items:
                return {'error': f"Bundle {args['bundle_id']} not found"}
            count = len(items)
            removed_files = set()
            for a in items:
                Transaction.query.filter_by(linked_asset_id=a.id).delete()
                for doc in Document.query.filter_by(entity_type='asset', entity_id=a.id).all():
                    if doc.filename not in removed_files:
                        from flask import current_app
                        archive_file(current_app.config['UPLOAD_FOLDER'], doc.filename)
                        removed_files.add(doc.filename)
                    db.session.delete(doc)
                db.session.delete(a)
            db.session.commit()
            return {'status': 'deleted', 'count': count, 'bundle_id': args['bundle_id']}

        # Single asset delete
        if 'id' not in args:
            return {'error': 'Either id or bundle_id is required'}
        a = Asset.query.get(args['id'])
        if not a:
            return {'error': f"Asset {args['id']} not found"}
        Transaction.query.filter_by(linked_asset_id=a.id).delete()
        # Archive all attached documents
        for doc in Document.query.filter_by(entity_type='asset', entity_id=a.id).all():
            from flask import current_app
            archive_file(current_app.config['UPLOAD_FOLDER'], doc.filename)
            db.session.delete(doc)
        db.session.delete(a)
        db.session.commit()
        return {'status': 'deleted', 'id': args['id']}

    if name == 'dispose_asset':
        # Support single id or multiple ids
        ids = args.get('ids', [])
        if not ids and 'id' in args:
            ids = [args['id']]
        if not ids:
            return {'error': 'Either id or ids is required'}

        tax_treatment = args.get('disposal_tax_treatment', 'none')
        if settings.tax_mode == 'kleinunternehmer':
            tax_treatment = 'none'
        total_gross = args.get('disposal_price_gross', 0)

        count = len(ids)
        unit_gross = round(total_gross / count, 2) if count > 1 else total_gross
        unit_net, unit_tax, eff_rate = _apply_tax(unit_gross, tax_treatment, settings, args.get('custom_tax_rate'))

        disposal_date = parse_date(args['disposal_date'])
        disposal_reason = args.get('disposal_reason', 'sold')

        disposed = []
        errors = []
        for aid in ids:
            a = Asset.query.get(aid)
            if not a:
                errors.append(f"Asset {aid} not found")
                continue
            if a.disposal_date:
                errors.append(f"Asset {aid} ({a.name}) already disposed")
                continue
            a.disposal_date = disposal_date
            a.disposal_price_gross = unit_gross
            a.disposal_price = unit_net
            a.disposal_tax_treatment = tax_treatment
            a.disposal_tax_rate = eff_rate
            a.disposal_tax_amount = unit_tax
            a.disposal_reason = disposal_reason
            disposed.append(_asset_to_dict(a))

        db.session.commit()
        result = {'status': 'disposed', 'count': len(disposed), 'assets': disposed}
        if errors:
            result['errors'] = errors
        return result

    # ---- Accounts ----
    if name == 'list_accounts':
        accs = Account.query.order_by(Account.sort_order, Account.name).all()
        return [_account_to_dict(a) for a in accs]

    if name == 'create_account':
        a = Account(
            name=args['name'],
            description=args.get('description'),
            initial_balance=args.get('initial_balance', 0.0),
            sort_order=args.get('sort_order', 0),
        )
        db.session.add(a)
        db.session.commit()
        return {'status': 'created', 'account': _account_to_dict(a)}

    if name == 'edit_account':
        a = Account.query.get(args['id'])
        if not a:
            return {'error': f"Account {args['id']} not found"}
        for k in ('name', 'description', 'initial_balance', 'sort_order'):
            if k in args:
                setattr(a, k, args[k])
        db.session.commit()
        return {'status': 'updated', 'account': _account_to_dict(a)}

    if name == 'delete_account':
        a = Account.query.get(args['id'])
        if not a:
            return {'error': f"Account {args['id']} not found"}
        tx_count = Transaction.query.filter(
            (Transaction.account_id == a.id) | (Transaction.transfer_to_account_id == a.id)
        ).count()
        if tx_count > 0:
            return {'error': f'Account has {tx_count} transactions and cannot be deleted'}
        db.session.delete(a)
        db.session.commit()
        return {'status': 'deleted', 'id': args['id']}

    if name == 'create_transfer':
        from_id = args['from_account_id']
        to_id = args['to_account_id']
        if from_id == to_id:
            return {'error': 'Source and destination account must be different'}
        if not Account.query.get(from_id):
            return {'error': f'Source account {from_id} not found'}
        if not Account.query.get(to_id):
            return {'error': f'Destination account {to_id} not found'}
        amount = args['amount']
        t = Transaction(
            date=parse_date(args['date']),
            type='transfer',
            description=args.get('description', 'Umbuchung'),
            amount=amount,
            net_amount=amount,
            tax_amount=0.0,
            tax_treatment='none',
            tax_rate=0.0,
            account_id=from_id,
            transfer_to_account_id=to_id,
            notes=args.get('notes'),
        )
        db.session.add(t)
        db.session.commit()
        return {'status': 'created', 'transaction': _tx_to_dict(t)}

    # ---- Depreciation Categories ----
    if name == 'list_depreciation_categories':
        cats = DepreciationCategory.query.order_by(
            DepreciationCategory.sort_order, DepreciationCategory.name
        ).all()
        return [_dep_cat_to_dict(c) for c in cats]

    if name == 'create_depreciation_category':
        c = DepreciationCategory(
            name=args['name'],
            useful_life_months=args['useful_life_months'],
            default_method=args.get('default_method', 'linear'),
            description=args.get('description'),
            sort_order=args.get('sort_order', 0),
        )
        db.session.add(c)
        db.session.commit()
        return {'status': 'created', 'depreciation_category': _dep_cat_to_dict(c)}

    if name == 'edit_depreciation_category':
        c = DepreciationCategory.query.get(args['id'])
        if not c:
            return {'error': f"DepreciationCategory {args['id']} not found"}
        for k in ('name', 'useful_life_months', 'default_method', 'description', 'sort_order'):
            if k in args:
                setattr(c, k, args[k])
        db.session.commit()
        return {'status': 'updated', 'depreciation_category': _dep_cat_to_dict(c)}

    if name == 'delete_depreciation_category':
        c = DepreciationCategory.query.get(args['id'])
        if not c:
            return {'error': f"DepreciationCategory {args['id']} not found"}
        Asset.query.filter_by(depreciation_category_id=c.id).update({'depreciation_category_id': None})
        db.session.delete(c)
        db.session.commit()
        return {'status': 'deleted', 'id': args['id']}

    # ---- Settings ----
    if name == 'get_settings':
        s = settings
        return {
            'business_name': s.business_name,
            'address_lines': s.address_lines,
            'contact_lines': s.contact_lines,
            'bank_lines': s.bank_lines,
            'tax_number': s.tax_number,
            'vat_id': s.vat_id,
            'tax_mode': s.tax_mode,
            'tax_rate': s.tax_rate,
            'tax_rate_reduced': s.tax_rate_reduced,
        }

    if name == 'update_settings':
        s = settings
        for k in ('business_name', 'address_lines', 'contact_lines', 'bank_lines',
                   'tax_number', 'vat_id', 'tax_mode', 'tax_rate', 'tax_rate_reduced'):
            if k in args:
                setattr(s, k, args[k])
        db.session.commit()
        return {'status': 'updated', 'settings': execute_tool('get_settings', {})}

    # ---- Dashboard ----
    if name == 'get_dashboard_summary':
        year = args.get('year', date.today().year)
        txs = Transaction.query.filter(db.extract('year', Transaction.date) == year).all()
        total_income = sum(t.amount for t in txs if t.type == 'income')
        total_expenses = sum(t.amount for t in txs if t.type == 'expense')
        monthly = {}
        months_map = {1: 'Jan', 2: 'Feb', 3: 'Mär', 4: 'Apr', 5: 'Mai', 6: 'Jun',
                      7: 'Jul', 8: 'Aug', 9: 'Sep', 10: 'Okt', 11: 'Nov', 12: 'Dez'}
        for m in range(1, 13):
            mt = [t for t in txs if t.date.month == m]
            inc = sum(t.amount for t in mt if t.type == 'income')
            exp = sum(t.amount for t in mt if t.type == 'expense')
            monthly[months_map[m]] = {'income': inc, 'expenses': exp, 'profit': inc - exp}
        return {
            'year': year,
            'total_income': total_income,
            'total_expenses': total_expenses,
            'profit': total_income - total_expenses,
            'transaction_count': len(txs),
            'monthly': monthly,
        }

    # ---- Users ----
    if name == 'list_users':
        if not current_user.is_admin:
            return {'error': 'Admin privileges required'}
        return [{'id': u.id, 'username': u.username, 'display_name': u.display_name,
                 'is_admin': u.is_admin} for u in User.query.order_by(User.username).all()]

    if name == 'create_user':
        if not current_user.is_admin:
            return {'error': 'Admin privileges required'}
        from werkzeug.security import generate_password_hash
        if User.query.filter_by(username=args['username']).first():
            return {'error': f"Username '{args['username']}' already exists"}
        u = User(
            username=args['username'],
            password_hash=generate_password_hash(args['password']),
            display_name=args.get('display_name', ''),
            is_admin=args.get('is_admin', False),
        )
        db.session.add(u)
        db.session.commit()
        return {'status': 'created', 'user': {'id': u.id, 'username': u.username}}

    if name == 'edit_user':
        if not current_user.is_admin:
            return {'error': 'Admin privileges required'}
        u = User.query.get(args['id'])
        if not u:
            return {'error': f"User {args['id']} not found"}
        if 'username' in args:
            existing = User.query.filter_by(username=args['username']).first()
            if existing and existing.id != u.id:
                return {'error': f"Username '{args['username']}' already taken"}
            u.username = args['username']
        if 'display_name' in args:
            u.display_name = args['display_name']
        if 'is_admin' in args:
            u.is_admin = args['is_admin']
        if 'password' in args:
            from werkzeug.security import generate_password_hash
            u.password_hash = generate_password_hash(args['password'])
        db.session.commit()
        return {'status': 'updated', 'user': {'id': u.id, 'username': u.username}}

    if name == 'delete_user':
        if not current_user.is_admin:
            return {'error': 'Admin privileges required'}
        u = User.query.get(args['id'])
        if not u:
            return {'error': f"User {args['id']} not found"}
        if u.id == current_user.id:
            return {'error': 'Cannot delete yourself'}
        db.session.delete(u)
        db.session.commit()
        return {'status': 'deleted', 'id': args['id']}

    # ---- Web access ----
    if name == 'fetch_url':
        import httpx
        import re
        url = args.get('url', '')
        max_len = min(args.get('max_length', 10000), 50000)
        if not url.startswith(('http://', 'https://')):
            return {'error': 'URL must start with http:// or https://'}
        try:
            resp = httpx.get(url, timeout=15, follow_redirects=True, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; AccountingBot/1.0)'
            })
            resp.raise_for_status()
            ct = resp.headers.get('content-type', '')
            if 'html' in ct:
                text = resp.text
                # Strip script/style blocks
                text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', text, flags=re.DOTALL|re.IGNORECASE)
                # Strip HTML tags
                text = re.sub(r'<[^>]+>', ' ', text)
                # Collapse whitespace
                text = re.sub(r'\s+', ' ', text).strip()
            else:
                text = resp.text
            if len(text) > max_len:
                text = text[:max_len] + '\n... (truncated)'
            return {'url': url, 'content': text, 'length': len(text)}
        except httpx.HTTPStatusError as e:
            return {'error': f'HTTP {e.response.status_code} for {url}'}
        except Exception as e:
            return {'error': f'Failed to fetch {url}: {str(e)}'}

    if name == 'python_eval':
        code = args.get('code', '')
        if not code.strip():
            return {'error': 'No code provided'}

        # ---- Security: block known sandbox-escape patterns ----
        import re as _re
        _forbidden_patterns = _re.compile(
            r'__\s*(subclasses|bases|mro|class|globals|locals|builtins|dict|init'
            r'|import|loader|spec|code|func|self|reduce|getstate|module'
            r'|qualname|wrapped|closure|call|delattr|setattr|getattr'
            r'|getattribute)\s*__'
            r'|importlib|__import__|subprocess|shutil'
            r'|\bos\b\s*\.\s*(system|popen|exec|spawn|remove|unlink|rmdir|rename|listdir|walk|makedirs|path)'
            r'|\bopen\s*\('
            r'|\beval\s*\('
            r'|\bexec\s*\('
            r'|\bcompile\s*\('
            r'|\bglobals\s*\('
            r'|\blocals\s*\('
            r'|\bbreakpoint\s*\('
            r'|\bvars\s*\('
            r'|\bgetattr\s*\('
            r'|\bsetattr\s*\('
            r'|\bdelattr\s*\(',
            _re.IGNORECASE
        )
        if _forbidden_patterns.search(code):
            return {'error': 'Code contains disallowed operations (security restriction)'}

        try:
            import io
            import contextlib
            import math
            import decimal
            import statistics
            import itertools
            from datetime import date as _date, datetime as _datetime, timedelta as _timedelta

            # Restricted global namespace — NO open, __import__, exec, eval,
            # compile, getattr, setattr, delattr, vars, globals, locals, breakpoint
            safe_globals = {
                '__builtins__': {
                    'abs': abs, 'all': all, 'any': any, 'bin': bin,
                    'bool': bool, 'chr': chr, 'dict': dict,
                    'divmod': divmod, 'enumerate': enumerate, 'filter': filter,
                    'float': float, 'format': format, 'frozenset': frozenset,
                    'hash': hash, 'hex': hex, 'int': int, 'isinstance': isinstance,
                    'issubclass': issubclass, 'iter': iter, 'len': len,
                    'list': list, 'map': map, 'max': max, 'min': min,
                    'next': next, 'oct': oct, 'ord': ord, 'pow': pow,
                    'print': print, 'range': range, 'repr': repr,
                    'reversed': reversed, 'round': round, 'set': set,
                    'slice': slice, 'sorted': sorted, 'str': str,
                    'sum': sum, 'tuple': tuple, 'type': type, 'zip': zip,
                    'True': True, 'False': False, 'None': None,
                    'ValueError': ValueError, 'TypeError': TypeError,
                    'ZeroDivisionError': ZeroDivisionError,
                    'ArithmeticError': ArithmeticError,
                },
                'math': math,
                'decimal': decimal,
                'Decimal': decimal.Decimal,
                'statistics': statistics,
                'itertools': itertools,
                'date': _date,
                'datetime': _datetime,
                'timedelta': _timedelta,
            }

            stdout_capture = io.StringIO()
            safe_locals = {}

            # Run with a timeout to prevent infinite loops
            import signal

            def _timeout_handler(signum, frame):
                raise TimeoutError('Code execution timed out (5s limit)')

            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(5)  # 5 second limit
            try:
                with contextlib.redirect_stdout(stdout_capture):
                    exec(compile(code, '<ai_calc>', 'exec'), safe_globals, safe_locals)
            finally:
                signal.alarm(0)  # cancel alarm
                signal.signal(signal.SIGALRM, old_handler)

            output = stdout_capture.getvalue()
            if not output.strip():
                # If nothing was printed, try to get the last expression value
                # by re-evaluating the last line as an expression
                lines = [l for l in code.strip().splitlines() if l.strip() and not l.strip().startswith('#')]
                if lines:
                    try:
                        last_val = eval(lines[-1], safe_globals, safe_locals)
                        if last_val is not None:
                            output = repr(last_val)
                    except Exception:
                        pass

            if not output.strip():
                output = '(no output)'

            # Limit output size
            if len(output) > 10000:
                output = output[:10000] + '\n... (truncated)'

            return {'output': output.strip()}
        except Exception as e:
            return {'error': f'{type(e).__name__}: {str(e)}'}

    if name == 'web_search':
        import httpx
        query = args.get('query', '')
        max_results = min(args.get('max_results', 5), 10)
        if not query:
            return {'error': 'query is required'}
        try:
            # Use DuckDuckGo HTML search
            resp = httpx.get(
                'https://html.duckduckgo.com/html/',
                params={'q': query},
                timeout=15,
                headers={'User-Agent': 'Mozilla/5.0 (compatible; AccountingBot/1.0)'},
                follow_redirects=True,
            )
            resp.raise_for_status()
            import re
            # Parse results from DDG HTML
            results = []
            # Find result blocks
            blocks = re.findall(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
                r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                resp.text, re.DOTALL
            )
            for href, title, snippet in blocks[:max_results]:
                # Clean HTML from title/snippet
                title = re.sub(r'<[^>]+>', '', title).strip()
                snippet = re.sub(r'<[^>]+>', '', snippet).strip()
                # DDG wraps URLs in a redirect; extract actual URL
                import urllib.parse
                if 'uddg=' in href:
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                    href = parsed.get('uddg', [href])[0]
                results.append({'title': title, 'url': href, 'snippet': snippet})
            return {'query': query, 'results': results, 'count': len(results)}
        except Exception as e:
            return {'error': f'Search failed: {str(e)}'}

    return {'error': f'Unknown tool: {name}'}


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------

# Tools that only read data (no confirmation needed)
READ_ONLY_TOOLS = {
    'list_categories', 'list_transactions', 'list_assets',
    'list_depreciation_categories', 'list_accounts',
    'get_transaction', 'get_asset',
    'get_settings', 'get_dashboard_summary', 'list_users',
    'fetch_url', 'web_search', 'python_eval',
}

# Human-readable labels for tool calls
TOOL_LABELS = {
    'list_categories': '📋 Kategorien auflisten',
    'create_category': '➕ Kategorie erstellen',
    'edit_category': '✏️ Kategorie bearbeiten',
    'delete_category': '🗑️ Kategorie löschen',
    'list_transactions': '📋 Buchungen auflisten',
    'get_transaction': '🔍 Buchung abrufen',
    'create_transaction': '➕ Buchung erstellen',
    'edit_transaction': '✏️ Buchung bearbeiten',
    'delete_transaction': '🗑️ Buchung löschen',
    'list_assets': '📋 Anlagegüter auflisten',
    'get_asset': '🔍 Anlagegut abrufen',
    'create_asset': '➕ Anlagegut erstellen',
    'edit_asset': '✏️ Anlagegut bearbeiten',
    'delete_asset': '🗑️ Anlagegut löschen',
    'dispose_asset': '📤 Anlagegut Abgang',
    'list_accounts': '📋 Konten auflisten',
    'create_account': '➕ Konto erstellen',
    'edit_account': '✏️ Konto bearbeiten',
    'delete_account': '🗑️ Konto löschen',
    'create_transfer': '↔ Umbuchung erstellen',
    'list_depreciation_categories': '📋 AfA-Kategorien auflisten',
    'create_depreciation_category': '➕ AfA-Kategorie erstellen',
    'edit_depreciation_category': '✏️ AfA-Kategorie bearbeiten',
    'delete_depreciation_category': '🗑️ AfA-Kategorie löschen',
    'get_settings': '⚙️ Einstellungen abrufen',
    'update_settings': '⚙️ Einstellungen ändern',
    'get_dashboard_summary': '📊 Dashboard-Übersicht',
    'list_users': '👥 Benutzer auflisten',
    'create_user': '➕ Benutzer erstellen',
    'edit_user': '✏️ Benutzer bearbeiten',
    'delete_user': '🗑️ Benutzer löschen',
    'fetch_url': '🌐 Webseite abrufen',
    'web_search': '🔍 Websuche',
    'python_eval': '🧮 Berechnung ausführen',
}

# German labels for argument keys (for display)
ARG_LABELS = {
    'name': 'Name', 'type': 'Typ', 'description': 'Beschreibung',
    'sort_order': 'Sortierung', 'id': 'ID', 'date': 'Datum',
    'amount': 'Betrag', 'category_id': 'Kategorie-ID',
    'tax_treatment': 'Steuerbehandlung', 'custom_tax_rate': 'Steuersatz',
    'notes': 'Notizen', 'year': 'Jahr', 'month': 'Monat',
    'type_filter': 'Typ-Filter', 'limit': 'Limit', 'search': 'Suche',
    'status': 'Status', 'purchase_date': 'Kaufdatum',
    'purchase_price_gross': 'Kaufpreis (brutto)',
    'depreciation_method': 'AfA-Methode', 'useful_life_months': 'Nutzungsdauer (Monate)',
    'salvage_value': 'Restwert', 'depreciation_category_id': 'AfA-Kategorie-ID',
    'purchase_tax_treatment': 'Steuerbehandlung (Kauf)',
    'disposal_date': 'Abgangsdatum', 'disposal_price_gross': 'Verkaufspreis (brutto)',
    'disposal_reason': 'Abgangsgrund', 'disposal_tax_treatment': 'Steuerbehandlung (Verkauf)',
    'default_method': 'Standard-Methode',
    'business_name': 'Firmenname', 'address_lines': 'Adresse',
    'contact_lines': 'Kontakt', 'bank_lines': 'Bankverbindung',
    'tax_number': 'Steuernummer', 'vat_id': 'USt-IdNr.',
    'tax_mode': 'Steuermodus', 'tax_rate': 'Steuersatz',
    'tax_rate_reduced': 'Ermäßigter Steuersatz',
    'username': 'Benutzername', 'password': 'Passwort',
    'display_name': 'Anzeigename', 'is_admin': 'Administrator',
    'url': 'URL', 'max_length': 'Max. Zeichen', 'query': 'Suchbegriff',
    'max_results': 'Max. Ergebnisse',
    'quantity': 'Stückzahl', 'ids': 'IDs',
    'account_id': 'Konto-ID', 'from_account_id': 'Von-Konto-ID',
    'to_account_id': 'Nach-Konto-ID', 'initial_balance': 'Startsaldo',
    'code': 'Python-Code',
}


def _resolve_entity_name(tool_name, entity_id):
    """Look up a human-readable name for an entity ID based on the tool context."""
    try:
        if 'category' in tool_name and 'depreciation' not in tool_name:
            obj = Category.query.get(entity_id)
            return obj.name if obj else None
        if 'depreciation_category' in tool_name:
            obj = DepreciationCategory.query.get(entity_id)
            return obj.name if obj else None
        if 'account' in tool_name and 'transfer' not in tool_name:
            obj = Account.query.get(entity_id)
            return obj.name if obj else None
        if 'asset' in tool_name:
            obj = Asset.query.get(entity_id)
            return obj.name if obj else None
        if 'transaction' in tool_name or 'transfer' in tool_name:
            obj = Transaction.query.get(entity_id)
            if obj:
                return f"{obj.date.isoformat()} {obj.amount:.2f}€ ({obj.type})"
            return None
        if 'user' in tool_name:
            obj = User.query.get(entity_id)
            return obj.username if obj else None
    except Exception:
        pass
    return None


def _enrich_args_for_display(tool_name, args):
    """Return a copy of args with ID fields annotated with entity names for display."""
    enriched = dict(args)

    # Resolve primary 'id'
    if 'id' in enriched:
        label = _resolve_entity_name(tool_name, enriched['id'])
        if label:
            enriched['id'] = f"{enriched['id']} ({label})"
        else:
            enriched['id'] = f"{enriched['id']} (nicht gefunden ✗)"

    # Resolve 'ids' array
    if 'ids' in enriched and isinstance(enriched['ids'], list):
        resolved = []
        for eid in enriched['ids']:
            label = _resolve_entity_name(tool_name, eid)
            if label:
                resolved.append(f"{eid} ({label})")
            else:
                resolved.append(f"{eid} (nicht gefunden ✗)")
        enriched['ids'] = ', '.join(resolved)

    # Resolve 'bundle_id'
    if 'bundle_id' in enriched:
        bid = enriched['bundle_id']
        items = Asset.query.filter_by(bundle_id=bid).all()
        if items:
            base = items[0].name.rsplit(' (', 1)[0] if '(' in items[0].name else items[0].name
            enriched['bundle_id'] = f"{bid[:8]}… ({base}, {len(items)} Stk.)"
        else:
            enriched['bundle_id'] = f"{bid[:8]}… (nicht gefunden ✗)"

    # Resolve 'category_id'
    if 'category_id' in enriched:
        cat = Category.query.get(enriched['category_id'])
        if cat:
            enriched['category_id'] = f"{enriched['category_id']} ({cat.name})"
        else:
            enriched['category_id'] = f"{enriched['category_id']} (nicht gefunden ✗)"

    # Resolve 'depreciation_category_id'
    if 'depreciation_category_id' in enriched:
        dc = DepreciationCategory.query.get(enriched['depreciation_category_id'])
        if dc:
            enriched['depreciation_category_id'] = f"{enriched['depreciation_category_id']} ({dc.name})"
        else:
            enriched['depreciation_category_id'] = f"{enriched['depreciation_category_id']} (nicht gefunden ✗)"

    # Resolve 'account_id'
    if 'account_id' in enriched:
        acc = Account.query.get(enriched['account_id'])
        if acc:
            enriched['account_id'] = f"{enriched['account_id']} ({acc.name})"
        else:
            enriched['account_id'] = f"{enriched['account_id']} (nicht gefunden ✗)"

    # Resolve 'from_account_id'
    if 'from_account_id' in enriched:
        acc = Account.query.get(enriched['from_account_id'])
        if acc:
            enriched['from_account_id'] = f"{enriched['from_account_id']} ({acc.name})"
        else:
            enriched['from_account_id'] = f"{enriched['from_account_id']} (nicht gefunden ✗)"

    # Resolve 'to_account_id'
    if 'to_account_id' in enriched:
        acc = Account.query.get(enriched['to_account_id'])
        if acc:
            enriched['to_account_id'] = f"{enriched['to_account_id']} ({acc.name})"
        else:
            enriched['to_account_id'] = f"{enriched['to_account_id']} (nicht gefunden ✗)"

    return enriched


def _summarize_result(name, result):
    """Create a short German summary of a tool result."""
    if isinstance(result, list):
        return f'{len(result)} Ergebnis(se)'
    if isinstance(result, dict):
        if 'error' in result:
            return f"⚠ {result['error']}"
        status = result.get('status', '')
        # Include entity name in summary when available
        entity = (result.get('asset') or result.get('category') or
                  result.get('transaction') or result.get('depreciation_category') or
                  result.get('user') or {})
        entity_name = entity.get('name') or entity.get('username') or ''
        suffix = f' — {entity_name}' if entity_name else ''
        if status == 'created':
            qty = result.get('quantity')
            if qty and qty > 1:
                return f'{qty}× erstellt ✓{suffix}'
            return f'Erstellt ✓{suffix}'
        if status == 'updated':
            count = result.get('count')
            if count and count > 1:
                return f'{count}× aktualisiert ✓{suffix}'
            return f'Aktualisiert ✓{suffix}'
        if status == 'deleted':
            count = result.get('count')
            if count and count > 1:
                return f'{count}× gelöscht ✓'
            return f'Gelöscht ✓{suffix}'
        if status == 'disposed':
            count = result.get('count')
            if count and count > 1:
                return f'{count}× Abgang erfasst ✓'
            return f'Abgang erfasst ✓{suffix}'
        return 'OK'
    return str(result)[:80]


SYSTEM_PROMPT_TEMPLATE = """\
You are a helpful accounting assistant for a German small-business bookkeeping application (EÜR – Einnahmenüberschussrechnung).
You can query and manipulate ALL data: categories (Buchungskategorien), transactions (Buchungen), accounts (Konten),
assets (Anlagegüter), depreciation categories (AfA-Kategorien), business settings, and users.

Accounts (Konten): Every transaction must be assigned to an account. Transfers between accounts use the create_transfer tool.

IMPORTANT – Asset purchases (Anlagekäufe):
When creating an asset, you MUST always pass an account_id to book the linked cash outflow transaction (Abgangsbuchung). This ensures the payment is correctly recorded in the account balance. The outflow transaction is NOT counted in the EÜR (it is linked to the asset instead).
- If the user specifies which account (e.g. "vom Geschäftskonto", "bar bezahlt"), use that account.
- If only one account exists, use that one automatically.
- If multiple accounts exist and the user did NOT specify which one, ask them first OR look up the available accounts with list_accounts and pick the most logical one (e.g. the main business account).
- NEVER skip the account_id unless the user explicitly says the payment should not be booked.

Current business settings:
- Firmenname: {business_name}
- Besteuerungsart: {tax_mode_label}
- Regelsteuersatz: {tax_rate}%
- Ermäßigter Steuersatz: {tax_rate_reduced}%
{extra_tax_info}

Use the provided tools to read and modify data.
IMPORTANT: Write operations (create, edit, delete, dispose, update) will be shown to the user for approval before execution. Do NOT ask the user for confirmation yourself – the system handles that automatically. Just call the tool when you think it's the right action.

CRITICAL – Always ask clarifying questions BEFORE calling any tool if:
- The user's request is vague or ambiguous (e.g. "lösch die Buchung" – which one?)
- Required information is missing (e.g. amount, date, category, type)
- There are multiple possible interpretations of the request
- You are unsure about the tax treatment, category assignment, or any other detail
- The user refers to something by name but multiple matches could exist (ask which one, or look up the data first)
- A date, amount, or other value was not explicitly stated
Do NOT guess or assume values the user hasn't provided. When in doubt, ask. It's always better to ask one extra question than to create incorrect data.
If a request involves multiple steps, confirm the overall plan with the user before starting.

When listing data, format it nicely for the user (use formatted tables, bullets, etc.).
Respond in the same language the user writes in (German or English).
Monetary values are in EUR. Dates are YYYY-MM-DD.
The current date is {today}.

You have web access via `web_search` (DuckDuckGo) and `fetch_url` tools. Use them to look up current information when needed (e.g. tax rates, exchange rates, legal info, AfA tables).

You have a `python_eval` tool that executes Python code in a sandbox. Use it for ANY calculations:
- Arithmetic (addition, subtraction, multiplication, division)
- Percentages, tax calculations, profit margins
- Date calculations (days between dates, deadlines)
- Statistical analysis (averages, sums, min/max over data)
- Currency conversions, compound interest, depreciation schedules
- Any complex math the user asks about
Always use `python_eval` instead of doing mental math – it's more reliable. Available modules: math, decimal, Decimal, statistics, itertools, date, datetime, timedelta.
"""


def _build_system_prompt():
    """Build system prompt with current settings context."""
    settings = SiteSettings.get_settings()
    tax_mode_label = 'Regelbesteuerung' if settings.tax_mode == 'regular' else 'Kleinunternehmer (§ 19 UStG)'

    extra = ''
    if settings.tax_mode == 'kleinunternehmer':
        extra = '- Hinweis: Als Kleinunternehmer wird KEINE USt ausgewiesen. Alle Beträge sind brutto = netto.'
    else:
        extra = '- Hinweis: Regelbesteuert – bei Buchungen immer die korrekte Steuerbehandlung (tax_treatment) setzen.'

    if settings.tax_number:
        extra += f'\n- Steuernummer: {settings.tax_number}'
    if settings.vat_id:
        extra += f'\n- USt-IdNr.: {settings.vat_id}'

    return SYSTEM_PROMPT_TEMPLATE.format(
        business_name=settings.business_name or 'Nicht konfiguriert',
        tax_mode_label=tax_mode_label,
        tax_rate=settings.tax_rate,
        tax_rate_reduced=settings.tax_rate_reduced,
        extra_tax_info=extra,
        today=date.today().isoformat(),
    )


def _build_openai_tools():
    """Return tool definitions in OpenAI format."""
    return TOOL_DEFINITIONS


def _build_anthropic_tools():
    """Convert OpenAI-format tool defs to Anthropic format."""
    tools = []
    for td in TOOL_DEFINITIONS:
        f = td['function']
        tools.append({
            'name': f['name'],
            'description': f['description'],
            'input_schema': f['parameters'],
        })
    return tools


def _call_openai(messages, api_key, model, base_url=None, tool_calls_log=None):
    """
    Call OpenAI-compatible API with tool use loop.
    Returns (reply_or_None, messages, tool_calls_log, pending_actions).
    If pending_actions is non-empty, the loop paused for user confirmation.
    """
    import httpx

    if tool_calls_log is None:
        tool_calls_log = []

    url = (base_url.rstrip('/') if base_url else 'https://api.openai.com/v1') + '/chat/completions'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    tools = _build_openai_tools()

    for _ in range(20):  # safety limit
        payload = {
            'model': model,
            'messages': messages,
            'tools': tools,
            'tool_choice': 'auto',
        }
        resp = httpx.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        choice = data['choices'][0]
        msg = choice['message']
        messages.append(msg)

        if not msg.get('tool_calls'):
            return msg.get('content', ''), messages, tool_calls_log, []

        # Separate read vs write tool calls
        pending_writes = []
        for tc in msg['tool_calls']:
            fn_name = tc['function']['name']
            try:
                fn_args = json.loads(tc['function']['arguments'])
            except json.JSONDecodeError:
                fn_args = {}

            if fn_name in READ_ONLY_TOOLS:
                result = execute_tool(fn_name, fn_args)
                tool_calls_log.append({
                    'name': fn_name,
                    'label': TOOL_LABELS.get(fn_name, fn_name),
                    'args': _enrich_args_for_display(fn_name, fn_args),
                    'is_write': False,
                    'result_summary': _summarize_result(fn_name, result),
                })
                messages.append({
                    'role': 'tool',
                    'tool_call_id': tc['id'],
                    'content': json.dumps(result, ensure_ascii=False, default=str),
                })
            else:
                display_args = _enrich_args_for_display(fn_name, fn_args)
                tool_calls_log.append({
                    'name': fn_name,
                    'label': TOOL_LABELS.get(fn_name, fn_name),
                    'args': display_args,
                    'is_write': True,
                    'pending': True,
                })
                pending_writes.append({
                    'tool_call_id': tc['id'],
                    'name': fn_name,
                    'label': TOOL_LABELS.get(fn_name, fn_name),
                    'args': display_args,
                    'raw_args': fn_args,
                })

        if pending_writes:
            return None, messages, tool_calls_log, pending_writes

    return 'Zu viele Iterationen. Bitte stellen Sie eine einfachere Anfrage.', messages, tool_calls_log, []


def _call_anthropic(messages, api_key, model, tool_calls_log=None):
    """
    Call Anthropic API with tool use loop.
    Returns (reply_or_None, messages, tool_calls_log, pending_actions, anthropic_messages).
    """
    import httpx

    if tool_calls_log is None:
        tool_calls_log = []

    url = 'https://api.anthropic.com/v1/messages'
    headers = {
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01',
        'Content-Type': 'application/json',
    }
    tools = _build_anthropic_tools()

    # Convert messages from OpenAI format to Anthropic format
    anthropic_messages = []
    for m in messages:
        if m['role'] == 'system':
            continue
        anthropic_messages.append({'role': m['role'], 'content': m['content']})

    system_text = _build_system_prompt()

    for _ in range(20):
        payload = {
            'model': model,
            'max_tokens': 4096,
            'system': system_text,
            'messages': anthropic_messages,
            'tools': tools,
        }
        resp = httpx.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        has_tool_use = any(block['type'] == 'tool_use' for block in data.get('content', []))

        if not has_tool_use:
            text_parts = [b['text'] for b in data.get('content', []) if b['type'] == 'text']
            final_text = '\n'.join(text_parts)
            messages.append({'role': 'assistant', 'content': final_text})
            return final_text, messages, tool_calls_log, [], anthropic_messages

        anthropic_messages.append({'role': 'assistant', 'content': data['content']})

        # Separate read vs write tool uses
        pending_writes = []
        read_results = []  # store for later
        all_are_reads = True

        for block in data['content']:
            if block['type'] != 'tool_use':
                continue
            fn_name = block['name']
            fn_args = block['input']

            if fn_name in READ_ONLY_TOOLS:
                result = execute_tool(fn_name, fn_args)
                tool_calls_log.append({
                    'name': fn_name,
                    'label': TOOL_LABELS.get(fn_name, fn_name),
                    'args': _enrich_args_for_display(fn_name, fn_args),
                    'is_write': False,
                    'result_summary': _summarize_result(fn_name, result),
                })
                read_results.append({
                    'type': 'tool_result',
                    'tool_use_id': block['id'],
                    'content': json.dumps(result, ensure_ascii=False, default=str),
                })
            else:
                all_are_reads = False
                display_args = _enrich_args_for_display(fn_name, fn_args)
                tool_calls_log.append({
                    'name': fn_name,
                    'label': TOOL_LABELS.get(fn_name, fn_name),
                    'args': display_args,
                    'is_write': True,
                    'pending': True,
                })
                pending_writes.append({
                    'tool_use_id': block['id'],
                    'name': fn_name,
                    'label': TOOL_LABELS.get(fn_name, fn_name),
                    'args': display_args,
                    'raw_args': fn_args,
                })

        if all_are_reads:
            # All reads – add results and continue loop
            anthropic_messages.append({'role': 'user', 'content': read_results})
        else:
            # Has writes – pause for confirmation, store read results in state
            return None, messages, tool_calls_log, pending_writes, anthropic_messages

    messages.append({'role': 'assistant', 'content': 'Zu viele Iterationen.'})
    return 'Zu viele Iterationen.', messages, tool_calls_log, [], anthropic_messages


def _resume_openai(messages, pending, approved, correction, api_key, model, base_url, tool_calls_log):
    """Resume an OpenAI loop after confirmation/rejection."""
    if approved:
        for p in pending:
            exec_args = p.get('raw_args', p['args'])
            result = execute_tool(p['name'], exec_args)
            # Update log entry
            for entry in tool_calls_log:
                if entry.get('pending') and entry['name'] == p['name'] and entry['args'] == p['args']:
                    entry['pending'] = False
                    entry['result_summary'] = _summarize_result(p['name'], result)
                    break
            messages.append({
                'role': 'tool',
                'tool_call_id': p['tool_call_id'],
                'content': json.dumps(result, ensure_ascii=False, default=str),
            })
    else:
        rejection = 'Der Benutzer hat diese Aktion abgelehnt.'
        if correction:
            rejection += f' Feedback: {correction}'
        for p in pending:
            for entry in tool_calls_log:
                if entry.get('pending') and entry['name'] == p['name'] and entry['args'] == p['args']:
                    entry['pending'] = False
                    entry['result_summary'] = 'Abgelehnt ✗'
                    break
            messages.append({
                'role': 'tool',
                'tool_call_id': p['tool_call_id'],
                'content': json.dumps({'rejected': True, 'message': rejection}, ensure_ascii=False),
            })

    # Continue the loop
    return _call_openai(messages, api_key, model, base_url, tool_calls_log)


def _resume_anthropic(messages, anthropic_messages, pending, approved, correction, api_key, model, tool_calls_log):
    """Resume an Anthropic loop after confirmation/rejection."""
    # Build the tool_results user message (reads were already computed, need to re-fetch or use stored)
    # For simplicity, we need to produce results for ALL tool_use blocks in the last assistant message
    last_assistant = anthropic_messages[-1]  # the assistant message with tool_use blocks
    tool_results = []

    for block in last_assistant['content']:
        if block['type'] != 'tool_use':
            continue
        fn_name = block['name']
        # Check if this was a pending write
        matching_pending = [p for p in pending if p['tool_use_id'] == block['id']]
        if matching_pending:
            p = matching_pending[0]
            if approved:
                exec_args = p.get('raw_args', p['args'])
                result = execute_tool(p['name'], exec_args)
                for entry in tool_calls_log:
                    if entry.get('pending') and entry['name'] == p['name'] and entry['args'] == p['args']:
                        entry['pending'] = False
                        entry['result_summary'] = _summarize_result(p['name'], result)
                        break
                tool_results.append({
                    'type': 'tool_result',
                    'tool_use_id': block['id'],
                    'content': json.dumps(result, ensure_ascii=False, default=str),
                })
            else:
                rejection = 'Der Benutzer hat diese Aktion abgelehnt.'
                if correction:
                    rejection += f' Feedback: {correction}'
                for entry in tool_calls_log:
                    if entry.get('pending') and entry['name'] == p['name'] and entry['args'] == p['args']:
                        entry['pending'] = False
                        entry['result_summary'] = 'Abgelehnt ✗'
                        break
                tool_results.append({
                    'type': 'tool_result',
                    'tool_use_id': block['id'],
                    'content': json.dumps({'rejected': True, 'message': rejection}, ensure_ascii=False),
                })
        else:
            # Read tool – re-execute (cheap)
            result = execute_tool(fn_name, block['input'])
            tool_results.append({
                'type': 'tool_result',
                'tool_use_id': block['id'],
                'content': json.dumps(result, ensure_ascii=False, default=str),
            })

    anthropic_messages.append({'role': 'user', 'content': tool_results})

    # Continue – rebuild the Anthropic caller state and keep looping
    # We pass anthropic_messages as-is since we're resuming
    import httpx
    url = 'https://api.anthropic.com/v1/messages'
    headers = {
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01',
        'Content-Type': 'application/json',
    }
    tools = _build_anthropic_tools()

    for _ in range(20):
        payload = {
            'model': model,
            'max_tokens': 4096,
            'system': _build_system_prompt(),
            'messages': anthropic_messages,
            'tools': tools,
        }
        resp = httpx.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        has_tool_use = any(block['type'] == 'tool_use' for block in data.get('content', []))

        if not has_tool_use:
            text_parts = [b['text'] for b in data.get('content', []) if b['type'] == 'text']
            final_text = '\n'.join(text_parts)
            messages.append({'role': 'assistant', 'content': final_text})
            return final_text, messages, tool_calls_log, [], anthropic_messages

        anthropic_messages.append({'role': 'assistant', 'content': data['content']})

        pending_writes = []
        read_results = []
        all_reads = True

        for block in data['content']:
            if block['type'] != 'tool_use':
                continue
            fn_name = block['name']
            fn_args = block['input']

            if fn_name in READ_ONLY_TOOLS:
                result = execute_tool(fn_name, fn_args)
                tool_calls_log.append({
                    'name': fn_name,
                    'label': TOOL_LABELS.get(fn_name, fn_name),
                    'args': _enrich_args_for_display(fn_name, fn_args),
                    'is_write': False,
                    'result_summary': _summarize_result(fn_name, result),
                })
                read_results.append({
                    'type': 'tool_result',
                    'tool_use_id': block['id'],
                    'content': json.dumps(result, ensure_ascii=False, default=str),
                })
            else:
                all_reads = False
                display_args = _enrich_args_for_display(fn_name, fn_args)
                tool_calls_log.append({
                    'name': fn_name,
                    'label': TOOL_LABELS.get(fn_name, fn_name),
                    'args': display_args,
                    'is_write': True,
                    'pending': True,
                })
                pending_writes.append({
                    'tool_use_id': block['id'],
                    'name': fn_name,
                    'label': TOOL_LABELS.get(fn_name, fn_name),
                    'args': display_args,
                    'raw_args': fn_args,
                })

        if all_reads:
            anthropic_messages.append({'role': 'user', 'content': read_results})
        else:
            return None, messages, tool_calls_log, pending_writes, anthropic_messages

    messages.append({'role': 'assistant', 'content': 'Zu viele Iterationen.'})
    return 'Zu viele Iterationen.', messages, tool_calls_log, [], anthropic_messages


def _clean_history(messages):
    """Extract user-visible history from messages."""
    clean = []
    for m in messages:
        if isinstance(m, dict) and m.get('role') in ('user', 'assistant') and isinstance(m.get('content'), str):
            clean.append({'role': m['role'], 'content': m['content']})
    return clean


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@ai_bp.route('/ai-chat')
@login_required
def chat_page():
    provider, api_key, model, _ = _get_ai_config()
    configured = bool(api_key)
    return render_template('ai_chat.html',
                           ai_configured=configured,
                           ai_provider=provider,
                           ai_model=model)


@ai_bp.route('/ai-chat/send', methods=['POST'])
@login_required
def chat_send():
    """
    Receive the conversation, call the AI.
    Returns reply + tool_calls_log.
    If write tools are needed, pauses and returns pending_actions + conversation_state.
    """
    provider, api_key, model, base_url = _get_ai_config()

    if not api_key:
        return jsonify({'error': 'AI not configured. Set AI_API_KEY in your environment.'}), 400

    data = request.get_json(force=True)
    user_message = data.get('message', '').strip()
    history = data.get('history', [])

    if not user_message:
        return jsonify({'error': 'Empty message'}), 400

    messages = [{'role': 'system', 'content': _build_system_prompt()}]
    for h in history:
        messages.append({'role': h['role'], 'content': h['content']})
    messages.append({'role': 'user', 'content': user_message})

    try:
        if provider == 'anthropic':
            reply, messages, log, pending, anth_msgs = _call_anthropic(messages, api_key, model)
        else:
            reply, messages, log, pending = _call_openai(
                messages, api_key, model, base_url if provider == 'custom' else None
            )
            anth_msgs = None
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'AI API Fehler: {str(e)}'}), 502

    result = {
        'tool_calls_log': log,
        'history': _clean_history(messages),
    }

    if pending:
        # Paused for confirmation
        result['reply'] = None
        result['pending_actions'] = pending
        state = {'provider': provider, 'messages': messages, 'pending': pending}
        if anth_msgs is not None:
            state['anthropic_messages'] = anth_msgs
        result['conversation_state'] = state
    else:
        result['reply'] = reply

    return jsonify(result)


@ai_bp.route('/ai-chat/confirm', methods=['POST'])
@login_required
def chat_confirm():
    """
    Handle user confirmation or rejection of pending write actions.
    Executes or rejects the tools, then continues the AI loop.
    """
    provider_cfg, api_key, model, base_url = _get_ai_config()

    if not api_key:
        return jsonify({'error': 'AI not configured.'}), 400

    data = request.get_json(force=True)
    approved = data.get('approved', False)
    correction = data.get('correction', '').strip()
    conv_state = data.get('conversation_state', {})
    prev_log = data.get('tool_calls_log', [])

    provider = conv_state.get('provider', provider_cfg)
    messages = conv_state.get('messages', [])
    pending = conv_state.get('pending', [])

    try:
        if provider == 'anthropic':
            anth_msgs = conv_state.get('anthropic_messages', [])
            reply, messages, log, new_pending, anth_msgs = _resume_anthropic(
                messages, anth_msgs, pending, approved, correction, api_key, model, prev_log
            )
        else:
            reply, messages, log, new_pending = _resume_openai(
                messages, pending, approved, correction, api_key, model,
                base_url if provider == 'custom' else None, prev_log
            )
            anth_msgs = None
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'AI API Fehler: {str(e)}'}), 502

    result = {
        'tool_calls_log': log,
        'history': _clean_history(messages),
    }

    if new_pending:
        result['reply'] = None
        result['pending_actions'] = new_pending
        state = {'provider': provider, 'messages': messages, 'pending': new_pending}
        if anth_msgs is not None:
            state['anthropic_messages'] = anth_msgs
        result['conversation_state'] = state
    else:
        result['reply'] = reply

    return jsonify(result)


# ---------------------------------------------------------------------------
# Chat persistence (per user, single current chat)
# ---------------------------------------------------------------------------

@ai_bp.route('/ai-chat/save', methods=['POST'])
@login_required
def chat_save():
    """Save current chat state for the logged-in user."""
    data = request.get_json(force=True)
    history_json = json.dumps(data.get('history', []), ensure_ascii=False)
    html_content = data.get('html', '')

    rec = ChatHistory.query.filter_by(user_id=current_user.id).first()
    if rec:
        rec.history_json = history_json
        rec.html_content = html_content
    else:
        rec = ChatHistory(user_id=current_user.id,
                          history_json=history_json,
                          html_content=html_content)
        db.session.add(rec)
    db.session.commit()
    return jsonify({'ok': True})


@ai_bp.route('/ai-chat/load')
@login_required
def chat_load():
    """Load persisted chat for the logged-in user."""
    rec = ChatHistory.query.filter_by(user_id=current_user.id).first()
    if not rec or rec.history_json == '[]':
        return jsonify({'history': [], 'html': ''})
    return jsonify({
        'history': json.loads(rec.history_json),
        'html': rec.html_content,
    })


@ai_bp.route('/ai-chat/clear', methods=['POST'])
@login_required
def chat_clear():
    """Delete persisted chat for the logged-in user."""
    ChatHistory.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({'ok': True})
