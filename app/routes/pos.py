from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy import func
from app import db
from app.models import Product, ProductVariant, Category, Sale, SaleItem, PaymentMethod, UserRole
from app.utils.decorators import role_required, tenant_active_required
from datetime import date as date_cls
#from app.models import   # ajoute Tenant à l'import existant de app.models si pas déjà présent
from app.models import ClientCredit, CreditPayment, User, Tenant, Customer

#from app.models import ClientCredit, CreditPayment
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
        sale_type='detail',
        ticket_number=_get_next_ticket_number(_tid()),   # ← ligne ajoutée
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



# ── CAISSE FIDÉLITÉ ──────────────────────────────────────────────────────────
@pos_bp.route('/fidelite')
@_any_staff
def fidelite_interface():
    all_products = Product.query.filter_by(tenant_id=_tid()).order_by(Product.designation).all()
    catalog    = [p for p in all_products if p.total_stock_magasin > 0]
    categories = Category.query.filter_by(tenant_id=_tid()).order_by(Category.ordre, Category.nom).all()

    return render_template('pos/fidelite.html',
        catalog=catalog,
        categories=categories,
        is_manager=current_user.is_manager or current_user.is_super_admin)


@pos_bp.route('/api/customers/search')
@_any_staff
def search_customers():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    customers = Customer.query.filter(
        Customer.tenant_id == _tid()
    ).filter(
        db.or_(
            Customer.nom.ilike(f'%{q}%'),
            Customer.telephone.ilike(f'%{q}%')
        )
    ).limit(10).all()
    return jsonify([{
        'id': c.id, 'nom': c.nom, 'telephone': c.telephone or '',
        'nb_achats': c.nb_achats, 'total_achats': c.total_achats,
    } for c in customers])


@pos_bp.route('/api/customers/create', methods=['POST'])
@_any_staff
def create_customer_quick():
    data = request.get_json()
    nom       = (data.get('nom') or '').strip()
    telephone = (data.get('telephone') or '').strip() or None

    if not nom:
        return jsonify({'error': 'Le nom du client est obligatoire.'}), 400

    customer = Customer(
        tenant_id=_tid(), nom=nom, telephone=telephone, created_by=current_user.id
    )
    db.session.add(customer)
    db.session.commit()

    return jsonify({
        'id': customer.id, 'nom': customer.nom, 'telephone': customer.telephone or '',
        'nb_achats': 0, 'total_achats': 0,
    })


