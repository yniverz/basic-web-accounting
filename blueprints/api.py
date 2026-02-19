"""
REST API blueprint for programmatic access to accounting data.

Authentication: API key via header  Authorization: Bearer <API_KEY>
The API key is set via the environment variable API_KEY.

All responses are JSON. Monetary values are in EUR (float).
Dates are ISO 8601 (YYYY-MM-DD).
"""

import os
from datetime import date
from functools import wraps

from flask import Blueprint, jsonify, request

from helpers import (
    calculate_tax, get_tax_rate_for_treatment, parse_date,
    TAX_TREATMENT_LABELS,
)
from models import Category, SiteSettings, Transaction, db

api_bp = Blueprint('api', __name__)


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
        'notes': t.notes,
        'document_filename': t.document_filename,
        'created_at': t.created_at.isoformat() if t.created_at else None,
        'updated_at': t.updated_at.isoformat() if t.updated_at else None,
    }


def _cat_to_dict(c):
    """Serialize a Category to a dict."""
    return {
        'id': c.id,
        'name': c.name,
        'type': c.type,
        'description': c.description,
        'sort_order': c.sort_order,
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
    if type_filter in ('income', 'expense'):
        q = q.filter(Transaction.type == type_filter)

    cat_id = request.args.get('category_id', type=int)
    if cat_id:
        q = q.filter(Transaction.category_id == cat_id)

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
        category_id:    int                    (optional)
        tax_treatment:  string                 (optional, default "none")
        custom_tax_rate: number                (optional, only if tax_treatment="custom")
        notes:          string                 (optional)
    }
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
    """Delete a transaction by ID."""
    t = Transaction.query.get(tx_id)
    if not t:
        return jsonify({'error': f'Transaction {tx_id} not found'}), 404

    if t.document_filename:
        import os
        from flask import current_app
        fp = os.path.join(current_app.config['UPLOAD_FOLDER'], t.document_filename)
        if os.path.exists(fp):
            os.remove(fp)

    db.session.delete(t)
    db.session.commit()
    return jsonify({'deleted': True, 'id': tx_id})


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

    total_income = sum(t.amount for t in txs if t.type == 'income')
    total_expenses = sum(t.amount for t in txs if t.type == 'expense')
    total_income_net = sum((t.net_amount or t.amount) for t in txs if t.type == 'income')
    total_expenses_net = sum((t.net_amount or t.amount) for t in txs if t.type == 'expense')
    total_tax_income = sum((t.tax_amount or 0) for t in txs if t.type == 'income')
    total_tax_expenses = sum((t.tax_amount or 0) for t in txs if t.type == 'expense')

    monthly = {}
    for m in range(1, 13):
        mt = [t for t in txs if t.date.month == m]
        inc = sum(t.amount for t in mt if t.type == 'income')
        exp = sum(t.amount for t in mt if t.type == 'expense')
        monthly[str(m)] = {
            'income': round(inc, 2),
            'expenses': round(exp, 2),
            'profit': round(inc - exp, 2),
        }

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
    })
