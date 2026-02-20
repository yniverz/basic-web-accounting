import os
from datetime import date, datetime
from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app, send_from_directory
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from models import db, User, Transaction, Category, SiteSettings, Asset, DepreciationCategory, Account, Document, AuditLog
from werkzeug.security import generate_password_hash
from audit import archive_file, verify_integrity, repair_chain
from helpers import parse_date, parse_amount, calculate_tax, calculate_tax_from_net, get_year_choices, get_month_names, format_currency, TAX_TREATMENT_LABELS, get_tax_rate_for_treatment
from depreciation import (
    get_depreciation_schedule, get_depreciation_for_year, get_book_value,
    get_disposal_result, suggest_method, DEPRECIATION_METHODS, USEFUL_LIFE_PRESETS, RULES
)

admin_bp = Blueprint('admin', __name__, template_folder='../templates/admin')

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@admin_bp.before_request
@login_required
def require_login():
    """All admin routes require authentication."""
    pass


# --- Dashboard ---

@admin_bp.route('/')
def dashboard():
    year = request.args.get('year', date.today().year, type=int)

    # Query transactions for the selected year
    transactions = Transaction.query.filter(
        db.extract('year', Transaction.date) == year
    ).order_by(Transaction.date.desc()).all()

    total_income = sum(t.amount for t in transactions if t.type == 'income')
    total_expenses = sum(t.amount for t in transactions if t.type == 'expense')
    profit = total_income - total_expenses

    # Monthly breakdown
    months = get_month_names()
    monthly_data = {}
    for m in range(1, 13):
        month_transactions = [t for t in transactions if t.date.month == m]
        monthly_data[m] = {
            'name': months[m],
            'income': sum(t.amount for t in month_transactions if t.type == 'income'),
            'expenses': sum(t.amount for t in month_transactions if t.type == 'expense'),
        }
        monthly_data[m]['profit'] = monthly_data[m]['income'] - monthly_data[m]['expenses']

    # Recent transactions (last 10)
    recent = transactions[:10]

    # Account balances
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()

    return render_template('dashboard.html',
                           year=year,
                           years=get_year_choices(),
                           total_income=total_income,
                           total_expenses=total_expenses,
                           profit=profit,
                           monthly_data=monthly_data,
                           recent_transactions=recent,
                           transaction_count=len(transactions),
                           accounts=accounts)


# --- Transactions ---

@admin_bp.route('/transactions')
def transactions():
    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', 0, type=int)
    type_filter = request.args.get('type', '')
    category_id = request.args.get('category', 0, type=int)
    account_id = request.args.get('account', 0, type=int)

    query = Transaction.query.filter(db.extract('year', Transaction.date) == year)

    if month > 0:
        query = query.filter(db.extract('month', Transaction.date) == month)
    if type_filter in ('income', 'expense', 'transfer'):
        query = query.filter(Transaction.type == type_filter)
    if category_id > 0:
        query = query.filter(Transaction.category_id == category_id)
    if account_id > 0:
        query = query.filter(
            (Transaction.account_id == account_id) | (Transaction.transfer_to_account_id == account_id)
        )

    transactions_list = query.order_by(Transaction.date.desc()).all()
    categories = Category.query.order_by(Category.sort_order, Category.name).all()
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()

    total_income = sum(t.amount for t in transactions_list if t.type == 'income')
    total_expenses = sum(t.amount for t in transactions_list if t.type == 'expense')

    return render_template('transactions.html',
                           transactions=transactions_list,
                           categories=categories,
                           accounts=accounts,
                           year=year,
                           month=month,
                           type_filter=type_filter,
                           category_id=category_id,
                           account_id=account_id,
                           years=get_year_choices(),
                           months=get_month_names(),
                           total_income=total_income,
                           total_expenses=total_expenses)


@admin_bp.route('/transactions/new', methods=['GET', 'POST'])
def transaction_new():
    if request.method == 'POST':
        try:
            t = Transaction(
                date=parse_date(request.form.get('date')),
                type=request.form.get('type', 'expense'),
                description=request.form.get('description', '').strip(),
                amount=0,  # will be set below
                category_id=request.form.get('category_id', type=int) or None,
                account_id=request.form.get('account_id', type=int) or None,
                notes=request.form.get('notes', '').strip() or None,
            )

            # Tax treatment
            settings = SiteSettings.get_settings()
            tax_treatment = request.form.get('tax_treatment', 'none')
            if settings.tax_mode == 'kleinunternehmer':
                tax_treatment = 'none'
            t.tax_treatment = tax_treatment

            # Determine effective tax rate
            custom_rate = parse_amount(request.form.get('custom_tax_rate', '0'))
            effective_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)
            t.tax_rate = effective_rate

            # Handle brutto/netto input mode
            input_mode = request.form.get('input_mode', 'gross')
            if input_mode == 'net':
                net_input = parse_amount(request.form.get('net_input'))
                if effective_rate > 0:
                    gross, tax = calculate_tax_from_net(net_input, effective_rate)
                    t.amount = gross
                    t.net_amount = net_input
                    t.tax_amount = tax
                else:
                    t.amount = net_input
                    t.net_amount = net_input
                    t.tax_amount = 0.0
            else:
                t.amount = parse_amount(request.form.get('amount'))
                if effective_rate > 0:
                    t.net_amount, t.tax_amount = calculate_tax(t.amount, effective_rate)
                else:
                    t.net_amount = t.amount
                    t.tax_amount = 0.0

            db.session.add(t)
            db.session.flush()  # get ID for document links

            # File uploads (multiple)
            files = request.files.getlist('documents')
            for file in files:
                if file and file.filename and allowed_file(file.filename):
                    stored = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                    file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], stored))
                    doc = Document(filename=stored, original_filename=file.filename,
                                   entity_type='transaction', entity_id=t.id)
                    db.session.add(doc)

            db.session.commit()
            flash('Buchung wurde erstellt.', 'success')
            return redirect(url_for('admin.transactions'))
        except Exception as e:
            flash(f'Fehler beim Erstellen: {str(e)}', 'error')

    categories = Category.query.order_by(Category.sort_order, Category.name).all()
    settings = SiteSettings.get_settings()
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()
    return render_template('transaction_form.html',
                           transaction=None,
                           categories=categories,
                           accounts=accounts,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat())


@admin_bp.route('/transactions/<int:id>/edit', methods=['GET', 'POST'])
def transaction_edit(id):
    t = Transaction.query.get_or_404(id)

    # Linked asset transactions cannot be edited directly
    if t.linked_asset_id:
        flash('Diese Buchung ist mit einem Anlagegut verknüpft und kann nicht direkt bearbeitet werden.', 'error')
        return redirect(url_for('admin.transactions'))

    # Transfer transactions cannot be edited via the regular form
    if t.type == 'transfer':
        flash('Umbuchungen können nicht über das Buchungsformular bearbeitet werden.', 'error')
        return redirect(url_for('admin.transactions'))

    if request.method == 'POST':
        try:
            t.date = parse_date(request.form.get('date'))
            t.type = request.form.get('type', 'expense')
            t.description = request.form.get('description', '').strip()
            t.category_id = request.form.get('category_id', type=int) or None
            t.account_id = request.form.get('account_id', type=int) or None
            t.notes = request.form.get('notes', '').strip() or None

            # Tax treatment
            settings = SiteSettings.get_settings()
            tax_treatment = request.form.get('tax_treatment', 'none')
            if settings.tax_mode == 'kleinunternehmer':
                tax_treatment = 'none'
            t.tax_treatment = tax_treatment

            # Determine effective tax rate
            custom_rate = parse_amount(request.form.get('custom_tax_rate', '0'))
            effective_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)
            t.tax_rate = effective_rate

            # Handle brutto/netto input mode
            input_mode = request.form.get('input_mode', 'gross')
            if input_mode == 'net':
                net_input = parse_amount(request.form.get('net_input'))
                if effective_rate > 0:
                    gross, tax = calculate_tax_from_net(net_input, effective_rate)
                    t.amount = gross
                    t.net_amount = net_input
                    t.tax_amount = tax
                else:
                    t.amount = net_input
                    t.net_amount = net_input
                    t.tax_amount = 0.0
            else:
                t.amount = parse_amount(request.form.get('amount'))
                if effective_rate > 0:
                    t.net_amount, t.tax_amount = calculate_tax(t.amount, effective_rate)
                else:
                    t.net_amount = t.amount
                    t.tax_amount = 0.0

            # Handle document removals
            remove_doc_ids = request.form.getlist('remove_documents')
            for doc_id in remove_doc_ids:
                doc = Document.query.get(int(doc_id))
                if doc and doc.entity_type == 'transaction' and doc.entity_id == t.id:
                    archive_file(current_app.config['UPLOAD_FOLDER'], doc.filename)
                    db.session.delete(doc)

            # File uploads (multiple, appended)
            files = request.files.getlist('documents')
            for file in files:
                if file and file.filename and allowed_file(file.filename):
                    stored = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                    file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], stored))
                    doc = Document(filename=stored, original_filename=file.filename,
                                   entity_type='transaction', entity_id=t.id)
                    db.session.add(doc)

            db.session.commit()
            flash('Buchung wurde aktualisiert.', 'success')
            return redirect(url_for('admin.transactions'))
        except Exception as e:
            flash(f'Fehler beim Aktualisieren: {str(e)}', 'error')

    categories = Category.query.order_by(Category.sort_order, Category.name).all()
    settings = SiteSettings.get_settings()
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()
    return render_template('transaction_form.html',
                           transaction=t,
                           categories=categories,
                           accounts=accounts,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat())


