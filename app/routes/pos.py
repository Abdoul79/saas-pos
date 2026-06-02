from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func
from app import db
from app.models import Product, ProductVariant, Category, Sale, SaleItem, PaymentMethod, UserRole
from app.utils.decorators import role_required, tenant_active_required

pos_bp = Blueprint('pos', __name__)


def _any_staff(f):
    from functools import wraps
    @wraps(f)
    @login_required
    @role_required(UserRole.CASHIER, UserRole.MANAGER)
    @tenant_active_required
    def wrapped(*args, **kwargs): return f(*args, **kwargs)
    return wrapped


def _tid(): return current_user.tenant_id


# ── Interface principale ───────────────────────────────────────────────────
@pos_bp.route('/interface')
@_any_staff
def interface():
    all_products = Product.query.filter_by(tenant_id=_tid())                                .order_by(Product.designation).all()

    # Séparer produits en rayon et produits en rupture
    catalog     = [p for p in all_products if p.total_stock_magasin > 0]
    out_of_stock= [p for p in all_products if p.total_stock_magasin == 0
                   and p.total_stock_entrepot > 0]  # épuisé MAIS entrepôt dispo

    categories  = Category.query.filter_by(tenant_id=_tid())                                 .order_by(Category.ordre, Category.nom).all()

    return render_template('pos/interface.html',
        payment_methods=[PaymentMethod.CASH, PaymentMethod.CARD, PaymentMethod.MOBILE_MONEY],
        catalog=catalog,
        out_of_stock=out_of_stock,
        categories=categories,
        is_manager=current_user.is_manager or current_user.is_super_admin)


# ── Scan code-barres ───────────────────────────────────────────────────────
@pos_bp.route('/api/product/scan')
@_any_staff
def scan_product():
    barcode = request.args.get('barcode', '').strip()
    name    = request.args.get('name',    '').strip()

    if barcode:
        # 1. Chercher dans les variantes d'abord
        variant = ProductVariant.query.filter_by(barcode=barcode, tenant_id=_tid()).first()
        if variant and variant.is_active:
            if variant.stock_magasin <= 0:
                return jsonify({'error': f'« {variant.product.designation} / {variant.nom} » est en rupture.'}), 409
            return jsonify(_variant_pos_json(variant))

        # 2. Chercher dans les produits simples
        product = Product.query.filter_by(barcode=barcode, tenant_id=_tid()).first()
        if not product:
            return jsonify({'error': 'Produit introuvable.'}), 404
        if product.has_variants:
            # Retourner le produit avec flag pour ouvrir le sélecteur
            return jsonify({**_product_pos_json(product), 'needs_variant': True,
                            'variants': [_variant_pos_json(v) for v in product.variants.filter_by(is_active=True).all()]})
        if product.stock_magasin <= 0:
            return jsonify({'error': f'« {product.designation} » est en rupture.'}), 409
        return jsonify(_product_pos_json(product))

    elif name:
        products = Product.query.filter(
            Product.tenant_id == _tid(),
            Product.designation.ilike(f'%{name}%')
        ).limit(15).all()
        result = []
        for p in products:
            if p.total_stock_magasin > 0:
                result.append({**_product_pos_json(p),
                                'needs_variant': p.has_variants,
                                'variants': [_variant_pos_json(v) for v in p.variants.filter_by(is_active=True).all()] if p.has_variants else []})
        return jsonify(result)

    return jsonify({'error': 'Paramètre manquant.'}), 400


