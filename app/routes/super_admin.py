from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import date, timedelta
from sqlalchemy import func
from app import db
from app.models import Tenant, TenantStatus, User, UserRole, Sale, Paiement, StatutPaiement, Config
from app.utils.decorators import role_required

super_admin_bp = Blueprint('super_admin', __name__)


def _super_admin_only(f):
    from functools import wraps
    @wraps(f)
    @login_required
    @role_required(UserRole.SUPER_ADMIN)
    def wrapped(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapped


def _admin_or_activateur(f):
    """Super admin OU activateur (peut activer/rejeter comptes)."""
    from functools import wraps
    @wraps(f)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role not in (UserRole.SUPER_ADMIN, UserRole.ACTIVATEUR):
            flash('Accès refusé.', 'danger')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return wrapped


@super_admin_bp.route('/dashboard')
@_super_admin_only
def dashboard():
    from datetime import date
    pending        = Tenant.query.filter_by(status=TenantStatus.PENDING).order_by(Tenant.created_at.desc()).all()
    active_tenants = Tenant.query.filter_by(status=TenantStatus.ACTIVE).order_by(Tenant.nom).all()
    suspended      = Tenant.query.filter_by(status=TenantStatus.SUSPENDED).count()

    # Paiements du mois en cours
    mois_courant   = date.today().strftime('%Y-%m')
    paiements_mois = {p.tenant_id: p for p in
                      Paiement.query.filter_by(mois=mois_courant).all()}
    total_encaisse = sum(float(p.montant) for p in paiements_mois.values()
                         if p.statut == StatutPaiement.PAYE)

    return render_template('admin/dashboard.html',
        pending_tenants=pending,
        active_tenants=active_tenants,
        active_count=len(active_tenants),
        suspended_count=suspended,
        paiements_mois=paiements_mois,
        total_encaisse=total_encaisse,
        mois_courant=mois_courant,
        StatutPaiement=StatutPaiement,
    )


@super_admin_bp.route('/tenants')
@_super_admin_only
def tenants():
    status_filter = request.args.get('status', '')
    q = Tenant.query
    if status_filter:
        q = q.filter_by(status=status_filter)
    tenants = q.order_by(Tenant.created_at.desc()).all()
    return render_template('admin/tenants.html', tenants=tenants, current_filter=status_filter)


@super_admin_bp.route('/tenant/<int:tenant_id>/activate', methods=['POST'])
@_super_admin_only
def activate_tenant(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    days = int(request.form.get('days', 30))
    tenant.status = TenantStatus.ACTIVE
    tenant.licence_expiry = date.today() + timedelta(days=days)
    # Réactiver tous les utilisateurs du tenant (désactivés lors de la suspension)
    User.query.filter_by(tenant_id=tenant_id).update({'is_active': True})
    db.session.commit()
    flash(f'Compte "{tenant.email}" activé pour {days} jours.', 'success')
    return redirect(url_for('super_admin.dashboard'))





@super_admin_bp.route('/tenant/<int:tenant_id>/reject', methods=['POST'])
@_super_admin_only
def reject_tenant(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    tenant.status = TenantStatus.REJECTED
    db.session.commit()
    flash(f'Demande de "{tenant.email}" rejetée.', 'danger')
    return redirect(url_for('super_admin.dashboard'))


@super_admin_bp.route('/tenant/<int:tenant_id>/extend', methods=['POST'])
@_super_admin_only
def extend_licence(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    days = int(request.form.get('days', 30))
    base = max(tenant.licence_expiry or date.today(), date.today())
    tenant.licence_expiry = base + timedelta(days=days)
    db.session.commit()
    flash(f'Licence de "{tenant.email}" prolongée de {days} jours.', 'success')
    return redirect(url_for('super_admin.tenants'))


# ── PAIEMENTS ABONNEMENTS ─────────────────────────────────────────────────
@super_admin_bp.route('/paiements')
@_super_admin_only
def paiements():
    from datetime import date
    mois  = request.args.get('mois', date.today().strftime('%Y-%m'))
    plist = (Paiement.query.filter_by(mois=mois)
             .join(Tenant, Tenant.id == Paiement.tenant_id)
             .order_by(Tenant.nom).all())
    tenants_actifs = Tenant.query.filter_by(status=TenantStatus.ACTIVE).order_by(Tenant.nom).all()
    return render_template('admin/paiements.html',
                           paiements=plist, mois=mois,
                           tenants=tenants_actifs,
                           StatutPaiement=StatutPaiement)


@super_admin_bp.route('/tenant/<int:tenant_id>/paiement', methods=['POST'])
@_super_admin_only
def enregistrer_paiement(tenant_id):
    from datetime import date
    t       = Tenant.query.get_or_404(tenant_id)
    mois    = request.form.get('mois', date.today().strftime('%Y-%m'))
    _raw    = request.form.get('montant', '').strip() or str(t.montant_mensuel or 0)
    montant = float(_raw) if _raw else 0.0
    statut  = request.form.get('statut', StatutPaiement.PAYE)
    ref     = request.form.get('reference', '').strip() or None
    notes   = request.form.get('notes', '').strip() or None

    existing = Paiement.query.filter_by(tenant_id=tenant_id, mois=mois).first()
    if existing:
        existing.statut        = statut
        existing.montant       = montant
        existing.reference     = ref
        existing.notes         = notes
        existing.date_paiement = date.today() if statut == StatutPaiement.PAYE else None
    else:
        p = Paiement(
            tenant_id=tenant_id, mois=mois, montant=montant,
            statut=statut, reference=ref, notes=notes,
            date_paiement=date.today() if statut == StatutPaiement.PAYE else None,
        )
        db.session.add(p)

    # Mettre à jour le montant mensuel du tenant si changé
    if montant and float(montant) > 0:
        t.montant_mensuel = montant
    db.session.commit()
    flash(f'Paiement {mois} enregistré pour {t.prenom} {t.nom}.', 'success')
    return redirect(request.referrer or url_for('super_admin.dashboard'))


@super_admin_bp.route('/tenant/<int:tenant_id>/paiement/<mois>/pdf')
@_super_admin_only
def paiement_pdf(tenant_id, mois):
    from datetime import date
    from flask import Response, current_app
    t = Tenant.query.get_or_404(tenant_id)
    p = Paiement.query.filter_by(tenant_id=tenant_id, mois=mois).first()
    if not p:
        flash('Aucun paiement enregistré pour ce mois.', 'warning')
        return redirect(url_for('super_admin.dashboard'))

    html = render_template('admin/paiement_pdf.html', tenant=t, paiement=p,
                           today=date.today())
    try:
        from weasyprint import HTML
        pdf = HTML(string=html, base_url=current_app.root_path).write_pdf()
        fname = f'recu_abonnement_{t.nom}_{mois}.pdf'
        return Response(pdf, mimetype='application/pdf',
                        headers={'Content-Disposition': f'attachment; filename="{fname}"'})
    except Exception as e:
        flash(f'Erreur PDF : {e}', 'danger')
        return redirect(url_for('super_admin.dashboard'))


@super_admin_bp.route('/tenant/<int:tenant_id>/set-montant', methods=['POST'])
@_super_admin_only
def set_montant_mensuel(tenant_id):
    t = Tenant.query.get_or_404(tenant_id)
    montant = request.form.get('montant_mensuel', '').strip()
    if montant:
        t.montant_mensuel = float(montant)
        db.session.commit()
        flash(f'Montant mensuel mis à jour : {montant} FCFA.', 'success')
    return redirect(request.referrer or url_for('super_admin.dashboard'))


@super_admin_bp.route('/tenant/<int:tenant_id>/toggle-engros', methods=['POST'])
@_super_admin_only
def toggle_engros(tenant_id):
    t = Tenant.query.get_or_404(tenant_id)
    t.vente_engros_active = not t.vente_engros_active
    db.session.commit()
    state = 'activée' if t.vente_engros_active else 'désactivée'
    flash(f'Vente en gros {state} pour {t.prenom} {t.nom}.', 'success')
    return redirect(request.referrer or url_for('super_admin.tenants'))


@super_admin_bp.route('/tenant/<int:tenant_id>/detail')
@_super_admin_only
def tenant_detail(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    tx_count = Sale.query.filter_by(tenant_id=tenant_id).count()
    tx_total = db.session.query(func.sum(Sale.total_amount)).filter_by(tenant_id=tenant_id).scalar() or 0
    return render_template('admin/tenant_detail.html', tenant=tenant, tx_count=tx_count, tx_total=tx_total)



# ── SUSPEND / DELETE TENANT ──────────────────────────────────────────────────
@super_admin_bp.route('/tenant/<int:tenant_id>/suspend', methods=['POST'])
@_super_admin_only
def suspend_tenant(tenant_id):
    t = Tenant.query.get_or_404(tenant_id)
    t.status = TenantStatus.SUSPENDED
    User.query.filter_by(tenant_id=tenant_id).update({'is_active': False})
    db.session.commit()
    flash(f'Compte {t.prenom} {t.nom} suspendu.', 'warning')
    return redirect(url_for('super_admin.dashboard'))


@super_admin_bp.route('/tenant/<int:tenant_id>/delete', methods=['POST'])
@_super_admin_only
def delete_tenant(tenant_id):
    from sqlalchemy import text
    t = Tenant.query.get_or_404(tenant_id)
    name = f'{t.prenom} {t.nom}'
    tid  = tenant_id

    try:
        # Utiliser une connexion brute pour contrôler les savepoints
        with db.engine.begin() as conn:

            # 1. Nullifier les refs produits dans sale_items
            conn.execute(text(
                "UPDATE sale_items SET product_id=NULL, variant_id=NULL "
                "WHERE product_id IN (SELECT id FROM products WHERE tenant_id=:tid)"
            ), {'tid': tid})

            # 2. Supprimer les sale_items liés aux ventes du tenant
            conn.execute(text(
                "DELETE FROM sale_items "
                "WHERE sale_id IN (SELECT id FROM sales WHERE tenant_id=:tid)"
            ), {'tid': tid})

            # 3. Supprimer chaque table avec savepoint (isole les erreurs)
            for table in ['loss_fiche_items', 'loss_fiches',
                          'supplier_order_items', 'supplier_orders',
                          'stock_transfers', 'sales',
                          'product_variants', 'products',
                          'paiements', 'categories', 'suppliers', 'users']:
                try:
                    conn.execute(text(f"SAVEPOINT sp_{table}"))
                    conn.execute(text(
                        f"DELETE FROM {table} WHERE tenant_id=:tid"
                    ), {'tid': tid})
                except Exception as e:
                    conn.execute(text(f"ROLLBACK TO SAVEPOINT sp_{table}"))
                    print(f"[delete_tenant] skip {table}: {e}")

            # 4. Supprimer le tenant
            conn.execute(text("DELETE FROM tenants WHERE id=:tid"), {'tid': tid})

        flash(f'Compte "{name}" supprime avec succes.', 'success')

    except Exception as e:
        flash(f'Erreur suppression : {str(e)[:200]}', 'danger')

    return redirect(url_for('super_admin.dashboard'))

# ── PARAMÈTRES SUPER ADMIN ───────────────────────────────────────────────────
@super_admin_bp.route('/settings', methods=['GET', 'POST'])
@_super_admin_only
def settings():
    from app.models import Config
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'password':
            old_pw  = request.form.get('old_password', '')
            new_pw  = request.form.get('new_password', '')
            conf_pw = request.form.get('confirm_password', '')
            if not current_user.check_password(old_pw):
                flash('Mot de passe actuel incorrect.', 'danger')
            elif len(new_pw) < 8:
                flash('Minimum 8 caracteres.', 'danger')
            elif new_pw != conf_pw:
                flash('Les mots de passe ne correspondent pas.', 'danger')
            else:
                current_user.set_password(new_pw)
                db.session.commit()
                flash('Mot de passe mis a jour.', 'success')

        elif action == 'profil':
            current_user.nom    = request.form.get('nom', '').strip() or current_user.nom
            current_user.prenom = request.form.get('prenom', '').strip() or current_user.prenom
            for key in ('saas_adresse', 'saas_telephone', 'saas_email', 'saas_ville'):
                Config.set(key, request.form.get(key, '').strip())
            db.session.commit()
            flash('Profil mis a jour.', 'success')

        elif action == 'montant':
            montant = request.form.get('montant_defaut', '').strip()
            if montant:
                Config.set('montant_mensuel_defaut', montant)
                db.session.commit()
                flash(f'Montant mensuel defaut : {montant} FCFA.', 'success')

        return redirect(url_for('super_admin.settings'))

    cfg = {k: Config.get(k, '') for k in
           ('saas_adresse', 'saas_telephone', 'saas_email', 'saas_ville', 'montant_mensuel_defaut')}
    activateurs = User.query.filter_by(role=UserRole.ACTIVATEUR).all()
    return render_template('admin/settings.html', cfg=cfg, activateurs=activateurs)


@super_admin_bp.route('/activateurs/create', methods=['POST'])
@_super_admin_only
def create_activateur():
    nom    = request.form.get('nom', '').strip()
    prenom = request.form.get('prenom', '').strip()
    email  = request.form.get('email', '').strip().lower()
    pw     = request.form.get('password', '')
    if not all([nom, prenom, email, pw]):
        flash('Tous les champs sont requis.', 'danger')
        return redirect(url_for('super_admin.settings'))
    if User.query.filter_by(email=email).first():
        flash('Email deja utilise.', 'danger')
        return redirect(url_for('super_admin.settings'))
    u = User(nom=nom, prenom=prenom, email=email, role=UserRole.ACTIVATEUR)
    u.set_password(pw)
    db.session.add(u); db.session.commit()
    flash(f'Activateur {prenom} {nom} cree.', 'success')
    return redirect(url_for('super_admin.settings'))


@super_admin_bp.route('/activateurs/<int:uid>/delete', methods=['POST'])
@_super_admin_only
def delete_activateur(uid):
    u = User.query.filter_by(id=uid, role=UserRole.ACTIVATEUR).first_or_404()
    db.session.delete(u); db.session.commit()
    flash(f'Activateur {u.full_name} supprime.', 'info')
    return redirect(url_for('super_admin.settings'))


@super_admin_bp.route('/activateur/dashboard')
@login_required
def activateur_dashboard():
    if current_user.role not in (UserRole.SUPER_ADMIN, UserRole.ACTIVATEUR):
        return redirect(url_for('auth.login'))
    pending = Tenant.query.filter_by(status=TenantStatus.PENDING).order_by(Tenant.created_at.desc()).all()
    return render_template('admin/activateur_dashboard.html', pending_tenants=pending)


# ── TEST STORAGE (debug Railway) ─────────────────────────────────────────────
@super_admin_bp.route('/test-storage')
@_super_admin_only
def test_storage():
    import base64, requests as req
    from flask import current_app
    url    = current_app.config.get('SUPABASE_URL', '').rstrip('/')
    key    = current_app.config.get('SUPABASE_KEY', '')
    bucket = current_app.config.get('SUPABASE_BUCKET', 'product-images')

    if not url or not key:
        return f"<pre>❌ SUPABASE_URL={url!r} SUPABASE_KEY={'SET' if key else 'EMPTY'}</pre>"

    # Upload pixel test
    PNG = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
    )
    upload_url = f"{url}/storage/v1/object/{bucket}/products/railway_test.png"
    r = req.post(upload_url, data=PNG,
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'image/png', 'x-upsert': 'true'},
        timeout=15)

    pub = f"{url}/storage/v1/object/public/{bucket}/products/railway_test.png"
    return f"""<pre>
SUPABASE_URL    = {url}
SUPABASE_KEY    = {key[:20]}...
SUPABASE_BUCKET = {bucket}

Upload status  = {r.status_code}
Upload response= {r.text[:200]}

Public URL     = {pub}
✅ OK si status 200/201
</pre>"""
