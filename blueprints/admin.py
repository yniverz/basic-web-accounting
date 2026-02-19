import os
from datetime import date, datetime
from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app, send_from_directory
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from models import db, User, Transaction, Category, SiteSettings, Asset, DepreciationCategory
from werkzeug.security import generate_password_hash
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

    return render_template('dashboard.html',
                           year=year,
                           years=get_year_choices(),
                           total_income=total_income,
                           total_expenses=total_expenses,
                           profit=profit,
                           monthly_data=monthly_data,
                           recent_transactions=recent,
                           transaction_count=len(transactions))


# --- Transactions ---

@admin_bp.route('/transactions')
def transactions():
    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', 0, type=int)
    type_filter = request.args.get('type', '')
    category_id = request.args.get('category', 0, type=int)

    query = Transaction.query.filter(db.extract('year', Transaction.date) == year)

    if month > 0:
        query = query.filter(db.extract('month', Transaction.date) == month)
    if type_filter in ('income', 'expense'):
        query = query.filter(Transaction.type == type_filter)
    if category_id > 0:
        query = query.filter(Transaction.category_id == category_id)

    transactions_list = query.order_by(Transaction.date.desc()).all()
    categories = Category.query.order_by(Category.sort_order, Category.name).all()

    total_income = sum(t.amount for t in transactions_list if t.type == 'income')
    total_expenses = sum(t.amount for t in transactions_list if t.type == 'expense')

    return render_template('transactions.html',
                           transactions=transactions_list,
                           categories=categories,
                           year=year,
                           month=month,
                           type_filter=type_filter,
                           category_id=category_id,
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

            # File upload
            file = request.files.get('document')
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))
                t.document_filename = filename

            db.session.add(t)
            db.session.commit()
            flash('Buchung wurde erstellt.', 'success')
            return redirect(url_for('admin.transactions'))
        except Exception as e:
            flash(f'Fehler beim Erstellen: {str(e)}', 'error')

    categories = Category.query.order_by(Category.sort_order, Category.name).all()
    settings = SiteSettings.get_settings()
    return render_template('transaction_form.html',
                           transaction=None,
                           categories=categories,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat())


@admin_bp.route('/transactions/<int:id>/edit', methods=['GET', 'POST'])
def transaction_edit(id):
    t = Transaction.query.get_or_404(id)

    if request.method == 'POST':
        try:
            t.date = parse_date(request.form.get('date'))
            t.type = request.form.get('type', 'expense')
            t.description = request.form.get('description', '').strip()
            t.category_id = request.form.get('category_id', type=int) or None
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

            # File upload
            file = request.files.get('document')
            if file and file.filename and allowed_file(file.filename):
                # Remove old file
                if t.document_filename:
                    old_path = os.path.join(current_app.config['UPLOAD_FOLDER'], t.document_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                filename = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))
                t.document_filename = filename

            db.session.commit()
            flash('Buchung wurde aktualisiert.', 'success')
            return redirect(url_for('admin.transactions'))
        except Exception as e:
            flash(f'Fehler beim Aktualisieren: {str(e)}', 'error')

    categories = Category.query.order_by(Category.sort_order, Category.name).all()
    settings = SiteSettings.get_settings()
    return render_template('transaction_form.html',
                           transaction=t,
                           categories=categories,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat())


@admin_bp.route('/transactions/<int:id>/delete', methods=['POST'])
def transaction_delete(id):
    t = Transaction.query.get_or_404(id)
    if t.document_filename:
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], t.document_filename)
        if os.path.exists(filepath):
            os.remove(filepath)
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


# --- Assets (Anlagegüter / AfA) ---

