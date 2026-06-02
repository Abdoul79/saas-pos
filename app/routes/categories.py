from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import Category, Product, UserRole, CATEGORY_COLORS
from app.utils.decorators import role_required, tenant_active_required

categories_bp = Blueprint('categories', __name__)


def _mgr(f):
    from functools import wraps
    @wraps(f)
    @login_required
    @role_required(UserRole.MANAGER)
    @tenant_active_required
    def w(*a, **kw): return f(*a, **kw)
    return w


def _tid(): return current_user.tenant_id


@categories_bp.route('/')
@_mgr
def index():
    cats = Category.query.filter_by(tenant_id=_tid()).order_by(Category.ordre, Category.nom).all()
    return render_template('manager/categories/index.html', categories=cats)


@categories_bp.route('/create', methods=['GET', 'POST'])
@_mgr
def create():
    if request.method == 'POST':
        nom = request.form.get('nom', '').strip()
        if not nom:
            flash('Le nom est obligatoire.', 'danger')
            return render_template('manager/categories/form.html', colors=CATEGORY_COLORS)
        c = Category(
            tenant_id   = _tid(),
            nom         = nom,
            description = request.form.get('description', '').strip() or None,
            couleur     = request.form.get('couleur', '#f5a623'),
            icone       = request.form.get('icone', '📦').strip() or '📦',
            ordre       = int(request.form.get('ordre', 0) or 0),
        )
        db.session.add(c)
        db.session.commit()
        flash(f'Catégorie « {c.nom} » créée.', 'success')
        return redirect(url_for('categories.index'))
    return render_template('manager/categories/form.html', colors=CATEGORY_COLORS)


@categories_bp.route('/<int:cid>/edit', methods=['GET', 'POST'])
@_mgr
def edit(cid):
    c = Category.query.filter_by(id=cid, tenant_id=_tid()).first_or_404()
    if request.method == 'POST':
        c.nom         = request.form.get('nom', c.nom).strip()
        c.description = request.form.get('description', '').strip() or None
        c.couleur     = request.form.get('couleur', c.couleur)
        c.icone       = request.form.get('icone', c.icone).strip() or '📦'
        c.ordre       = int(request.form.get('ordre', c.ordre) or 0)
        db.session.commit()
        flash('Catégorie mise à jour.', 'success')
        return redirect(url_for('categories.index'))
    return render_template('manager/categories/form.html', category=c, colors=CATEGORY_COLORS)


@categories_bp.route('/<int:cid>/delete', methods=['POST'])
@_mgr
def delete(cid):
    c = Category.query.filter_by(id=cid, tenant_id=_tid()).first_or_404()
    if c.product_count > 0:
        flash(f'Impossible : {c.product_count} produit(s) utilisent cette catégorie.', 'danger')
        return redirect(url_for('categories.index'))
    db.session.delete(c)
    db.session.commit()
    flash(f'Catégorie « {c.nom} » supprimée.', 'info')
    return redirect(url_for('categories.index'))


@categories_bp.route('/api/list')
@_mgr
def api_list():
    cats = Category.query.filter_by(tenant_id=_tid()).order_by(Category.ordre, Category.nom).all()
    return jsonify([{'id': c.id, 'nom': c.nom, 'couleur': c.couleur, 'icone': c.icone} for c in cats])