@admin_bp.route('/transactions/<int:id>/delete', methods=['POST'])
def transaction_delete(id):
    t = Transaction.query.get_or_404(id)
    # Linked asset transactions cannot be deleted directly
    if t.linked_asset_id:
        flash('Diese Buchung ist mit einem Anlagegut verknüpft und kann nicht direkt gelöscht werden.', 'error')
        return redirect(url_for('admin.transactions'))
    # Archive all attached documents
    for doc in Document.query.filter_by(entity_type='transaction', entity_id=t.id).all():
        archive_file(current_app.config['UPLOAD_FOLDER'], doc.filename)
        db.session.delete(doc)
    db.session.delete(t)
    db.session.commit()
    flash('Buchung wurde gelöscht.', 'success')
    return redirect(url_for('admin.transactions'))


@admin_bp.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], filename)


# --- Categories ---

@admin_bp.route('/categories')
def categories():
    income_cats = Category.query.filter_by(type='income').order_by(Category.sort_order, Category.name).all()
    expense_cats = Category.query.filter_by(type='expense').order_by(Category.sort_order, Category.name).all()
    return render_template('categories.html',
                           income_categories=income_cats,
                           expense_categories=expense_cats)


@admin_bp.route('/categories/new', methods=['GET', 'POST'])
def category_new():
    if request.method == 'POST':
        cat = Category(
            name=request.form.get('name', '').strip(),
            type=request.form.get('type', 'expense'),
            description=request.form.get('description', '').strip() or None,
            sort_order=request.form.get('sort_order', 0, type=int),
        )
        db.session.add(cat)
        db.session.commit()
        flash('Kategorie wurde erstellt.', 'success')
        return redirect(url_for('admin.categories'))

    return render_template('category_form.html', category=None)


@admin_bp.route('/categories/<int:id>/edit', methods=['GET', 'POST'])
def category_edit(id):
    cat = Category.query.get_or_404(id)
    if request.method == 'POST':
        cat.name = request.form.get('name', '').strip()
        cat.type = request.form.get('type', 'expense')
        cat.description = request.form.get('description', '').strip() or None
        cat.sort_order = request.form.get('sort_order', 0, type=int)
        db.session.commit()
        flash('Kategorie wurde aktualisiert.', 'success')
        return redirect(url_for('admin.categories'))

    return render_template('category_form.html', category=cat)


@admin_bp.route('/categories/<int:id>/delete', methods=['POST'])
def category_delete(id):
    cat = Category.query.get_or_404(id)
    # Unlink transactions first
    Transaction.query.filter_by(category_id=id).update({'category_id': None})
    db.session.delete(cat)
    db.session.commit()
    flash('Kategorie wurde gelöscht.', 'success')
    return redirect(url_for('admin.categories'))


# --- Depreciation Categories (AfA-Kategorien) ---

@admin_bp.route('/depreciation-categories')
def depreciation_categories():
    cats = DepreciationCategory.query.order_by(DepreciationCategory.sort_order, DepreciationCategory.name).all()
    return render_template('depreciation_categories.html',
                           categories=cats,
                           methods=DEPRECIATION_METHODS)


@admin_bp.route('/depreciation-categories/new', methods=['GET', 'POST'])
def depreciation_category_new():
    if request.method == 'POST':
        cat = DepreciationCategory(
            name=request.form.get('name', '').strip(),
            useful_life_months=request.form.get('useful_life_months', 36, type=int),
            default_method=request.form.get('default_method', 'linear'),
            description=request.form.get('description', '').strip() or None,
            sort_order=request.form.get('sort_order', 0, type=int),
        )
        db.session.add(cat)
        db.session.commit()
        flash('AfA-Kategorie wurde erstellt.', 'success')
        return redirect(url_for('admin.depreciation_categories'))

    return render_template('depreciation_category_form.html',
                           category=None,
                           methods=DEPRECIATION_METHODS)


@admin_bp.route('/depreciation-categories/<int:id>/edit', methods=['GET', 'POST'])
def depreciation_category_edit(id):
    cat = DepreciationCategory.query.get_or_404(id)
    if request.method == 'POST':
        cat.name = request.form.get('name', '').strip()
        cat.useful_life_months = request.form.get('useful_life_months', 36, type=int)
        cat.default_method = request.form.get('default_method', 'linear')
        cat.description = request.form.get('description', '').strip() or None
        cat.sort_order = request.form.get('sort_order', 0, type=int)
        db.session.commit()
        flash('AfA-Kategorie wurde aktualisiert.', 'success')
        return redirect(url_for('admin.depreciation_categories'))

    return render_template('depreciation_category_form.html',
                           category=cat,
                           methods=DEPRECIATION_METHODS)


@admin_bp.route('/depreciation-categories/<int:id>/delete', methods=['POST'])
def depreciation_category_delete(id):
    cat = DepreciationCategory.query.get_or_404(id)
    # Unlink assets from this category
    Asset.query.filter_by(depreciation_category_id=id).update({'depreciation_category_id': None})
    db.session.delete(cat)
    db.session.commit()
    flash('AfA-Kategorie wurde gelöscht.', 'success')
    return redirect(url_for('admin.depreciation_categories'))


# --- Accounts (Konten) ---

@admin_bp.route('/accounts')
def accounts():
    accounts_list = Account.query.order_by(Account.sort_order, Account.name).all()
    for acc in accounts_list:
        acc._balance = acc.get_balance()
    return render_template('accounts.html', accounts=accounts_list)


@admin_bp.route('/accounts/new', methods=['GET', 'POST'])
def account_new():
    if request.method == 'POST':
        acc = Account(
            name=request.form.get('name', '').strip(),
            description=request.form.get('description', '').strip() or None,
            initial_balance=parse_amount(request.form.get('initial_balance', '0')),
            sort_order=request.form.get('sort_order', 0, type=int),
        )
        db.session.add(acc)
        db.session.commit()
        flash('Konto wurde erstellt.', 'success')
        return redirect(url_for('admin.accounts'))
    return render_template('account_form.html', account=None)


@admin_bp.route('/accounts/<int:id>/edit', methods=['GET', 'POST'])
def account_edit(id):
    acc = Account.query.get_or_404(id)
    if request.method == 'POST':
        acc.name = request.form.get('name', '').strip()
        acc.description = request.form.get('description', '').strip() or None
        acc.initial_balance = parse_amount(request.form.get('initial_balance', '0'))
        acc.sort_order = request.form.get('sort_order', 0, type=int)
        db.session.commit()
        flash('Konto wurde aktualisiert.', 'success')
        return redirect(url_for('admin.accounts'))
    return render_template('account_form.html', account=acc)


@admin_bp.route('/accounts/<int:id>/delete', methods=['POST'])
def account_delete(id):
    acc = Account.query.get_or_404(id)
    # Check if there are transactions on this account
    tx_count = Transaction.query.filter(
        (Transaction.account_id == id) | (Transaction.transfer_to_account_id == id)
    ).count()
    if tx_count > 0:
        flash(f'Konto kann nicht gelöscht werden – es sind noch {tx_count} Buchungen zugeordnet.', 'error')
        return redirect(url_for('admin.accounts'))
    db.session.delete(acc)
    db.session.commit()
    flash('Konto wurde gelöscht.', 'success')
    return redirect(url_for('admin.accounts'))


@admin_bp.route('/accounts/<int:id>')
def account_detail(id):
    acc = Account.query.get_or_404(id)
    year = request.args.get('year', date.today().year, type=int)

    # Get all transactions for this account (as source or transfer target)
    txns = Transaction.query.filter(
        (Transaction.account_id == id) | (Transaction.transfer_to_account_id == id)
    ).filter(
        db.extract('year', Transaction.date) == year
    ).order_by(Transaction.date.asc(), Transaction.id.asc()).all()

    # Calculate running balance
    balance = acc.get_balance(up_to_date=date(year - 1, 12, 31))
    running = []
    for t in txns:
        if t.type == 'transfer':
            if t.account_id == id:
                balance -= t.amount
                direction = 'out'
            else:
                balance += t.amount
                direction = 'in'
        elif t.type == 'income':
            if t.account_id == id:
                balance += t.amount
                direction = 'in'
            else:
                direction = 'in'
        else:  # expense
            if t.account_id == id:
                balance -= t.amount
                direction = 'out'
            else:
                direction = 'out'
        running.append({'tx': t, 'balance': balance, 'direction': direction})

    total_balance = acc.get_balance()

    return render_template('account_detail.html',
                           account=acc,
                           running=running,
                           total_balance=total_balance,
                           year=year,
                           years=get_year_choices())


# --- Transfers (Umbuchungen) ---

@admin_bp.route('/transfers/new', methods=['GET', 'POST'])
def transfer_new():
    if request.method == 'POST':
        try:
            from_id = request.form.get('from_account_id', type=int)
            to_id = request.form.get('to_account_id', type=int)
            if not from_id or not to_id or from_id == to_id:
                flash('Bitte zwei verschiedene Konten auswählen.', 'error')
            else:
                amount = parse_amount(request.form.get('amount'))
                t = Transaction(
                    date=parse_date(request.form.get('date')),
                    type='transfer',
                    description=request.form.get('description', '').strip() or 'Umbuchung',
                    amount=amount,
                    net_amount=amount,
                    tax_amount=0.0,
                    tax_treatment='none',
                    tax_rate=0.0,
                    account_id=from_id,
                    transfer_to_account_id=to_id,
                    notes=request.form.get('notes', '').strip() or None,
                )
                db.session.add(t)
                db.session.commit()
                flash('Umbuchung wurde erstellt.', 'success')
                return redirect(url_for('admin.transactions'))
        except Exception as e:
            flash(f'Fehler bei der Umbuchung: {str(e)}', 'error')

    accounts = Account.query.order_by(Account.sort_order, Account.name).all()
    return render_template('transfer_form.html',
                           accounts=accounts,
                           today=date.today().isoformat())


# --- Assets (Anlagegüter / AfA) ---

