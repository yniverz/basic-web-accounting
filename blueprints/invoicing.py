"""
Invoicing blueprint – Customers, Quotes (Angebote) & Invoices (Rechnungen).

Provides CRUD for customers, quote/invoice creation with PDF generation,
marking invoices as paid (creates accounting transaction), and asset-disposal
integration.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, send_file, current_app,
)
from flask_login import login_required, current_user

from models import (
    db, SiteSettings, Customer, Quote, QuoteItem,
    Invoice, InvoiceItem, Transaction, Account, Category,
    Asset, Document,
)
from helpers import (
    parse_amount, parse_date, format_currency, format_date,
    calculate_tax, calculate_tax_from_net, get_tax_rate_for_treatment,
    TAX_TREATMENT_LABELS, get_year_choices,
)

invoicing_bp = Blueprint('invoicing', __name__)


# ── Helpers ──────────────────────────────────────────────────────────────

def _next_number(prefix: str, model_class, number_field: str, year: int | None = None) -> str:
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


def _settings_dict(settings: SiteSettings) -> dict:
    """Build the dict expected by the PDF generators."""
    return {
        'business_name': settings.business_name or '',
        'address_lines': [l.strip() for l in (settings.address_lines or '').split('\n') if l.strip()],
        'contact_lines': [l.strip() for l in (settings.contact_lines or '').split('\n') if l.strip()],
        'bank_lines': [l.strip() for l in (settings.bank_lines or '').split('\n') if l.strip()],
        'tax_number': settings.tax_number,
        'vat_id': settings.vat_id,
        'tax_mode': settings.tax_mode,
        'tax_rate': settings.tax_rate,
    }


def _save_pdf(pdf_bytes: bytes, prefix: str, number: str) -> str:
    """Save generated PDF to uploads and return the filename."""
    safe_number = number.replace('/', '-')
    filename = f'{prefix}_{safe_number}_{datetime.now().strftime("%Y%m%d%H%M%S")}.pdf'
    upload_dir = current_app.config['UPLOAD_FOLDER']
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(pdf_bytes)
    return filename


def _positions_from_items(items) -> list[dict]:
    """Convert QuoteItem/InvoiceItem list to generator-compatible positions."""
    return [
        {
            'name': item.description,
            'quantity': item.quantity,
            'unit_price': item.unit_price,
            'price_per_day': item.unit_price,  # compatibility with generator
            'total': item.total,
            'is_bundle': False,
        }
        for item in items
    ]


def _get_effective_tax_rate(treatment: str, settings: SiteSettings, custom_rate: float | None = None) -> float:
    """Resolve the effective tax rate for a treatment."""
    return get_tax_rate_for_treatment(treatment, settings, custom_rate)


def _logo_path(settings: SiteSettings) -> str | None:
    """Get the logo file path if configured."""
    if settings.logo_filename:
        path = os.path.join(current_app.config['UPLOAD_FOLDER'], settings.logo_filename)
        if os.path.exists(path):
            return path
    return None


# ── Customers ────────────────────────────────────────────────────────────

@invoicing_bp.route('/customers')
@login_required
def customers():
    """List all customers."""
    all_customers = Customer.query.order_by(Customer.name).all()
    return render_template('admin/invoicing/customers.html', customers=all_customers)


@invoicing_bp.route('/customers/new', methods=['GET', 'POST'])
@login_required
def customer_new():
    """Create a new customer."""
    if request.method == 'POST':
        customer = Customer(
            name=request.form['name'].strip(),
            company=request.form.get('company', '').strip() or None,
            address=request.form.get('address', '').strip() or None,
            email=request.form.get('email', '').strip() or None,
            phone=request.form.get('phone', '').strip() or None,
            notes=request.form.get('notes', '').strip() or None,
        )
        db.session.add(customer)
        db.session.commit()
        flash('Kunde wurde angelegt.', 'success')
        return redirect(url_for('invoicing.customers'))
    return render_template('admin/invoicing/customer_form.html', customer=None)


@invoicing_bp.route('/customers/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def customer_edit(id):
    """Edit an existing customer."""
    customer = Customer.query.get_or_404(id)
    if request.method == 'POST':
        customer.name = request.form['name'].strip()
        customer.company = request.form.get('company', '').strip() or None
        customer.address = request.form.get('address', '').strip() or None
        customer.email = request.form.get('email', '').strip() or None
        customer.phone = request.form.get('phone', '').strip() or None
        customer.notes = request.form.get('notes', '').strip() or None
        db.session.commit()
        flash('Kunde wurde aktualisiert.', 'success')
        return redirect(url_for('invoicing.customers'))
    return render_template('admin/invoicing/customer_form.html', customer=customer)


@invoicing_bp.route('/customers/<int:id>/delete', methods=['POST'])
@login_required
def customer_delete(id):
    """Delete a customer (only if no quotes/invoices reference it)."""
    customer = Customer.query.get_or_404(id)
    if customer.quotes.count() > 0 or customer.invoices.count() > 0:
        flash('Kunde kann nicht gelöscht werden – es existieren verknüpfte Angebote oder Rechnungen.', 'danger')
        return redirect(url_for('invoicing.customers'))
    db.session.delete(customer)
    db.session.commit()
    flash('Kunde wurde gelöscht.', 'success')
    return redirect(url_for('invoicing.customers'))


# ── Quotes (Angebote) ───────────────────────────────────────────────────

@invoicing_bp.route('/quotes')
@login_required
def quotes():
    """List all quotes with optional filters."""
    status_filter = request.args.get('status', '')
    year = request.args.get('year', date.today().year, type=int)

    query = Quote.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    if year:
        query = query.filter(db.extract('year', Quote.date) == year)
    all_quotes = query.order_by(Quote.date.desc(), Quote.id.desc()).all()

    return render_template('admin/invoicing/quotes.html',
                           quotes=all_quotes,
                           status_filter=status_filter,
                           year=year,
                           years=get_year_choices())


@invoicing_bp.route('/quotes/new', methods=['GET', 'POST'])
@login_required
def quote_new():
    """Create a new quote."""
    settings = SiteSettings.get_settings()
    customers = Customer.query.order_by(Customer.name).all()

    # Pre-fill from asset if linked
    asset_id = request.args.get('asset_id', type=int)
    prefill_asset = Asset.query.get(asset_id) if asset_id else None

    if request.method == 'POST':
        prefix = settings.quote_number_prefix or 'A'
        quote_number = _next_number(prefix, Quote, 'quote_number')

        valid_until_str = request.form.get('valid_until', '')
        payment_terms = request.form.get('payment_terms_days', '14')

        quote = Quote(
            quote_number=quote_number,
            customer_id=request.form.get('customer_id', type=int) or None,
            date=parse_date(request.form['date']),
            valid_until=parse_date(valid_until_str) if valid_until_str else None,
            status='draft',
            tax_treatment=request.form.get('tax_treatment', 'none'),
            tax_rate=_get_effective_tax_rate(
                request.form.get('tax_treatment', 'none'),
                settings,
                float(request.form.get('custom_tax_rate', 0) or 0)
            ),
            discount_percent=float(request.form.get('discount_percent', 0) or 0),
            notes=request.form.get('notes', '').strip() or None,
            agb_text=request.form.get('agb_text', '').strip() or None,
            payment_terms_days=int(payment_terms) if payment_terms else 14,
            linked_asset_id=request.form.get('linked_asset_id', type=int) or None,
        )
        db.session.add(quote)
        db.session.flush()  # Get ID

        # Parse items
        _save_items(quote, request.form)

        db.session.commit()
        flash(f'Angebot {quote.quote_number} wurde erstellt.', 'success')
        return redirect(url_for('invoicing.quote_detail', id=quote.id))

    return render_template('admin/invoicing/quote_form.html',
                           quote=None,
                           customers=customers,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat(),
                           default_agb=settings.default_agb_text or '',
                           prefill_asset=prefill_asset)


@invoicing_bp.route('/quotes/<int:id>')
@login_required
def quote_detail(id):
    """View quote details."""
    quote = Quote.query.get_or_404(id)
    settings = SiteSettings.get_settings()
    return render_template('admin/invoicing/quote_detail.html',
                           quote=quote,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS)


@invoicing_bp.route('/quotes/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def quote_edit(id):
    """Edit an existing quote."""
    quote = Quote.query.get_or_404(id)
    settings = SiteSettings.get_settings()
    customers = Customer.query.order_by(Customer.name).all()

    if request.method == 'POST':
        quote.customer_id = request.form.get('customer_id', type=int) or None
        quote.date = parse_date(request.form['date'])
        valid_until_str = request.form.get('valid_until', '')
        quote.valid_until = parse_date(valid_until_str) if valid_until_str else None
        quote.tax_treatment = request.form.get('tax_treatment', 'none')
        quote.tax_rate = _get_effective_tax_rate(
            quote.tax_treatment, settings,
            float(request.form.get('custom_tax_rate', 0) or 0)
        )
        quote.discount_percent = float(request.form.get('discount_percent', 0) or 0)
        quote.notes = request.form.get('notes', '').strip() or None
        quote.agb_text = request.form.get('agb_text', '').strip() or None
        payment_terms = request.form.get('payment_terms_days', '14')
        quote.payment_terms_days = int(payment_terms) if payment_terms else 14

        # Replace items
        QuoteItem.query.filter_by(quote_id=quote.id).delete()
        _save_items(quote, request.form)

        # Regenerate PDF if it existed
        if quote.document_filename:
            _generate_quote_pdf(quote, settings)

        db.session.commit()
        flash('Angebot wurde aktualisiert.', 'success')
        return redirect(url_for('invoicing.quote_detail', id=quote.id))

    return render_template('admin/invoicing/quote_form.html',
                           quote=quote,
                           customers=customers,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat(),
                           default_agb=settings.default_agb_text or '',
                           prefill_asset=None)


@invoicing_bp.route('/quotes/<int:id>/generate-pdf', methods=['POST'])
@login_required
def quote_generate_pdf(id):
    """Generate or regenerate the quote PDF."""
    quote = Quote.query.get_or_404(id)
    settings = SiteSettings.get_settings()
    _generate_quote_pdf(quote, settings)
    db.session.commit()
    flash('PDF wurde generiert.', 'success')
    return redirect(url_for('invoicing.quote_detail', id=quote.id))


@invoicing_bp.route('/quotes/<int:id>/download')
@login_required
def quote_download(id):
    """Download the quote PDF."""
    quote = Quote.query.get_or_404(id)
    if not quote.document_filename:
        flash('Kein PDF vorhanden. Bitte zuerst generieren.', 'warning')
        return redirect(url_for('invoicing.quote_detail', id=quote.id))
    filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], quote.document_filename)
    if not os.path.exists(filepath):
        flash('PDF-Datei nicht gefunden.', 'danger')
        return redirect(url_for('invoicing.quote_detail', id=quote.id))
    return send_file(filepath, as_attachment=True,
                     download_name=f'Angebot_{quote.quote_number}.pdf')


@invoicing_bp.route('/quotes/<int:id>/set-status', methods=['POST'])
@login_required
def quote_set_status(id):
    """Change quote status."""
    quote = Quote.query.get_or_404(id)
    new_status = request.form.get('status', 'draft')
    allowed = ['draft', 'sent', 'accepted', 'rejected']
    if new_status in allowed:
        quote.status = new_status
        db.session.commit()
        labels = {'draft': 'Entwurf', 'sent': 'Versendet', 'accepted': 'Angenommen', 'rejected': 'Abgelehnt'}
        flash(f'Status → {labels.get(new_status, new_status)}', 'success')
    return redirect(url_for('invoicing.quote_detail', id=quote.id))


@invoicing_bp.route('/quotes/<int:id>/create-invoice', methods=['POST'])
@login_required
def quote_create_invoice(id):
    """Create an invoice from a quote."""
    quote = Quote.query.get_or_404(id)
    settings = SiteSettings.get_settings()

    prefix = settings.invoice_number_prefix or 'R'
    invoice_number = _next_number(prefix, Invoice, 'invoice_number')

    invoice = Invoice(
        invoice_number=invoice_number,
        quote_id=quote.id,
        customer_id=quote.customer_id,
        date=date.today(),
        due_date=date.today() + timedelta(days=quote.payment_terms_days or 14),
        status='draft',
        tax_treatment=quote.tax_treatment,
        tax_rate=quote.tax_rate,
        discount_percent=quote.discount_percent,
        notes=quote.notes,
        payment_terms_days=quote.payment_terms_days,
        linked_asset_id=quote.linked_asset_id,
    )
    db.session.add(invoice)
    db.session.flush()

    # Copy items from quote
    for qi in quote.items:
        ii = InvoiceItem(
            invoice_id=invoice.id,
            position=qi.position,
            description=qi.description,
            quantity=qi.quantity,
            unit=qi.unit,
            unit_price=qi.unit_price,
        )
        db.session.add(ii)

    # Mark quote as invoiced
    quote.status = 'invoiced'

    # Generate invoice PDF
    _generate_invoice_pdf(invoice, settings)

    db.session.commit()
    flash(f'Rechnung {invoice.invoice_number} wurde aus Angebot erstellt.', 'success')
    return redirect(url_for('invoicing.invoice_detail', id=invoice.id))


@invoicing_bp.route('/quotes/<int:id>/delete', methods=['POST'])
@login_required
def quote_delete(id):
    """Delete a quote."""
    quote = Quote.query.get_or_404(id)
    # Check if invoices reference this quote
    if quote.invoices.count() > 0:
        flash('Angebot kann nicht gelöscht werden – es existieren verknüpfte Rechnungen.', 'danger')
        return redirect(url_for('invoicing.quote_detail', id=quote.id))

    # Archive PDF if exists
    if quote.document_filename:
        _archive_document(quote.document_filename)

    db.session.delete(quote)
    db.session.commit()
    flash('Angebot wurde gelöscht.', 'success')
    return redirect(url_for('invoicing.quotes'))


# ── Invoices (Rechnungen) ────────────────────────────────────────────────

@invoicing_bp.route('/invoices')
@login_required
def invoices():
    """List all invoices with optional filters."""
    status_filter = request.args.get('status', '')
    year = request.args.get('year', date.today().year, type=int)

    query = Invoice.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    if year:
        query = query.filter(db.extract('year', Invoice.date) == year)
    all_invoices = query.order_by(Invoice.date.desc(), Invoice.id.desc()).all()

    # Calculate totals
    total_amount = sum(inv.total for inv in all_invoices)
    paid_amount = sum(inv.total for inv in all_invoices if inv.status == 'paid')
    open_amount = sum(inv.total for inv in all_invoices if inv.status in ('draft', 'sent'))

    return render_template('admin/invoicing/invoices.html',
                           invoices=all_invoices,
                           status_filter=status_filter,
                           year=year,
                           years=get_year_choices(),
                           total_amount=total_amount,
                           paid_amount=paid_amount,
                           open_amount=open_amount)


@invoicing_bp.route('/invoices/new', methods=['GET', 'POST'])
@login_required
def invoice_new():
    """Create a new invoice directly (without quote)."""
    settings = SiteSettings.get_settings()
    customers = Customer.query.order_by(Customer.name).all()

    if request.method == 'POST':
        prefix = settings.invoice_number_prefix or 'R'
        invoice_number = _next_number(prefix, Invoice, 'invoice_number')

        payment_terms = request.form.get('payment_terms_days', '14')
        pt_days = int(payment_terms) if payment_terms else 14
        inv_date = parse_date(request.form['date'])

        invoice = Invoice(
            invoice_number=invoice_number,
            customer_id=request.form.get('customer_id', type=int) or None,
            date=inv_date,
            due_date=inv_date + timedelta(days=pt_days),
            status='draft',
            tax_treatment=request.form.get('tax_treatment', 'none'),
            tax_rate=_get_effective_tax_rate(
                request.form.get('tax_treatment', 'none'),
                settings,
                float(request.form.get('custom_tax_rate', 0) or 0)
            ),
            discount_percent=float(request.form.get('discount_percent', 0) or 0),
            notes=request.form.get('notes', '').strip() or None,
            payment_terms_days=pt_days,
            linked_asset_id=request.form.get('linked_asset_id', type=int) or None,
        )
        db.session.add(invoice)
        db.session.flush()

        _save_invoice_items(invoice, request.form)

        db.session.commit()
        flash(f'Rechnung {invoice.invoice_number} wurde erstellt.', 'success')
        return redirect(url_for('invoicing.invoice_detail', id=invoice.id))

    return render_template('admin/invoicing/invoice_form.html',
                           invoice=None,
                           customers=customers,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat())


@invoicing_bp.route('/invoices/<int:id>')
@login_required
def invoice_detail(id):
    """View invoice details."""
    invoice = Invoice.query.get_or_404(id)
    settings = SiteSettings.get_settings()
    accounts = Account.query.order_by(Account.sort_order, Account.name).all()
    income_categories = Category.query.filter_by(type='income').order_by(Category.sort_order).all()
    return render_template('admin/invoicing/invoice_detail.html',
                           invoice=invoice,
                           settings=settings,
                           accounts=accounts,
                           income_categories=income_categories,
                           tax_treatment_labels=TAX_TREATMENT_LABELS)


@invoicing_bp.route('/invoices/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def invoice_edit(id):
    """Edit an existing invoice."""
    invoice = Invoice.query.get_or_404(id)
    settings = SiteSettings.get_settings()
    customers = Customer.query.order_by(Customer.name).all()

    if invoice.status == 'paid':
        flash('Bezahlte Rechnungen können nicht bearbeitet werden.', 'warning')
        return redirect(url_for('invoicing.invoice_detail', id=invoice.id))

    if request.method == 'POST':
        invoice.customer_id = request.form.get('customer_id', type=int) or None
        invoice.date = parse_date(request.form['date'])
        invoice.tax_treatment = request.form.get('tax_treatment', 'none')
        invoice.tax_rate = _get_effective_tax_rate(
            invoice.tax_treatment, settings,
            float(request.form.get('custom_tax_rate', 0) or 0)
        )
        invoice.discount_percent = float(request.form.get('discount_percent', 0) or 0)
        invoice.notes = request.form.get('notes', '').strip() or None
        payment_terms = request.form.get('payment_terms_days', '14')
        invoice.payment_terms_days = int(payment_terms) if payment_terms else 14
        invoice.due_date = invoice.date + timedelta(days=invoice.payment_terms_days)

        InvoiceItem.query.filter_by(invoice_id=invoice.id).delete()
        _save_invoice_items(invoice, request.form)

        if invoice.document_filename:
            _generate_invoice_pdf(invoice, settings)

        db.session.commit()
        flash('Rechnung wurde aktualisiert.', 'success')
        return redirect(url_for('invoicing.invoice_detail', id=invoice.id))

    return render_template('admin/invoicing/invoice_form.html',
                           invoice=invoice,
                           customers=customers,
                           settings=settings,
                           tax_treatment_labels=TAX_TREATMENT_LABELS,
                           today=date.today().isoformat())


@invoicing_bp.route('/invoices/<int:id>/generate-pdf', methods=['POST'])
@login_required
def invoice_generate_pdf(id):
    """Generate or regenerate the invoice PDF."""
    invoice = Invoice.query.get_or_404(id)
    settings = SiteSettings.get_settings()
    _generate_invoice_pdf(invoice, settings)
    db.session.commit()
    flash('PDF wurde generiert.', 'success')
    return redirect(url_for('invoicing.invoice_detail', id=invoice.id))


@invoicing_bp.route('/invoices/<int:id>/download')
@login_required
def invoice_download(id):
    """Download the invoice PDF."""
    invoice = Invoice.query.get_or_404(id)
    if not invoice.document_filename:
        flash('Kein PDF vorhanden. Bitte zuerst generieren.', 'warning')
        return redirect(url_for('invoicing.invoice_detail', id=invoice.id))
    filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], invoice.document_filename)
    if not os.path.exists(filepath):
        flash('PDF-Datei nicht gefunden.', 'danger')
        return redirect(url_for('invoicing.invoice_detail', id=invoice.id))
    return send_file(filepath, as_attachment=True,
                     download_name=f'Rechnung_{invoice.invoice_number}.pdf')


@invoicing_bp.route('/invoices/<int:id>/set-status', methods=['POST'])
@login_required
def invoice_set_status(id):
    """Change invoice status (not paid – use mark-paid for that)."""
    invoice = Invoice.query.get_or_404(id)
    new_status = request.form.get('status', 'draft')
    allowed = ['draft', 'sent', 'cancelled']
    if new_status in allowed:
        if new_status == 'cancelled' and invoice.linked_transaction_id:
            flash('Rechnung mit verknüpfter Buchung kann nicht storniert werden. '
                  'Entfernen Sie zuerst die Buchung.', 'danger')
            return redirect(url_for('invoicing.invoice_detail', id=invoice.id))
        invoice.status = new_status
        db.session.commit()
        labels = {'draft': 'Entwurf', 'sent': 'Versendet', 'cancelled': 'Storniert'}
        flash(f'Status → {labels.get(new_status, new_status)}', 'success')
    return redirect(url_for('invoicing.invoice_detail', id=invoice.id))


@invoicing_bp.route('/invoices/<int:id>/mark-paid', methods=['POST'])
@login_required
def invoice_mark_paid(id):
    """Mark invoice as paid → create accounting transaction."""
    invoice = Invoice.query.get_or_404(id)
    settings = SiteSettings.get_settings()

    if invoice.status == 'paid':
        flash('Rechnung ist bereits als bezahlt markiert.', 'info')
        return redirect(url_for('invoicing.invoice_detail', id=invoice.id))

    account_id = request.form.get('account_id', type=int)
    category_id = request.form.get('category_id', type=int)
    payment_date_str = request.form.get('payment_date', '')
    payment_date = parse_date(payment_date_str) if payment_date_str else date.today()

    if not account_id:
        flash('Bitte wählen Sie ein Konto aus.', 'danger')
        return redirect(url_for('invoicing.invoice_detail', id=invoice.id))

    # Calculate tax amounts
    gross = invoice.total
    tax_treatment = invoice.tax_treatment or 'none'
    tax_rate = invoice.tax_rate or 0

    if settings.tax_mode == 'kleinunternehmer':
        tax_treatment = 'none'
        tax_rate = 0

    if tax_rate > 0:
        net_amount, tax_amount = calculate_tax(gross, tax_rate)
    else:
        net_amount = gross
        tax_amount = 0

    # Create income transaction
    tx = Transaction(
        date=payment_date,
        type='income',
        description=f'Rechnung {invoice.invoice_number}'
                    + (f' – {invoice.customer.display_name}' if invoice.customer else ''),
        amount=gross,
        net_amount=net_amount,
        tax_amount=tax_amount,
        tax_treatment=tax_treatment,
        tax_rate=tax_rate if tax_rate > 0 else None,
        category_id=category_id,
        account_id=account_id,
        linked_asset_id=invoice.linked_asset_id,
    )
    db.session.add(tx)
    db.session.flush()

    # Attach invoice PDF as document to transaction
    if invoice.document_filename:
        doc = Document(
            filename=invoice.document_filename,
            original_filename=f'Rechnung_{invoice.invoice_number}.pdf',
            entity_type='transaction',
            entity_id=tx.id,
        )
        db.session.add(doc)

    invoice.linked_transaction_id = tx.id
    invoice.status = 'paid'

    db.session.commit()
    flash(f'Rechnung als bezahlt markiert. Buchung #{tx.id} wurde erstellt.', 'success')
    return redirect(url_for('invoicing.invoice_detail', id=invoice.id))


@invoicing_bp.route('/invoices/<int:id>/unmark-paid', methods=['POST'])
@login_required
def invoice_unmark_paid(id):
    """Reverse paid status → delete accounting transaction."""
    invoice = Invoice.query.get_or_404(id)

    if invoice.status != 'paid' or not invoice.linked_transaction_id:
        flash('Rechnung ist nicht als bezahlt markiert.', 'info')
        return redirect(url_for('invoicing.invoice_detail', id=invoice.id))

    # Delete linked transaction and its documents
    tx = Transaction.query.get(invoice.linked_transaction_id)
    if tx:
        Document.query.filter_by(entity_type='transaction', entity_id=tx.id).delete()
        db.session.delete(tx)

    invoice.linked_transaction_id = None
    invoice.status = 'sent'  # Revert to sent

    db.session.commit()
    flash('Zahlung wurde rückgängig gemacht. Buchung wurde gelöscht.', 'success')
    return redirect(url_for('invoicing.invoice_detail', id=invoice.id))


@invoicing_bp.route('/invoices/<int:id>/delete', methods=['POST'])
@login_required
def invoice_delete(id):
    """Delete an invoice."""
    invoice = Invoice.query.get_or_404(id)

    if invoice.linked_transaction_id:
        flash('Rechnung mit verknüpfter Buchung kann nicht gelöscht werden. '
              'Machen Sie zuerst die Zahlung rückgängig.', 'danger')
        return redirect(url_for('invoicing.invoice_detail', id=invoice.id))

    # Unlink from quote
    if invoice.quote_id:
        quote = Quote.query.get(invoice.quote_id)
        if quote and quote.status == 'invoiced':
            # Check if there are other invoices from this quote
            other_invoices = Invoice.query.filter(
                Invoice.quote_id == quote.id,
                Invoice.id != invoice.id
            ).count()
            if other_invoices == 0:
                quote.status = 'accepted'

    # Archive PDF
    if invoice.document_filename:
        _archive_document(invoice.document_filename)

    db.session.delete(invoice)
    db.session.commit()
    flash('Rechnung wurde gelöscht.', 'success')
    return redirect(url_for('invoicing.invoices'))


# ── Internal helpers ─────────────────────────────────────────────────────

def _save_items(quote: Quote, form):
    """Parse line items from form and save to quote."""
    descriptions = form.getlist('item_description[]')
    quantities = form.getlist('item_quantity[]')
    units = form.getlist('item_unit[]')
    prices = form.getlist('item_price[]')

    for i, desc in enumerate(descriptions):
        if not desc.strip():
            continue
        qty = float(quantities[i]) if i < len(quantities) and quantities[i] else 1
        unit = units[i].strip() if i < len(units) and units[i] else 'Stk.'
        price = parse_amount(prices[i]) if i < len(prices) and prices[i] else 0

        item = QuoteItem(
            quote_id=quote.id,
            position=i + 1,
            description=desc.strip(),
            quantity=qty,
            unit=unit,
            unit_price=price,
        )
        db.session.add(item)


def _save_invoice_items(invoice: Invoice, form):
    """Parse line items from form and save to invoice."""
    descriptions = form.getlist('item_description[]')
    quantities = form.getlist('item_quantity[]')
    units = form.getlist('item_unit[]')
    prices = form.getlist('item_price[]')

    for i, desc in enumerate(descriptions):
        if not desc.strip():
            continue
        qty = float(quantities[i]) if i < len(quantities) and quantities[i] else 1
        unit = units[i].strip() if i < len(units) and units[i] else 'Stk.'
        price = parse_amount(prices[i]) if i < len(prices) and prices[i] else 0

        item = InvoiceItem(
            invoice_id=invoice.id,
            position=i + 1,
            description=desc.strip(),
            quantity=qty,
            unit=unit,
            unit_price=price,
        )
        db.session.add(item)


def _generate_quote_pdf(quote: Quote, settings: SiteSettings):
    """Generate the PDF for a quote and store it."""
    from generators.angebot import build_angebot_pdf

    sd = _settings_dict(settings)
    positions = _positions_from_items(quote.items)

    recipient_lines = []
    if quote.customer:
        recipient_lines = quote.customer.recipient_lines
    else:
        recipient_lines = ['']

    valid_until = quote.valid_until or (quote.date + timedelta(days=quote.payment_terms_days or 14))

    pdf_bytes = build_angebot_pdf(
        issuer_name=sd['business_name'],
        issuer_address=sd['address_lines'],
        contact_lines=sd['contact_lines'],
        bank_lines=sd['bank_lines'],
        tax_number=sd['tax_number'],
        vat_id=sd['vat_id'],
        tax_mode=sd['tax_mode'],
        tax_rate=quote.tax_rate or sd['tax_rate'],
        logo_path=_logo_path(settings),
        recipient_lines=recipient_lines,
        reference_number=quote.quote_number,
        angebot_datum=quote.date.strftime('%d.%m.%Y'),
        gueltig_bis=valid_until.strftime('%d.%m.%Y'),
        positions=positions,
        discount_percent=quote.discount_percent or 0,
        discount_amount=quote.discount_amount,
        subtotal=quote.subtotal,
        total=quote.total,
        payment_terms_days=quote.payment_terms_days or 14,
        notes=quote.notes,
        terms_and_conditions_text=quote.agb_text,
        simple_mode=True,
    )

    # Archive old PDF if exists
    if quote.document_filename:
        _archive_document(quote.document_filename)

    quote.document_filename = _save_pdf(pdf_bytes, 'Angebot', quote.quote_number)


def _generate_invoice_pdf(invoice: Invoice, settings: SiteSettings):
    """Generate the PDF for an invoice and store it."""
    from generators.rechnung import build_rechnung_pdf

    sd = _settings_dict(settings)
    positions = _positions_from_items(invoice.items)

    recipient_lines = []
    if invoice.customer:
        recipient_lines = invoice.customer.recipient_lines
    else:
        recipient_lines = ['']

    pdf_bytes = build_rechnung_pdf(
        issuer_name=sd['business_name'],
        issuer_address=sd['address_lines'],
        contact_lines=sd['contact_lines'],
        bank_lines=sd['bank_lines'],
        tax_number=sd['tax_number'],
        vat_id=sd['vat_id'],
        tax_mode=sd['tax_mode'],
        tax_rate=invoice.tax_rate or sd['tax_rate'],
        logo_path=_logo_path(settings),
        recipient_lines=recipient_lines,
        reference_number=invoice.invoice_number,
        rechnungs_datum=invoice.date.strftime('%d.%m.%Y'),
        positions=positions,
        discount_percent=invoice.discount_percent or 0,
        discount_amount=invoice.discount_amount,
        subtotal=invoice.subtotal,
        total=invoice.total,
        payment_terms_days=invoice.payment_terms_days or 14,
        notes=invoice.notes,
        simple_mode=True,
    )

    # Try to embed ZUGFeRD XML
    try:
        from generators.einvoice import get_standard
        from generators.einvoice.base import EInvoiceData, EInvoiceLineItem
        from generators.einvoice.embed import embed_xml_in_pdf

        line_items = []
        tax_rate = invoice.tax_rate or sd['tax_rate']
        tax_factor = 1 + tax_rate / 100

        for item in invoice.items:
            net_price = round(item.unit_price / tax_factor, 2)
            line_total_net = round(item.total / tax_factor, 2)
            li = EInvoiceLineItem(
                position_number=str(item.position),
                name=item.description,
                quantity=item.quantity,
                unit_code='C62',
                unit_price_net=net_price,
                line_total_net=line_total_net,
                tax_rate=tax_rate,
                tax_category='S' if tax_rate > 0 else 'E',
            )
            line_items.append(li)

        gross_total = invoice.total
        net_total = round(gross_total / tax_factor, 2)
        tax_total = round(gross_total - net_total, 2)

        buyer_name = ''
        if invoice.customer:
            buyer_name = invoice.customer.company or invoice.customer.name

        einvoice_data = EInvoiceData(
            invoice_number=invoice.invoice_number,
            invoice_date=invoice.date,
            seller_name=sd['business_name'],
            seller_address_lines=sd['address_lines'],
            seller_tax_number=sd['tax_number'],
            seller_vat_id=sd['vat_id'],
            buyer_name=buyer_name,
            buyer_address_lines=invoice.customer.recipient_lines if invoice.customer else [],
            tax_rate=tax_rate,
            tax_mode=sd['tax_mode'],
            total_net=net_total,
            total_tax=tax_total,
            total_gross=gross_total,
            payment_terms_days=invoice.payment_terms_days or 14,
            bank_lines=sd['bank_lines'],
            line_items=line_items,
        )

        standard = get_standard('zugferd')
        xml_bytes = standard.generate_xml(einvoice_data)
        pdf_bytes = embed_xml_in_pdf(pdf_bytes, xml_bytes, standard)
    except Exception:
        pass  # ZUGFeRD embedding is best-effort

    # Archive old PDF if exists
    if invoice.document_filename:
        _archive_document(invoice.document_filename)

    invoice.document_filename = _save_pdf(pdf_bytes, 'Rechnung', invoice.invoice_number)


def _archive_document(filename: str):
    """Move a document to the archive folder."""
    from audit import archive_file
    upload_dir = current_app.config['UPLOAD_FOLDER']
    archive_file(upload_dir, filename)
