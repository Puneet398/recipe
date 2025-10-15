from flask import Blueprint, request, redirect, render_template, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User

auth_bp = Blueprint('auth', __name__, template_folder='templates')

@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        try:
            username = request.form.get('username')
            password = request.form.get('password')
            role = request.form.get('role', 'user')

            if not username or not password:
                flash('Username and password are required.')
                return redirect(url_for('auth.signup'))

            if User.query.filter_by(username=username).first():
                flash('Username already exists.')
                return redirect(url_for('auth.signup'))

            hashed_pw = generate_password_hash(password)
            new_user = User(username=username, password=hashed_pw, role=role)
            db.session.add(new_user)
            db.session.commit()

            flash('Signup successful. Please log in.')
            return redirect(url_for('auth.login'))

        except Exception as e:
            print(f"Signup error: {e}")
            return "Internal error", 500

    return render_template('signup.html')

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            flash('Both fields are required.')
            return redirect(url_for('auth.login'))

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            flash(f'Welcome back, {user.username}!')
            return redirect('/') # Make sure this route exists
        else:
            flash('Invalid credentials.')
            return redirect(url_for('auth.login'))

    return render_template('login.html')

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out successfully.')
    return redirect('/')

@auth_bp.route('/dashboard')
@login_required
def dashboard():
    role_templates = {
        'admin': 'admin_dashboard.html',
        'family': 'family_dashboard.html',
        'user': 'user_dashboard.html'
    }
    template = role_templates.get(current_user.role, 'user_dashboard.html')
    return render_template(template, username=current_user.username)