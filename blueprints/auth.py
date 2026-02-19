from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from models import db, User

auth_bp = Blueprint('auth', __name__, template_folder='../templates/auth')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('admin.dashboard'))
        else:
            flash('Benutzername oder Passwort ungültig.', 'error')

    return render_template('login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sie wurden erfolgreich abgemeldet.', 'success')
    return redirect(url_for('auth.login'))


@auth_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        display_name = request.form.get('display_name', '').strip()
        username = request.form.get('username', '').strip()
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        # Validate username change
        if username and username != current_user.username:
            existing = User.query.filter_by(username=username).first()
            if existing:
                flash('Dieser Benutzername ist bereits vergeben.', 'error')
                return redirect(url_for('auth.profile'))
            current_user.username = username

        current_user.display_name = display_name

        if new_password:
            if new_password != confirm_password:
                flash('Passwörter stimmen nicht überein.', 'error')
                return redirect(url_for('auth.profile'))
            if len(new_password) < 6:
                flash('Passwort muss mindestens 6 Zeichen lang sein.', 'error')
                return redirect(url_for('auth.profile'))
            current_user.password_hash = generate_password_hash(new_password)

        db.session.commit()
        flash('Profil wurde aktualisiert.', 'success')
        return redirect(url_for('auth.profile'))

    return render_template('admin/profile.html')
