"""Gestion boutique en ligne — côté manager."""
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime, date
from app import db
from app.models import (OnlineOrder, OnlineOrderItem, OnlineOrderStatus,
                        OnlineCustomer, ProductReview, Product, UserRole, Tenant)
from app.utils.decorators import role_required, tenant_active_required

online_bp = Blueprint('online', __name__)


def _mgr(f):
    from functools import wraps
    @wraps(f)
    @login_required
    @role_required(UserRole.MANAGER)
    @tenant_active_required
    def w(*a, **kw): return f(*a, **kw)
    return w


def _tid(): return current_user.tenant_id


# ── DASHBOARD EN LIGNE ────────────────────────────────────────────────────

@online_bp.route('/dashboard')
@_mgr
def dashboard():
    today = date.today()
    orders = OnlineOrder.query.filter_by(tenant_id=_tid())\
             .order_by(OnlineOrder.created_at.desc()).all()

    pending   = [o for o in orders if o.status == OnlineOrderStatus.PENDING]
    confirmed = [o for o in orders if o.status == OnlineOrderStatus.CONFIRMED]
    preparing = [o for o in orders if o.status == OnlineOrderStatus.PREPARING]
    shipped   = [o for o in orders if o.status == OnlineOrderStatus.SHIPPED]
    delivered = [o for o in orders if o.status == OnlineOrderStatus.DELIVERED]

    total_ca = sum(float(o.total_amount) for o in orders if o.status != OnlineOrderStatus.CANCELLED)
    today_orders = [o for o in orders
                    if o.created_at and o.created_at.date() == today
                    and o.status != OnlineOrderStatus.CANCELLED]
    today_ca = sum(float(o.total_amount) for o in today_orders)

    customers = OnlineCustomer.query.filter_by(tenant_id=_tid()).count()
    reviews   = ProductReview.query.filter_by(tenant_id=_tid(), is_approved=False).count()

    return render_template('manager/online/dashboard.html',
                           orders=orders, pending=pending, confirmed=confirmed,
                           preparing=preparing, shipped=shipped, delivered=delivered,
                           total_ca=total_ca, today_ca=today_ca,
                           today_orders=today_orders,
                           nb_customers=customers, nb_pending_reviews=reviews,
                           OnlineOrderStatus=OnlineOrderStatus,
                           tenant=current_user.tenant)


# ── COMMANDES ──────────────────────────────────────────────────────────────

@online_bp.route('/commandes')
@_mgr
def orders_list():
    status_filter = request.args.get('status', '')
    q = OnlineOrder.query.filter_by(tenant_id=_tid())
    if status_filter:
        q = q.filter_by(status=status_filter)
    orders = q.order_by(OnlineOrder.created_at.desc()).all()
    return render_template('manager/online/orders.html', orders=orders,
                           status_filter=status_filter,
                           OnlineOrderStatus=OnlineOrderStatus)


@online_bp.route('/commande/<int:oid>')
@_mgr
def order_detail(oid):
    order = OnlineOrder.query.filter_by(id=oid, tenant_id=_tid()).first_or_404()
    return render_template('manager/online/order_detail.html', order=order,
                           OnlineOrderStatus=OnlineOrderStatus)


@online_bp.route('/commande/<int:oid>/status', methods=['POST'])
@_mgr
def update_status(oid):
    order = OnlineOrder.query.filter_by(id=oid, tenant_id=_tid()).first_or_404()
    new_status = request.form.get('status', '')
    if new_status in OnlineOrderStatus.all():
        order.status = new_status
        note = request.form.get('note', '').strip()
        if note:
            order.note_manager = note
        db.session.commit()
        flash(f'Commande {order.reference} → {OnlineOrderStatus.label(new_status)}', 'success')
    return redirect(url_for('online.order_detail', oid=oid))


# ── CLIENTS ────────────────────────────────────────────────────────────────

@online_bp.route('/clients')
@_mgr
def customers_list():
    customers = OnlineCustomer.query.filter_by(tenant_id=_tid())\
                .order_by(OnlineCustomer.created_at.desc()).all()
    return render_template('manager/online/customers.html', customers=customers)


# ── AVIS ───────────────────────────────────────────────────────────────────

@online_bp.route('/avis')
@_mgr
def reviews_list():
    reviews = ProductReview.query.filter_by(tenant_id=_tid())\
              .order_by(ProductReview.created_at.desc()).all()
    return render_template('manager/online/reviews.html', reviews=reviews)


@online_bp.route('/avis/<int:rid>/approuver', methods=['POST'])
@_mgr
def approve_review(rid):
    r = ProductReview.query.filter_by(id=rid, tenant_id=_tid()).first_or_404()
    r.is_approved = True
    db.session.commit()
    flash('Avis approuvé.', 'success')
    return redirect(url_for('online.reviews_list'))


@online_bp.route('/avis/<int:rid>/supprimer', methods=['POST'])
@_mgr
def delete_review(rid):
    r = ProductReview.query.filter_by(id=rid, tenant_id=_tid()).first_or_404()
    db.session.delete(r)
    db.session.commit()
    flash('Avis supprimé.', 'success')
    return redirect(url_for('online.reviews_list'))


# ── PARAMÈTRES BOUTIQUE EN LIGNE ───────────────────────────────────────────

@online_bp.route('/parametres', methods=['GET', 'POST'])
@_mgr
def settings():
    t = current_user.tenant
    if request.method == 'POST':
        slug = request.form.get('slug', '').strip().lower()
        slug = ''.join(c for c in slug if c.isalnum() or c == '-')
        if slug:
            existing = Tenant.query.filter(Tenant.shop_slug == slug, Tenant.id != t.id).first()
            if existing:
                flash('Ce slug est déjà pris.', 'danger')
                return redirect(url_for('online.settings'))
            t.shop_slug = slug
        t.shop_description = request.form.get('description', '').strip()
        db.session.commit()
        flash('Paramètres boutique en ligne sauvegardés.', 'success')
        return redirect(url_for('online.settings'))
    return render_template('manager/online/settings.html', tenant=t)
