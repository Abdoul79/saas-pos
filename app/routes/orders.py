from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, Response, current_app
from flask_login import login_required, current_user
from datetime import datetime, date
from app import db
from app.models import (Supplier, Product, ProductVariant, SupplierOrder,
                         SupplierOrderItem, OrderStatus, UserRole)
from app.utils.decorators import role_required, tenant_active_required

orders_bp = Blueprint('orders', __name__)


def _mgr(f):
    from functools import wraps
    @wraps(f)
    @login_required
    @role_required(UserRole.MANAGER)
    @tenant_active_required
    def w(*a, **kw): return f(*a, **kw)
    return w


def _tid(): return current_user.tenant_id


def _next_ref():
    """Generate auto reference like CMD-2026-0042."""
    year  = date.today().year
    count = SupplierOrder.query.filter_by(tenant_id=_tid()).count() + 1
    return f'CMD-{year}-{str(count).zfill(4)}'


# ── LIST (global + per supplier) ──────────────────────────────────────────
@orders_bp.route('/')
@_mgr
def index():
    statut      = request.args.get('statut', '')
    supplier_id = request.args.get('supplier_id', 0, type=int)
    q = SupplierOrder.query.filter_by(tenant_id=_tid())
    if statut:
        q = q.filter_by(statut=statut)
    if supplier_id:
        q = q.filter_by(supplier_id=supplier_id)
    orders    = q.order_by(SupplierOrder.created_at.desc()).all()
    suppliers = Supplier.query.filter_by(tenant_id=_tid(), is_active=True).order_by(Supplier.nom).all()
    return render_template('manager/orders/index.html',
                           orders=orders, suppliers=suppliers,
                           current_statut=statut,
                           current_supplier=supplier_id,
                           OrderStatus=OrderStatus)


# ── CREATE ────────────────────────────────────────────────────────────────
@orders_bp.route('/create', methods=['GET', 'POST'])
@orders_bp.route('/create/<int:sid>', methods=['GET', 'POST'])
@_mgr
def create(sid=None):
    suppliers = Supplier.query.filter_by(tenant_id=_tid(), is_active=True).order_by(Supplier.nom).all()
    products  = Product.query.filter_by(tenant_id=_tid()).order_by(Product.designation).all()

    if request.method == 'POST':
        supplier_id = int(request.form.get('supplier_id', 0))
        if not supplier_id:
            flash('Veuillez sélectionner un fournisseur.', 'danger')
            return render_template('manager/orders/form.html',
                                   suppliers=suppliers, products=products, preselect=sid)

        supplier = Supplier.query.filter_by(id=supplier_id, tenant_id=_tid()).first_or_404()

        order = SupplierOrder(
            tenant_id   = _tid(),
            supplier_id = supplier_id,
            created_by_id = current_user.id,
            reference   = request.form.get('reference', '').strip() or _next_ref(),
            ref_fournisseur = request.form.get('ref_fournisseur', '').strip() or None,
            statut      = OrderStatus.DRAFT,
            notes       = request.form.get('notes', '').strip() or None,
            date_livraison_prevue = _parse_date(request.form.get('date_livraison_prevue', '')),
        )
        db.session.add(order)
        db.session.flush()

        # Items
        product_ids = request.form.getlist('product_id[]')
        variant_ids = request.form.getlist('variant_id[]')
        quantities  = request.form.getlist('quantite[]')
        prix_list   = request.form.getlist('prix_achat[]')

        for pid, vid, qty_s, prix_s in zip(product_ids, variant_ids, quantities, prix_list):
            qty  = int(qty_s or 0)
            prix = float(prix_s or 0)
            if qty <= 0:
                continue
            product = Product.query.filter_by(id=int(pid), tenant_id=_tid()).first()
            if not product:
                continue

            if vid and int(vid):
                variant = ProductVariant.query.filter_by(id=int(vid), product_id=int(pid)).first()
                desig   = f'{product.designation} — {variant.attributs_display}' if variant else product.designation
                sku     = (variant.sku or product.sku) if variant else product.sku
                vid_int = int(vid)
            else:
                variant = None
                desig   = product.designation
                sku     = product.sku
                vid_int = None

            item = SupplierOrderItem(
                order_id            = order.id,
                product_id          = int(pid),
                variant_id          = vid_int,
                designation         = desig,
                sku                 = sku,
                quantite_commandee  = qty,
                quantite_recue      = 0,
                prix_achat_unitaire = prix,
                total               = round(qty * prix, 2),
            )
            db.session.add(item)

        db.session.commit()
        flash(f'Commande {order.reference} créée pour {supplier.nom}.', 'success')
        return redirect(url_for('orders.detail', oid=order.id))

    return render_template('manager/orders/form.html',
                           suppliers=suppliers, products=products, preselect=sid)