@pos_bp.route('/api/sale/fidelite', methods=['POST'])
@_any_staff
def validate_sale_fidelite():
    data = request.get_json()
    if not data or not data.get('items'):
        return jsonify({'error': 'Panier vide.'}), 400

    customer_id    = data.get('customer_id')
    payment_method = data.get('payment_method', PaymentMethod.CASH)
    amount_given   = float(data.get('amount_given', 0))
    discount_type  = data.get('discount_type')       # 'percent', 'amount', ou None
    discount_value = float(data.get('discount_value', 0) or 0)

    if discount_type not in (None, '', 'percent', 'amount'):
        return jsonify({'error': 'Type de remise invalide.'}), 400

    customer = None
    if customer_id:
        customer = Customer.query.filter_by(id=customer_id, tenant_id=_tid()).first()
        if not customer:
            return jsonify({'error': 'Client introuvable.'}), 404

    items_data     = data['items']
    sale_items_obj = []
    total_ttc = total_ht = total_tva = 0.0
    stock_updates = []

    for item in items_data:
        variant_id = item.get('variant_id')
        product_id = item['product_id']
        qty        = int(item['quantity'])
        if qty <= 0:
            return jsonify({'error': 'Quantité invalide.'}), 400

        if variant_id:
            v = (ProductVariant.query
                 .join(Product, Product.id == ProductVariant.product_id)
                 .filter(ProductVariant.id == int(variant_id), Product.tenant_id == _tid())
                 .with_for_update().first())
            if not v:
                return jsonify({'error': f'Variante ID {variant_id} introuvable.'}), 404
            if v.stock_magasin < qty:
                return jsonify({'error': (
                    f'Stock insuffisant pour « {v.product.designation} / {v.nom} ». '
                    f'Dispo en rayon : {v.stock_magasin}'
                )}), 409
            real_product_id = v.product_id
            unit_ttc = float(v.prix_vente_ttc)
            unit_ht  = float(v.prix_vente_ht)
            tva_rate = float(v.taux_tva)
            label    = f'{v.product.designation} — {v.nom}'
            subtotal = round(unit_ttc * qty, 2)
            total_ttc += subtotal; total_ht += round(unit_ht * qty, 2)
            total_tva += round((unit_ttc - unit_ht) * qty, 2)
            sale_items_obj.append(SaleItem(
                product_id=real_product_id, variant_id=int(variant_id),
                designation=label, prix_vente=unit_ttc, taux_tva=tva_rate,
                quantity=qty, subtotal=subtotal))
            stock_updates.append((v, qty))
        else:
            p = Product.query.filter_by(id=product_id, tenant_id=_tid()).with_for_update().first()
            if not p:
                return jsonify({'error': f'Produit ID {product_id} introuvable.'}), 404
            if p.stock_magasin < qty:
                return jsonify({'error': f'Stock insuffisant pour « {p.designation} ». Dispo : {p.stock_magasin}'}), 409
            unit_ttc = float(p.prix_vente_ttc)
            unit_ht  = float(p.prix_vente_ht)
            tva_rate = float(p.taux_tva)
            subtotal = round(unit_ttc * qty, 2)
            total_ttc += subtotal; total_ht += round(unit_ht * qty, 2)
            total_tva += round((unit_ttc - unit_ht) * qty, 2)
            sale_items_obj.append(SaleItem(product_id=p.id, designation=p.designation,
                                           prix_vente=unit_ttc, taux_tva=tva_rate,
                                           quantity=qty, subtotal=subtotal))
            stock_updates.append((p, qty))

    subtotal_before = round(total_ttc, 2)

    # ── Calcul de la remise ─────────────────────────────────────────────────
    discount_amount = 0.0
    if discount_type == 'percent':
        if discount_value < 0 or discount_value > 100:
            return jsonify({'error': 'Pourcentage de remise invalide (0-100).'}), 400
        discount_amount = round(subtotal_before * discount_value / 100, 2)
    elif discount_type == 'amount':
        if discount_value < 0:
            return jsonify({'error': 'Montant de remise invalide.'}), 400
        if discount_value > subtotal_before:
            return jsonify({'error': 'La remise ne peut pas dépasser le total du panier.'}), 400
        discount_amount = round(discount_value, 2)

    final_total = round(subtotal_before - discount_amount, 2)

    if payment_method == PaymentMethod.CASH and amount_given < final_total:
        return jsonify({'error': 'Montant donné insuffisant.'}), 400

    sale = Sale(
        tenant_id=_tid(), cashier_id=current_user.id, customer_id=customer.id if customer else None,
        total_ht=total_ht, total_tva=total_tva, total_amount=final_total,
        subtotal_before_discount=subtotal_before,
        discount_type=discount_type or None,
        discount_value=discount_value if discount_type else 0,
        discount_amount=discount_amount,
        amount_given=amount_given if amount_given > 0 else None,
        change_given=round(amount_given - final_total, 2) if amount_given > 0 else None,
        payment_method=payment_method,
        sale_type='fidelite',
        ticket_number=_get_next_ticket_number(_tid()),
    )
    db.session.add(sale)
    db.session.flush()

    for si in sale_items_obj:
        si.sale_id = sale.id
        db.session.add(si)

    for obj, qty in stock_updates:
        obj.stock_magasin -= qty

    db.session.commit()

    return jsonify({
        'success'  : True,
        'sale_id'  : sale.id,
        'subtotal' : subtotal_before,
        'discount' : discount_amount,
        'total'    : final_total,
        'change'   : sale.change_given,
        'customer' : customer.nom if customer else None,
    })


