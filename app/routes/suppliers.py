import os
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import Supplier, Product, UserRole
from app.utils.decorators import role_required, tenant_active_required

suppliers_bp = Blueprint('suppliers', __name__)


def _manager_access(f):
    from functools import wraps
    @wraps(f)
    @login_required
    @role_required(UserRole.MANAGER)
    @tenant_active_required
    def wrapped(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapped


def _tid():
    return current_user.tenant_id


# ── LIST ──────────────────────────────────────────────────────────────────
@suppliers_bp.route('/')
@_manager_access
def index():
    q         = request.args.get('q', '')
    query     = Supplier.query.filter_by(tenant_id=_tid())
    if q:
        query = query.filter(Supplier.nom.ilike(f'%{q}%'))
    suppliers = query.order_by(Supplier.nom).all()
    return render_template('manager/suppliers/index.html', suppliers=suppliers, q=q)


# ── CREATE ────────────────────────────────────────────────────────────────
@suppliers_bp.route('/create', methods=['GET', 'POST'])
@_manager_access
def create():
    if request.method == 'POST':
        nom = request.form.get('nom', '').strip()
        if not nom:
            flash('Le nom du fournisseur est obligatoire.', 'danger')
            return render_template('manager/suppliers/form.html')

        s = Supplier(
            tenant_id = _tid(),
            nom       = nom,
            contact   = request.form.get('contact',   '').strip() or None,
            telephone = request.form.get('telephone', '').strip() or None,
            email     = request.form.get('email',     '').strip() or None,
            adresse   = request.form.get('adresse',   '').strip() or None,
            ville     = request.form.get('ville',     '').strip() or None,
            notes     = request.form.get('notes',     '').strip() or None,
        )
        db.session.add(s)
        db.session.commit()
        flash(f'Fournisseur « {s.nom} » créé avec succès.', 'success')
        return redirect(url_for('suppliers.index'))

    return render_template('manager/suppliers/form.html')


# ── EDIT ──────────────────────────────────────────────────────────────────
@suppliers_bp.route('/<int:sid>/edit', methods=['GET', 'POST'])
@_manager_access
def edit(sid):
    s = Supplier.query.filter_by(id=sid, tenant_id=_tid()).first_or_404()

    if request.method == 'POST':
        s.nom       = request.form.get('nom', s.nom).strip()
        s.contact   = request.form.get('contact',   '').strip() or None
        s.telephone = request.form.get('telephone', '').strip() or None
        s.email     = request.form.get('email',     '').strip() or None
        s.adresse   = request.form.get('adresse',   '').strip() or None
        s.ville     = request.form.get('ville',     '').strip() or None
        s.notes     = request.form.get('notes',     '').strip() or None
        db.session.commit()
        flash('Fournisseur mis à jour.', 'success')
        return redirect(url_for('suppliers.index'))

    return render_template('manager/suppliers/form.html', supplier=s)


# ── TOGGLE ACTIVE ─────────────────────────────────────────────────────────
@suppliers_bp.route('/<int:sid>/toggle', methods=['POST'])
@_manager_access
def toggle(sid):
    s = Supplier.query.filter_by(id=sid, tenant_id=_tid()).first_or_404()
    s.is_active = not s.is_active
    db.session.commit()
    state = 'activé' if s.is_active else 'désactivé'
    flash(f'Fournisseur « {s.nom} » {state}.', 'info')
    return redirect(url_for('suppliers.index'))


# ── DETAIL / Products list ─────────────────────────────────────────────────
@suppliers_bp.route('/<int:sid>')
@_manager_access
def detail(sid):
    s        = Supplier.query.filter_by(id=sid, tenant_id=_tid()).first_or_404()
    products = Product.query.filter_by(supplier_id=sid, tenant_id=_tid())\
                            .order_by(Product.designation).all()
    return render_template('manager/suppliers/detail.html', supplier=s, products=products)


# ── API: return JSON list for product form select ─────────────────────────
@suppliers_bp.route('/api/list')
@_manager_access
def api_list():
    suppliers = Supplier.query.filter_by(tenant_id=_tid(), is_active=True)\
                              .order_by(Supplier.nom).all()
    return jsonify([{'id': s.id, 'nom': s.nom} for s in suppliers])
