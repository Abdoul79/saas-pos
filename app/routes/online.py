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

    # Statistiques visiteurs
    from app.models import ShopVisit
    from sqlalchemy import func
    visitors_today = ShopVisit.query.filter(
        ShopVisit.tenant_id == _tid(),
        func.date(ShopVisit.visited_at) == today
    ).count()
    visitors_month = ShopVisit.query.filter(
        ShopVisit.tenant_id == _tid(),
        func.extract('month', ShopVisit.visited_at) == today.month,
        func.extract('year', ShopVisit.visited_at) == today.year
    ).count()

    # Visiteurs par jour (7 derniers jours pour le graphe)
    from datetime import timedelta
    visitors_7days = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        count = ShopVisit.query.filter(
            ShopVisit.tenant_id == _tid(),
            func.date(ShopVisit.visited_at) == d
        ).count()
        visitors_7days.append({'date': d.strftime('%d/%m'), 'count': count})

    # Top 5 produits les plus visités + 5 moins visités (dernières 24h)
    from sqlalchemy import func as sqlfunc
    from datetime import timedelta as td
    last_24h = datetime.utcnow() - td(hours=24)
    product_views = db.session.query(
        Product.id, Product.designation,
        sqlfunc.count(ShopVisit.id).label('views')
    ).join(ShopVisit, ShopVisit.product_id == Product.id)\
     .filter(Product.tenant_id == _tid(), ShopVisit.product_id.isnot(None),
             ShopVisit.visited_at >= last_24h)\
     .group_by(Product.id, Product.designation)\
     .order_by(sqlfunc.count(ShopVisit.id).desc()).all()

    # Ajouter image_url (c'est une @property)
    top_5_products = []
    bottom_5_products_raw = []
    for pid, name, views in product_views[:5]:
        p = Product.query.get(pid)
        top_5_products.append((pid, name, p.image_url if p else '', views))
    if len(product_views) > 5:
        for pid, name, views in reversed(product_views[-5:]):
            p = Product.query.get(pid)
            bottom_5_products_raw.append((pid, name, p.image_url if p else '', views))
    bottom_5_products = bottom_5_products_raw

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
                           visitors_today=visitors_today,
                           visitors_month=visitors_month,
                           visitors_7days=visitors_7days,
                           top_5_products=top_5_products,
                           bottom_5_products=bottom_5_products,
                           OnlineOrderStatus=OnlineOrderStatus,
                           tenant=current_user.tenant)


#adress qr code for shop
@online_bp.route('/qr-boutique')
@_mgr
def shop_qr():
    """QR code PNG de l'URL de la boutique."""
    import qrcode, io
    from flask import Response, current_app
    t = current_user.tenant
    if not t.shop_slug:
        abort(404)
    url = request.host_url.rstrip('/') + f'/shop/{t.shop_slug}'
    qr = qrcode.QRCode(box_size=8, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#1e293b', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return Response(buf.getvalue(), mimetype='image/png',
                    headers={'Content-Disposition': f'inline; filename=qr-{t.shop_slug}.png'})

# carte viiste de la boutique
@online_bp.route('/carte-visite')
@_mgr
def business_card():
    """Page carte de visite avec QR code - téléchargeable."""
    import qrcode, io, base64
    t = current_user.tenant
    if not t.shop_slug:
        flash('Configurez votre slug d\'abord.', 'warning')
        return redirect(url_for('online.settings'))
    url = request.host_url.rstrip('/') + f'/shop/{t.shop_slug}'
    qr = qrcode.QRCode(box_size=7, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#1e293b', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return render_template('manager/online/business_card.html',
                           tenant=t, qr_b64=qr_b64, shop_url=url)



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
    # Vérifier le stock ENTREPÔT avant de confirmer (sauf mode restaurant)
    if new_status == OnlineOrderStatus.CONFIRMED and order.status == OnlineOrderStatus.PENDING:
        tenant = current_user.tenant
        is_restaurant = (tenant.shop_mode == 'restaurant')

        if not is_restaurant:
            from app.models import ProductVariant
            stock_errors = []
            for item in order.items:
                if item.variant_id:
                    v = ProductVariant.query.get(item.variant_id)
                    avail = v.stock_entrepot if v else 0
                elif item.product_id:
                    p = Product.query.get(item.product_id)
                    avail = p.stock_entrepot if p else 0
                else:
                    avail = 0
                if avail < item.quantity:
                    stock_errors.append(f"{item.designation} : demandé {item.quantity}, entrepôt {avail}")

            if stock_errors:
                flash(f'Stock entrepôt insuffisant — {" · ".join(stock_errors)}', 'danger')
                return redirect(url_for('online.order_detail', oid=oid))

            # Décrémenter le stock entrepôt
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

    # Email notification pour les autres étapes
    elif new_status in (OnlineOrderStatus.PREPARING, OnlineOrderStatus.SHIPPED, OnlineOrderStatus.DELIVERED):
        try:
            _send_status_email(order, new_status)
            flash(f'Commande {order.reference} → {OnlineOrderStatus.label(new_status)} · Email envoyé à {order.customer.email}', 'success')
        except Exception as e:
            print(f'[status_email] Erreur: {e}')
            flash(f'Commande {order.reference} → {OnlineOrderStatus.label(new_status)} (email non envoyé: {e})', 'warning')
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

