import os
import hashlib
import requests
from flask import Flask, send_file, request
from flask_login import LoginManager
from models import db, User, SiteSettings
from werkzeug.security import generate_password_hash
from helpers import format_currency, format_date

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

    # Create tables and seed default data
    with app.app_context():
        db.create_all()
        _migrate_schema(db)
        _seed_defaults(app)

    # Context processors
    @app.context_processor
    def inject_globals():
        settings = SiteSettings.get_settings()
        return {
            'site_settings': settings,
            'has_favicon': _favicon_data is not None,
            'favicon_mimetype': _favicon_mimetype,
        }

    # Template filters
    app.jinja_env.filters['currency'] = format_currency
    app.jinja_env.filters['date_format'] = format_date

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
        if _favicon_data:
            from io import BytesIO
            return send_file(BytesIO(_favicon_data), mimetype=_favicon_mimetype)
        return '', 204

    # Register blueprints
    from blueprints.auth import auth_bp
    from blueprints.admin import admin_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')

    # Redirect root to admin (no public pages in this app)
    @app.route('/')
    def index():
        from flask import redirect, url_for
        return redirect(url_for('admin.dashboard'))

    return app


# --- Favicon loading ---
_favicon_data = None
_favicon_mimetype = 'image/x-icon'


def _load_favicon():
    global _favicon_data, _favicon_mimetype
    favicon_url = os.environ.get('FAVICON_URL')
    if not favicon_url:
        return
    try:
        resp = requests.get(favicon_url, timeout=10)
        if resp.status_code == 200:
            _favicon_data = resp.content
            ct = resp.headers.get('Content-Type', '')
            if ct:
                _favicon_mimetype = ct.split(';')[0].strip()
    except Exception:
        pass


def _migrate_schema(db):
    """Add missing columns to existing tables (simple SQLite migration)."""
    import sqlite3
    conn = db.engine.raw_connection()
    cursor = conn.cursor()

    # Check and add missing columns
    migrations = [
        ('assets', 'depreciation_category_id', 'INTEGER'),
        ('assets', 'disposal_price_gross', 'REAL'),
    ]

    for table, column, col_type in migrations:
        try:
            cursor.execute(f"SELECT {column} FROM {table} LIMIT 1")
        except Exception:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()

    conn.close()


def _seed_defaults(app):
    """Create default admin user and settings if they don't exist."""
    admin_username = os.environ.get('ADMIN_USERNAME', 'admin')
    admin_password = os.environ.get('ADMIN_PASSWORD', 'password123')

    if not User.query.filter_by(username=admin_username).first():
        admin = User(
            username=admin_username,
            password_hash=generate_password_hash(admin_password),
            display_name='Administrator',
            is_admin=True,
        )
        db.session.add(admin)
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


# Load favicon on import
_load_favicon()

# Create app
app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
