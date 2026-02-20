"""
REST API blueprint for programmatic access to accounting data.

Authentication: API key via header  Authorization: Bearer <API_KEY>
The API key is set via the environment variable API_KEY.

All responses are JSON. Monetary values are in EUR (float).
Dates are ISO 8601 (YYYY-MM-DD).
"""

import os
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from helpers import (
    calculate_tax, get_tax_rate_for_treatment, parse_date,
    TAX_TREATMENT_LABELS,
)
from models import (
    Account, Category, Customer, Document, SiteSettings, Transaction,
    Quote, QuoteItem, Invoice, InvoiceItem, Asset, db,
)
from audit import archive_file

api_bp = Blueprint('api', __name__)

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp'}


def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _get_api_key():
    return os.environ.get('API_KEY', '')


def require_api_key(f):
    """Decorator: require a valid API key in the Authorization header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = _get_api_key()
        if not api_key:
            return jsonify({'error': 'API not configured. Set API_KEY environment variable.'}), 503

        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or auth[7:] != api_key:
            return jsonify({'error': 'Unauthorized. Provide header: Authorization: Bearer <API_KEY>'}), 401

        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Helper: tax calculation
# ---------------------------------------------------------------------------

def _apply_tax(amount_gross, tax_treatment, settings, custom_rate=None):
    """Compute net_amount, tax_amount, effective_rate from gross."""
    effective_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)
    if effective_rate > 0:
        net, tax = calculate_tax(amount_gross, effective_rate)
    else:
        net, tax = amount_gross, 0.0
    return net, tax, effective_rate


def _tx_to_dict(t):
    """Serialize a Transaction to a dict."""
    docs = Document.query.filter_by(entity_type='transaction', entity_id=t.id).all()
    d = {
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
        'notes': t.notes,
        'documents': [{'id': doc.id, 'filename': doc.filename, 'original_filename': doc.original_filename} for doc in docs],
        'document_filename': t.document_filename,  # legacy field
        'created_at': t.created_at.isoformat() if t.created_at else None,
        'updated_at': t.updated_at.isoformat() if t.updated_at else None,
    }
    if t.type == 'transfer':
        d['transfer_to_account_id'] = t.transfer_to_account_id
        d['transfer_to_account_name'] = t.transfer_to_account.name if t.transfer_to_account else None
    if t.linked_asset_id:
        d['linked_asset_id'] = t.linked_asset_id
    return d


def _cat_to_dict(c):
    """Serialize a Category to a dict."""
    return {
        'id': c.id,
        'name': c.name,
        'type': c.type,
        'description': c.description,
        'sort_order': c.sort_order,
    }


def _account_to_dict(a):
    """Serialize an Account to a dict."""
    return {
        'id': a.id,
        'name': a.name,
        'description': a.description,
        'initial_balance': a.initial_balance,
        'current_balance': round(a.get_balance(), 2),
        'sort_order': a.sort_order,
    }


def _customer_to_dict(c):
    """Serialize a Customer to a dict."""
    return {
        'id': c.id,
        'name': c.name,
        'company': c.company,
        'address': c.address,
        'email': c.email,
        'phone': c.phone,
        'notes': c.notes,
        'display_name': c.display_name,
        'created_at': c.created_at.isoformat() if c.created_at else None,
        'updated_at': c.updated_at.isoformat() if c.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Settings (read-only)
# ---------------------------------------------------------------------------

@api_bp.route('/settings', methods=['GET'])
@require_api_key
def get_settings():
    """Return current site/business settings (tax mode, rates, etc.)."""
    s = SiteSettings.get_settings()
    return jsonify({
        'business_name': s.business_name,
        'tax_mode': s.tax_mode,
        'tax_rate': s.tax_rate,
        'tax_rate_reduced': s.tax_rate_reduced,
        'tax_number': s.tax_number,
        'vat_id': s.vat_id,
    })


# ---------------------------------------------------------------------------
# Tax treatments (read-only)
# ---------------------------------------------------------------------------

@api_bp.route('/tax-treatments', methods=['GET'])
@require_api_key
def list_tax_treatments():
    """Return all valid tax_treatment values with labels."""
    return jsonify({
        'tax_treatments': [
            {'value': k, 'label': v}
            for k, v in TAX_TREATMENT_LABELS.items()
        ]
    })


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@api_bp.route('/customers', methods=['GET'])
@require_api_key
def list_customers():
    """
    List customers.   Optional query param: q (search name/company/email).
    """
    q = request.args.get('q', '').strip()
    query = Customer.query
    if q:
        pattern = f'%{q}%'
        query = query.filter(
            db.or_(
                Customer.name.ilike(pattern),
                Customer.company.ilike(pattern),
                Customer.email.ilike(pattern),
            )
        )
    customers = query.order_by(Customer.name).all()
    return jsonify({'customers': [_customer_to_dict(c) for c in customers]})


@api_bp.route('/customers', methods=['POST'])
@require_api_key
def create_customer():
    """
    Create a new customer.
    Body: { name (required), company?, address?, email?, phone?, notes? }
    """
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400

    c = Customer(
        name=name,
        company=data.get('company', '').strip() or None,
        address=data.get('address', '').strip() or None,
        email=data.get('email', '').strip() or None,
        phone=data.get('phone', '').strip() or None,
        notes=data.get('notes', '').strip() or None,
    )
    db.session.add(c)
    db.session.commit()
    return jsonify({'customer': _customer_to_dict(c)}), 201


@api_bp.route('/customers/<int:customer_id>', methods=['GET'])
@require_api_key
def get_customer(customer_id):
    """Get a single customer by ID."""
    c = Customer.query.get(customer_id)
    if not c:
        return jsonify({'error': f'Customer {customer_id} not found'}), 404
    return jsonify({'customer': _customer_to_dict(c)})


@api_bp.route('/customers/<int:customer_id>', methods=['PUT', 'PATCH'])
@require_api_key
def update_customer(customer_id):
    """
    Update a customer.
    Body: { name?, company?, address?, email?, phone?, notes? }
    """
    c = Customer.query.get(customer_id)
    if not c:
        return jsonify({'error': f'Customer {customer_id} not found'}), 404

    data = request.get_json(force=True)
    if 'name' in data:
        name = data['name'].strip()
        if not name:
            return jsonify({'error': 'name cannot be empty'}), 400
        c.name = name
    if 'company' in data:
        c.company = data['company'].strip() or None
    if 'address' in data:
        c.address = data['address'].strip() or None
    if 'email' in data:
        c.email = data['email'].strip() or None
    if 'phone' in data:
        c.phone = data['phone'].strip() or None
    if 'notes' in data:
        c.notes = data['notes'].strip() or None

    db.session.commit()
    return jsonify({'customer': _customer_to_dict(c)})


@api_bp.route('/customers/<int:customer_id>', methods=['DELETE'])
@require_api_key
def delete_customer(customer_id):
    """Delete a customer. Fails if quotes or invoices reference it."""
    c = Customer.query.get(customer_id)
    if not c:
        return jsonify({'error': f'Customer {customer_id} not found'}), 404

    quote_count = c.quotes.count()
    invoice_count = c.invoices.count()
    if quote_count > 0 or invoice_count > 0:
        return jsonify({
            'error': f'Cannot delete customer with {quote_count} quote(s) and {invoice_count} invoice(s). '
                     'Delete linked documents first.'
        }), 409

    db.session.delete(c)
    db.session.commit()
    return jsonify({'deleted': True, 'id': customer_id})


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@api_bp.route('/accounts', methods=['GET'])
@require_api_key
def list_accounts():
    """List all accounts with current balances."""
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()
    return jsonify({'accounts': [_account_to_dict(a) for a in accounts]})


@api_bp.route('/accounts', methods=['POST'])
@require_api_key
def create_account():
    """
    Create a new account.
    Body: { name, description?, initial_balance?, sort_order? }
    """
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400

    a = Account(
        name=name,
        description=data.get('description'),
        initial_balance=float(data.get('initial_balance', 0)),
        sort_order=data.get('sort_order', 0),
    )
    db.session.add(a)
    db.session.commit()
    return jsonify({'account': _account_to_dict(a)}), 201


@api_bp.route('/accounts/<int:account_id>', methods=['GET'])
@require_api_key
def get_account(account_id):
    """Get a single account by ID with current balance."""
    a = Account.query.get(account_id)
    if not a:
        return jsonify({'error': f'Account {account_id} not found'}), 404
    return jsonify({'account': _account_to_dict(a)})


@api_bp.route('/accounts/<int:account_id>', methods=['PUT', 'PATCH'])
@require_api_key
def update_account(account_id):
    """
    Update an account.
    Body: { name?, description?, initial_balance?, sort_order? }
    """
    a = Account.query.get(account_id)
    if not a:
        return jsonify({'error': f'Account {account_id} not found'}), 404

    data = request.get_json(force=True)
    if 'name' in data:
        name = data['name'].strip()
        if not name:
            return jsonify({'error': 'name cannot be empty'}), 400
        a.name = name
    if 'description' in data:
        a.description = data['description']
    if 'initial_balance' in data:
        a.initial_balance = float(data['initial_balance'])
    if 'sort_order' in data:
        a.sort_order = data['sort_order']

    db.session.commit()
    return jsonify({'account': _account_to_dict(a)})


@api_bp.route('/accounts/<int:account_id>', methods=['DELETE'])
@require_api_key
def delete_account(account_id):
    """Delete an account. Fails if transactions reference it."""
    a = Account.query.get(account_id)
    if not a:
        return jsonify({'error': f'Account {account_id} not found'}), 404

    tx_count = Transaction.query.filter(
        db.or_(Transaction.account_id == a.id, Transaction.transfer_to_account_id == a.id)
    ).count()
    if tx_count > 0:
        return jsonify({'error': f'Cannot delete account with {tx_count} linked transaction(s). Move or delete them first.'}), 409

    db.session.delete(a)
    db.session.commit()
    return jsonify({'deleted': True, 'id': account_id})


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

@api_bp.route('/transfers', methods=['POST'])
@require_api_key
def create_transfer():
    """
    Create a transfer between two accounts.
    Body: {
        date:            "YYYY-MM-DD"  (required)
        amount:          number         (required, > 0)
        from_account_id: int            (required)
        to_account_id:   int            (required)
        description:     string         (optional)
        notes:           string         (optional)
    }
    Transfers are not counted in EÜR.
    """
    data = request.get_json(force=True)

    errors = []
    if not data.get('date'):
        errors.append('date is required (YYYY-MM-DD)')
    if data.get('amount') is None:
        errors.append('amount is required')
    if not data.get('from_account_id'):
        errors.append('from_account_id is required')
    if not data.get('to_account_id'):
        errors.append('to_account_id is required')
    if errors:
        return jsonify({'errors': errors}), 400

    try:
        tx_date = parse_date(data['date'])
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    amount = float(data['amount'])
    if amount <= 0:
        return jsonify({'error': 'amount must be positive'}), 400

    from_id = int(data['from_account_id'])
    to_id = int(data['to_account_id'])
    if from_id == to_id:
        return jsonify({'error': 'from_account_id and to_account_id must be different'}), 400

    from_acc = Account.query.get(from_id)
    if not from_acc:
        return jsonify({'error': f'Account {from_id} not found'}), 404
    to_acc = Account.query.get(to_id)
    if not to_acc:
        return jsonify({'error': f'Account {to_id} not found'}), 404

    desc = data.get('description', '').strip() or f'Umbuchung {from_acc.name} → {to_acc.name}'

    t = Transaction(
        date=tx_date,
        type='transfer',
        description=desc,
        amount=amount,
        net_amount=amount,
        tax_amount=0,
        tax_treatment='none',
        tax_rate=0,
        account_id=from_id,
        transfer_to_account_id=to_id,
        notes=data.get('notes'),
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({'transaction': _tx_to_dict(t)}), 201


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

@api_bp.route('/categories', methods=['GET'])
@require_api_key
def list_categories():
    """
    List all categories.
    Query params: type (income|expense) to filter.
    """
    q = Category.query.order_by(Category.sort_order, Category.name)
    type_filter = request.args.get('type')
    if type_filter in ('income', 'expense'):
        q = q.filter_by(type=type_filter)
    return jsonify({'categories': [_cat_to_dict(c) for c in q.all()]})


@api_bp.route('/categories', methods=['POST'])
@require_api_key
def create_category():
    """
    Create a new category.
    Body: { name, type (income|expense), description?, sort_order? }
    """
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    cat_type = data.get('type', '').strip()

    if not name:
        return jsonify({'error': 'name is required'}), 400
    if cat_type not in ('income', 'expense'):
        return jsonify({'error': "type must be 'income' or 'expense'"}), 400

    c = Category(
        name=name,
        type=cat_type,
        description=data.get('description'),
        sort_order=data.get('sort_order', 0),
    )
    db.session.add(c)
    db.session.commit()
    return jsonify({'category': _cat_to_dict(c)}), 201


@api_bp.route('/categories/<int:category_id>', methods=['GET'])
@require_api_key
def get_category(category_id):
    """Get a single category by ID."""
    c = Category.query.get(category_id)
    if not c:
        return jsonify({'error': f'Category {category_id} not found'}), 404
    return jsonify({'category': _cat_to_dict(c)})


@api_bp.route('/categories/<int:category_id>', methods=['PUT', 'PATCH'])
@require_api_key
def update_category(category_id):
    """
    Update a category.
    Body: { name?, type?, description?, sort_order? }
    """
    c = Category.query.get(category_id)
    if not c:
        return jsonify({'error': f'Category {category_id} not found'}), 404

    data = request.get_json(force=True)
    if 'name' in data:
        c.name = data['name'].strip()
    if 'type' in data:
        if data['type'] not in ('income', 'expense'):
            return jsonify({'error': "type must be 'income' or 'expense'"}), 400
        c.type = data['type']
    if 'description' in data:
        c.description = data['description']
    if 'sort_order' in data:
        c.sort_order = data['sort_order']

    db.session.commit()
    return jsonify({'category': _cat_to_dict(c)})


@api_bp.route('/categories/<int:category_id>', methods=['DELETE'])
@require_api_key
def delete_category(category_id):
    """Delete a category. Transactions will be unlinked (category_id set to null)."""
    c = Category.query.get(category_id)
    if not c:
        return jsonify({'error': f'Category {category_id} not found'}), 404

    Transaction.query.filter_by(category_id=c.id).update({'category_id': None})
    db.session.delete(c)
    db.session.commit()
    return jsonify({'deleted': True, 'id': category_id})


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@api_bp.route('/transactions', methods=['GET'])
@require_api_key
def list_transactions():
    """
    List transactions with optional filters.
    Query params:
      year       – filter by year (int)
      month      – filter by month 1-12 (int)
      type       – income | expense
      category_id – filter by category
      search     – text search in description/notes
      limit      – max results (default 100)
      offset     – pagination offset (default 0)
      sort       – date_asc | date_desc (default date_desc)
    """
    q = Transaction.query

    year = request.args.get('year', type=int)
    if year:
        q = q.filter(db.extract('year', Transaction.date) == year)

    month = request.args.get('month', type=int)
    if month:
        q = q.filter(db.extract('month', Transaction.date) == month)

    type_filter = request.args.get('type')
    if type_filter in ('income', 'expense', 'transfer'):
        q = q.filter(Transaction.type == type_filter)

    cat_id = request.args.get('category_id', type=int)
    if cat_id:
        q = q.filter(Transaction.category_id == cat_id)

    acc_id = request.args.get('account_id', type=int)
    if acc_id:
        q = q.filter(
            db.or_(Transaction.account_id == acc_id, Transaction.transfer_to_account_id == acc_id)
        )

    search = request.args.get('search', '').strip()
    if search:
        like = f'%{search}%'
        q = q.filter(
            db.or_(
                Transaction.description.ilike(like),
                Transaction.notes.ilike(like),
            )
        )

    # Sorting
    sort = request.args.get('sort', 'date_desc')
    if sort == 'date_asc':
        q = q.order_by(Transaction.date.asc(), Transaction.id.asc())
    else:
        q = q.order_by(Transaction.date.desc(), Transaction.id.desc())

    # Count before pagination
    total = q.count()

    # Pagination
    limit = min(request.args.get('limit', 100, type=int), 1000)
    offset = request.args.get('offset', 0, type=int)
    txs = q.limit(limit).offset(offset).all()

    return jsonify({
        'transactions': [_tx_to_dict(t) for t in txs],
        'total': total,
        'limit': limit,
        'offset': offset,
    })


@api_bp.route('/transactions', methods=['POST'])
@require_api_key
def create_transaction():
    """
    Create a new transaction.
    Body: {
        date:           "YYYY-MM-DD"          (required)
        type:           "income" | "expense"   (required)
        description:    string                 (required)
        amount:         number                 (required, gross/brutto in EUR)
        account_id:     int                    (required)
        category_id:    int                    (optional)
        tax_treatment:  string                 (optional, default "none")
        custom_tax_rate: number                (optional, only if tax_treatment="custom")
        notes:          string                 (optional)
    }
    Note: For transfers between accounts, use POST /transfers instead.
    """
    data = request.get_json(force=True)
    settings = SiteSettings.get_settings()

    # Validate required fields
    errors = []
    if not data.get('date'):
        errors.append('date is required (YYYY-MM-DD)')
    if data.get('type') not in ('income', 'expense'):
        errors.append("type must be 'income' or 'expense'")
    if not data.get('description', '').strip():
        errors.append('description is required')
    if data.get('amount') is None:
        errors.append('amount is required (gross/brutto)')
    if not data.get('account_id'):
        errors.append('account_id is required')
    if errors:
        return jsonify({'errors': errors}), 400

    try:
        tx_date = parse_date(data['date'])
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    amount = float(data['amount'])
    if amount <= 0:
        return jsonify({'error': 'amount must be positive'}), 400

    tax_treatment = data.get('tax_treatment', 'none')
    if tax_treatment not in TAX_TREATMENT_LABELS:
        return jsonify({
            'error': f'Invalid tax_treatment. Valid values: {", ".join(TAX_TREATMENT_LABELS.keys())}'
        }), 400

    if settings.tax_mode == 'kleinunternehmer':
        tax_treatment = 'none'

    # Validate account exists
    account_id = int(data['account_id'])
    acc = Account.query.get(account_id)
    if not acc:
        return jsonify({'error': f'Account {account_id} not found'}), 404

    # Validate category exists
    category_id = data.get('category_id')
    if category_id is not None:
        cat = Category.query.get(category_id)
        if not cat:
            return jsonify({'error': f'Category {category_id} not found'}), 404

    net, tax, eff_rate = _apply_tax(amount, tax_treatment, settings, data.get('custom_tax_rate'))

    t = Transaction(
        date=tx_date,
        type=data['type'],
        description=data['description'].strip(),
        amount=amount,
        net_amount=net,
        tax_amount=tax,
        tax_treatment=tax_treatment,
        tax_rate=eff_rate,
        account_id=account_id,
        category_id=category_id,
        notes=data.get('notes'),
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({'transaction': _tx_to_dict(t)}), 201


@api_bp.route('/transactions/<int:tx_id>', methods=['GET'])
@require_api_key
def get_transaction(tx_id):
    """Get a single transaction by ID."""
    t = Transaction.query.get(tx_id)
    if not t:
        return jsonify({'error': f'Transaction {tx_id} not found'}), 404
    return jsonify({'transaction': _tx_to_dict(t)})


@api_bp.route('/transactions/<int:tx_id>', methods=['PUT', 'PATCH'])
@require_api_key
def update_transaction(tx_id):
    """
    Update a transaction. Only provided fields are changed.
    Body: { date?, type?, description?, amount?, category_id?, tax_treatment?, custom_tax_rate?, notes? }
    """
    t = Transaction.query.get(tx_id)
    if not t:
        return jsonify({'error': f'Transaction {tx_id} not found'}), 404

    # Protect linked and transfer transactions
    if t.linked_asset_id:
        return jsonify({'error': 'Cannot edit a transaction linked to an asset. Manage it via the asset.'}), 409
    if t.type == 'transfer':
        return jsonify({'error': 'Cannot edit a transfer. Delete and recreate it.'}), 409

    data = request.get_json(force=True)
    settings = SiteSettings.get_settings()

    if 'date' in data:
        try:
            t.date = parse_date(data['date'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    if 'type' in data:
        if data['type'] not in ('income', 'expense'):
            return jsonify({'error': "type must be 'income' or 'expense'"}), 400
        t.type = data['type']

    if 'description' in data:
        desc = data['description'].strip()
        if not desc:
            return jsonify({'error': 'description cannot be empty'}), 400
        t.description = desc

    if 'category_id' in data:
        if data['category_id'] is not None:
            cat = Category.query.get(data['category_id'])
            if not cat:
                return jsonify({'error': f"Category {data['category_id']} not found"}), 404
        t.category_id = data['category_id']

    if 'account_id' in data:
        acc = Account.query.get(data['account_id'])
        if not acc:
            return jsonify({'error': f"Account {data['account_id']} not found"}), 404
        t.account_id = data['account_id']

    if 'notes' in data:
        t.notes = data['notes'] or None

    # Recalculate tax
    tax_treatment = data.get('tax_treatment', t.tax_treatment or 'none')
    if tax_treatment not in TAX_TREATMENT_LABELS:
        return jsonify({
            'error': f'Invalid tax_treatment. Valid values: {", ".join(TAX_TREATMENT_LABELS.keys())}'
        }), 400
    if settings.tax_mode == 'kleinunternehmer':
        tax_treatment = 'none'
    t.tax_treatment = tax_treatment

    amount = float(data.get('amount', t.amount))
    if amount <= 0:
        return jsonify({'error': 'amount must be positive'}), 400

    net, tax, eff_rate = _apply_tax(amount, tax_treatment, settings, data.get('custom_tax_rate'))
    t.amount = amount
    t.net_amount = net
    t.tax_amount = tax
    t.tax_rate = eff_rate

    db.session.commit()
    return jsonify({'transaction': _tx_to_dict(t)})


@api_bp.route('/transactions/<int:tx_id>', methods=['DELETE'])
@require_api_key
def delete_transaction(tx_id):
    """Delete a transaction by ID. Linked asset transactions cannot be deleted via this endpoint."""
    t = Transaction.query.get(tx_id)
    if not t:
        return jsonify({'error': f'Transaction {tx_id} not found'}), 404

    if t.linked_asset_id:
        return jsonify({'error': 'Cannot delete a transaction linked to an asset. Manage it via the asset.'}), 409

    # Archive all attached documents
    for doc in Document.query.filter_by(entity_type='transaction', entity_id=t.id).all():
        archive_file(current_app.config['UPLOAD_FOLDER'], doc.filename)
        db.session.delete(doc)

    db.session.delete(t)
    db.session.commit()
    return jsonify({'deleted': True, 'id': tx_id})


# ---------------------------------------------------------------------------
# Transaction documents (upload / download / delete)
# ---------------------------------------------------------------------------

@api_bp.route('/transactions/<int:tx_id>/documents', methods=['POST'])
@require_api_key
def upload_transaction_documents(tx_id):
    """
    Upload one or more documents to a transaction.
    Content-Type: multipart/form-data with file field(s) named 'documents'.
    Allowed types: pdf, png, jpg, jpeg, gif, webp. Max 16 MB total.
    """
    t = Transaction.query.get(tx_id)
    if not t:
        return jsonify({'error': f'Transaction {tx_id} not found'}), 404

    files = request.files.getlist('documents')
    if not files or not any(f.filename for f in files):
        return jsonify({'error': "No files provided. Send file(s) in field 'documents'."}), 400

    created = []
    errors = []
    for i, file in enumerate(files):
        if not file or not file.filename:
            continue
        if not _allowed_file(file.filename):
            errors.append({'index': i, 'filename': file.filename,
                           'error': f'File type not allowed. Allowed: {", ".join(sorted(ALLOWED_EXTENSIONS))}'})
            continue
        stored = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
        file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], stored))
        doc = Document(filename=stored, original_filename=file.filename,
                       entity_type='transaction', entity_id=t.id)
        db.session.add(doc)
        db.session.flush()
        created.append({'id': doc.id, 'filename': stored, 'original_filename': file.filename})

    if not created and errors:
        return jsonify({'errors': errors}), 400

    db.session.commit()
    return jsonify({
        'transaction_id': t.id,
        'documents': created,
        'errors': errors,
    }), 201


@api_bp.route('/transactions/<int:tx_id>/documents', methods=['GET'])
@require_api_key
def list_transaction_documents(tx_id):
    """List all documents attached to a transaction."""
    t = Transaction.query.get(tx_id)
    if not t:
        return jsonify({'error': f'Transaction {tx_id} not found'}), 404

    docs = Document.query.filter_by(entity_type='transaction', entity_id=t.id).all()
    return jsonify({
        'transaction_id': t.id,
        'documents': [{'id': d.id, 'filename': d.filename, 'original_filename': d.original_filename,
                        'created_at': d.created_at.isoformat() if d.created_at else None} for d in docs],
    })


@api_bp.route('/transactions/<int:tx_id>/documents/<int:doc_id>', methods=['GET'])
@require_api_key
def download_transaction_document(tx_id, doc_id):
    """Download a specific document attached to a transaction."""
    t = Transaction.query.get(tx_id)
    if not t:
        return jsonify({'error': f'Transaction {tx_id} not found'}), 404

    doc = Document.query.get(doc_id)
    if not doc or doc.entity_type != 'transaction' or doc.entity_id != t.id:
        return jsonify({'error': f'Document {doc_id} not found for this transaction.'}), 404

    upload_folder = current_app.config['UPLOAD_FOLDER']
    fp = os.path.join(upload_folder, doc.filename)
    if not os.path.exists(fp):
        return jsonify({'error': 'Document file not found on disk.'}), 404

    return send_from_directory(upload_folder, doc.filename)


@api_bp.route('/transactions/<int:tx_id>/documents/<int:doc_id>', methods=['DELETE'])
@require_api_key
def delete_transaction_document(tx_id, doc_id):
    """Remove a specific document from a transaction (deletes the file on disk)."""
    t = Transaction.query.get(tx_id)
    if not t:
        return jsonify({'error': f'Transaction {tx_id} not found'}), 404

    doc = Document.query.get(doc_id)
    if not doc or doc.entity_type != 'transaction' or doc.entity_id != t.id:
        return jsonify({'error': f'Document {doc_id} not found for this transaction.'}), 404

    fp = os.path.join(current_app.config['UPLOAD_FOLDER'], doc.filename)
    archive_file(current_app.config['UPLOAD_FOLDER'], doc.filename)

    db.session.delete(doc)
    db.session.commit()

    return jsonify({'deleted': True, 'document_id': doc_id, 'transaction_id': tx_id})


# ---------------------------------------------------------------------------
# Bulk create transactions
# ---------------------------------------------------------------------------

@api_bp.route('/transactions/bulk', methods=['POST'])
@require_api_key
def bulk_create_transactions():
    """
    Create multiple transactions at once.
    Body: { transactions: [ {date, type, description, amount, ...}, ... ] }
    Returns created transactions and any errors (by index).
    """
    data = request.get_json(force=True)
    items = data.get('transactions', [])
    if not items:
        return jsonify({'error': 'transactions array is required and must not be empty'}), 400
    if len(items) > 500:
        return jsonify({'error': 'Maximum 500 transactions per bulk request'}), 400

    settings = SiteSettings.get_settings()
    created = []
    errors = []

    for i, item in enumerate(items):
        try:
            # Validate
            if not item.get('date'):
                raise ValueError('date is required')
            if item.get('type') not in ('income', 'expense'):
                raise ValueError("type must be 'income' or 'expense'")
            if not item.get('description', '').strip():
                raise ValueError('description is required')
            if item.get('amount') is None:
                raise ValueError('amount is required')

            tx_date = parse_date(item['date'])
            amount = float(item['amount'])
            if amount <= 0:
                raise ValueError('amount must be positive')

            tax_treatment = item.get('tax_treatment', 'none')
            if tax_treatment not in TAX_TREATMENT_LABELS:
                raise ValueError(f'Invalid tax_treatment: {tax_treatment}')
            if settings.tax_mode == 'kleinunternehmer':
                tax_treatment = 'none'

            # Validate account
            if not item.get('account_id'):
                raise ValueError('account_id is required')
            account_id = int(item['account_id'])
            acc = Account.query.get(account_id)
            if not acc:
                raise ValueError(f'Account {account_id} not found')

            category_id = item.get('category_id')
            if category_id is not None:
                cat = Category.query.get(category_id)
                if not cat:
                    raise ValueError(f'Category {category_id} not found')

            net, tax, eff_rate = _apply_tax(amount, tax_treatment, settings, item.get('custom_tax_rate'))

            t = Transaction(
                date=tx_date,
                type=item['type'],
                description=item['description'].strip(),
                amount=amount,
                net_amount=net,
                tax_amount=tax,
                tax_treatment=tax_treatment,
                tax_rate=eff_rate,
                account_id=account_id,
                category_id=category_id,
                notes=item.get('notes'),
            )
            db.session.add(t)
            db.session.flush()  # get ID
            created.append(_tx_to_dict(t))
        except Exception as e:
            errors.append({'index': i, 'error': str(e)})

    if errors and not created:
        db.session.rollback()
        return jsonify({'errors': errors, 'created': []}), 400

    db.session.commit()
    return jsonify({
        'created': created,
        'errors': errors,
        'count': len(created),
    }), 201


# ---------------------------------------------------------------------------
# Summary / Dashboard
# ---------------------------------------------------------------------------

@api_bp.route('/summary', methods=['GET'])
@require_api_key
def summary():
    """
    Financial summary for a given year.
    Query params: year (default: current year)
    """
    year = request.args.get('year', date.today().year, type=int)
    txs = Transaction.query.filter(db.extract('year', Transaction.date) == year).all()

    # EÜR-relevant transactions only (exclude transfers and linked asset transactions)
    eur_txs = [t for t in txs if t.type in ('income', 'expense') and not t.linked_asset_id]

    total_income = sum(t.amount for t in eur_txs if t.type == 'income')
    total_expenses = sum(t.amount for t in eur_txs if t.type == 'expense')
    total_income_net = sum((t.net_amount or t.amount) for t in eur_txs if t.type == 'income')
    total_expenses_net = sum((t.net_amount or t.amount) for t in eur_txs if t.type == 'expense')
    total_tax_income = sum((t.tax_amount or 0) for t in eur_txs if t.type == 'income')
    total_tax_expenses = sum((t.tax_amount or 0) for t in eur_txs if t.type == 'expense')

    monthly = {}
    for m in range(1, 13):
        mt = [t for t in eur_txs if t.date.month == m]
        inc = sum(t.amount for t in mt if t.type == 'income')
        exp = sum(t.amount for t in mt if t.type == 'expense')
        monthly[str(m)] = {
            'income': round(inc, 2),
            'expenses': round(exp, 2),
            'profit': round(inc - exp, 2),
        }

    # Account balances
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()

    return jsonify({
        'year': year,
        'total_income': round(total_income, 2),
        'total_expenses': round(total_expenses, 2),
        'profit': round(total_income - total_expenses, 2),
        'total_income_net': round(total_income_net, 2),
        'total_expenses_net': round(total_expenses_net, 2),
        'vat_collected': round(total_tax_income, 2),
        'vat_paid': round(total_tax_expenses, 2),
        'vat_payable': round(total_tax_income - total_tax_expenses, 2),
        'transaction_count': len(txs),
        'monthly': monthly,
        'accounts': [_account_to_dict(a) for a in accounts],
    })


# ---------------------------------------------------------------------------
# Helpers: invoicing (quotes & invoices)
# ---------------------------------------------------------------------------

def _quote_item_to_dict(item):
    """Serialize a QuoteItem to a dict."""
    return {
        'id': item.id,
        'position': item.position,
        'description': item.description,
        'quantity': item.quantity,
        'unit': item.unit,
        'unit_price': item.unit_price,
        'total': item.total,
    }


def _quote_to_dict(q, include_items=True):
    """Serialize a Quote to a dict."""
    d = {
        'id': q.id,
        'quote_number': q.quote_number,
        'customer_id': q.customer_id,
        'customer_name': q.customer.display_name if q.customer else None,
        'date': q.date.isoformat() if q.date else None,
        'valid_until': q.valid_until.isoformat() if q.valid_until else None,
        'status': q.status,
        'tax_treatment': q.tax_treatment,
        'tax_rate': q.tax_rate,
        'discount_percent': q.discount_percent,
        'subtotal': q.subtotal,
        'discount_amount': q.discount_amount,
        'total': q.total,
        'notes': q.notes,
        'agb_text': q.agb_text,
        'payment_terms_days': q.payment_terms_days,
        'linked_asset_id': q.linked_asset_id,
        'has_pdf': bool(q.document_filename),
        'created_at': q.created_at.isoformat() if q.created_at else None,
        'updated_at': q.updated_at.isoformat() if q.updated_at else None,
    }
    if include_items:
        d['items'] = [_quote_item_to_dict(i) for i in q.items]
    return d


def _invoice_item_to_dict(item):
    """Serialize an InvoiceItem to a dict."""
    return {
        'id': item.id,
        'position': item.position,
        'description': item.description,
        'quantity': item.quantity,
        'unit': item.unit,
        'unit_price': item.unit_price,
        'total': item.total,
    }


def _invoice_to_dict(inv, include_items=True):
    """Serialize an Invoice to a dict."""
    d = {
        'id': inv.id,
        'invoice_number': inv.invoice_number,
        'quote_id': inv.quote_id,
        'customer_id': inv.customer_id,
        'customer_name': inv.customer.display_name if inv.customer else None,
        'date': inv.date.isoformat() if inv.date else None,
        'due_date': inv.due_date.isoformat() if inv.due_date else None,
        'status': inv.status,
        'tax_treatment': inv.tax_treatment,
        'tax_rate': inv.tax_rate,
        'discount_percent': inv.discount_percent,
        'subtotal': inv.subtotal,
        'discount_amount': inv.discount_amount,
        'total': inv.total,
        'notes': inv.notes,
        'payment_terms_days': inv.payment_terms_days,
        'linked_asset_id': inv.linked_asset_id,
        'linked_transaction_id': inv.linked_transaction_id,
        'has_pdf': bool(inv.document_filename),
        'created_at': inv.created_at.isoformat() if inv.created_at else None,
        'updated_at': inv.updated_at.isoformat() if inv.updated_at else None,
    }
    if include_items:
        d['items'] = [_invoice_item_to_dict(i) for i in inv.items]
    return d


def _save_quote_items(quote, items_data):
    """Create QuoteItem records from a list of item dicts."""
    for i, item in enumerate(items_data):
        desc = item.get('description', '').strip()
        if not desc:
            continue
        qi = QuoteItem(
            quote_id=quote.id,
            position=item.get('position', i + 1),
            description=desc,
            quantity=float(item.get('quantity', 1)),
            unit=item.get('unit', 'Stk.').strip() or 'Stk.',
            unit_price=float(item.get('unit_price', 0)),
        )
        db.session.add(qi)


def _save_invoice_items_api(invoice, items_data):
    """Create InvoiceItem records from a list of item dicts."""
    for i, item in enumerate(items_data):
        desc = item.get('description', '').strip()
        if not desc:
            continue
        ii = InvoiceItem(
            invoice_id=invoice.id,
            position=item.get('position', i + 1),
            description=desc,
            quantity=float(item.get('quantity', 1)),
            unit=item.get('unit', 'Stk.').strip() or 'Stk.',
            unit_price=float(item.get('unit_price', 0)),
        )
        db.session.add(ii)


def _next_number_api(prefix, model_class, number_field, year=None):
    """Generate the next sequential document number, e.g. A-2026-0001."""
    if year is None:
        year = date.today().year
    pattern = f'{prefix}-{year}-%'
    col = getattr(model_class, number_field)
    last = (model_class.query
            .filter(col.like(pattern))
            .order_by(col.desc())
            .first())
    if last:
        last_num = getattr(last, number_field)
        try:
            seq = int(last_num.rsplit('-', 1)[1]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1
    return f'{prefix}-{year}-{seq:04d}'


def _generate_quote_pdf_api(quote, settings):
    """Generate the PDF for a quote and store it."""
    from blueprints.invoicing import _generate_quote_pdf
    _generate_quote_pdf(quote, settings)


def _generate_invoice_pdf_api(invoice, settings):
    """Generate the PDF for an invoice and store it."""
    from blueprints.invoicing import _generate_invoice_pdf
    _generate_invoice_pdf(invoice, settings)


def _archive_document_api(filename):
    """Move a document file to the archive folder."""
    upload_dir = current_app.config['UPLOAD_FOLDER']
    archive_file(upload_dir, filename)


# ---------------------------------------------------------------------------
# Quotes (Angebote)
# ---------------------------------------------------------------------------

@api_bp.route('/quotes', methods=['GET'])
@require_api_key
def list_quotes():
    """
    List quotes with optional filters.
    Query params:
      status   – draft, sent, accepted, rejected, invoiced
      year     – filter by year
      customer_id – filter by customer
      limit    – max results (default 100, max 1000)
      offset   – pagination offset (default 0)
    """
    q = Quote.query

    status = request.args.get('status', '').strip()
    if status:
        q = q.filter_by(status=status)

    year = request.args.get('year', type=int)
    if year:
        q = q.filter(db.extract('year', Quote.date) == year)

    customer_id = request.args.get('customer_id', type=int)
    if customer_id:
        q = q.filter_by(customer_id=customer_id)

    q = q.order_by(Quote.date.desc(), Quote.id.desc())

    total = q.count()
    limit = min(request.args.get('limit', 100, type=int), 1000)
    offset = request.args.get('offset', 0, type=int)
    quotes = q.limit(limit).offset(offset).all()

    return jsonify({
        'quotes': [_quote_to_dict(qu, include_items=False) for qu in quotes],
        'total': total,
        'limit': limit,
        'offset': offset,
    })


@api_bp.route('/quotes', methods=['POST'])
@require_api_key
def create_quote():
    """
    Create a new quote.
    Body: {
        customer_id:        int       (optional)
        date:               "YYYY-MM-DD" (required)
        valid_until:        "YYYY-MM-DD" (optional)
        tax_treatment:      string    (optional, default "none")
        custom_tax_rate:    number    (optional, only if tax_treatment="custom")
        discount_percent:   number    (optional, default 0)
        notes:              string    (optional)
        agb_text:           string    (optional)
        payment_terms_days: int       (optional, default 14)
        linked_asset_id:    int       (optional)
        items: [
            {
                description: string  (required)
                quantity:    number   (optional, default 1)
                unit:        string   (optional, default "Stk.")
                unit_price:  number   (required, gross price per unit)
                position:    int      (optional, auto-assigned)
            }
        ]
    }
    """
    data = request.get_json(force=True)
    settings = SiteSettings.get_settings()

    # Validate required fields
    errors = []
    if not data.get('date'):
        errors.append('date is required (YYYY-MM-DD)')
    items = data.get('items', [])
    if not items:
        errors.append('items array is required with at least one item')
    if errors:
        return jsonify({'errors': errors}), 400

    try:
        q_date = parse_date(data['date'])
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    valid_until = None
    if data.get('valid_until'):
        try:
            valid_until = parse_date(data['valid_until'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid valid_until date format. Use YYYY-MM-DD.'}), 400

    # Validate customer exists
    customer_id = data.get('customer_id')
    if customer_id is not None:
        cust = Customer.query.get(customer_id)
        if not cust:
            return jsonify({'error': f'Customer {customer_id} not found'}), 404

    # Validate linked asset exists
    linked_asset_id = data.get('linked_asset_id')
    if linked_asset_id is not None:
        asset = Asset.query.get(linked_asset_id)
        if not asset:
            return jsonify({'error': f'Asset {linked_asset_id} not found'}), 404

    # Tax
    tax_treatment = data.get('tax_treatment', 'none')
    if tax_treatment not in TAX_TREATMENT_LABELS:
        return jsonify({
            'error': f'Invalid tax_treatment. Valid values: {", ".join(TAX_TREATMENT_LABELS.keys())}'
        }), 400

    if settings.tax_mode == 'kleinunternehmer':
        tax_treatment = 'none'

    custom_rate = float(data.get('custom_tax_rate', 0) or 0)
    tax_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)

    # Validate items
    for i, item in enumerate(items):
        if not item.get('description', '').strip():
            errors.append(f'items[{i}].description is required')
        if item.get('unit_price') is None:
            errors.append(f'items[{i}].unit_price is required')
    if errors:
        return jsonify({'errors': errors}), 400

    # Generate number
    prefix = settings.quote_number_prefix or 'A'
    quote_number = _next_number_api(prefix, Quote, 'quote_number')

    payment_terms = int(data.get('payment_terms_days', 14) or 14)

    quote = Quote(
        quote_number=quote_number,
        customer_id=customer_id,
        date=q_date,
        valid_until=valid_until,
        status='draft',
        tax_treatment=tax_treatment,
        tax_rate=tax_rate,
        discount_percent=float(data.get('discount_percent', 0) or 0),
        notes=data.get('notes', '').strip() or None,
        agb_text=data.get('agb_text', '').strip() or None,
        payment_terms_days=payment_terms,
        linked_asset_id=linked_asset_id,
    )
    db.session.add(quote)
    db.session.flush()

    _save_quote_items(quote, items)
    db.session.commit()

    return jsonify({'quote': _quote_to_dict(quote)}), 201


@api_bp.route('/quotes/<int:quote_id>', methods=['GET'])
@require_api_key
def get_quote(quote_id):
    """Get a single quote by ID with items."""
    q = Quote.query.get(quote_id)
    if not q:
        return jsonify({'error': f'Quote {quote_id} not found'}), 404
    return jsonify({'quote': _quote_to_dict(q)})


@api_bp.route('/quotes/<int:quote_id>', methods=['PUT', 'PATCH'])
@require_api_key
def update_quote(quote_id):
    """
    Update a quote. Only provided fields are changed.
    If 'items' is provided, all existing items are replaced.
    Body: {
        customer_id?, date?, valid_until?, tax_treatment?, custom_tax_rate?,
        discount_percent?, notes?, agb_text?, payment_terms_days?,
        items?: [ { description, quantity?, unit?, unit_price, position? } ]
    }
    """
    q = Quote.query.get(quote_id)
    if not q:
        return jsonify({'error': f'Quote {quote_id} not found'}), 404

    if q.status == 'invoiced':
        return jsonify({'error': 'Cannot edit a quote that has been invoiced.'}), 409

    data = request.get_json(force=True)
    settings = SiteSettings.get_settings()

    if 'date' in data:
        try:
            q.date = parse_date(data['date'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    if 'valid_until' in data:
        if data['valid_until']:
            try:
                q.valid_until = parse_date(data['valid_until'])
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid valid_until date format. Use YYYY-MM-DD.'}), 400
        else:
            q.valid_until = None

    if 'customer_id' in data:
        if data['customer_id'] is not None:
            cust = Customer.query.get(data['customer_id'])
            if not cust:
                return jsonify({'error': f"Customer {data['customer_id']} not found"}), 404
        q.customer_id = data['customer_id']

    if 'linked_asset_id' in data:
        if data['linked_asset_id'] is not None:
            asset = Asset.query.get(data['linked_asset_id'])
            if not asset:
                return jsonify({'error': f"Asset {data['linked_asset_id']} not found"}), 404
        q.linked_asset_id = data['linked_asset_id']

    if 'tax_treatment' in data:
        tax_treatment = data['tax_treatment']
        if tax_treatment not in TAX_TREATMENT_LABELS:
            return jsonify({
                'error': f'Invalid tax_treatment. Valid values: {", ".join(TAX_TREATMENT_LABELS.keys())}'
            }), 400
        if settings.tax_mode == 'kleinunternehmer':
            tax_treatment = 'none'
        q.tax_treatment = tax_treatment
        custom_rate = float(data.get('custom_tax_rate', 0) or 0)
        q.tax_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)

    if 'discount_percent' in data:
        q.discount_percent = float(data['discount_percent'] or 0)

    if 'notes' in data:
        q.notes = data['notes'].strip() or None if data['notes'] else None

    if 'agb_text' in data:
        q.agb_text = data['agb_text'].strip() or None if data['agb_text'] else None

    if 'payment_terms_days' in data:
        q.payment_terms_days = int(data['payment_terms_days'] or 14)

    # Replace items if provided
    if 'items' in data:
        items = data['items']
        if not items:
            return jsonify({'error': 'items array must contain at least one item'}), 400
        errors = []
        for i, item in enumerate(items):
            if not item.get('description', '').strip():
                errors.append(f'items[{i}].description is required')
            if item.get('unit_price') is None:
                errors.append(f'items[{i}].unit_price is required')
        if errors:
            return jsonify({'errors': errors}), 400

        QuoteItem.query.filter_by(quote_id=q.id).delete()
        _save_quote_items(q, items)

    # Regenerate PDF if it existed
    if q.document_filename:
        _generate_quote_pdf_api(q, settings)

    db.session.commit()
    return jsonify({'quote': _quote_to_dict(q)})


@api_bp.route('/quotes/<int:quote_id>', methods=['DELETE'])
@require_api_key
def delete_quote(quote_id):
    """Delete a quote. Fails if invoices reference it."""
    q = Quote.query.get(quote_id)
    if not q:
        return jsonify({'error': f'Quote {quote_id} not found'}), 404

    if q.invoices.count() > 0:
        return jsonify({
            'error': f'Cannot delete quote with {q.invoices.count()} linked invoice(s). Delete them first.'
        }), 409

    if q.document_filename:
        _archive_document_api(q.document_filename)

    db.session.delete(q)
    db.session.commit()
    return jsonify({'deleted': True, 'id': quote_id})


@api_bp.route('/quotes/<int:quote_id>/status', methods=['POST', 'PUT'])
@require_api_key
def set_quote_status(quote_id):
    """
    Change quote status.
    Body: { "status": "draft" | "sent" | "accepted" | "rejected" }
    """
    q = Quote.query.get(quote_id)
    if not q:
        return jsonify({'error': f'Quote {quote_id} not found'}), 404

    data = request.get_json(force=True)
    new_status = data.get('status', '').strip()
    allowed = ['draft', 'sent', 'accepted', 'rejected']
    if new_status not in allowed:
        return jsonify({
            'error': f'Invalid status. Allowed: {", ".join(allowed)}'
        }), 400

    q.status = new_status
    db.session.commit()
    return jsonify({'quote': _quote_to_dict(q, include_items=False)})


@api_bp.route('/quotes/<int:quote_id>/generate-pdf', methods=['POST'])
@require_api_key
def generate_quote_pdf(quote_id):
    """Generate or regenerate the quote PDF."""
    q = Quote.query.get(quote_id)
    if not q:
        return jsonify({'error': f'Quote {quote_id} not found'}), 404

    settings = SiteSettings.get_settings()
    _generate_quote_pdf_api(q, settings)
    db.session.commit()

    return jsonify({
        'quote_id': q.id,
        'quote_number': q.quote_number,
        'has_pdf': True,
        'message': 'PDF generated successfully.',
    })


@api_bp.route('/quotes/<int:quote_id>/pdf', methods=['GET'])
@require_api_key
def download_quote_pdf(quote_id):
    """Download the quote PDF."""
    q = Quote.query.get(quote_id)
    if not q:
        return jsonify({'error': f'Quote {quote_id} not found'}), 404

    if not q.document_filename:
        return jsonify({'error': 'No PDF available. Generate it first via POST /quotes/:id/generate-pdf.'}), 404

    upload_folder = current_app.config['UPLOAD_FOLDER']
    fp = os.path.join(upload_folder, q.document_filename)
    if not os.path.exists(fp):
        return jsonify({'error': 'PDF file not found on disk.'}), 404

    return send_from_directory(upload_folder, q.document_filename,
                               as_attachment=False,
                               download_name=f'Angebot_{q.quote_number}.pdf')


@api_bp.route('/quotes/<int:quote_id>/create-invoice', methods=['POST'])
@require_api_key
def create_invoice_from_quote(quote_id):
    """
    Create an invoice from a quote.
    Copies all items and settings. Marks quote as 'invoiced'. Generates invoice PDF.
    Optional body: { "date": "YYYY-MM-DD" }   (defaults to today)
    """
    q = Quote.query.get(quote_id)
    if not q:
        return jsonify({'error': f'Quote {quote_id} not found'}), 404

    if q.status == 'invoiced':
        return jsonify({'error': 'Quote has already been invoiced.'}), 409

    data = request.get_json(silent=True) or {}
    settings = SiteSettings.get_settings()

    inv_date = date.today()
    if data.get('date'):
        try:
            inv_date = parse_date(data['date'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    prefix = settings.invoice_number_prefix or 'R'
    invoice_number = _next_number_api(prefix, Invoice, 'invoice_number')

    invoice = Invoice(
        invoice_number=invoice_number,
        quote_id=q.id,
        customer_id=q.customer_id,
        date=inv_date,
        due_date=inv_date + timedelta(days=q.payment_terms_days or 14),
        status='draft',
        tax_treatment=q.tax_treatment,
        tax_rate=q.tax_rate,
        discount_percent=q.discount_percent,
        notes=q.notes,
        payment_terms_days=q.payment_terms_days,
        linked_asset_id=q.linked_asset_id,
    )
    db.session.add(invoice)
    db.session.flush()

    for qi in q.items:
        ii = InvoiceItem(
            invoice_id=invoice.id,
            position=qi.position,
            description=qi.description,
            quantity=qi.quantity,
            unit=qi.unit,
            unit_price=qi.unit_price,
        )
        db.session.add(ii)

    q.status = 'invoiced'

    _generate_invoice_pdf_api(invoice, settings)
    db.session.commit()

    return jsonify({'invoice': _invoice_to_dict(invoice)}), 201


# ---------------------------------------------------------------------------
# Invoices (Rechnungen)
# ---------------------------------------------------------------------------

@api_bp.route('/invoices', methods=['GET'])
@require_api_key
def list_invoices():
    """
    List invoices with optional filters.
    Query params:
      status      – draft, sent, paid, cancelled
      year        – filter by year
      customer_id – filter by customer
      limit       – max results (default 100, max 1000)
      offset      – pagination offset (default 0)
    """
    q = Invoice.query

    status = request.args.get('status', '').strip()
    if status:
        q = q.filter_by(status=status)

    year = request.args.get('year', type=int)
    if year:
        q = q.filter(db.extract('year', Invoice.date) == year)

    customer_id = request.args.get('customer_id', type=int)
    if customer_id:
        q = q.filter_by(customer_id=customer_id)

    q = q.order_by(Invoice.date.desc(), Invoice.id.desc())

    total = q.count()
    limit = min(request.args.get('limit', 100, type=int), 1000)
    offset = request.args.get('offset', 0, type=int)
    invoices = q.limit(limit).offset(offset).all()

    # Aggregate totals
    total_amount = sum(inv.total for inv in invoices)
    paid_amount = sum(inv.total for inv in invoices if inv.status == 'paid')
    open_amount = sum(inv.total for inv in invoices if inv.status in ('draft', 'sent'))

    return jsonify({
        'invoices': [_invoice_to_dict(inv, include_items=False) for inv in invoices],
        'total': total,
        'limit': limit,
        'offset': offset,
        'total_amount': round(total_amount, 2),
        'paid_amount': round(paid_amount, 2),
        'open_amount': round(open_amount, 2),
    })


@api_bp.route('/invoices', methods=['POST'])
@require_api_key
def create_invoice():
    """
    Create a new invoice (without quote).
    Body: {
        customer_id:        int       (required)
        date:               "YYYY-MM-DD" (required)
        tax_treatment:      string    (optional, default "none")
        custom_tax_rate:    number    (optional, only if tax_treatment="custom")
        discount_percent:   number    (optional, default 0)
        notes:              string    (optional)
        payment_terms_days: int       (optional, default 14)
        linked_asset_id:    int       (optional)
        items: [
            {
                description: string  (required)
                quantity:    number   (optional, default 1)
                unit:        string   (optional, default "Stk.")
                unit_price:  number   (required, gross price per unit)
                position:    int      (optional, auto-assigned)
            }
        ]
    }
    """
    data = request.get_json(force=True)
    settings = SiteSettings.get_settings()

    errors = []
    if not data.get('date'):
        errors.append('date is required (YYYY-MM-DD)')
    if not data.get('customer_id'):
        errors.append('customer_id is required')
    items = data.get('items', [])
    if not items:
        errors.append('items array is required with at least one item')
    if errors:
        return jsonify({'errors': errors}), 400

    try:
        inv_date = parse_date(data['date'])
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    # Validate customer
    customer_id = int(data['customer_id'])
    cust = Customer.query.get(customer_id)
    if not cust:
        return jsonify({'error': f'Customer {customer_id} not found'}), 404

    # Validate linked asset
    linked_asset_id = data.get('linked_asset_id')
    if linked_asset_id is not None:
        asset = Asset.query.get(linked_asset_id)
        if not asset:
            return jsonify({'error': f'Asset {linked_asset_id} not found'}), 404

    # Tax
    tax_treatment = data.get('tax_treatment', 'none')
    if tax_treatment not in TAX_TREATMENT_LABELS:
        return jsonify({
            'error': f'Invalid tax_treatment. Valid values: {", ".join(TAX_TREATMENT_LABELS.keys())}'
        }), 400

    if settings.tax_mode == 'kleinunternehmer':
        tax_treatment = 'none'

    custom_rate = float(data.get('custom_tax_rate', 0) or 0)
    tax_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)

    # Validate items
    for i, item in enumerate(items):
        if not item.get('description', '').strip():
            errors.append(f'items[{i}].description is required')
        if item.get('unit_price') is None:
            errors.append(f'items[{i}].unit_price is required')
    if errors:
        return jsonify({'errors': errors}), 400

    prefix = settings.invoice_number_prefix or 'R'
    invoice_number = _next_number_api(prefix, Invoice, 'invoice_number')

    payment_terms = int(data.get('payment_terms_days', 14) or 14)

    invoice = Invoice(
        invoice_number=invoice_number,
        customer_id=customer_id,
        date=inv_date,
        due_date=inv_date + timedelta(days=payment_terms),
        status='draft',
        tax_treatment=tax_treatment,
        tax_rate=tax_rate,
        discount_percent=float(data.get('discount_percent', 0) or 0),
        notes=data.get('notes', '').strip() or None,
        payment_terms_days=payment_terms,
        linked_asset_id=linked_asset_id,
    )
    db.session.add(invoice)
    db.session.flush()

    _save_invoice_items_api(invoice, items)
    db.session.commit()

    return jsonify({'invoice': _invoice_to_dict(invoice)}), 201


@api_bp.route('/invoices/<int:invoice_id>', methods=['GET'])
@require_api_key
def get_invoice(invoice_id):
    """Get a single invoice by ID with items."""
    inv = Invoice.query.get(invoice_id)
    if not inv:
        return jsonify({'error': f'Invoice {invoice_id} not found'}), 404
    return jsonify({'invoice': _invoice_to_dict(inv)})


@api_bp.route('/invoices/<int:invoice_id>', methods=['PUT', 'PATCH'])
@require_api_key
def update_invoice(invoice_id):
    """
    Update an invoice. Only provided fields are changed.
    If 'items' is provided, all existing items are replaced.
    Paid invoices cannot be edited.
    Body: {
        customer_id?, date?, tax_treatment?, custom_tax_rate?,
        discount_percent?, notes?, payment_terms_days?,
        items?: [ { description, quantity?, unit?, unit_price, position? } ]
    }
    """
    inv = Invoice.query.get(invoice_id)
    if not inv:
        return jsonify({'error': f'Invoice {invoice_id} not found'}), 404

    if inv.status == 'paid':
        return jsonify({'error': 'Cannot edit a paid invoice. Unmark payment first.'}), 409

    data = request.get_json(force=True)
    settings = SiteSettings.get_settings()

    if 'date' in data:
        try:
            inv.date = parse_date(data['date'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    if 'customer_id' in data:
        if data['customer_id'] is not None:
            cust = Customer.query.get(data['customer_id'])
            if not cust:
                return jsonify({'error': f"Customer {data['customer_id']} not found"}), 404
        inv.customer_id = data['customer_id']

    if 'linked_asset_id' in data:
        if data['linked_asset_id'] is not None:
            asset = Asset.query.get(data['linked_asset_id'])
            if not asset:
                return jsonify({'error': f"Asset {data['linked_asset_id']} not found"}), 404
        inv.linked_asset_id = data['linked_asset_id']

    if 'tax_treatment' in data:
        tax_treatment = data['tax_treatment']
        if tax_treatment not in TAX_TREATMENT_LABELS:
            return jsonify({
                'error': f'Invalid tax_treatment. Valid values: {", ".join(TAX_TREATMENT_LABELS.keys())}'
            }), 400
        if settings.tax_mode == 'kleinunternehmer':
            tax_treatment = 'none'
        inv.tax_treatment = tax_treatment
        custom_rate = float(data.get('custom_tax_rate', 0) or 0)
        inv.tax_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)

    if 'discount_percent' in data:
        inv.discount_percent = float(data['discount_percent'] or 0)

    if 'notes' in data:
        inv.notes = data['notes'].strip() or None if data['notes'] else None

    if 'payment_terms_days' in data:
        inv.payment_terms_days = int(data['payment_terms_days'] or 14)

    # Recalculate due_date
    inv.due_date = inv.date + timedelta(days=inv.payment_terms_days)

    # Replace items if provided
    if 'items' in data:
        items = data['items']
        if not items:
            return jsonify({'error': 'items array must contain at least one item'}), 400
        errors = []
        for i, item in enumerate(items):
            if not item.get('description', '').strip():
                errors.append(f'items[{i}].description is required')
            if item.get('unit_price') is None:
                errors.append(f'items[{i}].unit_price is required')
        if errors:
            return jsonify({'errors': errors}), 400

        InvoiceItem.query.filter_by(invoice_id=inv.id).delete()
        _save_invoice_items_api(inv, items)

    # Regenerate PDF if it existed
    if inv.document_filename:
        _generate_invoice_pdf_api(inv, settings)

    db.session.commit()
    return jsonify({'invoice': _invoice_to_dict(inv)})


@api_bp.route('/invoices/<int:invoice_id>', methods=['DELETE'])
@require_api_key
def delete_invoice(invoice_id):
    """Delete an invoice. Fails if a payment transaction is linked."""
    inv = Invoice.query.get(invoice_id)
    if not inv:
        return jsonify({'error': f'Invoice {invoice_id} not found'}), 404

    if inv.linked_transaction_id:
        return jsonify({
            'error': 'Cannot delete an invoice with a linked payment. '
                     'Unmark payment first via POST /invoices/:id/unmark-paid.'
        }), 409

    # Unlink from quote
    if inv.quote_id:
        quote = Quote.query.get(inv.quote_id)
        if quote and quote.status == 'invoiced':
            other = Invoice.query.filter(
                Invoice.quote_id == quote.id,
                Invoice.id != inv.id
            ).count()
            if other == 0:
                quote.status = 'accepted'

    if inv.document_filename:
        _archive_document_api(inv.document_filename)

    db.session.delete(inv)
    db.session.commit()
    return jsonify({'deleted': True, 'id': invoice_id})


@api_bp.route('/invoices/<int:invoice_id>/status', methods=['POST', 'PUT'])
@require_api_key
def set_invoice_status(invoice_id):
    """
    Change invoice status (not paid – use mark-paid for that).
    Body: { "status": "draft" | "sent" | "cancelled" }
    """
    inv = Invoice.query.get(invoice_id)
    if not inv:
        return jsonify({'error': f'Invoice {invoice_id} not found'}), 404

    data = request.get_json(force=True)
    new_status = data.get('status', '').strip()
    allowed = ['draft', 'sent', 'cancelled']
    if new_status not in allowed:
        return jsonify({
            'error': f'Invalid status. Allowed: {", ".join(allowed)}. Use /mark-paid to set paid status.'
        }), 400

    if new_status == 'cancelled' and inv.linked_transaction_id:
        return jsonify({
            'error': 'Cannot cancel an invoice with a linked payment. Unmark payment first.'
        }), 409

    inv.status = new_status
    db.session.commit()
    return jsonify({'invoice': _invoice_to_dict(inv, include_items=False)})


@api_bp.route('/invoices/<int:invoice_id>/generate-pdf', methods=['POST'])
@require_api_key
def generate_invoice_pdf(invoice_id):
    """Generate or regenerate the invoice PDF."""
    inv = Invoice.query.get(invoice_id)
    if not inv:
        return jsonify({'error': f'Invoice {invoice_id} not found'}), 404

    settings = SiteSettings.get_settings()
    _generate_invoice_pdf_api(inv, settings)
    db.session.commit()

    return jsonify({
        'invoice_id': inv.id,
        'invoice_number': inv.invoice_number,
        'has_pdf': True,
        'message': 'PDF generated successfully.',
    })


@api_bp.route('/invoices/<int:invoice_id>/pdf', methods=['GET'])
@require_api_key
def download_invoice_pdf(invoice_id):
    """Download the invoice PDF."""
    inv = Invoice.query.get(invoice_id)
    if not inv:
        return jsonify({'error': f'Invoice {invoice_id} not found'}), 404

    if not inv.document_filename:
        return jsonify({'error': 'No PDF available. Generate it first via POST /invoices/:id/generate-pdf.'}), 404

    upload_folder = current_app.config['UPLOAD_FOLDER']
    fp = os.path.join(upload_folder, inv.document_filename)
    if not os.path.exists(fp):
        return jsonify({'error': 'PDF file not found on disk.'}), 404

    return send_from_directory(upload_folder, inv.document_filename,
                               as_attachment=False,
                               download_name=f'Rechnung_{inv.invoice_number}.pdf')


@api_bp.route('/invoices/<int:invoice_id>/mark-paid', methods=['POST'])
@require_api_key
def mark_invoice_paid(invoice_id):
    """
    Mark invoice as paid → creates an accounting transaction.
    Body: {
        account_id:     int       (required)
        category_id:    int       (optional)
        payment_date:   "YYYY-MM-DD" (optional, default today)
    }
    """
    inv = Invoice.query.get(invoice_id)
    if not inv:
        return jsonify({'error': f'Invoice {invoice_id} not found'}), 404

    if inv.status == 'paid':
        return jsonify({'error': 'Invoice is already marked as paid.'}), 409

    data = request.get_json(force=True)
    settings = SiteSettings.get_settings()

    account_id = data.get('account_id')
    if not account_id:
        return jsonify({'error': 'account_id is required'}), 400

    acc = Account.query.get(account_id)
    if not acc:
        return jsonify({'error': f'Account {account_id} not found'}), 404

    category_id = data.get('category_id')
    if category_id is not None:
        cat = Category.query.get(category_id)
        if not cat:
            return jsonify({'error': f'Category {category_id} not found'}), 404

    payment_date = date.today()
    if data.get('payment_date'):
        try:
            payment_date = parse_date(data['payment_date'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid payment_date format. Use YYYY-MM-DD.'}), 400

    # Calculate tax
    gross = inv.total
    tax_treatment = inv.tax_treatment or 'none'
    tax_rate = inv.tax_rate or 0

    if settings.tax_mode == 'kleinunternehmer':
        tax_treatment = 'none'
        tax_rate = 0

    if tax_rate > 0:
        net_amount, tax_amount = calculate_tax(gross, tax_rate)
    else:
        net_amount = gross
        tax_amount = 0

    tx = Transaction(
        date=payment_date,
        type='income',
        description=f'Rechnung {inv.invoice_number}'
                    + (f' – {inv.customer.display_name}' if inv.customer else ''),
        amount=gross,
        net_amount=net_amount,
        tax_amount=tax_amount,
        tax_treatment=tax_treatment,
        tax_rate=tax_rate if tax_rate > 0 else None,
        category_id=category_id,
        account_id=account_id,
        linked_asset_id=inv.linked_asset_id,
    )
    db.session.add(tx)
    db.session.flush()

    # Attach invoice PDF as document to transaction
    if inv.document_filename:
        doc = Document(
            filename=inv.document_filename,
            original_filename=f'Rechnung_{inv.invoice_number}.pdf',
            entity_type='transaction',
            entity_id=tx.id,
        )
        db.session.add(doc)

    inv.linked_transaction_id = tx.id
    inv.status = 'paid'

    db.session.commit()
    return jsonify({
        'invoice': _invoice_to_dict(inv, include_items=False),
        'transaction': _tx_to_dict(tx),
    })


@api_bp.route('/invoices/<int:invoice_id>/unmark-paid', methods=['POST'])
@require_api_key
def unmark_invoice_paid(invoice_id):
    """
    Reverse paid status → deletes the linked accounting transaction.
    Invoice status reverts to 'sent'.
    """
    inv = Invoice.query.get(invoice_id)
    if not inv:
        return jsonify({'error': f'Invoice {invoice_id} not found'}), 404

    if inv.status != 'paid' or not inv.linked_transaction_id:
        return jsonify({'error': 'Invoice is not marked as paid.'}), 409

    tx = Transaction.query.get(inv.linked_transaction_id)
    if tx:
        Document.query.filter_by(entity_type='transaction', entity_id=tx.id).delete()
        db.session.delete(tx)

    inv.linked_transaction_id = None
    inv.status = 'sent'

    db.session.commit()
    return jsonify({'invoice': _invoice_to_dict(inv, include_items=False)})