@admin_bp.route('/assets')
def assets():
    status_filter = request.args.get('status', 'active')  # active, disposed, all
    query = Asset.query

    if status_filter == 'active':
        query = query.filter(Asset.disposal_date.is_(None))
    elif status_filter == 'disposed':
        query = query.filter(Asset.disposal_date.isnot(None))

    assets_list = query.order_by(Asset.purchase_date.desc(), Asset.id).all()

    # Calculate book values
    for asset in assets_list:
        asset._book_value = get_book_value(asset)

    # Group by bundle_id for display
    from collections import OrderedDict
    bundles = OrderedDict()  # bundle_id -> list of assets
    standalone = []          # assets without bundle
    for a in assets_list:
        if a.bundle_id:
            bundles.setdefault(a.bundle_id, []).append(a)
        else:
            standalone.append(a)

    total_purchase = sum(a.purchase_price_net for a in assets_list)
    total_book_value = sum(a._book_value for a in assets_list if a.disposal_date is None)

    return render_template('assets.html',
                           assets=assets_list,
                           bundles=bundles,
                           standalone=standalone,
                           status_filter=status_filter,
                           total_purchase=total_purchase,
                           total_book_value=total_book_value,
                           methods=DEPRECIATION_METHODS)


@admin_bp.route('/assets/new', methods=['GET', 'POST'])
def asset_new():
    if request.method == 'POST':
        try:
            settings = SiteSettings.get_settings()
            tax_treatment = request.form.get('purchase_tax_treatment', 'none')
            if settings.tax_mode == 'kleinunternehmer':
                tax_treatment = 'none'

            custom_rate = parse_amount(request.form.get('purchase_custom_tax_rate', '0'))
            effective_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)

            # Handle brutto/netto input mode
            input_mode = request.form.get('purchase_input_mode', 'gross')
            if input_mode == 'net':
                net_val = parse_amount(request.form.get('purchase_price_net'))
                if effective_rate > 0:
                    gross_val, tax_val = calculate_tax_from_net(net_val, effective_rate)
                else:
                    gross_val = net_val
                    tax_val = 0.0
            else:
                gross_val = parse_amount(request.form.get('purchase_price_gross'))
                if effective_rate > 0:
                    net_val, tax_val = calculate_tax(gross_val, effective_rate)
                else:
                    net_val = gross_val
                    tax_val = 0.0

            quantity = max(1, request.form.get('quantity', 1, type=int))
            base_name = request.form.get('name', '').strip()

            # For bundles: gross/net/tax are total → compute per-unit
            if quantity > 1:
                import uuid
                bundle_id = str(uuid.uuid4())
                unit_gross = round(gross_val / quantity, 2)
                unit_net = round(net_val / quantity, 2)
                unit_tax = round(tax_val / quantity, 2)
            else:
                bundle_id = None
                unit_gross = gross_val
                unit_net = net_val
                unit_tax = tax_val

            # File uploads (shared documents for all bundle items)
            uploaded_docs = []
            files = request.files.getlist('documents')
            for file in files:
                if file and file.filename and allowed_file(file.filename):
                    stored = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                    file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], stored))
                    uploaded_docs.append((stored, file.filename))

            created_ids = []
            for i in range(quantity):
                item_name = f"{base_name} ({i+1}/{quantity})" if quantity > 1 else base_name
                asset = Asset(
                    name=item_name,
                    description=request.form.get('description', '').strip() or None,
                    bundle_id=bundle_id,
                    purchase_date=parse_date(request.form.get('purchase_date')),
                    purchase_price_gross=unit_gross,
                    purchase_price_net=unit_net,
                    purchase_tax_treatment=tax_treatment,
                    purchase_tax_rate=effective_rate,
                    purchase_tax_amount=unit_tax,
                    depreciation_method=request.form.get('depreciation_method', 'linear'),
                    useful_life_months=request.form.get('useful_life_months', type=int) or None,
                    salvage_value=parse_amount(request.form.get('salvage_value', '0')),
                    depreciation_category_id=request.form.get('depreciation_category_id', type=int) or None,
                    notes=request.form.get('notes', '').strip() or None,
                )
                db.session.add(asset)
                db.session.flush()
                created_ids.append(asset.id)

                # Attach uploaded documents to each asset
                for stored_name, orig_name in uploaded_docs:
                    doc = Document(filename=stored_name, original_filename=orig_name,
                                   entity_type='asset', entity_id=asset.id)
                    db.session.add(doc)

            # Optional: Create linked cash outflow transaction
            book_outflow = request.form.get('book_outflow') == '1'
            outflow_account_id = request.form.get('outflow_account_id', type=int)
            if book_outflow and outflow_account_id:
                # Link to first asset (or bundle representative)
                link_asset_id = created_ids[0]
                outflow_tx = Transaction(
                    date=parse_date(request.form.get('purchase_date')),
                    type='expense',
                    description=f'Anlagekauf: {base_name}',
                    amount=gross_val,
                    net_amount=net_val,
                    tax_amount=tax_val,
                    tax_treatment=tax_treatment,
                    tax_rate=effective_rate,
                    account_id=outflow_account_id,
                    linked_asset_id=link_asset_id,
                    notes=f'Automatisch erstellt bei Anlage von Anlagegut',
                )
                db.session.add(outflow_tx)

            db.session.commit()

            if quantity > 1:
                flash(f'{quantity} Anlagegüter als Bündel erstellt.', 'success')
                return redirect(url_for('admin.assets'))
            else:
                flash('Anlagegut wurde erstellt.', 'success')
                return redirect(url_for('admin.asset_detail', id=created_ids[0]))
        except Exception as e:
            flash(f'Fehler beim Erstellen: {str(e)}', 'error')

    dep_cats = DepreciationCategory.query.order_by(DepreciationCategory.sort_order, DepreciationCategory.name).all()
    settings = SiteSettings.get_settings()
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()
    return render_template('asset_form.html',
                           asset=None,
                           methods=DEPRECIATION_METHODS,
                           depreciation_categories=dep_cats,
                           accounts=accounts,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           rules=RULES,
                           today=date.today().isoformat())


@admin_bp.route('/assets/<int:id>')
def asset_detail(id):
    asset = Asset.query.get_or_404(id)
    schedule = get_depreciation_schedule(asset)
    book_value = get_book_value(asset)
    disposal_result = get_disposal_result(asset)

    # Bundle info
    bundle_count = 0
    bundle_active = 0
    if asset.bundle_id:
        siblings = Asset.query.filter_by(bundle_id=asset.bundle_id).all()
        bundle_count = len(siblings)
        bundle_active = sum(1 for s in siblings if s.disposal_date is None)

    settings = SiteSettings.get_settings()
    purchase_tx = Transaction.query.filter_by(linked_asset_id=asset.id, type='expense').first()
    disposal_tx = Transaction.query.filter_by(linked_asset_id=asset.id, type='income').first()
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()
    return render_template('asset_detail.html',
                           asset=asset,
                           schedule=schedule,
                           book_value=book_value,
                           disposal_result=disposal_result,
                           methods=DEPRECIATION_METHODS,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           current_year=date.today().year,
                           bundle_count=bundle_count,
                           bundle_active=bundle_active,
                           purchase_tx=purchase_tx,
                           disposal_tx=disposal_tx,
                           accounts=accounts)


@admin_bp.route('/assets/<int:id>/book-outflow', methods=['POST'])
def asset_book_outflow(id):
    asset = Asset.query.get_or_404(id)

    # Bundle members must use the bundle detail page
    if asset.bundle_id:
        flash('Dieses Anlagegut ist Teil eines Bündels. Bitte die Kontobuchung über die Bündel-Detailseite verwalten.', 'warning')
        return redirect(url_for('admin.asset_detail', id=asset.id))

    # Check if already has a linked purchase transaction
    existing = Transaction.query.filter_by(linked_asset_id=asset.id, type='expense').first()
    if existing:
        flash('Es existiert bereits eine verknüpfte Kaufbuchung für dieses Anlagegut.', 'warning')
        return redirect(url_for('admin.asset_detail', id=asset.id))

    account_id = request.form.get('outflow_account_id', type=int)
    if not account_id:
        flash('Bitte ein Konto auswählen.', 'error')
        return redirect(url_for('admin.asset_detail', id=asset.id))

    try:
        outflow_tx = Transaction(
            date=asset.purchase_date,
            type='expense',
            description=f'Anlagekauf: {asset.name}',
            amount=asset.purchase_price_gross,
            net_amount=asset.purchase_price_net,
            tax_amount=asset.purchase_tax_amount or 0,
            tax_treatment=asset.purchase_tax_treatment or 'none',
            tax_rate=asset.purchase_tax_rate or 0,
            account_id=account_id,
            linked_asset_id=asset.id,
            notes='Automatisch erstellt bei nachträglicher Kontobuchung',
        )
        db.session.add(outflow_tx)
        db.session.commit()
        flash('Kontoabgang wurde gebucht.', 'success')
    except Exception as e:
        flash(f'Fehler: {str(e)}', 'error')

    return redirect(url_for('admin.asset_detail', id=asset.id))


@admin_bp.route('/assets/<int:id>/unlink-outflow', methods=['POST'])
def asset_unlink_outflow(id):
    asset = Asset.query.get_or_404(id)

    # Bundle members must use the bundle detail page
    if asset.bundle_id:
        flash('Dieses Anlagegut ist Teil eines Bündels. Bitte die Kontobuchung über die Bündel-Detailseite verwalten.', 'warning')
        return redirect(url_for('admin.asset_detail', id=asset.id))
    deleted = Transaction.query.filter_by(linked_asset_id=asset.id, type='expense').delete()
    db.session.commit()
    if deleted:
        flash('Verknüpfte Kontobuchung wurde entfernt.', 'success')
    else:
        flash('Keine verknüpfte Buchung gefunden.', 'warning')
    return redirect(url_for('admin.asset_detail', id=asset.id))


