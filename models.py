from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, date

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """Application user for authentication."""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(200), default='')
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<User {self.username}>'


class SiteSettings(db.Model):
    """Global application settings (single row)."""
    __tablename__ = 'site_settings'

    id = db.Column(db.Integer, primary_key=True)
    business_name = db.Column(db.String(200), default='Meine Buchhaltung')
    address_lines = db.Column(db.Text, nullable=True)
    contact_lines = db.Column(db.Text, nullable=True)
    bank_lines = db.Column(db.Text, nullable=True)
    tax_number = db.Column(db.String(100), nullable=True)
    vat_id = db.Column(db.String(100), nullable=True)  # USt-IdNr.
    tax_mode = db.Column(db.String(20), default='kleinunternehmer')  # 'kleinunternehmer' or 'regular'
    tax_rate = db.Column(db.Float, default=19.0)  # Regelsteuersatz
    tax_rate_reduced = db.Column(db.Float, default=7.0)  # Ermäßigter Steuersatz
    favicon_filename = db.Column(db.String(200), nullable=True)

    @staticmethod
    def get_settings():
        settings = SiteSettings.query.first()
        if not settings:
            settings = SiteSettings()
            db.session.add(settings)
            db.session.commit()
        return settings


class Account(db.Model):
    """A financial account (e.g. Bank, Bargeld, PayPal)."""
    __tablename__ = 'accounts'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    initial_balance = db.Column(db.Float, default=0.0)  # Startsaldo
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transactions = db.relationship('Transaction', backref='account',
                                   foreign_keys='Transaction.account_id', lazy='dynamic')
    incoming_transfers = db.relationship('Transaction', backref='transfer_to_account',
                                         foreign_keys='Transaction.transfer_to_account_id', lazy='dynamic')

    def get_balance(self, up_to_date=None):
        """Calculate running balance: initial + income - expense + transfers_in - transfers_out."""
        query = Transaction.query.filter_by(account_id=self.id)
        if up_to_date:
            query = query.filter(Transaction.date <= up_to_date)
        txns = query.all()
        balance = self.initial_balance or 0.0
        for t in txns:
            if t.type == 'transfer':
                balance -= t.amount  # outgoing transfer
            elif t.type == 'income':
                balance += t.amount
            elif t.type == 'expense':
                balance -= t.amount
        # Add incoming transfers
        q_in = Transaction.query.filter_by(transfer_to_account_id=self.id)
        if up_to_date:
            q_in = q_in.filter(Transaction.date <= up_to_date)
        for t in q_in.all():
            balance += t.amount
        return balance

    def __repr__(self):
        return f'<Account {self.name}>'


class Category(db.Model):
    """Categories for organizing transactions (EÜR categories)."""
    __tablename__ = 'categories'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # 'income' or 'expense'
    description = db.Column(db.Text, nullable=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    transactions = db.relationship('Transaction', backref='category', lazy='dynamic')

    def __repr__(self):
        return f'<Category {self.name} ({self.type})>'


class Transaction(db.Model):
    """A single income or expense transaction for EÜR."""
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=date.today)
    type = db.Column(db.String(20), nullable=False)  # 'income', 'expense', or 'transfer'
    description = db.Column(db.String(500), nullable=False)
    amount = db.Column(db.Float, nullable=False)  # Gross amount (brutto)
    net_amount = db.Column(db.Float, nullable=True)  # Net amount (netto), calculated
    tax_amount = db.Column(db.Float, nullable=True)  # Tax portion, calculated
    tax_treatment = db.Column(db.String(30), default='none')  # Tax treatment type
    # 'none'           = Keine USt (Kleinunternehmer / nicht steuerbar)
    # 'standard'       = Regelsteuersatz (z.B. 19%)
    # 'reduced'        = Ermäßigter Satz (z.B. 7%)
    # 'tax_free'       = Steuerfrei (0%, z.B. §4 UStG, Ausfuhrlieferungen)
    # 'reverse_charge' = Reverse Charge (§13b UStG)
    # 'intra_eu'       = Innergemeinschaftlich (ig. Erwerb/Lieferung)
    # 'custom'         = Benutzerdefinierter Steuersatz
    tax_rate = db.Column(db.Float, nullable=True)  # Actual tax rate used for this transaction
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=True)
    transfer_to_account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=True)
    linked_asset_id = db.Column(db.Integer, db.ForeignKey('assets.id'), nullable=True)
    document_filename = db.Column(db.String(300), nullable=True)  # Receipt/invoice scan
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    linked_asset = db.relationship('Asset', backref='linked_transactions', foreign_keys=[linked_asset_id])

    def __repr__(self):
        return f'<Transaction {self.type} {self.amount} {self.date}>'