@admin_bp.route('/assets')
def assets():
    status_filter = request.args.get('status', 'active')  # active, disposed, all
    query = Asset.query

    if status_filter == 'active':
        query = query.filter(Asset.disposal_date.is_(None))
    elif status_filter == 'disposed':
        query = query.filter(Asset.disposal_date.isnot(None))

    assets_list = query.order_by(Asset.purchase_date.desc()).all()

    # Calculate book values
    for asset in assets_list:
        asset._book_value = get_book_value(asset)

    total_purchase = sum(a.purchase_price_net for a in assets_list)
    total_book_value = sum(a._book_value for a in assets_list if a.disposal_date is None)

    return render_template('assets.html',
                           assets=assets_list,
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

            asset = Asset(
                name=request.form.get('name', '').strip(),
                description=request.form.get('description', '').strip() or None,
                purchase_date=parse_date(request.form.get('purchase_date')),
                purchase_price_gross=gross_val,
                purchase_price_net=net_val,
                purchase_tax_treatment=tax_treatment,
                purchase_tax_rate=effective_rate,
                purchase_tax_amount=tax_val,
                depreciation_method=request.form.get('depreciation_method', 'linear'),
                useful_life_months=request.form.get('useful_life_months', type=int) or None,
                salvage_value=parse_amount(request.form.get('salvage_value', '0')),
                depreciation_category_id=request.form.get('depreciation_category_id', type=int) or None,
                notes=request.form.get('notes', '').strip() or None,
            )

            # File upload
            file = request.files.get('document')
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))
                asset.document_filename = filename

            db.session.add(asset)
            db.session.commit()
            flash('Anlagegut wurde erstellt.', 'success')
            return redirect(url_for('admin.asset_detail', id=asset.id))
        except Exception as e:
            flash(f'Fehler beim Erstellen: {str(e)}', 'error')

    dep_cats = DepreciationCategory.query.order_by(DepreciationCategory.sort_order, DepreciationCategory.name).all()
    settings = SiteSettings.get_settings()
    return render_template('asset_form.html',
                           asset=None,
                           methods=DEPRECIATION_METHODS,
                           depreciation_categories=dep_cats,
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

    settings = SiteSettings.get_settings()
    return render_template('asset_detail.html',
                           asset=asset,
                           schedule=schedule,
                           book_value=book_value,
                           disposal_result=disposal_result,
                           methods=DEPRECIATION_METHODS,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           current_year=date.today().year)


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

            # File upload
            file = request.files.get('document')
            if file and file.filename and allowed_file(file.filename):
                if asset.document_filename:
                    old_path = os.path.join(current_app.config['UPLOAD_FOLDER'], asset.document_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                filename = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))
                asset.document_filename = filename

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

            asset.disposal_date = parse_date(request.form.get('disposal_date'))
            asset.disposal_price_gross = gross_val
            asset.disposal_price = net_val
            asset.disposal_tax_treatment = tax_treatment
            asset.disposal_tax_rate = effective_rate
            asset.disposal_tax_amount = tax_val
            asset.disposal_reason = request.form.get('disposal_reason', 'sold')

            db.session.commit()
            flash('Abgang wurde ' + ('aktualisiert.' if is_edit else 'erfasst.'), 'success')
            return redirect(url_for('admin.asset_detail', id=asset.id))
        except Exception as e:
            flash(f'Fehler: {str(e)}', 'error')

    book_value = get_book_value(asset)
    settings = SiteSettings.get_settings()
    return render_template('asset_dispose.html',
                           asset=asset,
                           book_value=book_value,
                           is_edit=is_edit,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat())


@admin_bp.route('/assets/<int:id>/undispose', methods=['POST'])
def asset_undispose(id):
    asset = Asset.query.get_or_404(id)
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
    if asset.document_filename:
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], asset.document_filename)
        if os.path.exists(filepath):
            os.remove(filepath)
    db.session.delete(asset)
    db.session.commit()
    flash('Anlagegut wurde gel\u00f6scht.', 'success')
    return redirect(url_for('admin.assets'))


# --- EÜR Report ---

@admin_bp.route('/report')
def report():
    year = request.args.get('year', date.today().year, type=int)

    transactions = Transaction.query.filter(
        db.extract('year', Transaction.date) == year
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
        favicon_file = request.files.get('favicon')
        if favicon_file and favicon_file.filename:
            fname = secure_filename(favicon_file.filename)
            fname = 'favicon_' + fname
            upload_dir = current_app.config['UPLOAD_FOLDER']
            # Remove old favicon
            if settings.favicon_filename:
                old_path = os.path.join(upload_dir, settings.favicon_filename)
                if os.path.exists(old_path):
                    os.remove(old_path)
            favicon_file.save(os.path.join(upload_dir, fname))
            settings.favicon_filename = fname

        # Remove favicon
        if request.form.get('remove_favicon'):
            if settings.favicon_filename:
                old_path = os.path.join(current_app.config['UPLOAD_FOLDER'], settings.favicon_filename)
                if os.path.exists(old_path):
                    os.remove(old_path)
                settings.favicon_filename = None

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