# ── Valider la vente ───────────────────────────────────────────────────────
@pos_bp.route('/api/sale', methods=['POST'])
@_any_staff
def validate_sale():
    data = request.get_json()
    if not data or not data.get('items'):
        return jsonify({'error': 'Panier vide.'}), 400

    items_data     = data['items']
    payment_method = data.get('payment_method', PaymentMethod.CASH)
    amount_given   = float(data.get('amount_given', 0))

    sale_items_obj = []
    total_ttc = total_ht = total_tva = 0.0
    stock_updates = []

    for item in items_data:
        variant_id = item.get('variant_id')
        product_id = item['product_id']
        qty        = int(item['quantity'])

        if qty <= 0:
            return jsonify({'error': 'Quantité invalide.'}), 400

        # Cas variante
        if variant_id:
            # Sécurité : retrouver la variante via JOIN product pour garantir
            # l'appartenance au tenant, sans dépendre du product_id du payload
            v = (ProductVariant.query
                 .join(Product, Product.id == ProductVariant.product_id)
                 .filter(
                     ProductVariant.id == int(variant_id),
                     Product.tenant_id == _tid()
                 )
                 .with_for_update()
                 .first())
            if not v:
                return jsonify({'error': f'Variante ID {variant_id} introuvable ou accès refusé.'}), 404
            if v.stock_magasin < qty:
                return jsonify({'error': (
                    f'Stock insuffisant pour « {v.product.designation} / {v.nom} ». '
                    f'Dispo en rayon : {v.stock_magasin}'
                )}), 409
            # product_id vient de la DB, pas du payload (sécurité + robustesse)
            real_product_id = v.product_id
            unit_ttc  = float(v.prix_vente_ttc)
            unit_ht   = float(v.prix_vente_ht)
            tva_rate  = float(v.taux_tva)
            label     = f'{v.product.designation} — {v.nom}'
            subtotal  = round(unit_ttc * qty, 2)
            total_ttc += subtotal; total_ht += round(unit_ht * qty, 2)
            total_tva += round((unit_ttc - unit_ht) * qty, 2)
            sale_items_obj.append(SaleItem(
                product_id=real_product_id, variant_id=int(variant_id),
                designation=label, prix_vente=unit_ttc, taux_tva=tva_rate,
                quantity=qty, subtotal=subtotal))
            stock_updates.append(('variant', v, qty))

        # Cas produit simple
        else:
            p = Product.query.filter_by(id=product_id, tenant_id=_tid()).with_for_update().first()
            if not p:
                return jsonify({'error': f'Produit ID {product_id} introuvable.'}), 404
            if p.stock_magasin < qty:
                return jsonify({'error': f'Stock insuffisant pour « {p.designation} ». Dispo : {p.stock_magasin}'}), 409
            unit_ttc  = float(p.prix_vente_ttc)
            unit_ht   = float(p.prix_vente_ht)
            tva_rate  = float(p.taux_tva)
            subtotal  = round(unit_ttc * qty, 2)
            total_ttc += subtotal; total_ht += round(unit_ht * qty, 2); total_tva += round((unit_ttc - unit_ht) * qty, 2)
            sale_items_obj.append(SaleItem(product_id=p.id, designation=p.designation,
                                           prix_vente=unit_ttc, taux_tva=tva_rate,
                                           quantity=qty, subtotal=subtotal))
            stock_updates.append(('product', p, qty))

    total_ttc = round(total_ttc, 2)
    total_ht  = round(total_ht,  2)
    total_tva = round(total_tva, 2)

    if payment_method == PaymentMethod.CASH and amount_given < total_ttc:
        return jsonify({'error': 'Montant donné insuffisant.'}), 400

    sale = Sale(
        tenant_id=_tid(), cashier_id=current_user.id,
        total_ht=total_ht, total_tva=total_tva, total_amount=total_ttc,
        amount_given=amount_given if amount_given > 0 else None,
        change_given=round(amount_given - total_ttc, 2) if amount_given > 0 else None,
        payment_method=payment_method,
        sale_type='detail'
    )
    db.session.add(sale)
    db.session.flush()

    for si in sale_items_obj:
        si.sale_id = sale.id
        db.session.add(si)

    for kind, obj, qty in stock_updates:
        obj.stock_magasin -= qty

    db.session.commit()

    return jsonify({
        'success'   : True,
        'sale_id'   : sale.id,
        'total'     : total_ttc,
        'total_ht'  : total_ht,
        'total_tva' : total_tva,
        'change'    : sale.change_given,
    })