@admin_bp.route('/assets/<int:id>/book-disposal-inflow', methods=['POST'])
def asset_book_disposal_inflow(id):
    """Book disposal proceeds as income transaction for an asset."""
    asset = Asset.query.get_or_404(id)
    if not asset.disposal_date:
        flash('Dieses Anlagegut hat keinen Abgang.', 'warning')
        return redirect(url_for('admin.asset_detail', id=asset.id))

    existing = Transaction.query.filter_by(linked_asset_id=asset.id, type='income').first()
    if existing:
        flash('Es existiert bereits eine Veräußerungsbuchung für dieses Anlagegut.', 'warning')
        return redirect(url_for('admin.asset_detail', id=asset.id))

    account_id = request.form.get('inflow_account_id', type=int)
    if not account_id:
        flash('Bitte ein Konto auswählen.', 'error')
        return redirect(url_for('admin.asset_detail', id=asset.id))

    try:
        inflow_tx = Transaction(
            date=asset.disposal_date,
            type='income',
            description=f'Veräußerung: {asset.name}',
            amount=asset.disposal_price_gross or 0,
            net_amount=asset.disposal_price or asset.disposal_price_gross or 0,
            tax_amount=asset.disposal_tax_amount or 0,
            tax_treatment=asset.disposal_tax_treatment or 'none',
            tax_rate=asset.disposal_tax_rate or 0,
            account_id=account_id,
            linked_asset_id=asset.id,
            notes='Nachträglich erstellt - Veräußerungserlös',
        )
        db.session.add(inflow_tx)
        db.session.commit()
        flash('Veräußerungserlös wurde auf Konto gebucht.', 'success')
    except Exception as e:
        flash(f'Fehler: {str(e)}', 'error')

    return redirect(url_for('admin.asset_detail', id=asset.id))


@admin_bp.route('/assets/<int:id>/unlink-disposal-inflow', methods=['POST'])
def asset_unlink_disposal_inflow(id):
    """Remove disposal inflow transaction for an asset."""
    asset = Asset.query.get_or_404(id)
    deleted = Transaction.query.filter_by(linked_asset_id=asset.id, type='income').delete()
    db.session.commit()
    if deleted:
        flash('Veräußerungsbuchung wurde entfernt.', 'success')
    else:
        flash('Keine Veräußerungsbuchung gefunden.', 'warning')
    return redirect(url_for('admin.asset_detail', id=asset.id))


@admin_bp.route('/assets/<int:id>/edit', methods=['GET', 'POST'])
def asset_edit(id):
    asset = Asset.query.get_or_404(id)

    if request.method == 'POST':
        try:
            settings = SiteSettings.get_settings()
            tax_treatment = request.form.get('purchase_tax_treatment', 'none')
            if settings.tax_mode == 'kleinunternehmer':
                tax_treatment = 'none'

            custom_rate = parse_amount(request.form.get('purchase_custom_tax_rate', '0'))
            effective_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)

            # Handle brutto/netto input mode
            input_mode = request.form.get('purchase_input_mode', 'gross')
            if input_mode == 'net':
                net_val = parse_amount(request.form.get('purchase_price_net'))
                if effective_rate > 0:
                    gross_val, tax_val = calculate_tax_from_net(net_val, effective_rate)
                else:
                    gross_val = net_val
                    tax_val = 0.0
            else:
                gross_val = parse_amount(request.form.get('purchase_price_gross'))
                if effective_rate > 0:
                    net_val, tax_val = calculate_tax(gross_val, effective_rate)
                else:
                    net_val = gross_val
                    tax_val = 0.0

            asset.name = request.form.get('name', '').strip()
            asset.description = request.form.get('description', '').strip() or None
            asset.purchase_date = parse_date(request.form.get('purchase_date'))
            asset.purchase_price_gross = gross_val
            asset.purchase_price_net = net_val
            asset.purchase_tax_treatment = tax_treatment
            asset.purchase_tax_rate = effective_rate
            asset.purchase_tax_amount = tax_val
            asset.depreciation_method = request.form.get('depreciation_method', 'linear')
            asset.useful_life_months = request.form.get('useful_life_months', type=int) or None
            asset.salvage_value = parse_amount(request.form.get('salvage_value', '0'))
            asset.depreciation_category_id = request.form.get('depreciation_category_id', type=int) or None
            asset.notes = request.form.get('notes', '').strip() or None

            # Handle document removals
            remove_doc_ids = request.form.getlist('remove_documents')
            for doc_id in remove_doc_ids:
                doc = Document.query.get(int(doc_id))
                if doc and doc.entity_type == 'asset' and doc.entity_id == asset.id:
                    archive_file(current_app.config['UPLOAD_FOLDER'], doc.filename)
                    db.session.delete(doc)

            # File uploads (multiple, appended)
            files = request.files.getlist('documents')
            for file in files:
                if file and file.filename and allowed_file(file.filename):
                    stored = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                    file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], stored))
                    doc = Document(filename=stored, original_filename=file.filename,
                                   entity_type='asset', entity_id=asset.id)
                    db.session.add(doc)

            db.session.commit()
            flash('Anlagegut wurde aktualisiert.', 'success')
            return redirect(url_for('admin.asset_detail', id=asset.id))
        except Exception as e:
            flash(f'Fehler beim Aktualisieren: {str(e)}', 'error')

    dep_cats = DepreciationCategory.query.order_by(DepreciationCategory.sort_order, DepreciationCategory.name).all()
    settings = SiteSettings.get_settings()
    return render_template('asset_form.html',
                           asset=asset,
                           methods=DEPRECIATION_METHODS,
                           depreciation_categories=dep_cats,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           rules=RULES,
                           today=date.today().isoformat())


@admin_bp.route('/assets/<int:id>/dispose', methods=['GET', 'POST'])
def asset_dispose(id):
    asset = Asset.query.get_or_404(id)
    is_edit = asset.disposal_date is not None

    if request.method == 'POST':
        try:
            settings = SiteSettings.get_settings()
            tax_treatment = request.form.get('disposal_tax_treatment', 'none')
            if settings.tax_mode == 'kleinunternehmer':
                tax_treatment = 'none'

            custom_rate = parse_amount(request.form.get('disposal_custom_tax_rate', '0'))
            effective_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)

            # Handle brutto/netto input mode
            input_mode = request.form.get('disposal_input_mode', 'gross')
            if input_mode == 'net':
                net_val = parse_amount(request.form.get('disposal_price', '0'))
                if effective_rate > 0:
                    gross_val, tax_val = calculate_tax_from_net(net_val, effective_rate)
                else:
                    gross_val = net_val
                    tax_val = 0.0
            else:
                gross_val = parse_amount(request.form.get('disposal_price_gross', '0'))
                if effective_rate > 0:
                    net_val, tax_val = calculate_tax(gross_val, effective_rate)
                else:
                    net_val = gross_val
                    tax_val = 0.0

            disposal_date = parse_date(request.form.get('disposal_date'))
            asset.disposal_date = disposal_date
            asset.disposal_price_gross = gross_val
            asset.disposal_price = net_val
            asset.disposal_tax_treatment = tax_treatment
            asset.disposal_tax_rate = effective_rate
            asset.disposal_tax_amount = tax_val
            asset.disposal_reason = request.form.get('disposal_reason', 'sold')

            # Handle disposal account booking (create/update/remove)
            book_inflow = request.form.get('book_inflow') == '1'
            inflow_account_id = request.form.get('inflow_account_id', type=int)
            # Remove existing disposal transaction if any
            Transaction.query.filter_by(linked_asset_id=asset.id, type='income').delete()
            if book_inflow and inflow_account_id and gross_val > 0:
                inflow_tx = Transaction(
                    date=disposal_date,
                    type='income',
                    description=f'Veräußerung: {asset.name}',
                    amount=gross_val,
                    net_amount=net_val,
                    tax_amount=tax_val,
                    tax_treatment=tax_treatment,
                    tax_rate=effective_rate,
                    account_id=inflow_account_id,
                    linked_asset_id=asset.id,
                    notes='Automatisch erstellt bei Veräußerung',
                )
                db.session.add(inflow_tx)

            db.session.commit()
            flash('Abgang wurde ' + ('aktualisiert.' if is_edit else 'erfasst.'), 'success')
            return redirect(url_for('admin.asset_detail', id=asset.id))
        except Exception as e:
            flash(f'Fehler: {str(e)}', 'error')

    book_value = get_book_value(asset)
    settings = SiteSettings.get_settings()
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()
    existing_disposal_tx = Transaction.query.filter_by(linked_asset_id=asset.id, type='income').first() if is_edit else None
    return render_template('asset_dispose.html',
                           asset=asset,
                           book_value=book_value,
                           is_edit=is_edit,
                           settings=settings,
                           accounts=accounts,
                           existing_disposal_tx=existing_disposal_tx,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat())


@admin_bp.route('/assets/<int:id>/undispose', methods=['POST'])
def asset_undispose(id):
    asset = Asset.query.get_or_404(id)
    # Delete any disposal-linked transactions
    Transaction.query.filter_by(linked_asset_id=asset.id, type='income').delete()
    asset.disposal_date = None
    asset.disposal_price = None
    asset.disposal_price_gross = None
    asset.disposal_tax_treatment = None
    asset.disposal_tax_rate = None
    asset.disposal_tax_amount = None
    asset.disposal_reason = None
    db.session.commit()
    flash('Abgang wurde r\u00fcckg\u00e4ngig gemacht.', 'success')
    return redirect(url_for('admin.asset_detail', id=asset.id))


@admin_bp.route('/assets/<int:id>/delete', methods=['POST'])
def asset_delete(id):
    asset = Asset.query.get_or_404(id)
    # Delete linked transactions
    Transaction.query.filter_by(linked_asset_id=id).delete()
    # Archive all attached documents
    for doc in Document.query.filter_by(entity_type='asset', entity_id=asset.id).all():
        archive_file(current_app.config['UPLOAD_FOLDER'], doc.filename)
        db.session.delete(doc)
    db.session.delete(asset)
    db.session.commit()
    flash('Anlagegut wurde gelöscht.', 'success')
    return redirect(url_for('admin.assets'))


