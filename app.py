import os
import hashlib
from flask import Flask, send_file, request
from flask_login import LoginManager
from models import db, User, SiteSettings, Account, Document, AuditLog
from werkzeug.security import generate_password_hash
from helpers import format_currency, format_date
from audit import init_audit

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Optional minification
try:
    import cssmin
    def minify_css(content):
        return cssmin.cssmin(content)
except ImportError:
    def minify_css(content):
        return content

try:
    import rjsmin
    def minify_js(content):
        return rjsmin.jsmin(content)
except ImportError:
    def minify_js(content):
        return content


def create_app():
    app = Flask(__name__, instance_relative_config=True)

    # Configuration
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.instance_path, 'accounting.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB upload limit
    app.config['UPLOAD_FOLDER'] = os.path.join(app.instance_path, 'uploads')

    # Ensure instance and upload folders exist
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'archive'), exist_ok=True)

    # Initialize extensions
    db.init_app(app)
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Bitte melden Sie sich an.'
    login_manager.login_message_category = 'info'

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Initialize audit trail (must be before first commit)
    init_audit(app, db)

    # Create tables and seed default data
    with app.app_context():
        db.create_all()
        _migrate_schema(db)
        _seed_defaults(app)

    # Context processors
    @app.context_processor
    def inject_globals():
        settings = SiteSettings.get_settings()
        has_fav = bool(settings.favicon_filename)
        fav_mime = 'image/x-icon'
        if has_fav:
            ext = os.path.splitext(settings.favicon_filename)[1].lower()
            fav_mime = FAVICON_MIMETYPES.get(ext, 'image/x-icon')
        return {
            'site_settings': settings,
            'has_favicon': has_fav,
            'favicon_mimetype': fav_mime,
        }

    # Template filters
    app.jinja_env.filters['currency'] = format_currency
    app.jinja_env.filters['date_format'] = format_date

    def pretty_json_filter(value):
        """Format a JSON string for display in the audit log."""
        import json as _json
        if not value:
            return value
        try:
            obj = _json.loads(value) if isinstance(value, str) else value
            return _json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)
        except Exception:
            return value
    app.jinja_env.filters['pretty_json'] = pretty_json_filter

    # Template globals – helper to load documents for an entity
    def get_documents(entity_type, entity_id):
        return Document.query.filter_by(entity_type=entity_type, entity_id=entity_id).all()
    app.jinja_env.globals['get_documents'] = get_documents

    # Static file serving with minification and ETag caching
    @app.route('/static/<path:filename>')
    def serve_static(filename):
        filepath = os.path.join(app.static_folder, filename)
        if not os.path.exists(filepath):
            return 'Not found', 404

        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        if filename.endswith('.css'):
            content = minify_css(content)
            mimetype = 'text/css'
        elif filename.endswith('.js'):
            content = minify_js(content)
            mimetype = 'application/javascript'
        else:
            return app.send_static_file(filename)

        etag = hashlib.md5(content.encode()).hexdigest()
        if request.headers.get('If-None-Match') == etag:
            return '', 304

        response = app.make_response(content)
        response.headers['Content-Type'] = mimetype
        response.headers['ETag'] = etag
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response

    # Favicon
    @app.route('/favicon.ico')
    def favicon():
        settings = SiteSettings.get_settings()
        if settings.favicon_filename:
            upload_dir = app.config['UPLOAD_FOLDER']
            fpath = os.path.join(upload_dir, settings.favicon_filename)
            if os.path.exists(fpath):
                ext = os.path.splitext(settings.favicon_filename)[1].lower()
                mime = FAVICON_MIMETYPES.get(ext, 'image/x-icon')
                return send_file(fpath, mimetype=mime)
        return '', 204

    # Register blueprints
    from blueprints.auth import auth_bp
    from blueprints.admin import admin_bp
    from blueprints.ai_chat import ai_bp
    from blueprints.api import api_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(ai_bp, url_prefix='/admin')
    app.register_blueprint(api_bp, url_prefix='/api/v1')

    # Redirect root to admin (no public pages in this app)
    @app.route('/')
    def index():
        from flask import redirect, url_for
        return redirect(url_for('admin.dashboard'))

    return app


# --- Favicon helpers ---

FAVICON_MIMETYPES = {
    '.ico': 'image/x-icon',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.svg': 'image/svg+xml',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
}