# ── Caisse Gros ─────────────────────────────────────────────────────────────
@pos_bp.route('/engros')
@_any_staff
def engros():
    tenant = current_user.tenant
    if not tenant or not tenant.vente_engros_active:
        flash('La vente en gros n\'est pas activee pour votre compte.', 'warning')
        return redirect(url_for('pos.interface'))

    products   = Product.query.filter_by(tenant_id=_tid()).order_by(Product.designation).all()
    catalog    = [p for p in products if p.total_stock_magasin > 0]
    categories = Category.query.filter_by(tenant_id=_tid()).order_by(Category.ordre, Category.nom).all()

    return render_template('pos/engros.html',
        payment_methods=[PaymentMethod.CASH, PaymentMethod.CARD, PaymentMethod.MOBILE_MONEY],
        catalog=catalog,
        categories=categories,
        is_manager=current_user.is_manager or current_user.is_super_admin)


@pos_bp.route('/api/sale/engros', methods=['POST'])
@_any_staff
def validate_sale_engros():
    tenant = current_user.tenant
    if not tenant or not tenant.vente_engros_active:
        return jsonify({'error': 'Vente en gros non autorisee.'}), 403

    data         = request.get_json()
    items_data   = data.get('items', [])
    method       = data.get('payment_method', PaymentMethod.CASH)
    amount_given = float(data.get('amount_given', 0))

    if not items_data:
        return jsonify({'error': 'Panier vide.'}), 400

    total_ttc = total_ht = total_tva = 0.0
    sale_items_obj = []
    stock_updates  = []

    for item in items_data:
        variant_id = item.get('variant_id')
        product_id = item['product_id']
        qty        = int(item['quantity'])
        if qty <= 0:
            continue

        if variant_id:
            v = (ProductVariant.query
                 .join(Product, Product.id == ProductVariant.product_id)
                 .filter(ProductVariant.id == int(variant_id), Product.tenant_id == _tid())
                 .with_for_update().first())
            if not v:
                return jsonify({'error': f'Variante ID {variant_id} introuvable.'}), 404
            if v.stock_magasin < qty:
                return jsonify({'error': f'Stock insuffisant : {v.product.designation} / {v.nom}. Dispo : {v.stock_magasin}'}), 409
            parent   = v.product
            unit_ttc = float(parent.prix_gros) if parent.prix_gros else float(v.prix_vente_ttc)
            unit_ht  = round(unit_ttc / (1 + float(v.taux_tva) / 100), 2)
            tva_rate = float(v.taux_tva)
            label    = f'{parent.designation} - {v.nom}'
            real_pid = parent.id
            stock_updates.append(('variant', v, qty))
        else:
            p = Product.query.filter_by(id=int(product_id), tenant_id=_tid()).with_for_update().first()
            if not p:
                return jsonify({'error': f'Produit ID {product_id} introuvable.'}), 404
            if p.stock_magasin < qty:
                return jsonify({'error': f'Stock insuffisant : {p.designation}. Dispo : {p.stock_magasin}'}), 409
            unit_ttc  = float(p.prix_gros) if p.prix_gros else float(p.prix_vente_ttc)
            unit_ht   = round(unit_ttc / (1 + float(p.taux_tva) / 100), 2)
            tva_rate  = float(p.taux_tva)
            label     = p.designation
            real_pid  = p.id
            variant_id = None
            stock_updates.append(('product', p, qty))

        subtotal   = round(unit_ttc * qty, 2)
        total_ttc += subtotal
        total_ht  += round(unit_ht * qty, 2)
        total_tva += round((unit_ttc - unit_ht) * qty, 2)
        sale_items_obj.append(SaleItem(
            product_id=real_pid,
            variant_id=int(variant_id) if variant_id else None,
            designation=label, prix_vente=unit_ttc, taux_tva=tva_rate,
            quantity=qty, subtotal=subtotal))

    change = round(amount_given - total_ttc, 2) if method == PaymentMethod.CASH else 0.0

    sale = Sale(
        tenant_id=_tid(), cashier_id=current_user.id,
        total_amount=round(total_ttc, 2),
        total_ht=round(total_ht, 2),
        total_tva=round(total_tva, 2),
        payment_method=method,
        amount_given=amount_given,
        change_given=max(0, change),
        sale_type='engros',
    )
    db.session.add(sale)
    db.session.flush()
    for si in sale_items_obj:
        si.sale_id = sale.id
        db.session.add(si)
    for kind, obj, qty in stock_updates:
        obj.stock_magasin -= qty
    db.session.commit()

    return jsonify({
        'sale_id'  : sale.id,
        'total'    : float(sale.total_amount),
        'total_ht' : float(sale.total_ht),
        'total_tva': float(sale.total_tva),
        'change'   : float(sale.change_given),
        'sale_type': 'engros',
    })