@admin_bp.route('/assets/bundle/<bundle_id>')
def bundle_detail(bundle_id):
    """Detail page for an asset bundle."""
    items = Asset.query.filter_by(bundle_id=bundle_id).order_by(Asset.id).all()
    if not items:
        flash('Bündel nicht gefunden.', 'error')
        return redirect(url_for('admin.assets'))

    representative = items[0]
    base_name = representative.name.rsplit(' (', 1)[0] if '(' in representative.name else representative.name

    for a in items:
        a._book_value = get_book_value(a)
        a._disposal_result = get_disposal_result(a)

    active_count = sum(1 for a in items if a.disposal_date is None)
    total_net = sum(a.purchase_price_net for a in items)
    total_gross = sum(a.purchase_price_gross for a in items)
    total_book_value = sum(a._book_value for a in items)

    # Linked transaction for purchase outflow (linked to first item)
    purchase_tx = Transaction.query.filter_by(linked_asset_id=representative.id, type='expense').first()

    # Disposal linked transactions (any item)
    disposal_txs = []
    disposed_without_tx = []
    for a in items:
        if a.disposal_date:
            dtxs = Transaction.query.filter_by(linked_asset_id=a.id, type='income').all()
            disposal_txs.extend(dtxs)
            if not dtxs:
                disposed_without_tx.append(a)

    disposed_count = sum(1 for a in items if a.disposal_date is not None)

    settings = SiteSettings.get_settings()
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()
    return render_template('bundle_detail.html',
                           items=items,
                           bundle_id=bundle_id,
                           base_name=base_name,
                           representative=representative,
                           active_count=active_count,
                           total_net=total_net,
                           total_gross=total_gross,
                           total_book_value=total_book_value,
                           purchase_tx=purchase_tx,
                           disposal_txs=disposal_txs,
                           disposed_without_tx=disposed_without_tx,
                           disposed_count=disposed_count,
                           accounts=accounts,
                           methods=DEPRECIATION_METHODS,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           current_year=date.today().year)


@admin_bp.route('/assets/bundle/<bundle_id>/book-outflow', methods=['POST'])
def bundle_book_outflow(bundle_id):
    """Book purchase outflow for an entire bundle."""
    items = Asset.query.filter_by(bundle_id=bundle_id).order_by(Asset.id).all()
    if not items:
        flash('Bündel nicht gefunden.', 'error')
        return redirect(url_for('admin.assets'))

    representative = items[0]
    existing = Transaction.query.filter_by(linked_asset_id=representative.id).filter(
        Transaction.description.like('Anlagekauf:%')
    ).first()
    if existing:
        flash('Es existiert bereits eine verknüpfte Kaufbuchung für dieses Bündel.', 'warning')
        return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))

    account_id = request.form.get('outflow_account_id', type=int)
    if not account_id:
        flash('Bitte ein Konto auswählen.', 'error')
        return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))

    base_name = representative.name.rsplit(' (', 1)[0] if '(' in representative.name else representative.name
    total_gross = sum(a.purchase_price_gross for a in items)
    total_net = sum(a.purchase_price_net for a in items)
    total_tax = sum(a.purchase_tax_amount or 0 for a in items)

    try:
        outflow_tx = Transaction(
            date=representative.purchase_date,
            type='expense',
            description=f'Anlagekauf: {base_name} ({len(items)} Stk.)',
            amount=total_gross,
            net_amount=total_net,
            tax_amount=total_tax,
            tax_treatment=representative.purchase_tax_treatment or 'none',
            tax_rate=representative.purchase_tax_rate or 0,
            account_id=account_id,
            linked_asset_id=representative.id,
            notes='Automatisch erstellt bei nachträglicher Kontobuchung (Bündel)',
        )
        db.session.add(outflow_tx)
        db.session.commit()
        flash('Kontoabgang wurde gebucht.', 'success')
    except Exception as e:
        flash(f'Fehler: {str(e)}', 'error')

    return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))


@admin_bp.route('/assets/bundle/<bundle_id>/unlink-outflow', methods=['POST'])
def bundle_unlink_outflow(bundle_id):
    """Remove purchase outflow transaction for a bundle."""
    items = Asset.query.filter_by(bundle_id=bundle_id).order_by(Asset.id).all()
    if not items:
        flash('Bündel nicht gefunden.', 'error')
        return redirect(url_for('admin.assets'))

    representative = items[0]
    deleted = Transaction.query.filter_by(linked_asset_id=representative.id, type='expense').delete()
    db.session.commit()
    if deleted:
        flash('Verknüpfte Kontobuchung wurde entfernt.', 'success')
    else:
        flash('Keine verknüpfte Buchung gefunden.', 'warning')
    return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))


@admin_bp.route('/assets/bundle/<bundle_id>/book-disposal-inflow', methods=['POST'])
def bundle_book_disposal_inflow(bundle_id):
    """Book disposal proceeds for bundle items."""
    items = Asset.query.filter_by(bundle_id=bundle_id).order_by(Asset.id).all()
    if not items:
        flash('Bündel nicht gefunden.', 'error')
        return redirect(url_for('admin.assets'))

    disposed_items = [a for a in items if a.disposal_date is not None]
    if not disposed_items:
        flash('Keine abgegangenen Items in diesem Bündel.', 'warning')
        return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))

    account_id = request.form.get('inflow_account_id', type=int)
    if not account_id:
        flash('Bitte ein Konto auswählen.', 'error')
        return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))

    # Find disposed items without a disposal transaction
    items_to_book = []
    for a in disposed_items:
        existing = Transaction.query.filter_by(linked_asset_id=a.id, type='income').first()
        if not existing:
            items_to_book.append(a)

    if not items_to_book:
        flash('Alle abgegangenen Items haben bereits eine Veräußerungsbuchung.', 'warning')
        return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))

    base_name = items[0].name.rsplit(' (', 1)[0] if '(' in items[0].name else items[0].name
    total_gross = sum(a.disposal_price_gross or 0 for a in items_to_book)
    total_net = sum(a.disposal_price or a.disposal_price_gross or 0 for a in items_to_book)
    total_tax = sum(a.disposal_tax_amount or 0 for a in items_to_book)
    first_item = items_to_book[0]

    try:
        inflow_tx = Transaction(
            date=first_item.disposal_date,
            type='income',
            description=f'Veräußerung: {base_name} ({len(items_to_book)} Stk.)',
            amount=total_gross,
            net_amount=total_net,
            tax_amount=total_tax,
            tax_treatment=first_item.disposal_tax_treatment or 'none',
            tax_rate=first_item.disposal_tax_rate or 0,
            account_id=account_id,
            linked_asset_id=first_item.id,
            notes='Nachträglich erstellt - Veräußerungserlös (Bündel)',
        )
        db.session.add(inflow_tx)
        db.session.commit()
        flash(f'Veräußerungserlös für {len(items_to_book)} Stk. wurde auf Konto gebucht.', 'success')
    except Exception as e:
        flash(f'Fehler: {str(e)}', 'error')

    return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))


@admin_bp.route('/assets/bundle/<bundle_id>/unlink-disposal-inflow/<int:tx_id>', methods=['POST'])
def bundle_unlink_disposal_inflow(bundle_id, tx_id):
    """Remove a specific disposal transaction for a bundle."""
    tx = Transaction.query.get_or_404(tx_id)
    if tx.type != 'income':
        flash('Ungültige Buchung.', 'error')
        return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))
    db.session.delete(tx)
    db.session.commit()
    flash('Veräußerungsbuchung wurde entfernt.', 'success')
    return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))


@admin_bp.route('/assets/bundle/<bundle_id>/undispose-all', methods=['POST'])
def bundle_undispose_all(bundle_id):
    """Undo all disposals in a bundle."""
    items = Asset.query.filter_by(bundle_id=bundle_id).order_by(Asset.id).all()
    if not items:
        flash('Bündel nicht gefunden.', 'error')
        return redirect(url_for('admin.assets'))

    count = 0
    for a in items:
        if a.disposal_date is not None:
            # Delete disposal-linked transactions
            Transaction.query.filter_by(linked_asset_id=a.id, type='income').delete()
            a.disposal_date = None
            a.disposal_price = None
            a.disposal_price_gross = None
            a.disposal_tax_treatment = None
            a.disposal_tax_rate = None
            a.disposal_tax_amount = None
            a.disposal_reason = None
            count += 1

    db.session.commit()
    if count:
        flash(f'{count} Abgänge wurden rückgängig gemacht.', 'success')
    else:
        flash('Keine Abgänge vorhanden.', 'warning')
    return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))