# ── DETAIL ────────────────────────────────────────────────────────────────
@orders_bp.route('/<int:oid>')
@_mgr
def detail(oid):
    order = SupplierOrder.query.filter_by(id=oid, tenant_id=_tid()).first_or_404()
    return render_template('manager/orders/detail.html', order=order, OrderStatus=OrderStatus)


# ── PDF (génère le bon de commande + passe statut à Envoyée) ──────────────
@orders_bp.route('/<int:oid>/pdf')
@_mgr
def download_pdf(oid):
    order = SupplierOrder.query.filter_by(id=oid, tenant_id=_tid()).first_or_404()

    # Rendre le template HTML dédié au PDF
    html_str = render_template('manager/orders/pdf.html',
                               order=order,
                               tenant=order.supplier.tenant,
                               today=date.today())

    try:
        from weasyprint import HTML, CSS
        pdf_bytes = HTML(string=html_str,
                         base_url=current_app.root_path).write_pdf()

        # Marquer comme envoyée si encore en brouillon
        if order.statut == OrderStatus.DRAFT:
            order.statut = OrderStatus.SENT
            db.session.commit()

        filename = f'commande_{order.reference or order.id}.pdf'
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        flash(f'Erreur génération PDF : {e}', 'danger')
        return redirect(url_for('orders.detail', oid=oid))


# ── RECEIVE (Saisie des quantités reçues → update stock entrepôt) ──────────
@orders_bp.route('/<int:oid>/receive', methods=['GET', 'POST'])
@_mgr
def receive(oid):
    order = SupplierOrder.query.filter_by(id=oid, tenant_id=_tid()).first_or_404()
    if not order.can_receive:
        flash('Cette commande ne peut pas être réceptionnée dans son état actuel.', 'warning')
        return redirect(url_for('orders.detail', oid=oid))

    if request.method == 'POST':
        all_received = True
        for item in order.items:
            field_key = f'recu_{item.id}'
            recu = int(request.form.get(field_key, 0) or 0)
            recu = min(recu, item.quantite_commandee)  # Ne pas dépasser la qté commandée
            if recu > 0 and recu != item.quantite_recue:
                delta = recu - item.quantite_recue
                # Mettre à jour le stock entrepôt
                if item.variant_id and item.variant:
                    item.variant.stock_entrepot += delta
                else:
                    item.product.stock_entrepot += delta
                item.quantite_recue = recu
            if item.quantite_recue < item.quantite_commandee:
                all_received = False

        # Mettre à jour le statut
        any_received = any(i.quantite_recue > 0 for i in order.items)
        if all_received:
            order.statut = OrderStatus.RECEIVED
        elif any_received:
            order.statut = OrderStatus.PARTIAL

        db.session.commit()
        flash('Réception enregistrée. Les stocks entrepôt ont été mis à jour.', 'success')
        return redirect(url_for('orders.detail', oid=oid))

    return render_template('manager/orders/receive.html', order=order)


# ── CANCEL ────────────────────────────────────────────────────────────────
@orders_bp.route('/<int:oid>/cancel', methods=['POST'])
@_mgr
def cancel(oid):
    order = SupplierOrder.query.filter_by(id=oid, tenant_id=_tid()).first_or_404()
    if order.statut == OrderStatus.RECEIVED:
        flash('Une commande reçue ne peut pas être annulée.', 'danger')
    else:
        order.statut = OrderStatus.CANCELLED
        db.session.commit()
        flash(f'Commande {order.reference} annulée.', 'info')
    return redirect(url_for('orders.detail', oid=oid))


# ── API: produits d'un fournisseur ─────────────────────────────────────────
@orders_bp.route('/api/supplier/<int:sid>/products')
@_mgr
def api_supplier_products(sid):
    products = Product.query.filter_by(supplier_id=sid, tenant_id=_tid())\
                            .order_by(Product.designation).all()
    result = []
    for p in products:
        if p.has_variants:
            for v in p.variants.filter_by(is_active=True).all():
                result.append({
                    'product_id': p.id,
                    'variant_id': v.id,
                    'designation': f'{p.designation} — {v.attributs_display}',
                    'sku': v.sku or p.sku or '',
                    'prix_achat': float(p.prix_achat),
                    'stock_entrepot': v.stock_entrepot,
                })
        else:
            result.append({
                'product_id': p.id,
                'variant_id': None,
                'designation': p.designation,
                'sku': p.sku or '',
                'prix_achat': float(p.prix_achat),
                'stock_entrepot': p.stock_entrepot,
            })
    return jsonify(result)


def _parse_date(s):
    try:
        return datetime.strptime(s, '%Y-%m-%d').date() if s else None
    except ValueError:
        return None