# ── Réapprovisionnement rapide depuis la caisse (manager seulement) ─────────
@pos_bp.route('/api/quick-restock', methods=['POST'])
@login_required
@role_required(UserRole.MANAGER)
@tenant_active_required
def quick_restock():
    from app.models import StockTransfer, UserRole
    data       = request.get_json()
    product_id = data.get('product_id')
    variant_id = data.get('variant_id')
    quantity   = int(data.get('quantity', 0))

    if quantity <= 0:
        return jsonify({'error': 'Quantité invalide.'}), 400

    if variant_id:
        v = (ProductVariant.query
             .join(Product, Product.id == ProductVariant.product_id)
             .filter(ProductVariant.id == int(variant_id), Product.tenant_id == _tid())
             .first())
        if not v:
            return jsonify({'error': 'Variante introuvable.'}), 404
        if v.stock_entrepot < quantity:
            return jsonify({'error': f'Stock entrepôt insuffisant. Disponible : {v.stock_entrepot}'}), 409
        v.stock_entrepot  -= quantity
        v.stock_magasin   += quantity
        real_product_id    = v.product_id
        label              = f'{v.product.designation} / {v.nom}'
        new_stock_magasin  = v.stock_magasin
        new_stock_entrepot = v.stock_entrepot
    else:
        p = Product.query.filter_by(id=int(product_id), tenant_id=_tid()).first()
        if not p:
            return jsonify({'error': 'Produit introuvable.'}), 404
        if p.stock_entrepot < quantity:
            return jsonify({'error': f'Stock entrepôt insuffisant. Disponible : {p.stock_entrepot}'}), 409
        p.stock_entrepot  -= quantity
        p.stock_magasin   += quantity
        real_product_id    = p.id
        label              = p.designation
        new_stock_magasin  = p.stock_magasin
        new_stock_entrepot = p.stock_entrepot

    # Enregistrer le mouvement de stock
    db.session.add(StockTransfer(
        tenant_id  = _tid(),
        product_id = real_product_id,
        variant_id = int(variant_id) if variant_id else None,
        manager_id = current_user.id,
        quantity   = quantity,
        note       = 'Réappro rapide depuis caisse'
    ))
    db.session.commit()

    return jsonify({
        'success'          : True,
        'label'            : label,
        'quantity'         : quantity,
        'new_stock_magasin': new_stock_magasin,
        'new_stock_entrepot': new_stock_entrepot,
    })


# ── Facture Vente en Gros ────────────────────────────────────────────────────
@pos_bp.route('/facture/<int:sale_id>')
@_any_staff
def facture_engros(sale_id):
    sale        = Sale.query.filter_by(id=sale_id, tenant_id=_tid()).first_or_404()
    tenant      = current_user.tenant
    num_facture = f'FAC-GROS-{sale.created_at.strftime("%Y%m")}-{str(sale_id).zfill(5)}'

    qr_b64 = None
    try:
        import qrcode, base64
        from io import BytesIO
        qr_data = (
            f'FACTURE {num_facture}\n'
            f'{tenant.nom_boutique or tenant.activite}\n'
            f'{sale.created_at.strftime("%d/%m/%Y %H:%M")}\n'
            f'TOTAL: {float(sale.total_amount):.0f} FCFA\n'
            f'VENTE EN GROS'
        )
        qr = qrcode.QRCode(version=2, box_size=4, border=2,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buf = BytesIO()
        img.save(buf, format='PNG')
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"QR code warning: {e}")

    return render_template('pos/facture_engros.html',
                           sale=sale,
                           tenant=tenant,
                           num_facture=num_facture,
                           qr_b64=qr_b64)


