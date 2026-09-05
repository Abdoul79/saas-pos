import os, uuid
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required, current_user
from datetime import datetime
from app import db
from app.utils.storage import upload_image
from app.models import User, Tenant, TenantStatus, UserRole, ACTIVITY_CHOICES

auth_bp = Blueprint('auth', __name__)

LOGO_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'static', 'uploads', 'logos')


@auth_bp.route('/cgu.pdf')
def cgu_pdf():
    """Sert les CGU en PDF via WeasyPrint."""
    from flask import Response, current_app, render_template
    from datetime import date
    html_str = render_template('auth/cgu.html', today=date.today().strftime('%d/%m/%Y'))
    try:
        from weasyprint import HTML
        pdf = HTML(string=html_str, base_url=current_app.root_path).write_pdf()
        return Response(pdf, mimetype='application/pdf',
                        headers={'Content-Disposition': 'inline; filename="CGU_SaaS_POS.pdf"'})
    except Exception as e:
        return f'Erreur génération PDF : {e}', 500


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return _redirect_by_role(current_user)

    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user     = User.query.filter_by(email=email).first()

        if not user or not user.check_password(password):
            flash('Email ou mot de passe incorrect.', 'danger')
            return render_template('auth/login.html')
        if not user.is_active:
            flash('Votre compte utilisateur est désactivé.', 'danger')
            return render_template('auth/login.html')
        # Ignorer le check de statut pour les super admins et activateurs
        if current_user.is_authenticated and not current_user.is_super_admin and not getattr(current_user, 'is_activateur', False):
            if current_user.tenant.status != TenantStatus.ACTIVE:
                flash('Votre espace commerçant est suspendu ou en attente d\'activation.', 'danger')
                
                return render_template('auth/login.html')

        user.last_login = datetime.utcnow()
        db.session.commit()
        login_user(user, remember=False)
        flash(f'Bienvenue, {user.full_name} !', 'success')
        return _redirect_by_role(user)

    return render_template('auth/login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return _redirect_by_role(current_user)

    if request.method == 'POST':
        nom      = request.form.get('nom', '').strip()
        prenom   = request.form.get('prenom', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        ville    = request.form.get('ville', '').strip()
        adresse  = request.form.get('adresse', '').strip()

        nom_boutique         = request.form.get('nom_boutique', '').strip() or None
        telephone_personnel  = request.form.get('telephone_personnel', '').strip() or None
        telephone_entreprise = request.form.get('telephone_entreprise', '').strip() or None
        cgu                  = request.form.get('cgu', '')

        # Secteur : custom si "Autre"
        activite_sel    = request.form.get('activite', '')
        activite_custom = request.form.get('activite_custom', '').strip()
        activite = activite_custom if activite_sel == 'Autre' and activite_custom else activite_sel

        confirm_password = request.form.get('confirm_password', '')

        errors = []
        if not all([nom, prenom, email, password, activite, ville, adresse]):
            errors.append('Tous les champs obligatoires (*) doivent être remplis.')
        if len(password) < 8:
            errors.append('Le mot de passe doit contenir au moins 8 caractères.')
        if not any(c.isupper() for c in password):
            errors.append('Le mot de passe doit contenir au moins une majuscule.')
        if not any(c.isdigit() for c in password):
            errors.append('Le mot de passe doit contenir au moins un chiffre.')
        if not any(c in '!@#$%^&*()_+-=[]{}|;:,.<>?' for c in password):
            errors.append('Le mot de passe doit contenir au moins un caractère spécial.')
        if password != confirm_password:
            errors.append('Les deux mots de passe ne correspondent pas.')
        if User.query.filter_by(email=email).first():
            errors.append('Cette adresse email est déjà utilisée.')
        if not cgu:
            errors.append("Vous devez accepter les conditions d'utilisation pour continuer.")

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('auth/register.html', activities=ACTIVITY_CHOICES, form_data=request.form)

        # Logo upload (optionnel)
        logo_fn   = None
        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename:
            logo_fn = upload_image(logo_file, folder='logos')

        tenant = Tenant(
            nom=nom, prenom=prenom, email=email,
            activite=activite, ville=ville, adresse=adresse,
            nom_boutique=nom_boutique,
            telephone_personnel=telephone_personnel,
            telephone_entreprise=telephone_entreprise,
            logo_filename=logo_fn,
            status=TenantStatus.PENDING
        )
        tenant.set_password(password)
        db.session.add(tenant)
        db.session.flush()

        manager = User(
            tenant_id=tenant.id,
            nom=nom, prenom=prenom, email=email,
            role=UserRole.MANAGER
        )
        manager.set_password(password)
        db.session.add(manager)
        db.session.commit()

        flash("Votre demande a été soumise. Un administrateur validera votre compte sous peu.", 'info')
        return redirect(url_for('auth.login'))

    return render_template('auth/register.html', activities=ACTIVITY_CHOICES)


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Vous avez été déconnecté.', 'info')
    return redirect(url_for('auth.login'))


def _redirect_by_role(user):
    if user.role == UserRole.SUPER_ADMIN:
        return redirect(url_for('super_admin.dashboard'))
    elif user.role == UserRole.ACTIVATEUR:
        return redirect(url_for('super_admin.activateur_dashboard'))
    elif user.role == UserRole.MANAGER:
        return redirect(url_for('manager.dashboard'))
    else:
        return redirect(url_for('pos.interface'))


# ── MOT DE PASSE OUBLIÉ ───────────────────────────────────────────────────────
@auth_bp.route('/check-email')
def check_email():
    """API : vérifie si un email existe dans la base de données."""
    from flask import jsonify
    email = request.args.get('email', '').strip().lower()
    user  = User.query.filter_by(email=email).first()
    return jsonify({'found': user is not None})


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    from flask_mail import Message
    from itsdangerous import URLSafeTimedSerializer
    from flask import current_app
    from app import mail

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        sent  = False

        # 1. Vérifier si l'email existe dans la base de données
        user = User.query.filter_by(email=email).first()

        if not user:
            flash('Aucun compte trouvé avec cette adresse email.', 'danger')
            return render_template('auth/forgot_password.html', email=email)

        # 2. Email trouvé — générer token et envoyer UNIQUEMENT par email
        import os
        mail_user   = (os.environ.get('MAIL_USERNAME') or
                       current_app.config.get('MAIL_USERNAME') or '').strip()
        resend_key  = os.environ.get('RESEND_API_KEY', '').strip()
        if not mail_user and not resend_key:
            flash('La configuration email nest pas activee. Contactez administrateur.', 'warning')
            return render_template('auth/forgot_password.html', email=email)

        try:
            s     = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
            token = s.dumps(email, salt='reset-password')
            # Le lien reste PRIVE — jamais affiché à l'écran
            link  = url_for('auth.reset_password', token=token, _external=True)

            email_body = (
                '<div style="font-family:Arial,sans-serif;max-width:500px;'
                'margin:0 auto;padding:2rem;background:#f9f9f9;border-radius:8px;">'
                '<h2 style="color:#1a1a2e;margin-bottom:.5rem;">&#9635; SaaS POS</h2>'
                '<p style="color:#555;margin-bottom:1.5rem;">'
                'Une demande de reinitialisation a ete effectuee pour votre compte.</p>'
                f'<a href="{link}" style="display:inline-block;background:#f5a623;'
                'color:#0d0f14;padding:.85rem 1.75rem;border-radius:8px;'
                'text-decoration:none;font-weight:700;font-size:1rem;">'
                'Reinitialiser mon mot de passe</a>'
                '<p style="color:#aaa;font-size:.8rem;margin-top:1.5rem;">'
                'Ce lien expire dans 30 minutes.<br>'
                'Si vous navez pas fait cette demande, ignorez cet email.</p>'
                '</div>'
            )
            # Envoyer via Resend API (HTTPS — pas SMTP, fonctionne sur Railway)
            import threading, requests as req, os
            resend_key = os.environ.get('RESEND_API_KEY', '')
            mail_user  = (os.environ.get('MAIL_USERNAME') or
                          current_app.config.get('MAIL_USERNAME') or '').strip()

            def send_email(to_email, html_content):
                """SendGrid → Brevo → Resend selon les variables configurées."""
                sendgrid = os.environ.get('SENDGRID_API_KEY', '').strip()
                brevo    = os.environ.get('BREVO_API_KEY', '').strip()
                resend   = os.environ.get('RESEND_API_KEY', '').strip()
                sender   = (os.environ.get('MAIL_DEFAULT_SENDER') or
                            os.environ.get('MAIL_USERNAME') or '').strip()

                if sendgrid:
                    # SendGrid — l'expediteur DOIT etre verifie dans SendGrid
                    from_email = sender  # doit etre verifie dans SendGrid Single Sender
                    if not from_email:
                        print('[sendgrid] MAIL_DEFAULT_SENDER non defini dans les variables')
                        return
                    r = req.post('https://api.sendgrid.com/v3/mail/send',
                        headers={
                            'Authorization': f'Bearer {sendgrid}',
                            'Content-Type' : 'application/json',
                        },
                        json={
                            'personalizations': [{'to': [{'email': to_email}]}],
                            'from'   : {'email': from_email, 'name': 'SaaS POS'},
                            'subject': 'Reinitialisation mot de passe — SaaS POS',
                            'content': [{'type': 'text/html', 'value': html_content}],
                        },
                        timeout=15)
                    if r.status_code == 202:
                        print(f'[sendgrid] OK → {to_email} (from: {from_email})')
                    else:
                        print(f'[sendgrid] Err {r.status_code}: {r.text[:200]}')

                elif brevo and sender:
                    r = req.post('https://api.brevo.com/v3/smtp/email',
                        headers={'api-key': brevo, 'Content-Type': 'application/json'},
                        json={'sender': {'name': 'SaaS POS', 'email': sender},
                              'to': [{'email': to_email}],
                              'subject': 'Reinitialisation mot de passe — SaaS POS',
                              'htmlContent': html_content},
                        timeout=15)
                    print(f"[brevo] {r.status_code} → {to_email}" if r.ok else f"[brevo] Err {r.status_code}: {r.text[:150]}")

                elif resend:
                    r = req.post('https://api.resend.com/emails',
                        headers={'Authorization': f'Bearer {resend}', 'Content-Type': 'application/json'},
                        json={'from': 'SaaS POS <onboarding@resend.dev>', 'to': [to_email],
                              'subject': 'Reinitialisation mot de passe — SaaS POS',
                              'html': html_content},
                        timeout=15)
                    print(f"[resend] {r.status_code} → {to_email}" if r.ok else f"[resend] Err {r.status_code}: {r.text[:150]}")

                else:
                    print("[email] Aucun service email configure (SENDGRID_API_KEY, BREVO_API_KEY ou RESEND_API_KEY)")

            t = threading.Thread(target=send_email, args=(email, email_body))
            t.daemon = True
            t.start()
            sent = True

        except Exception as e:
            print(f"[forgot_password] Email error: {e}")
            flash('Erreur lors de envoi email. Veuillez reessayer.', 'danger')
            return render_template('auth/forgot_password.html', email=email)

        return render_template('auth/forgot_password.html', email=email, sent=sent)

    return render_template('auth/forgot_password.html')


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
    from flask import current_app

    try:
        s     = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
        email = s.loads(token, salt='reset-password', max_age=1800)  # 30 min
    except (SignatureExpired, BadSignature):
        flash('Lien invalide ou expiré. Veuillez recommencer.', 'danger')
        return redirect(url_for('auth.forgot_password'))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash('Utilisateur introuvable.', 'danger')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        pw   = request.form.get('password', '')
        conf = request.form.get('confirm', '')
        if len(pw) < 8:
            flash('Le mot de passe doit contenir au moins 8 caractères.', 'danger')
        elif pw != conf:
            flash('Les mots de passe ne correspondent pas.', 'danger')
        else:
            user.set_password(pw)
            db.session.commit()
            flash('Mot de passe mis à jour ! Vous pouvez vous connecter.', 'success')
            return redirect(url_for('auth.login'))

    return render_template('auth/reset_password.html', token=token)