class Asset(db.Model):
    """
    A depreciable fixed asset (Anlagegut) for AfA tracking.

    Depreciation is calculated dynamically by the depreciation module,
    not stored, so law changes only require updating depreciation.py.
    """
    __tablename__ = 'assets'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text, nullable=True)
    bundle_id = db.Column(db.String(36), nullable=True, index=True)  # UUID grouping for bundle purchases

    # Purchase info
    purchase_date = db.Column(db.Date, nullable=False)
    purchase_price_gross = db.Column(db.Float, nullable=False)
    purchase_price_net = db.Column(db.Float, nullable=False)  # AfA-Bemessungsgrundlage
    purchase_tax_treatment = db.Column(db.String(30), default='none')  # Same options as Transaction
    purchase_tax_rate = db.Column(db.Float, nullable=True)  # Actual tax rate used
    purchase_tax_amount = db.Column(db.Float, nullable=True)  # Vorsteuer-Betrag

    # Depreciation settings
    depreciation_method = db.Column(db.String(20), nullable=False, default='linear')
    # 'sofort'       = GWG Sofortabschreibung (§ 6 Abs. 2)
    # 'linear'       = Lineare AfA (§ 7 Abs. 1)
    # 'sammelposten' = Pool depreciation (§ 6 Abs. 2a)
    # 'degressive'   = Degressive AfA (§ 7 Abs. 2)
    useful_life_months = db.Column(db.Integer, nullable=True)  # Nutzungsdauer in Monaten
    salvage_value = db.Column(db.Float, default=0.0)  # Erinnerungswert / Restwert
    depreciation_category_id = db.Column(db.Integer, db.ForeignKey('depreciation_categories.id'), nullable=True)

    # Disposal info (Abgang)
    disposal_date = db.Column(db.Date, nullable=True)
    disposal_price = db.Column(db.Float, nullable=True)  # Verkaufserlös (netto)
    disposal_price_gross = db.Column(db.Float, nullable=True)  # Verkaufserlös (brutto)
    disposal_tax_treatment = db.Column(db.String(30), nullable=True)  # Same options as Transaction
    disposal_tax_rate = db.Column(db.Float, nullable=True)  # Actual tax rate used
    disposal_tax_amount = db.Column(db.Float, nullable=True)  # USt-Betrag auf Verkauf
    disposal_reason = db.Column(db.String(50), nullable=True)
    # 'sold'         = Verkauft
    # 'scrapped'     = Verschrottet / Entsorgt
    # 'private_use'  = Privatentnahme
    # 'other'        = Sonstiger Abgang

    # Metadata
    document_filename = db.Column(db.String(300), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    depreciation_category = db.relationship('DepreciationCategory', backref='assets')

    @property
    def is_active(self):
        """Asset is active if not disposed."""
        return self.disposal_date is None

    @property
    def is_fully_depreciated(self):
        """Check if the asset has been fully depreciated."""
        from depreciation import get_book_value
        return get_book_value(self) <= (self.salvage_value or 0)

    def __repr__(self):
        return f'<Asset {self.name} ({self.depreciation_method})>'


class DepreciationCategory(db.Model):
    """
    User-defined depreciation categories (AfA-Kategorien).

    Based on official AfA-Tabellen but user-customizable.
    Each category defines a default useful life and depreciation method.
    """
    __tablename__ = 'depreciation_categories'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    useful_life_months = db.Column(db.Integer, nullable=False)  # Nutzungsdauer in Monaten
    default_method = db.Column(db.String(20), nullable=False, default='linear')
    description = db.Column(db.Text, nullable=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<DepreciationCategory {self.name} ({self.useful_life_months}m)>'


class ChatHistory(db.Model):
    """Persisted AI-chat state per user (single current chat, no archive)."""
    __tablename__ = 'chat_history'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    history_json = db.Column(db.Text, nullable=False, default='[]')
    html_content = db.Column(db.Text, nullable=False, default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<ChatHistory user_id={self.user_id}>'


class Document(db.Model):
    """
    A file attachment linked to a transaction or asset.
    Multiple documents can be attached to a single entity.
    """
    __tablename__ = 'documents'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300), nullable=False)          # stored filename on disk
    original_filename = db.Column(db.String(300), nullable=True)  # user-facing original name
    entity_type = db.Column(db.String(20), nullable=False)        # 'transaction' or 'asset'
    entity_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Document {self.filename} ({self.entity_type}:{self.entity_id})>'