@admin_bp.route('/assets/bundle/<bundle_id>/dispose', methods=['GET', 'POST'])
def bundle_dispose(bundle_id):
    """Dispose selected items from an asset bundle."""
    items = Asset.query.filter_by(bundle_id=bundle_id).order_by(Asset.id).all()
    if not items:
        flash('Bündel nicht gefunden.', 'error')
        return redirect(url_for('admin.assets'))

    active_items = [a for a in items if a.disposal_date is None]
    if not active_items:
        flash('Alle Anlagegüter in diesem Bündel sind bereits abgegangen.', 'warning')
        return redirect(url_for('admin.assets'))

    # Compute book values
    for a in items:
        a._book_value = get_book_value(a)

    if request.method == 'POST':
        try:
            settings = SiteSettings.get_settings()
            selected_ids = request.form.getlist('selected_ids', type=int)
            if not selected_ids:
                flash('Bitte mindestens ein Anlagegut auswählen.', 'error')
                return redirect(request.url)

            tax_treatment = request.form.get('disposal_tax_treatment', 'none')
            if settings.tax_mode == 'kleinunternehmer':
                tax_treatment = 'none'
            custom_rate = parse_amount(request.form.get('disposal_custom_tax_rate', '0'))
            effective_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)

            # Total disposal price, split equally among selected items
            input_mode = request.form.get('disposal_input_mode', 'gross')
            if input_mode == 'net':
                total_net = parse_amount(request.form.get('disposal_price', '0'))
                if effective_rate > 0:
                    total_gross, total_tax = calculate_tax_from_net(total_net, effective_rate)
                else:
                    total_gross = total_net
                    total_tax = 0.0
            else:
                total_gross = parse_amount(request.form.get('disposal_price_gross', '0'))
                if effective_rate > 0:
                    total_net, total_tax = calculate_tax(total_gross, effective_rate)
                else:
                    total_net = total_gross
                    total_tax = 0.0

            count = len(selected_ids)
            unit_gross = round(total_gross / count, 2)
            unit_net = round(total_net / count, 2)
            unit_tax = round(total_tax / count, 2)
            disposal_date = parse_date(request.form.get('disposal_date'))
            disposal_reason = request.form.get('disposal_reason', 'sold')

            disposed = 0
            first_disposed_id = None
            for a in active_items:
                if a.id in selected_ids:
                    a.disposal_date = disposal_date
                    a.disposal_price_gross = unit_gross
                    a.disposal_price = unit_net
                    a.disposal_tax_treatment = tax_treatment
                    a.disposal_tax_rate = effective_rate
                    a.disposal_tax_amount = unit_tax
                    a.disposal_reason = disposal_reason
                    if first_disposed_id is None:
                        first_disposed_id = a.id
                    disposed += 1

            # Optional: Book disposal proceeds to account
            book_inflow = request.form.get('book_inflow') == '1'
            inflow_account_id = request.form.get('inflow_account_id', type=int)
            if book_inflow and inflow_account_id and total_gross > 0 and first_disposed_id:
                base_name = items[0].name.rsplit(' (', 1)[0] if '(' in items[0].name else items[0].name
                inflow_tx = Transaction(
                    date=disposal_date,
                    type='income',
                    description=f'Veräußerung: {base_name} ({disposed} Stk.)',
                    amount=total_gross,
                    net_amount=total_net,
                    tax_amount=total_tax,
                    tax_treatment=tax_treatment,
                    tax_rate=effective_rate,
                    account_id=inflow_account_id,
                    linked_asset_id=first_disposed_id,
                    notes='Automatisch erstellt bei Veräußerung (Bündel-Teilabgang)',
                )
                db.session.add(inflow_tx)

            db.session.commit()
            flash(f'{disposed} Anlagegüter als abgegangen erfasst.', 'success')
            return redirect(url_for('admin.bundle_detail', bundle_id=bundle_id))
        except Exception as e:
            flash(f'Fehler: {str(e)}', 'error')

    base_name = items[0].name.rsplit(' (', 1)[0] if '(' in items[0].name else items[0].name
    settings = SiteSettings.get_settings()
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()
    return render_template('bundle_dispose.html',
                           items=items,
                           active_items=active_items,
                           bundle_id=bundle_id,
                           base_name=base_name,
                           settings=settings,
                           accounts=accounts,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat())


@admin_bp.route('/assets/bundle/<bundle_id>/edit', methods=['GET', 'POST'])
def bundle_edit(bundle_id):
    """Edit all items in an asset bundle at once."""
    items = Asset.query.filter_by(bundle_id=bundle_id).order_by(Asset.id).all()
    if not items:
        flash('Bündel nicht gefunden.', 'error')
        return redirect(url_for('admin.assets'))

    representative = items[0]
    base_name = representative.name.rsplit(' (', 1)[0] if '(' in representative.name else representative.name

    if request.method == 'POST':
        try:
            settings = SiteSettings.get_settings()
            new_base_name = request.form.get('name', '').strip()
            description = request.form.get('description', '').strip() or None
            purchase_date = parse_date(request.form.get('purchase_date'))

            tax_treatment = request.form.get('purchase_tax_treatment', 'none')
            if settings.tax_mode == 'kleinunternehmer':
                tax_treatment = 'none'
            custom_rate = parse_amount(request.form.get('purchase_custom_tax_rate', '0'))
            effective_rate = get_tax_rate_for_treatment(tax_treatment, settings, custom_rate)

            # Total price for the whole bundle
            input_mode = request.form.get('purchase_input_mode', 'gross')
            if input_mode == 'net':
                total_net = parse_amount(request.form.get('purchase_price_net'))
                if effective_rate > 0:
                    total_gross, total_tax = calculate_tax_from_net(total_net, effective_rate)
                else:
                    total_gross = total_net
                    total_tax = 0.0
            else:
                total_gross = parse_amount(request.form.get('purchase_price_gross'))
                if effective_rate > 0:
                    total_net, total_tax = calculate_tax(total_gross, effective_rate)
                else:
                    total_net = total_gross
                    total_tax = 0.0

            new_quantity = request.form.get('quantity', type=int) or len(items)
            old_count = len(items)

            # Handle quantity changes
            disposed_items = [a for a in items if a.disposal_date is not None]
            active_items = [a for a in items if a.disposal_date is None]
            min_quantity = len(disposed_items)  # can't go below disposed count

            if new_quantity < min_quantity:
                flash(f'Stückzahl kann nicht unter {min_quantity} reduziert werden ({min_quantity} bereits abgegangen).', 'error')
                return redirect(request.url)

            if new_quantity < old_count:
                # Remove excess active items (from the end)
                to_remove = old_count - new_quantity
                # Sort active items by id desc so we remove the last ones first
                removable = sorted(active_items, key=lambda a: a.id, reverse=True)
                for a in removable[:to_remove]:
                    # Remove documents attached to this asset
                    for doc in Document.query.filter_by(entity_type='asset', entity_id=a.id).all():
                        # Don't remove shared files yet, other items may reference them
                        db.session.delete(doc)
                    db.session.delete(a)
                    items.remove(a)
            elif new_quantity > old_count:
                # Add new items with same properties as representative
                import uuid
                for _ in range(new_quantity - old_count):
                    new_asset = Asset(
                        bundle_id=bundle_id,
                        name='',  # will be set below
                        purchase_date=representative.purchase_date,
                        purchase_price_gross=0,  # will be set below
                        purchase_price_net=0,
                        depreciation_method=representative.depreciation_method,
                        useful_life_months=representative.useful_life_months,
                        salvage_value=representative.salvage_value or 0,
                        depreciation_category_id=representative.depreciation_category_id,
                    )
                    db.session.add(new_asset)
                    items.append(new_asset)
                db.session.flush()  # assign IDs to new items

                # Copy documents from representative to new items
                rep_docs = Document.query.filter_by(entity_type='asset', entity_id=representative.id).all()
                for new_asset in items[-( new_quantity - old_count):]:
                    for rd in rep_docs:
                        doc = Document(filename=rd.filename, original_filename=rd.original_filename,
                                       entity_type='asset', entity_id=new_asset.id)
                        db.session.add(doc)

            count = new_quantity
            unit_gross = round(total_gross / count, 2)
            unit_net = round(total_net / count, 2)
            unit_tax = round(total_tax / count, 2)

            depreciation_method = request.form.get('depreciation_method', 'linear')
            useful_life_months = request.form.get('useful_life_months', type=int) or None
            salvage_value = parse_amount(request.form.get('salvage_value', '0'))
            depreciation_category_id = request.form.get('depreciation_category_id', type=int) or None
            notes = request.form.get('notes', '').strip() or None

            # Handle document removals for all bundle items
            remove_doc_ids = request.form.getlist('remove_documents')
            if remove_doc_ids:
                # Get the filenames to remove
                filenames_to_remove = set()
                for doc_id in remove_doc_ids:
                    doc = Document.query.get(int(doc_id))
                    if doc and doc.entity_type == 'asset':
                        filenames_to_remove.add(doc.filename)
                # Remove matching documents from ALL items in the bundle
                for a in items:
                    for doc in Document.query.filter_by(entity_type='asset', entity_id=a.id).all():
                        if doc.filename in filenames_to_remove:
                            db.session.delete(doc)
                # Archive the actual files from disk
                for fn in filenames_to_remove:
                    archive_file(current_app.config['UPLOAD_FOLDER'], fn)

            # File uploads (multiple, shared for all bundle items)
            uploaded_docs = []
            files = request.files.getlist('documents')
            for file in files:
                if file and file.filename and allowed_file(file.filename):
                    stored = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                    file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], stored))
                    uploaded_docs.append((stored, file.filename))

            # Sort items by id for consistent numbering
            items.sort(key=lambda a: a.id)
            for i, a in enumerate(items, 1):
                a.name = f"{new_base_name} ({i}/{count})"
                a.description = description
                a.purchase_date = purchase_date
                a.purchase_price_gross = unit_gross
                a.purchase_price_net = unit_net
                a.purchase_tax_treatment = tax_treatment
                a.purchase_tax_rate = effective_rate
                a.purchase_tax_amount = unit_tax
                a.depreciation_method = depreciation_method
                a.useful_life_months = useful_life_months
                a.salvage_value = salvage_value
                a.depreciation_category_id = depreciation_category_id
                a.notes = notes
                # Attach new documents to each item
                for stored_name, orig_name in uploaded_docs:
                    doc = Document(filename=stored_name, original_filename=orig_name,
                                   entity_type='asset', entity_id=a.id)
                    db.session.add(doc)

            db.session.commit()
            qty_info = ''
            if new_quantity != old_count:
                qty_info = f' (Stückzahl: {old_count} → {new_quantity})'
            flash(f'Bündel "{new_base_name}" ({count} Stk.) wurde aktualisiert.{qty_info}', 'success')
            return redirect(url_for('admin.assets'))
        except Exception as e:
            flash(f'Fehler beim Aktualisieren: {str(e)}', 'error')

    dep_cats = DepreciationCategory.query.order_by(DepreciationCategory.sort_order, DepreciationCategory.name).all()
    settings = SiteSettings.get_settings()

    # Compute total prices for the bundle
    total_gross = sum(a.purchase_price_gross for a in items)
    total_net = sum(a.purchase_price_net for a in items)
    disposed_count = sum(1 for a in items if a.disposal_date is not None)

    return render_template('bundle_edit.html',
                           items=items,
                           bundle_id=bundle_id,
                           base_name=base_name,
                           representative=representative,
                           total_gross=total_gross,
                           total_net=total_net,
                           disposed_count=disposed_count,
                           methods=DEPRECIATION_METHODS,
                           depreciation_categories=dep_cats,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           rules=RULES,
                           today=date.today().isoformat())