@pos_bp.route('/customers/<int:customer_id>')
@_any_staff
def customer_detail(customer_id):
    customer = Customer.query.filter_by(id=customer_id, tenant_id=_tid()).first_or_404()
    sales = customer.sales.order_by(Sale.created_at.desc()).all()
    return render_template('pos/customer_detail.html', customer=customer, sales=sales)


@pos_bp.route('/customers')
@_any_staff
def customers_list():
    customers = Customer.query.filter_by(tenant_id=_tid()).order_by(Customer.nom).all()
    return render_template('pos/customers_list.html', customers=customers)


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
        ticket_number=_get_next_ticket_number(_tid()),   # ← ligne ajoutée
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

def _get_next_ticket_number(tenant_id):
    """Numéro de ticket qui repart à 1 chaque jour, par tenant.
    Verrouille la ligne Tenant (with_for_update) pour éviter les doublons
    si deux ventes sont validées au même instant."""
    tenant_row = Tenant.query.filter_by(id=tenant_id).with_for_update().first()
    today = date_cls.today()
    if tenant_row.last_ticket_date != today:
        tenant_row.last_ticket_number = 0
        tenant_row.last_ticket_date = today
    tenant_row.last_ticket_number += 1
    return tenant_row.last_ticket_number


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

# ── CRÉDITS — vue caisse (gérant : tout / caissier : ses propres crédits) ──
@pos_bp.route('/credits')
@_any_staff
def pos_credits():
    is_manager_view = current_user.is_manager or current_user.is_super_admin

    q = ClientCredit.query.filter_by(tenant_id=_tid())
    if not is_manager_view:
        q = q.filter_by(created_by=current_user.id)

    all_credits = q.order_by(ClientCredit.date_echeance.asc()).all()

    # Mise à jour auto du statut "en_retard"
    for c in all_credits:
        if not c.is_solde and c.jours_avant_echeance < 0 and c.statut != 'en_retard':
            c.statut = 'en_retard'
    db.session.commit()

    total_du    = sum(c.montant_restant for c in all_credits if not c.is_solde)
    nb_en_cours = sum(1 for c in all_credits if not c.is_solde)
    nb_retard   = sum(1 for c in all_credits if not c.is_solde and c.jours_avant_echeance < 0)

    # Nom du créateur de chaque crédit (utile côté gérant)
    users_map = {u.id: u.full_name for u in User.query.filter_by(tenant_id=_tid()).all()}
    if current_user.id not in users_map:
        users_map[current_user.id] = current_user.full_name

    return render_template('pos/credits_list.html',
        credits=all_credits, total_du=total_du,
        nb_en_cours=nb_en_cours, nb_retard=nb_retard,
        is_manager_view=is_manager_view, users_map=users_map)


@pos_bp.route('/credits/<int:credit_id>/payment', methods=['POST'])
@_any_staff
def pos_add_credit_payment(credit_id):
    credit = ClientCredit.query.filter_by(id=credit_id, tenant_id=_tid()).first_or_404()

    is_manager_view = current_user.is_manager or current_user.is_super_admin
    if not is_manager_view and credit.created_by != current_user.id:
        flash("Vous ne pouvez encaisser que les crédits que vous avez vous-même accordés.", 'danger')
        return redirect(url_for('pos.pos_credits'))

    montant_raw = request.form.get('montant', '0')
    note        = request.form.get('note', '').strip() or None
    try:
        montant_f = float(montant_raw)
    except ValueError:
        flash('Montant invalide.', 'danger')
        return redirect(url_for('pos.pos_credits'))

    if montant_f <= 0:
        flash('Le montant doit être positif.', 'danger')
        return redirect(url_for('pos.pos_credits'))
    if montant_f > credit.montant_restant:
        flash(f'Le montant dépasse le solde restant ({credit.montant_restant:,.0f} FCFA).', 'danger')
        return redirect(url_for('pos.pos_credits'))

    db.session.add(CreditPayment(
        credit_id=credit.id, montant=montant_f, note=note, created_by=current_user.id
    ))
    if credit.montant_restant - montant_f <= 0:
        credit.statut = 'paye'
    db.session.commit()

    flash(f'Paiement de {montant_f:,.0f} FCFA encaissé pour {credit.client_nom}.', 'success')
    return redirect(url_for('pos.pos_credits'))