# ── Ticket ─────────────────────────────────────────────────────────────────
@pos_bp.route('/ticket/<int:sale_id>')
@_any_staff
def ticket(sale_id):
    sale   = Sale.query.filter_by(id=sale_id, tenant_id=_tid()).first_or_404()
    tenant = current_user.tenant

    cashier   = sale.cashier
    initiales = (cashier.prenom[:1] + cashier.nom[:1]).upper() if cashier else '??'
    def mask_name(n): return n[0] + '*' * (len(n) - 1) if n else ''
    cashier_masked = f"{mask_name(cashier.prenom)} {mask_name(cashier.nom)}" if cashier else ''

    qr_b64 = None
    try:
        import qrcode, base64
        from io import BytesIO
        qr_data = (
            f"TICKET #{sale.id}\n"
            f"{tenant.nom_boutique or tenant.activite}\n"
            f"{sale.created_at.strftime('%d/%m/%Y %H:%M')}\n"
            f"TOTAL: {float(sale.total_amount):.0f} FCFA\n"
            f"Caissier: {initiales}\n"
            f"Mode: {sale.payment_method}"
        )
        qr = qrcode.QRCode(version=2, box_size=4, border=2,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buf = BytesIO()
        img.save(buf, format='PNG')
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"QR code warning: {e}")

    return render_template('pos/ticket.html',
                           sale=sale,
                           tenant=tenant,
                           initiales=initiales,
                           cashier_masked=cashier_masked,
                           qr_b64=qr_b64)


# ── JSON helpers ───────────────────────────────────────────────────────────
def _product_pos_json(p):
    return {
        'id'            : p.id,
        'designation'   : p.designation,
        'sku'           : p.sku,
        'barcode'       : p.barcode,
        'category'      : p.category.nom if p.category else None,
        'category_color': p.category.couleur if p.category else None,
        'prix_vente'    : float(p.prix_vente_ttc),
        'prix_ht'       : float(p.prix_vente_ht),
        'taux_tva'      : float(p.taux_tva),
        'stock_magasin' : p.total_stock_magasin,
        'image_url'     : p.image_url or '',
        'supplier'      : p.supplier.nom if p.supplier else None,
        'has_variants'  : p.has_variants,
        'needs_variant' : False,
        'variants'      : [],
    }


def _variant_pos_json(v):
    return {
        'id'            : v.product_id,   # product_id parent — clé de regroupement
        'variant_id'    : v.id,           # ID réel de la variante — INDISPENSABLE
        'is_variant'    : True,           # flag pour cartKey() et validateSale()
        'designation'   : v.product.designation,
        'variant_label' : v.attributs_display,
        'sku'           : v.sku or v.product.sku,
        'barcode'       : v.barcode,
        'category'      : v.product.category.nom if v.product.category else None,
        'category_color': v.product.category.couleur if v.product.category else None,
        'prix_vente'    : v.prix_vente_ttc_f,
        'prix_ht'       : v.prix_vente_ht_f,
        'taux_tva'      : v.taux_tva_f,
        'stock_magasin' : v.stock_magasin,
        'image_url'     : v.image_url or '',
        'supplier'      : v.product.supplier.nom if v.product.supplier else None,
        'has_variants'  : False,
        'needs_variant' : False,
        'attributs'     : v.attributs,
    }


@pos_bp.route('/api/ping', methods=['POST', 'GET'])
@login_required
def ping():
    """Caissier ping — met a jour last_seen + etat caisse en temps reel."""
    from datetime import datetime
    from sqlalchemy import text
    import json as _json
    try:
        # Lire l'etat du panier depuis le body JSON
        state_json = None
        if request.method == 'POST' and request.is_json:
            body = request.get_json(silent=True) or {}
            if 'state' in body:
                state_json = _json.dumps(body['state'])

        db.session.execute(
            text("""UPDATE users
                 SET last_seen = :now
                 {% if state %}, pos_state = :state{% endif %}
                 WHERE id = :uid""".replace(
                     '{% if state %}, pos_state = :state{% endif %}',
                     ', pos_state = :state' if state_json else ''
                 )),
            {'now': datetime.utcnow(), 'uid': current_user.id,
             **({'state': state_json} if state_json else {})}
        )
        db.session.commit()
        return {'ok': True, 'user': current_user.full_name}
    except Exception as e:
        db.session.rollback()
        return {'ok': False, 'error': str(e)}, 500