@admin_bp.route('/assets/bundle/<bundle_id>/delete', methods=['POST'])
def bundle_delete(bundle_id):
    """Delete all items in an asset bundle."""
    items = Asset.query.filter_by(bundle_id=bundle_id).all()
    if not items:
        flash('Bündel nicht gefunden.', 'error')
        return redirect(url_for('admin.assets'))

    base_name = items[0].name.rsplit(' (', 1)[0] if '(' in items[0].name else items[0].name
    count = len(items)

    # Remove all documents and files for the bundle
    removed_files = set()
    for a in items:
        # Delete linked transactions
        Transaction.query.filter_by(linked_asset_id=a.id).delete()
        for doc in Document.query.filter_by(entity_type='asset', entity_id=a.id).all():
            if doc.filename not in removed_files:
                archive_file(current_app.config['UPLOAD_FOLDER'], doc.filename)
                removed_files.add(doc.filename)
            db.session.delete(doc)
        db.session.delete(a)

    db.session.commit()
    flash(f'Bündel "{base_name}" ({count} Stk.) wurde gelöscht.', 'success')
    return redirect(url_for('admin.assets'))


# --- EÜR Report ---

@admin_bp.route('/report')
def report():
    year = request.args.get('year', date.today().year, type=int)

    transactions = Transaction.query.filter(
        db.extract('year', Transaction.date) == year
    ).filter(
        Transaction.type.in_(['income', 'expense']),
        Transaction.linked_asset_id.is_(None)
    ).order_by(Transaction.date).all()

    # Group by category
    income_by_category = {}
    expense_by_category = {}

    # VAT tracking
    vat_collected = 0.0  # USt auf Einnahmen (Umsatzsteuer)
    vat_paid = 0.0       # VSt auf Ausgaben (Vorsteuer)
    reverse_charge_vat = 0.0  # Self-assessed USt on reverse charge / intra-EU
    vat_by_rate = {}     # Detailed breakdown by rate

    for t in transactions:
        cat_name = t.category.name if t.category else 'Ohne Kategorie'
        tax_treatment = t.tax_treatment or 'none'
        t_tax_amount = t.tax_amount or 0
        t_tax_rate = t.tax_rate or 0
        treatment_label = TAX_TREATMENT_LABELS.get(tax_treatment, tax_treatment)

        if t.type == 'income':
            if cat_name not in income_by_category:
                income_by_category[cat_name] = {'gross': 0, 'net': 0, 'tax': 0, 'count': 0}
            income_by_category[cat_name]['gross'] += t.amount
            income_by_category[cat_name]['net'] += (t.net_amount or t.amount)
            income_by_category[cat_name]['tax'] += t_tax_amount
            income_by_category[cat_name]['count'] += 1

            # USt collected
            if tax_treatment in ('standard', 'reduced', 'custom') and t_tax_amount > 0:
                vat_collected += t_tax_amount
                rate_key = f'{t_tax_rate}%'
                if rate_key not in vat_by_rate:
                    vat_by_rate[rate_key] = {'ust': 0, 'vst': 0}
                vat_by_rate[rate_key]['ust'] += t_tax_amount
        else:
            if cat_name not in expense_by_category:
                expense_by_category[cat_name] = {'gross': 0, 'net': 0, 'tax': 0, 'count': 0}
            expense_by_category[cat_name]['gross'] += t.amount
            expense_by_category[cat_name]['net'] += (t.net_amount or t.amount)
            expense_by_category[cat_name]['tax'] += t_tax_amount
            expense_by_category[cat_name]['count'] += 1

            # VSt paid (deductible input VAT)
            if tax_treatment in ('standard', 'reduced', 'custom') and t_tax_amount > 0:
                vat_paid += t_tax_amount
                rate_key = f'{t_tax_rate}%'
                if rate_key not in vat_by_rate:
                    vat_by_rate[rate_key] = {'ust': 0, 'vst': 0}
                vat_by_rate[rate_key]['vst'] += t_tax_amount

            # Reverse charge / intra-EU on expenses: self-assess AND deduct
            if tax_treatment in ('reverse_charge', 'intra_eu') and t_tax_amount > 0:
                reverse_charge_vat += t_tax_amount

    total_income_transactions = sum(v['gross'] for v in income_by_category.values())
    total_expenses_transactions = sum(v['gross'] for v in expense_by_category.values())

    # --- Depreciation (AfA) from assets ---
    all_assets = Asset.query.all()
    depreciation_by_method = {}  # method_label -> {amount, count, assets}
    total_depreciation = 0.0
    disposal_gains = 0.0
    disposal_losses = 0.0
    disposal_items = []  # for display

    for asset in all_assets:
        # Depreciation for this year
        afa = get_depreciation_for_year(asset, year)
        if afa > 0:
            method_label = DEPRECIATION_METHODS.get(asset.depreciation_method, asset.depreciation_method)
            if method_label not in depreciation_by_method:
                depreciation_by_method[method_label] = {'amount': 0, 'count': 0}
            depreciation_by_method[method_label]['amount'] += afa
            depreciation_by_method[method_label]['count'] += 1
            total_depreciation += afa

        # VSt from asset purchases in this year
        if asset.purchase_date and asset.purchase_date.year == year:
            p_treatment = asset.purchase_tax_treatment or 'none'
            p_tax = asset.purchase_tax_amount or 0
            p_rate = asset.purchase_tax_rate or 0
            if p_treatment in ('standard', 'reduced', 'custom') and p_tax > 0:
                vat_paid += p_tax
                rate_key = f'{p_rate}%'
                if rate_key not in vat_by_rate:
                    vat_by_rate[rate_key] = {'ust': 0, 'vst': 0}
                vat_by_rate[rate_key]['vst'] += p_tax
            if p_treatment in ('reverse_charge', 'intra_eu') and p_tax > 0:
                reverse_charge_vat += p_tax

        # Disposal gains/losses in this year
        if asset.disposal_date and asset.disposal_date.year == year:
            result = get_disposal_result(asset)
            if result:
                if result['is_sammelposten']:
                    # Sale price is pure income
                    if result['disposal_price'] > 0:
                        disposal_gains += result['disposal_price']
                        disposal_items.append({
                            'name': asset.name,
                            'amount': result['disposal_price'],
                            'type': 'gain',
                        })
                else:
                    if result['is_gain']:
                        disposal_gains += result['gain_or_loss']
                    else:
                        disposal_losses += abs(result['gain_or_loss'])
                    disposal_items.append({
                        'name': asset.name,
                        'amount': result['gain_or_loss'],
                        'type': 'gain' if result['is_gain'] else 'loss',
                        'book_value': result['book_value_at_disposal'],
                        'sale_price': result['disposal_price'],
                    })

            # USt from asset disposals in this year
            d_treatment = asset.disposal_tax_treatment or 'none'
            d_tax = asset.disposal_tax_amount or 0
            d_rate = asset.disposal_tax_rate or 0
            if d_treatment in ('standard', 'reduced', 'custom') and d_tax > 0:
                vat_collected += d_tax
                rate_key = f'{d_rate}%'
                if rate_key not in vat_by_rate:
                    vat_by_rate[rate_key] = {'ust': 0, 'vst': 0}
                vat_by_rate[rate_key]['ust'] += d_tax

    vat_payable = vat_collected - vat_paid  # Net VAT payable (or refundable if negative)

    total_income = total_income_transactions + disposal_gains
    total_expenses = total_expenses_transactions + total_depreciation + disposal_losses
    profit = total_income - total_expenses

    settings = SiteSettings.get_settings()

    return render_template('report.html',
                           year=year,
                           years=get_year_choices(),
                           income_by_category=income_by_category,
                           expense_by_category=expense_by_category,
                           total_income_transactions=total_income_transactions,
                           total_expenses_transactions=total_expenses_transactions,
                           depreciation_by_method=depreciation_by_method,
                           total_depreciation=total_depreciation,
                           disposal_gains=disposal_gains,
                           disposal_losses=disposal_losses,
                           disposal_items=disposal_items,
                           total_income=total_income,
                           total_expenses=total_expenses,
                           profit=profit,
                           vat_collected=vat_collected,
                           vat_paid=vat_paid,
                           vat_payable=vat_payable,
                           vat_by_rate=vat_by_rate,
                           reverse_charge_vat=reverse_charge_vat,
                           settings=settings)


# --- Settings ---