#delette credits par le gerant apres solde
@pos_bp.route('/credits/<int:credit_id>/delete', methods=['POST'])
@_any_staff
def pos_delete_credit(credit_id):
    credit = ClientCredit.query.filter_by(id=credit_id, tenant_id=_tid()).first_or_404()

    is_manager_view = current_user.is_manager or current_user.is_super_admin
    if not is_manager_view:
        flash("Seul le gérant peut supprimer un crédit.", 'danger')
        return redirect(url_for('pos.pos_credits'))

    if not credit.is_solde:
        flash("Ce crédit n'est pas encore soldé — impossible de le supprimer.", 'danger')
        return redirect(url_for('pos.pos_credits'))

    name = credit.client_nom
    db.session.delete(credit)
    db.session.commit()
    flash(f'Crédit soldé de {name} supprimé.', 'info')
    return redirect(url_for('pos.pos_credits'))

@pos_bp.route('/ticket/<int:sale_id>')
@_any_staff
def ticket(sale_id):
    sale   = Sale.query.filter_by(id=sale_id, tenant_id=_tid()).first_or_404()
    tenant = current_user.tenant

    cashier   = sale.cashier
    initiales = (cashier.prenom[:1] + cashier.nom[:1]).upper() if cashier else '??'
    def mask_name(n): return n[0] + '*' * (len(n) - 1) if n else ''
    cashier_masked = f"{mask_name(cashier.prenom)} {mask_name(cashier.nom)}" if cashier else ''

    # ── Infos crédit si vente à crédit ──────────────────────────────────────
    credit_info = None
    if sale.sale_type == 'credit':
        credit_info = ClientCredit.query.filter_by(sale_id=sale.id).first()

    qr_b64 = None
    try:
        import qrcode, base64
        from io import BytesIO
        qr_data = (
            f"TICKET #{sale.ticket_number or sale.id}\n"
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
                           credit_info=credit_info,
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

# ── Caisse Crédit ────────────────────────────────────────────────────────────
@pos_bp.route('/credit')
@_any_staff
def credit_interface():
    all_products = Product.query.filter_by(tenant_id=_tid()).order_by(Product.designation).all()
    catalog    = [p for p in all_products if p.total_stock_magasin > 0]
    categories = Category.query.filter_by(tenant_id=_tid()).order_by(Category.ordre, Category.nom).all()

    return render_template('pos/credit.html',
        catalog=catalog,
        categories=categories,
        is_manager=current_user.is_manager or current_user.is_super_admin)

@pos_bp.route('/api/sale/credit', methods=['POST'])
@_any_staff
def validate_sale_credit():
    data = request.get_json()
    if not data or not data.get('items'):
        return jsonify({'error': 'Panier vide.'}), 400

    client_nom        = (data.get('client_nom') or '').strip()
    client_telephone  = (data.get('client_telephone') or '').strip() or None
    date_echeance_raw = data.get('date_echeance', '')
    acompte           = float(data.get('acompte', 0) or 0)

    if not client_nom:
        return jsonify({'error': 'Le nom du client est obligatoire.'}), 400
    if not date_echeance_raw:
        return jsonify({'error': "La date d'échéance est obligatoire."}), 400
    try:
        date_echeance = date_cls.fromisoformat(date_echeance_raw)
    except ValueError:
        return jsonify({'error': "Date d'échéance invalide."}), 400

    items_data     = data['items']
    sale_items_obj = []
    total_ttc = total_ht = total_tva = 0.0
    stock_updates = []

    for item in items_data:
        variant_id = item.get('variant_id')
        product_id = item['product_id']
        qty        = int(item['quantity'])
        if qty <= 0:
            return jsonify({'error': 'Quantité invalide.'}), 400

        if variant_id:
            v = (ProductVariant.query
                 .join(Product, Product.id == ProductVariant.product_id)
                 .filter(ProductVariant.id == int(variant_id), Product.tenant_id == _tid())
                 .with_for_update().first())
            if not v:
                return jsonify({'error': f'Variante ID {variant_id} introuvable.'}), 404
            if v.stock_magasin < qty:
                return jsonify({'error': (
                    f'Stock insuffisant pour « {v.product.designation} / {v.nom} ». '
                    f'Dispo en rayon : {v.stock_magasin}'
                )}), 409
            real_product_id = v.product_id
            unit_ttc = float(v.prix_vente_ttc)
            unit_ht  = float(v.prix_vente_ht)
            tva_rate = float(v.taux_tva)
            label    = f'{v.product.designation} — {v.nom}'
            subtotal = round(unit_ttc * qty, 2)
            total_ttc += subtotal; total_ht += round(unit_ht * qty, 2)
            total_tva += round((unit_ttc - unit_ht) * qty, 2)
            sale_items_obj.append(SaleItem(
                product_id=real_product_id, variant_id=int(variant_id),
                designation=label, prix_vente=unit_ttc, taux_tva=tva_rate,
                quantity=qty, subtotal=subtotal))
            stock_updates.append((v, qty))
        else:
            p = Product.query.filter_by(id=product_id, tenant_id=_tid()).with_for_update().first()
            if not p:
                return jsonify({'error': f'Produit ID {product_id} introuvable.'}), 404
            if p.stock_magasin < qty:
                return jsonify({'error': f'Stock insuffisant pour « {p.designation} ». Dispo : {p.stock_magasin}'}), 409
            unit_ttc = float(p.prix_vente_ttc)
            unit_ht  = float(p.prix_vente_ht)
            tva_rate = float(p.taux_tva)
            subtotal = round(unit_ttc * qty, 2)
            total_ttc += subtotal; total_ht += round(unit_ht * qty, 2)
            total_tva += round((unit_ttc - unit_ht) * qty, 2)
            sale_items_obj.append(SaleItem(product_id=p.id, designation=p.designation,
                                           prix_vente=unit_ttc, taux_tva=tva_rate,
                                           quantity=qty, subtotal=subtotal))
            stock_updates.append((p, qty))

    total_ttc = round(total_ttc, 2)
    total_ht  = round(total_ht,  2)
    total_tva = round(total_tva, 2)

    if acompte < 0 or acompte > total_ttc:
        return jsonify({'error': "Montant de l'acompte invalide."}), 400

    # ── Créer la vente (marquée comme crédit) ──────────────────────────────
    sale = Sale(
        tenant_id=_tid(), cashier_id=current_user.id,
        total_ht=total_ht, total_tva=total_tva, total_amount=total_ttc,
        amount_given=acompte if acompte > 0 else None,
        change_given=None,
        payment_method='credit',
        sale_type='credit',
        ticket_number=_get_next_ticket_number(_tid()),
    )
    db.session.add(sale)
    db.session.flush()

    for si in sale_items_obj:
        si.sale_id = sale.id
        db.session.add(si)

    for obj, qty in stock_updates:
        obj.stock_magasin -= qty

    # ── Créer le crédit client lié à cette vente ───────────────────────────
    credit = ClientCredit(
        tenant_id=_tid(), client_nom=client_nom, client_telephone=client_telephone,
        montant_total=total_ttc, date_echeance=date_echeance,
        sale_id=sale.id, created_by=current_user.id,
        notes=f'Vente à crédit — Ticket #{sale.ticket_number}'
    )
    db.session.add(credit)
    db.session.flush()

    if acompte > 0:
        db.session.add(CreditPayment(
            credit_id=credit.id, montant=acompte,
            note='Acompte versé à la vente', created_by=current_user.id
        ))
        if acompte >= total_ttc:
            credit.statut = 'paye'

    db.session.commit()

    return jsonify({
        'success'  : True,
        'sale_id'  : sale.id,
        'credit_id': credit.id,
        'total'    : total_ttc,
        'total_ht' : total_ht,
        'total_tva': total_tva,
        'acompte'  : acompte,
        'reste'    : round(total_ttc - acompte, 2),
        'client_nom': client_nom,
        'date_echeance': date_echeance.strftime('%d/%m/%Y'),
    })