def _send_status_email(order, status):
    """Envoie un email de notification au client avec lien de suivi."""
    from app.utils.email import send_email_async

    tenant = Tenant.query.get(order.tenant_id)
    shop_name = tenant.nom_boutique or tenant.activite

    # Construire le lien de suivi
    if tenant.shop_slug:
        tracking_url = request.host_url.rstrip('/') + f'/shop/{tenant.shop_slug}/commande/{order.reference}'
    else:
        tracking_url = '#'

    # Messages par statut
    messages = {
        OnlineOrderStatus.PREPARING: {
            'icon': '📦',
            'title': 'Votre commande est en préparation !',
            'text': 'Notre équipe prépare votre colis avec soin. Vous recevrez une notification dès qu\'il sera expédié.',
            'color': '#a78bfa',
        },
        OnlineOrderStatus.SHIPPED: {
            'icon': '🚚',
            'title': 'Votre commande a été expédiée !',
            'text': 'Votre colis est en route ! Vous pouvez suivre son état en temps réel.',
            'color': '#06b6d4',
        },
        OnlineOrderStatus.DELIVERED: {
            'icon': '🎉',
            'title': 'Votre commande a été livrée !',
            'text': 'Nous espérons que vous êtes satisfait. N\'hésitez pas à laisser un avis !',
            'color': '#22c55e',
        },
    }

    msg = messages.get(status, {'icon': '📋', 'title': 'Mise à jour commande', 'text': '', 'color': '#6b7280'})

    email_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <div style="text-align:center;margin-bottom:20px;">
        <div style="font-size:48px;margin-bottom:10px;">{msg['icon']}</div>
        <h1 style="color:{msg['color']};font-size:22px;margin:0;">{msg['title']}</h1>
      </div>

      <p>Bonjour <strong>{order.customer.prenom}</strong>,</p>
      <p>{msg['text']}</p>

      <div style="background:#f8f9fa;border:1px solid #e5e7eb;border-radius:10px;padding:18px;margin:18px 0;text-align:center;">
        <div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">Numéro de commande</div>
        <div style="font-family:monospace;font-size:20px;font-weight:800;color:#111;letter-spacing:2px;">{order.reference}</div>
      </div>

      <div style="text-align:center;margin:24px 0;">
        <a href="{tracking_url}"
           style="display:inline-block;padding:14px 32px;background:{msg['color']};color:#fff;font-weight:700;font-size:15px;border-radius:8px;text-decoration:none;">
          📍 Suivre ma commande
        </a>
      </div>

      <div style="background:#f8f9fa;border:1px solid #e5e7eb;border-radius:8px;padding:14px;margin:15px 0;">
        <div style="font-size:13px;font-weight:700;margin-bottom:8px;">Récapitulatif</div>
        {''.join(f'<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:13px;border-bottom:1px solid #eee;"><span>{item.quantity}x {item.designation[:30]}</span><span style="font-weight:600;">{int(item.subtotal)} FCFA</span></div>' for item in order.items)}
        <div style="display:flex;justify-content:space-between;padding:8px 0 0;font-size:16px;font-weight:800;color:{msg['color']};border-top:2px solid #ddd;margin-top:5px;">
          <span>Total</span><span>{int(order.total_amount)} FCFA</span>
        </div>
      </div>

      <div style="font-size:13px;color:#6b7280;margin-top:15px;">
        📦 Livraison : {order.adresse_livraison}, <strong>{order.ville_livraison}</strong><br>
        📞 Contact : {order.telephone_contact}
      </div>

      <div style="text-align:center;margin-top:25px;padding-top:15px;border-top:1px solid #e5e7eb;font-size:12px;color:#9ca3af;">
        {shop_name} — {tenant.ville or ''}
        {f'<br>📞 {tenant.telephone_entreprise}' if tenant.telephone_entreprise else ''}
      </div>
    </div>
    """

    send_email_async(
        to_email=order.customer.email,
        subject=f'{msg["icon"]} {msg["title"]} — {order.reference}',
        html_content=email_html
    )


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
        t.frais_livraison = request.form.get('frais_livraison', 0, type=int)
        t.seuil_livraison_gratuite = request.form.get('seuil_livraison_gratuite', 0, type=int)
        t.shop_heure_ouverture = request.form.get('heure_ouverture', '08:00')
        t.shop_heure_fermeture = request.form.get('heure_fermeture', '22:00')
        t.shop_jours_fermes = ','.join(request.form.getlist('jours_fermes'))
        t.shop_mode = request.form.get('shop_mode', 'boutique')
        t.stripe_secret_key = request.form.get('stripe_sk', '').strip() or None
        t.stripe_publishable_key = request.form.get('stripe_pk', '').strip() or None
        t.shop_whatsapp = request.form.get('shop_whatsapp', '').strip() or None


        db.session.commit()
        flash('Paramètres boutique en ligne sauvegardés.', 'success')
        return redirect(url_for('online.settings'))
    return render_template('manager/online/settings.html', tenant=t)
