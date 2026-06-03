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

    # Vérifier stock pour commandes en attente
    low_stock_products = []
    for o in pending:
        for item in o.items:
            if item.variant_id:
                from app.models import ProductVariant
                v = ProductVariant.query.get(item.variant_id)
                avail = v.stock_entrepot if v else 0
            elif item.product_id:
                p = Product.query.get(item.product_id)
                avail = p.stock_entrepot if p else 0
            else:
                avail = 0
            if avail < item.quantity:
                low_stock_products.append({
                    'name': item.designation,
                    'requested': item.quantity,
                    'available': avail,
                    'order_ref': o.reference,
                })

    return render_template('manager/online/dashboard.html',
                           orders=orders, pending=pending, confirmed=confirmed,
                           preparing=preparing, shipped=shipped, delivered=delivered,
                           total_ca=total_ca, today_ca=today_ca,
                           today_orders=today_orders,
                           nb_customers=customers, nb_pending_reviews=reviews,
                           low_stock_products=low_stock_products,
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
    if new_status not in OnlineOrderStatus.all():
        flash('Statut invalide.', 'danger')
        return redirect(url_for('online.order_detail', oid=oid))

    # Vérifier le stock ENTREPÔT avant de confirmer
    if new_status == OnlineOrderStatus.CONFIRMED and order.status == OnlineOrderStatus.PENDING:
        from app.models import ProductVariant
        stock_errors = []
        for item in order.items:
            if item.variant_id:
                v = ProductVariant.query.get(item.variant_id)
                avail = v.stock_entrepot if v else 0
                label = item.designation
            elif item.product_id:
                p = Product.query.get(item.product_id)
                avail = p.stock_entrepot if p else 0
                label = item.designation
            else:
                avail = 0
                label = item.designation
            if avail < item.quantity:
                stock_errors.append(f"{label} : demandé {item.quantity}, entrepôt {avail}")

        if stock_errors:
            flash(f'Stock entrepôt insuffisant — impossible de confirmer : {" · ".join(stock_errors)}', 'danger')
            return redirect(url_for('online.order_detail', oid=oid))

        # Décrémenter le stock entrepôt à la confirmation
        for item in order.items:
            if item.variant_id:
                v = ProductVariant.query.get(item.variant_id)
                if v:
                    v.stock_entrepot = max(0, v.stock_entrepot - item.quantity)
            elif item.product_id:
                p = Product.query.get(item.product_id)
                if p:
                    p.stock_entrepot = max(0, p.stock_entrepot - item.quantity)

    order.status = new_status
    note = request.form.get('note', '').strip()
    if note:
        order.note_manager = note
    db.session.commit()

    # Générer et envoyer la facture par email à la confirmation
    if new_status == OnlineOrderStatus.CONFIRMED:
        try:
            _send_invoice(order)
            flash(f'Commande {order.reference} → {OnlineOrderStatus.label(new_status)} · Facture envoyée à {order.customer.email}', 'success')
        except Exception as e:
            print(f'[invoice] Erreur: {e}')
            flash(f'Commande {order.reference} → {OnlineOrderStatus.label(new_status)} (facture non envoyée: {e})', 'warning')
    else:
        flash(f'Commande {order.reference} → {OnlineOrderStatus.label(new_status)}', 'success')

    return redirect(url_for('online.order_detail', oid=oid))


def _send_invoice(order):
    """Génère la facture PDF et l'envoie au client par email."""
    import qrcode, io, base64
    from flask import current_app
    from weasyprint import HTML
    from app.utils.email import send_email_async
    from datetime import date

    tenant = Tenant.query.get(order.tenant_id)

    # QR code
    if tenant.shop_slug:
        url = request.host_url.rstrip('/') + f'/shop/{tenant.shop_slug}/commande/{order.reference}'
    else:
        url = order.reference
    qr = qrcode.make(url, box_size=5, border=2)
    buf = io.BytesIO()
    qr.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    # Render HTML
    html_str = render_template('manager/online/invoice_pdf.html',
                               order=order, tenant=tenant,
                               qr_b64=qr_b64, today=date.today())

    # Générer PDF
    pdf_bytes = HTML(string=html_str, base_url=current_app.root_path).write_pdf()

    # Email de confirmation
    email_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <div style="text-align:center;margin-bottom:20px;">
        <h1 style="color:#f5a623;font-size:24px;">✅ Commande confirmée !</h1>
      </div>
      <p>Bonjour <strong>{order.customer.prenom}</strong>,</p>
      <p>Votre commande <strong style="font-family:monospace;color:#f5a623;">{order.reference}</strong>
         a été confirmée et est en cours de préparation.</p>
      <div style="background:#f8f9fa;border:1px solid #e5e7eb;border-radius:8px;padding:15px;margin:15px 0;">
        <div style="font-size:14px;font-weight:700;margin-bottom:8px;">Récapitulatif</div>
        {''.join(f'<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:13px;border-bottom:1px solid #eee;"><span>{item.quantity}× {item.designation}</span><span style="font-weight:600;">{int(item.subtotal)} FCFA</span></div>' for item in order.items)}
        <div style="display:flex;justify-content:space-between;padding:8px 0 0;font-size:16px;font-weight:800;color:#f5a623;border-top:2px solid #ddd;margin-top:5px;">
          <span>Total</span><span>{int(order.total_amount)} FCFA</span>
        </div>
      </div>
      <p style="color:#555;">📦 Livraison : {order.adresse_livraison}, {order.ville_livraison}</p>
      <p style="font-size:13px;color:#888;">La facture est jointe à cet email en PDF.</p>
      <div style="text-align:center;margin-top:20px;font-size:12px;color:#aaa;">
        {tenant.nom_boutique or tenant.activite} — {tenant.ville}
      </div>
    </div>
    """

    # Envoyer en background
    send_email_async(
        to_email=order.customer.email,
        subject=f'✅ Commande {order.reference} confirmée — {tenant.nom_boutique or tenant.activite}',
        html_content=email_html,
        attachment=pdf_bytes,
        attachment_name=f'Facture_{order.reference}.pdf'
    )


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

@online_bp.route('/commande/<int:oid>/facture')
@_mgr
def invoice_pdf(oid):
    """Télécharger la facture PDF d'une commande."""
    import qrcode, io, base64
    from flask import current_app, Response
    from weasyprint import HTML
    from datetime import date

    order = OnlineOrder.query.filter_by(id=oid, tenant_id=_tid()).first_or_404()
    tenant = current_user.tenant

    # QR
    if tenant.shop_slug:
        url = request.host_url.rstrip('/') + f'/shop/{tenant.shop_slug}/commande/{order.reference}'
    else:
        url = order.reference
    qr = qrcode.make(url, box_size=5, border=2)
    buf = io.BytesIO()
    qr.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    html_str = render_template('manager/online/invoice_pdf.html',
                               order=order, tenant=tenant,
                               qr_b64=qr_b64, today=date.today())
    pdf = HTML(string=html_str, base_url=current_app.root_path).write_pdf()
    return Response(pdf, mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="Facture_{order.reference}.pdf"'})


@online_bp.route('/commande/<int:oid>/qr')
@_mgr
def order_qr(oid):
    """Génère un QR code pour la commande (à coller sur le colis)."""
    import qrcode, io, base64
    order = OnlineOrder.query.filter_by(id=oid, tenant_id=_tid()).first_or_404()
    # QR contient la référence + URL de suivi
    tenant = current_user.tenant
    if tenant.shop_slug:
        url = request.host_url.rstrip('/') + f'/shop/{tenant.shop_slug}/commande/{order.reference}'
    else:
        url = order.reference
    qr = qrcode.make(url, box_size=6, border=2)
    buf = io.BytesIO()
    qr.save(buf, format='PNG')
    buf.seek(0)
    from flask import Response
    return Response(buf.getvalue(), mimetype='image/png',
                    headers={'Content-Disposition': f'inline; filename=qr_{order.reference}.png'})


@online_bp.route('/commande/<int:oid>/etiquette')
@_mgr
def order_label(oid):
    """Étiquette colis avec QR code + détails commande."""
    order = OnlineOrder.query.filter_by(id=oid, tenant_id=_tid()).first_or_404()
    import qrcode, io, base64
    tenant = current_user.tenant
    if tenant.shop_slug:
        url = request.host_url.rstrip('/') + f'/shop/{tenant.shop_slug}/commande/{order.reference}'
    else:
        url = order.reference
    qr = qrcode.make(url, box_size=5, border=2)
    buf = io.BytesIO()
    qr.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return render_template('manager/online/label.html', order=order,
                           qr_b64=qr_b64, tenant=tenant)


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
