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
    type = db.Column(db.String(20), nullable=False)  # 'income' or 'expense'
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
    document_filename = db.Column(db.String(300), nullable=True)  # Receipt/invoice scan
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