@admin_bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if not current_user.is_admin:
        flash('Keine Berechtigung.', 'error')
        return redirect(url_for('admin.dashboard'))

    settings = SiteSettings.get_settings()

    if request.method == 'POST':
        settings.display_name = request.form.get('display_name', '').strip() or None
        settings.business_name = request.form.get('business_name', '').strip() or 'Meine Buchhaltung'
        settings.address_lines = request.form.get('address_lines', '').strip() or None
        settings.contact_lines = request.form.get('contact_lines', '').strip() or None
        settings.bank_lines = request.form.get('bank_lines', '').strip() or None
        settings.tax_number = request.form.get('tax_number', '').strip() or None
        settings.vat_id = request.form.get('vat_id', '').strip() or None
        settings.tax_mode = request.form.get('tax_mode', 'kleinunternehmer')
        settings.tax_rate = float(request.form.get('tax_rate', 19.0))
        settings.tax_rate_reduced = float(request.form.get('tax_rate_reduced', 7.0))

        # Favicon upload
        from audit import log_action
        favicon_file = request.files.get('favicon')
        if favicon_file and favicon_file.filename:
            fname = secure_filename(favicon_file.filename)
            fname = 'favicon_' + fname
            upload_dir = current_app.config['UPLOAD_FOLDER']
            old_favicon = settings.favicon_filename
            archived_name = None
            if old_favicon:
                archived_name = archive_file(upload_dir, old_favicon)
            favicon_file.save(os.path.join(upload_dir, fname))
            settings.favicon_filename = fname
            log_action('FILE_UPLOAD', 'SiteSettings', settings.id,
                       old_values={'favicon_filename': old_favicon} if old_favicon else None,
                       new_values={'favicon_filename': fname},
                       archived_files=[archived_name] if archived_name else None)

        # Remove favicon
        if request.form.get('remove_favicon'):
            if settings.favicon_filename:
                old_favicon = settings.favicon_filename
                archived_name = archive_file(current_app.config['UPLOAD_FOLDER'], old_favicon)
                settings.favicon_filename = None
                log_action('FILE_REMOVE', 'SiteSettings', settings.id,
                           old_values={'favicon_filename': old_favicon},
                           archived_files=[archived_name] if archived_name else None)

        # Logo upload (for PDFs)
        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename:
            fname = secure_filename(logo_file.filename)
            fname = 'logo_' + fname
            upload_dir = current_app.config['UPLOAD_FOLDER']
            old_logo = settings.logo_filename
            archived_name = None
            if old_logo:
                archived_name = archive_file(upload_dir, old_logo)
            logo_file.save(os.path.join(upload_dir, fname))
            settings.logo_filename = fname
            log_action('FILE_UPLOAD', 'SiteSettings', settings.id,
                       old_values={'logo_filename': old_logo} if old_logo else None,
                       new_values={'logo_filename': fname},
                       archived_files=[archived_name] if archived_name else None)

        # Remove logo
        if request.form.get('remove_logo'):
            if settings.logo_filename:
                old_logo = settings.logo_filename
                archived_name = archive_file(current_app.config['UPLOAD_FOLDER'], old_logo)
                settings.logo_filename = None
                log_action('FILE_REMOVE', 'SiteSettings', settings.id,
                           old_values={'logo_filename': old_logo},
                           archived_files=[archived_name] if archived_name else None)

        # Invoicing defaults
        settings.default_agb_text = request.form.get('default_agb_text', '').strip() or None
        pt_days = request.form.get('default_payment_terms_days', '14')
        settings.default_payment_terms_days = int(pt_days) if pt_days else 14
        settings.quote_number_prefix = request.form.get('quote_number_prefix', '').strip() or 'A'
        settings.invoice_number_prefix = request.form.get('invoice_number_prefix', '').strip() or 'R'

        db.session.commit()
        flash('Einstellungen wurden gespeichert.', 'success')
        return redirect(url_for('admin.settings'))

    return render_template('settings.html', settings=settings)


# --- User Management ---

@admin_bp.route('/users')
def users():
    if not current_user.is_admin:
        flash('Keine Berechtigung.', 'error')
        return redirect(url_for('admin.dashboard'))

    users_list = User.query.order_by(User.username).all()
    return render_template('users.html', users=users_list)


@admin_bp.route('/users/new', methods=['GET', 'POST'])
def user_new():
    if not current_user.is_admin:
        flash('Keine Berechtigung.', 'error')
        return redirect(url_for('admin.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        display_name = request.form.get('display_name', '').strip()
        password = request.form.get('password', '')
        is_admin = request.form.get('is_admin') == 'on'

        if not username or not password:
            flash('Benutzername und Passwort sind erforderlich.', 'error')
            return render_template('user_form.html', user=None)

        if User.query.filter_by(username=username).first():
            flash('Dieser Benutzername ist bereits vergeben.', 'error')
            return render_template('user_form.html', user=None)

        if len(password) < 6:
            flash('Passwort muss mindestens 6 Zeichen lang sein.', 'error')
            return render_template('user_form.html', user=None)

        user = User(
            username=username,
            display_name=display_name,
            password_hash=generate_password_hash(password),
            is_admin=is_admin,
        )
        db.session.add(user)
        db.session.commit()
        flash('Benutzer wurde erstellt.', 'success')
        return redirect(url_for('admin.users'))

    return render_template('user_form.html', user=None)


@admin_bp.route('/users/<int:id>/edit', methods=['GET', 'POST'])
def user_edit(id):
    if not current_user.is_admin:
        flash('Keine Berechtigung.', 'error')
        return redirect(url_for('admin.dashboard'))

    user = User.query.get_or_404(id)

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        display_name = request.form.get('display_name', '').strip()
        password = request.form.get('password', '')
        is_admin = request.form.get('is_admin') == 'on'

        if not username:
            flash('Benutzername ist erforderlich.', 'error')
            return render_template('user_form.html', user=user)

        # Check username uniqueness
        existing = User.query.filter_by(username=username).first()
        if existing and existing.id != user.id:
            flash('Dieser Benutzername ist bereits vergeben.', 'error')
            return render_template('user_form.html', user=user)

        user.username = username
        user.display_name = display_name
        user.is_admin = is_admin

        if password:
            if len(password) < 6:
                flash('Passwort muss mindestens 6 Zeichen lang sein.', 'error')
                return render_template('user_form.html', user=user)
            user.password_hash = generate_password_hash(password)
            db.session.flush()  # ensure automatic UPDATE audit entry is created first
            from audit import log_action
            log_action('PASSWORD_CHANGE', 'User', user.id,
                       new_values={'changed_by': current_user.username})

        db.session.commit()
        flash('Benutzer wurde aktualisiert.', 'success')
        return redirect(url_for('admin.users'))

    return render_template('user_form.html', user=user)


@admin_bp.route('/users/<int:id>/delete', methods=['POST'])
def user_delete(id):
    if not current_user.is_admin:
        flash('Keine Berechtigung.', 'error')
        return redirect(url_for('admin.dashboard'))

    user = User.query.get_or_404(id)

    if user.id == current_user.id:
        flash('Sie können sich nicht selbst löschen.', 'error')
        return redirect(url_for('admin.users'))

    db.session.delete(user)
    db.session.commit()
    flash('Benutzer wurde gelöscht.', 'success')
    return redirect(url_for('admin.users'))


# --- Audit Log ---

@admin_bp.route('/audit')
def audit_log():
    """Display the full audit trail with filters and pagination."""
    if not current_user.is_admin:
        flash('Nur Administratoren können das Änderungsprotokoll einsehen.', 'error')
        return redirect(url_for('admin.dashboard'))

    page = request.args.get('page', 1, type=int)
    per_page = 50

    # Collect filter values
    filters = {
        'entity_type': request.args.get('entity_type', ''),
        'action': request.args.get('action', ''),
        'source': request.args.get('source', ''),
        'user': request.args.get('user', ''),
        'date_from': request.args.get('date_from', ''),
        'date_to': request.args.get('date_to', ''),
        'entity_id': request.args.get('entity_id', ''),
    }

    query = AuditLog.query

    if filters['entity_type']:
        query = query.filter_by(entity_type=filters['entity_type'])
    if filters['action']:
        query = query.filter_by(action=filters['action'])
    if filters['source']:
        query = query.filter_by(source=filters['source'])
    if filters['user']:
        query = query.filter_by(username=filters['user'])
    if filters['entity_id']:
        try:
            query = query.filter_by(entity_id=int(filters['entity_id']))
        except ValueError:
            pass
    if filters['date_from']:
        try:
            from datetime import datetime as dt
            d = dt.strptime(filters['date_from'], '%Y-%m-%d')
            query = query.filter(AuditLog.timestamp >= d)
        except ValueError:
            pass
    if filters['date_to']:
        try:
            from datetime import datetime as dt
            d = dt.strptime(filters['date_to'], '%Y-%m-%d').replace(hour=23, minute=59, second=59)
            query = query.filter(AuditLog.timestamp <= d)
        except ValueError:
            pass

    total_count = query.count()
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(page, total_pages)
    entries = query.order_by(AuditLog.id.desc()).offset((page - 1) * per_page).limit(per_page).all()

    # Stats (on filtered set)
    count_create = query.filter_by(action='CREATE').count()
    count_update = query.filter_by(action='UPDATE').count()
    count_delete = query.filter_by(action='DELETE').count()

    # Distinct entity types for filter dropdown
    entity_types = [r[0] for r in db.session.query(AuditLog.entity_type).distinct().order_by(AuditLog.entity_type).all()]

    users = User.query.order_by(User.username).all()

    # Check if integrity verification was requested (via session)
    from flask import session as flask_session
    integrity = flask_session.pop('audit_integrity', None)

    return render_template('admin/audit_log.html',
                           entries=entries, page=page, total_pages=total_pages,
                           total_count=total_count, filters=filters,
                           count_create=count_create, count_update=count_update,
                           count_delete=count_delete, entity_types=entity_types,
                           users=users, integrity=integrity)


@admin_bp.route('/audit/verify', methods=['POST'])
def audit_verify():
    """Verify the hash chain integrity of the audit log."""
    if not current_user.is_admin:
        flash('Nur Administratoren können die Integrität prüfen.', 'error')
        return redirect(url_for('admin.dashboard'))

    is_valid, total, broken_id, message = verify_integrity(db)

    from flask import session as flask_session
    flask_session['audit_integrity'] = {
        'valid': is_valid,
        'total': total,
        'broken_id': broken_id,
        'message': message,
    }
    return redirect(url_for('admin.audit_log'))


@admin_bp.route('/audit/repair', methods=['POST'])
def audit_repair():
    """Repair broken hash chain by recalculating hashes."""
    if not current_user.is_admin:
        flash('Nur Administratoren können die Kette reparieren.', 'error')
        return redirect(url_for('admin.dashboard'))

    repaired, message = repair_chain(db)
    flash(message, 'success' if repaired else 'info')
    return redirect(url_for('admin.audit_log'))