def _migrate_schema(db):
    """Add missing columns to existing tables (simple SQLite migration)."""
    import sqlite3
    conn = db.engine.raw_connection()
    cursor = conn.cursor()

    # Check and add missing columns
    migrations = [
        ('assets', 'depreciation_category_id', 'INTEGER'),
        ('assets', 'disposal_price_gross', 'REAL'),
        ('site_settings', 'vat_id', 'VARCHAR(100)'),
        ('site_settings', 'tax_rate_reduced', 'REAL DEFAULT 7.0'),
        ('transactions', 'tax_treatment', "VARCHAR(30) DEFAULT 'none'"),
        ('transactions', 'tax_rate', 'REAL'),
        ('assets', 'purchase_tax_treatment', "VARCHAR(30) DEFAULT 'none'"),
        ('assets', 'purchase_tax_rate', 'REAL'),
        ('assets', 'purchase_tax_amount', 'REAL'),
        ('assets', 'disposal_tax_treatment', 'VARCHAR(30)'),
        ('assets', 'disposal_tax_rate', 'REAL'),
        ('assets', 'disposal_tax_amount', 'REAL'),
        ('site_settings', 'favicon_filename', 'VARCHAR(200)'),
        ('assets', 'bundle_id', 'VARCHAR(36)'),
        ('transactions', 'account_id', 'INTEGER'),
        ('transactions', 'transfer_to_account_id', 'INTEGER'),
        ('transactions', 'linked_asset_id', 'INTEGER'),
    ]

    for table, column, col_type in migrations:
        try:
            cursor.execute(f"SELECT {column} FROM {table} LIMIT 1")
        except Exception:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()

    # Ensure accounts table exists (create if missing for pre-accounts databases)
    try:
        cursor.execute("SELECT id FROM accounts LIMIT 1")
    except Exception:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                description TEXT,
                initial_balance REAL DEFAULT 0.0,
                sort_order INTEGER DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME
            )
        """)
        conn.commit()

    # Seed default "Bank" account and assign to existing transactions
    cursor.execute("SELECT COUNT(*) FROM accounts")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO accounts (name, description, initial_balance, sort_order, created_at, updated_at) "
            "VALUES ('Bank', 'Standard-Bankkonto', 0.0, 1, datetime('now'), datetime('now'))"
        )
        bank_id = cursor.lastrowid
        # Set all existing transactions to the default bank account
        cursor.execute("UPDATE transactions SET account_id = ? WHERE account_id IS NULL", (bank_id,))
        conn.commit()

    # Ensure documents table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename VARCHAR(300) NOT NULL,
            original_filename VARCHAR(300),
            entity_type VARCHAR(20) NOT NULL,
            entity_id INTEGER NOT NULL,
            created_at DATETIME
        )
    """)
    conn.commit()

    # Ensure audit_log table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            user_id INTEGER,
            username VARCHAR(100),
            ip_address VARCHAR(45),
            source VARCHAR(20) NOT NULL DEFAULT 'web',
            action VARCHAR(10) NOT NULL,
            entity_type VARCHAR(50) NOT NULL,
            entity_id INTEGER,
            old_values TEXT,
            new_values TEXT,
            archived_files TEXT,
            previous_hash VARCHAR(64) NOT NULL,
            entry_hash VARCHAR(64) NOT NULL
        )
    """)
    conn.commit()

    # Migrate legacy document_filename from transactions and assets into documents table
    _migrate_legacy_documents(cursor, conn, 'transactions', 'transaction')
    _migrate_legacy_documents(cursor, conn, 'assets', 'asset')

    conn.close()


def _migrate_legacy_documents(cursor, conn, table, entity_type):
    """Move existing document_filename values into the documents table."""
    try:
        cursor.execute(f"SELECT id, document_filename FROM {table} WHERE document_filename IS NOT NULL AND document_filename != ''")
    except Exception:
        return  # column doesn't exist yet

    rows = cursor.fetchall()
    for entity_id, filename in rows:
        # Check if already migrated
        cursor.execute(
            "SELECT COUNT(*) FROM documents WHERE entity_type = ? AND entity_id = ? AND filename = ?",
            (entity_type, entity_id, filename),
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "INSERT INTO documents (filename, original_filename, entity_type, entity_id, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (filename, filename, entity_type, entity_id),
            )
    conn.commit()


def _seed_defaults(app):
    """Create default admin user and settings if they don't exist."""
    admin_username = os.environ.get('ADMIN_USERNAME', 'admin')
    admin_password = os.environ.get('ADMIN_PASSWORD', '')

    existing_admin = User.query.filter_by(username=admin_username).first()
    if not existing_admin:
        # Check if there are ANY admin users at all; if not, create one
        if not User.query.filter_by(is_admin=True).first():
            admin = User(
                username=admin_username,
                password_hash=generate_password_hash(admin_password) if admin_password else generate_password_hash('password123'),
                display_name='Administrator',
                is_admin=True,
            )
            db.session.add(admin)
            db.session.commit()
    elif admin_password:
        # Admin user exists – update password if env var is set
        existing_admin.password_hash = generate_password_hash(admin_password)
        db.session.commit()

    # Ensure settings exist
    SiteSettings.get_settings()

    # Seed default EÜR categories
    from models import Category
    if Category.query.count() == 0:
        default_categories = [
            # Income categories
            Category(name='Umsatzerlöse', type='income', sort_order=1,
                     description='Erlöse aus Lieferungen und Leistungen'),
            Category(name='Sonstige Einnahmen', type='income', sort_order=2,
                     description='Zinsen, Erstattungen, etc.'),
            Category(name='Privateinlagen', type='income', sort_order=3,
                     description='Private Einlagen in das Unternehmen'),
            # Expense categories
            Category(name='Wareneinkauf', type='expense', sort_order=10,
                     description='Einkauf von Waren und Materialien'),
            Category(name='Personalkosten', type='expense', sort_order=11,
                     description='Gehälter, Löhne, Sozialabgaben'),
            Category(name='Miete & Nebenkosten', type='expense', sort_order=12,
                     description='Büromiete, Betriebskosten'),
            Category(name='Versicherungen', type='expense', sort_order=13,
                     description='Betriebliche Versicherungen'),
            Category(name='Fahrzeugkosten', type='expense', sort_order=14,
                     description='Kraftstoff, Wartung, Versicherung, Leasing'),
            Category(name='Reisekosten', type='expense', sort_order=15,
                     description='Geschäftsreisen, Verpflegungsmehraufwand'),
            Category(name='Porto & Telefon', type='expense', sort_order=16,
                     description='Kommunikationskosten'),
            Category(name='Bürobedarf', type='expense', sort_order=17,
                     description='Büromaterial, Druckerpatronen, etc.'),
            Category(name='Abschreibungen', type='expense', sort_order=18,
                     description='AfA auf Anlagegüter'),
            Category(name='Software & IT', type='expense', sort_order=19,
                     description='Software, Hosting, Domains'),
            Category(name='Beratungskosten', type='expense', sort_order=20,
                     description='Steuerberater, Rechtsanwalt'),
            Category(name='Werbekosten', type='expense', sort_order=21,
                     description='Marketing, Werbung, Visitenkarten'),
            Category(name='Sonstige Ausgaben', type='expense', sort_order=99,
                     description='Nicht zugeordnete Ausgaben'),
        ]
        db.session.add_all(default_categories)
        db.session.commit()

    # Seed default depreciation categories (AfA-Kategorien)
    from models import DepreciationCategory
    if DepreciationCategory.query.count() == 0:
        default_dep_cats = [
            DepreciationCategory(name='Computer / IT-Hardware', useful_life_months=36,
                                 default_method='linear', sort_order=1,
                                 description='Laptops, Desktop-PCs, Server'),
            DepreciationCategory(name='Software', useful_life_months=36,
                                 default_method='linear', sort_order=2,
                                 description='Standardsoftware, ERP-Systeme'),
            DepreciationCategory(name='Digitale Wirtschaftsgüter', useful_life_months=12,
                                 default_method='linear', sort_order=3,
                                 description='Gem. BMF-Schreiben: Hardware & Software'),
            DepreciationCategory(name='Büromöbel', useful_life_months=156,
                                 default_method='linear', sort_order=10,
                                 description='Schreibtische, Regale, Schränke'),
            DepreciationCategory(name='Bürostühle', useful_life_months=156,
                                 default_method='linear', sort_order=11,
                                 description='Büro- und Konferenzstühle'),
            DepreciationCategory(name='Drucker / Scanner', useful_life_months=72,
                                 default_method='linear', sort_order=12,
                                 description='Drucker, Scanner, Multifunktionsgeräte'),
            DepreciationCategory(name='Smartphone / Tablet', useful_life_months=60,
                                 default_method='linear', sort_order=13,
                                 description='Mobiltelefone, Tablets'),
            DepreciationCategory(name='Pkw', useful_life_months=72,
                                 default_method='linear', sort_order=20,
                                 description='Personenkraftwagen'),
            DepreciationCategory(name='Lkw / Transporter', useful_life_months=108,
                                 default_method='linear', sort_order=21,
                                 description='Nutzfahrzeuge, Transporter'),
            DepreciationCategory(name='Fahrrad / E-Bike', useful_life_months=84,
                                 default_method='linear', sort_order=22,
                                 description='Fahrräder, E-Bikes, Lastenräder'),
            DepreciationCategory(name='Werkzeuge', useful_life_months=60,
                                 default_method='linear', sort_order=30,
                                 description='Handwerkzeuge, Elektrowerkzeuge'),
            DepreciationCategory(name='Maschinen (allgemein)', useful_life_months=120,
                                 default_method='linear', sort_order=31,
                                 description='Produktionsmaschinen, Anlagen'),
            DepreciationCategory(name='Fotografie-Ausrüstung', useful_life_months=84,
                                 default_method='linear', sort_order=32,
                                 description='Kameras, Objektive, Beleuchtung'),
            DepreciationCategory(name='Haushaltsgeräte', useful_life_months=120,
                                 default_method='linear', sort_order=33,
                                 description='Kühlschrank, Spülmaschine, etc.'),
        ]
        db.session.add_all(default_dep_cats)
        db.session.commit()


# Create app
app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
