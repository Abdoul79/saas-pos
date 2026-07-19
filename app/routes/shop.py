"""Boutique en ligne — pages publiques par tenant."""
from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, session, abort, jsonify)
from app import db
from app.models import (Tenant, Product, ProductVariant, Category,
                        OnlineCustomer, OnlineOrder, OnlineOrderItem,
                        OnlineOrderStatus, ProductReview, TenantStatus,
                        ShopVisit, ProductFavorite, CustomerAddress)
from datetime import datetime, date
import secrets, hashlib
import os

shop_bp = Blueprint('shop', __name__, template_folder='../../templates/shop')


def _get_tenant(slug):
    t = Tenant.query.filter_by(shop_slug=slug, status=TenantStatus.ACTIVE,
                               boutique_en_ligne_active=True).first_or_404()
    return t


def _get_customer(tenant_id):
    cid = session.get(f'shop_customer_{tenant_id}')
    if cid:
        return OnlineCustomer.query.get(cid)
    return None


def _get_cart(tenant_id):
    return session.get(f'shop_cart_{tenant_id}', [])


def _save_cart(tenant_id, cart):
    session[f'shop_cart_{tenant_id}'] = cart
    session.modified = True


# ── VITRINE ────────────────────────────────────────────────────────────────

@shop_bp.route('/<slug>')
def home(slug):
    t = _get_tenant(slug)
    categories = Category.query.filter_by(tenant_id=t.id).order_by(Category.nom).all()
    cat_id = request.args.get('cat', 0, type=int)

    # ── Recherche ──
    search_query = request.args.get('q', '').strip()

    q = Product.query.filter_by(tenant_id=t.id)
    if cat_id:
        q = q.filter_by(category_id=cat_id)

    # ── Filtre recherche sur désignation ──
    if search_query:
        q = q.filter(Product.designation.ilike(f'%{search_query}%'))

    if (t.shop_mode or 'boutique') == 'restaurant':
        products = q.order_by(Product.designation).all()
    else:
        products = [p for p in q.order_by(Product.designation).all()
                    if p.total_stock_magasin > 0 or p.total_stock_entrepot > 0]

    cart = _get_cart(t.id)
    customer = _get_customer(t.id)

    # Enregistrer la visite
    try:
        ip = request.remote_addr or 'unknown'
        ip_hash = hashlib.md5(f"{ip}-{date.today()}".encode()).hexdigest()[:16]
        from sqlalchemy import func
        already = db.session.query(ShopVisit).filter(
            ShopVisit.tenant_id == t.id,
            ShopVisit.ip_hash == ip_hash
        ).first()
        if not already:
            db.session.add(ShopVisit(tenant_id=t.id, ip_hash=ip_hash))
            db.session.commit()
    except Exception:
        db.session.rollback()

    # Favoris du client connecté
    my_favs = set()
    if customer:
        my_favs = {f.product_id for f in ProductFavorite.query.filter_by(
            tenant_id=t.id, customer_id=customer.id).all()}

    return render_template('shop/home.html', tenant=t, products=products,
                           categories=categories, current_cat=cat_id,
                           search_query=search_query,
                           cart=cart, customer=customer, my_favs=my_favs)


@shop_bp.route('/<slug>/produit/<int:pid>')
def product_detail(slug, pid):
    t = _get_tenant(slug)
    p = Product.query.filter_by(id=pid, tenant_id=t.id).first_or_404()
    reviews = ProductReview.query.filter_by(product_id=pid, is_approved=True)\
              .order_by(ProductReview.created_at.desc()).all()
    avg_rating = 0
    if reviews:
        avg_rating = sum(r.rating for r in reviews) / len(reviews)
    variants = list(p.variants) if p.has_variants else []
    cart = _get_cart(t.id)
    customer = _get_customer(t.id)

    # Enregistrer visite produit
    try:
        ip = request.remote_addr or 'unknown'
        ip_hash = hashlib.md5(f"{ip}-{date.today()}-p{pid}".encode()).hexdigest()[:16]
        already = db.session.query(ShopVisit).filter(
            ShopVisit.tenant_id == t.id, ShopVisit.ip_hash == ip_hash
        ).first()
        if not already:
            db.session.add(ShopVisit(tenant_id=t.id, ip_hash=ip_hash, product_id=pid))
            db.session.commit()
    except Exception:
        db.session.rollback()

    # Favoris
    fav_count = ProductFavorite.query.filter_by(tenant_id=t.id, product_id=pid).count()
    is_fav = False
    if customer:
        is_fav = ProductFavorite.query.filter_by(
            tenant_id=t.id, product_id=pid, customer_id=customer.id).first() is not None

    # ── Produits de la même catégorie (exclure le produit actuel) ──
    related_products = []
    if p.category_id:
        related_products = [rp for rp in Product.query.filter(
            Product.tenant_id == t.id,
            Product.category_id == p.category_id,
            Product.id != p.id
        ).order_by(Product.designation).limit(8).all()
        if rp.total_stock_magasin + rp.total_stock_entrepot > 0]

    return render_template('shop/product.html', tenant=t, product=p,
                           variants=variants, reviews=reviews,
                           avg_rating=avg_rating, cart=cart, customer=customer,
                           fav_count=fav_count, is_fav=is_fav,
                           related_products=related_products)


# ── PANIER ─────────────────────────────────────────────────────────────────

@shop_bp.route('/<slug>/panier/ajouter', methods=['POST'])
def add_to_cart(slug):
    t = _get_tenant(slug)
    pid = request.form.get('product_id', 0, type=int)
    vid = request.form.get('variant_id', 0, type=int)
    qty = request.form.get('quantity', 1, type=int)

    p = Product.query.get_or_404(pid)
    cart = _get_cart(t.id)

    if vid:
        v = ProductVariant.query.get_or_404(vid)
        item_key = f'v{vid}'
        price = float(v.prix_vente_ttc)
        name = f"{p.designation} — {v.attributs_display}"
        stock = v.stock_magasin + v.stock_entrepot
        img = v.image_url if hasattr(v, 'image_url') and v.image_url else (p.image_url or '')
    else:
        item_key = f'p{pid}'
        price = float(p.prix_vente_ttc)
        name = p.designation
        stock = p.total_stock_magasin + p.total_stock_entrepot
        img = p.image_url or ''

    found = False
    for item in cart:
        if item['key'] == item_key:
            item['qty'] = min(item['qty'] + qty, stock)
            found = True
            break
    if not found:
        cart.append({
            'key': item_key,
            'product_id': pid,
            'variant_id': vid or None,
            'name': name,
            'price': price,
            'qty': min(qty, stock),
            'image': img,
        })

    _save_cart(t.id, cart)
    flash(f'{name} ajouté au panier.', 'success')
    return redirect(request.referrer or url_for('shop.home', slug=slug))


@shop_bp.route('/<slug>/panier')
def cart_view(slug):
    t = _get_tenant(slug)
    cart = _get_cart(t.id)
    subtotal = sum(item['price'] * item['qty'] for item in cart)
    frais = float(t.frais_livraison or 0)
    seuil = float(t.seuil_livraison_gratuite or 0)
    livraison_gratuite = (frais == 0) or (seuil > 0 and subtotal >= seuil)
    frais_final = 0 if livraison_gratuite else frais
    total = subtotal + frais_final
    customer = _get_customer(t.id)
    return render_template('shop/cart.html', tenant=t, cart=cart,
                           subtotal=subtotal, frais_livraison=frais_final,
                           livraison_gratuite=livraison_gratuite,
                           seuil=seuil, total=total, customer=customer)

@shop_bp.route('/<slug>/panier/update/<key>/<action>')
def update_cart_qty(slug, key, action):
    t = _get_tenant(slug)
    cart = _get_cart(t.id)
    for item in cart:
        if item['key'] == key:
            if action == 'plus':
                item['qty'] += 1
            elif action == 'minus' and item['qty'] > 1:
                item['qty'] -= 1
            break
    _save_cart(t.id, cart)
    return redirect(url_for('shop.cart_view', slug=slug))


@shop_bp.route('/<slug>/panier/supprimer/<key>')
def remove_from_cart(slug, key):
    t = _get_tenant(slug)
    cart = [i for i in _get_cart(t.id) if i['key'] != key]
    _save_cart(t.id, cart)
    return redirect(url_for('shop.cart_view', slug=slug))


@shop_bp.route('/<slug>/panier/vider')
def clear_cart(slug):
    t = _get_tenant(slug)
    _save_cart(t.id, [])
    return redirect(url_for('shop.cart_view', slug=slug))


# ── AUTH CLIENT ────────────────────────────────────────────────────────────

@shop_bp.route('/<slug>/connexion', methods=['GET', 'POST'])
def login(slug):
    t = _get_tenant(slug)
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        c = OnlineCustomer.query.filter_by(tenant_id=t.id, email=email).first()
        if c and c.check_password(password):
            session[f'shop_customer_{t.id}'] = c.id
            flash(f'Bienvenue {c.prenom} !', 'success')
            return redirect(url_for('shop.home', slug=slug))
        flash('Email ou mot de passe incorrect.', 'danger')
    return render_template('shop/login.html', tenant=t)


@shop_bp.route('/<slug>/inscription', methods=['GET', 'POST'])
def register(slug):
    t = _get_tenant(slug)
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if OnlineCustomer.query.filter_by(tenant_id=t.id, email=email).first():
            flash('Un compte avec cet email existe déjà.', 'danger')
        else:
            c = OnlineCustomer(
                tenant_id=t.id,
                nom=request.form.get('nom', '').strip(),
                prenom=request.form.get('prenom', '').strip(),
                email=email,
                telephone=request.form.get('telephone', '').strip(),
                adresse=request.form.get('adresse', '').strip(),
                ville=request.form.get('ville', '').strip(),
            )
            c.set_password(request.form.get('password', ''))
            db.session.add(c)
            db.session.commit()
            session[f'shop_customer_{t.id}'] = c.id
            flash('Compte créé avec succès !', 'success')
            return redirect(url_for('shop.home', slug=slug))
    return render_template('shop/register.html', tenant=t)


@shop_bp.route('/<slug>/deconnexion')
def logout(slug):
    t = _get_tenant(slug)
    session.pop(f'shop_customer_{t.id}', None)
    flash('Déconnecté.', 'success')
    return redirect(url_for('shop.home', slug=slug))


# ── COMMANDE ───────────────────────────────────────────────────────────────
@shop_bp.route('/<slug>/commander', methods=['GET', 'POST'])
def checkout(slug):
    t = _get_tenant(slug)
    customer = _get_customer(t.id)
    if not customer:
        flash('Connectez-vous pour commander.', 'warning')
        return redirect(url_for('shop.login', slug=slug))

    cart = _get_cart(t.id)
    if not cart:
        flash('Votre panier est vide.', 'warning')
        return redirect(url_for('shop.home', slug=slug))

    subtotal = sum(item['price'] * item['qty'] for item in cart)
    frais = float(t.frais_livraison or 0)
    seuil = float(t.seuil_livraison_gratuite or 0)
    livraison_gratuite = (frais == 0) or (seuil > 0 and subtotal >= seuil)
    frais_final = 0 if livraison_gratuite else frais
    total = subtotal + frais_final

    # Vérifier si la boutique est ouverte
    if not t.shop_is_open:
        h_open  = t.shop_heure_ouverture or '08:00'
        h_close = t.shop_heure_fermeture or '22:00'
        flash(f'La boutique est fermée. Horaires : {h_open} — {h_close}. Revenez plus tard.', 'warning')
        return redirect(url_for('shop.cart_view', slug=slug))

    if request.method == 'POST':
        # Récupérer le téléphone (ajouter +225 si pas présent)
        tel = request.form.get('telephone', customer.telephone or '').strip()
        if tel and not tel.startswith('+'):
            tel = '+225' + tel.replace(' ', '')

        ref = f"WEB-{t.id}-{datetime.utcnow().strftime('%y%m%d%H%M')}-{secrets.token_hex(2).upper()}"
        order = OnlineOrder(
            tenant_id=t.id,
            customer_id=customer.id,
            reference=ref,
            total_amount=total,
            total_ht=subtotal,
            total_tva=0,
            frais_livraison=frais_final,
            adresse_livraison=request.form.get('adresse', customer.adresse or ''),
            ville_livraison=request.form.get('ville', customer.ville or ''),
            telephone_contact=tel,
            note_client=request.form.get('note', ''),
            payment_method=request.form.get('payment_method', 'livraison'),
        )
        db.session.add(order)
        db.session.flush()

        for item in cart:
            oi = OnlineOrderItem(
                order_id=order.id,
                product_id=item['product_id'],
                variant_id=item.get('variant_id'),
                designation=item['name'],
                prix_vente=item['price'],
                quantity=item['qty'],
                subtotal=item['price'] * item['qty'],
            )
            db.session.add(oi)

        # Sauvegarder l'adresse si nouvelle
        addr_text = request.form.get('adresse', '').strip()
        addr_ville = request.form.get('ville', '').strip()
        if addr_text and addr_ville:
            existing_addr = CustomerAddress.query.filter_by(
                customer_id=customer.id,
                adresse=addr_text,
                ville=addr_ville
            ).first()
            if not existing_addr:
                db.session.add(CustomerAddress(
                    customer_id=customer.id,
                    adresse=addr_text,
                    ville=addr_ville,
                    telephone=tel,
                ))

        db.session.commit()
        _save_cart(t.id, [])
        flash('Commande passée avec succès !', 'success')
        return redirect(url_for('shop.order_detail', slug=slug, ref=ref))

    # Charger les adresses sauvegardées
    saved_addresses = CustomerAddress.query.filter_by(
        customer_id=customer.id
    ).order_by(CustomerAddress.created_at.desc()).limit(5).all()

    return render_template('shop/checkout.html', tenant=t, customer=customer,
                           cart=cart, subtotal=subtotal, frais_livraison=frais_final,
                           livraison_gratuite=livraison_gratuite, seuil=seuil, total=total,
                           saved_addresses=saved_addresses,
                           google_maps_key=os.environ.get('AIzaSyAfaGh1vSoP24dTMH6my8PJ8S9OkoKRYRg', ''))


@shop_bp.route('/<slug>/mes-commandes')
def my_orders(slug):
    t = _get_tenant(slug)
    customer = _get_customer(t.id)
    if not customer:
        return redirect(url_for('shop.login', slug=slug))
    orders = OnlineOrder.query.filter_by(tenant_id=t.id, customer_id=customer.id)\
             .order_by(OnlineOrder.created_at.desc()).all()
    return render_template('shop/my_orders.html', tenant=t, orders=orders,
                           customer=customer, OnlineOrderStatus=OnlineOrderStatus)

@shop_bp.route('/<slug>/commande/<ref>')
def order_detail(slug, ref):
    t = _get_tenant(slug)
    customer = _get_customer(t.id)
    order = OnlineOrder.query.filter_by(tenant_id=t.id, reference=ref).first_or_404()
    return render_template('shop/order_detail.html', tenant=t, order=order,
                           customer=customer, cart=_get_cart(t.id),
                           OnlineOrderStatus=OnlineOrderStatus)





# ── PAIEMENT STRIPE ────────────────────────────────────────────────────────

@shop_bp.route('/<slug>/paiement/stripe', methods=['POST'])
def stripe_checkout(slug):
    """Crée une session Stripe Checkout et redirige."""
    import stripe, os
    t = _get_tenant(slug)
    #stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
    stripe.api_key = t.stripe_secret_key or os.environ.get('STRIPE_SECRET_KEY', '')
    if not stripe.api_key:
        flash('Paiement par carte non configuré par le vendeur.', 'danger')
        return redirect(url_for('shop.checkout', slug=slug))

    if not stripe.api_key:
        flash('Paiement par carte indisponible.', 'danger')
        return redirect(url_for('shop.checkout', slug=slug))

   
    customer = _get_customer(t.id)
    if not customer:
        return redirect(url_for('shop.login', slug=slug))

    cart = _get_cart(t.id)
    if not cart:
        return redirect(url_for('shop.home', slug=slug))

    subtotal = sum(item['price'] * item['qty'] for item in cart)
    frais = float(t.frais_livraison or 0)
    seuil = float(t.seuil_livraison_gratuite or 0)
    livraison_gratuite = (frais == 0) or (seuil > 0 and subtotal >= seuil)
    frais_final = 0 if livraison_gratuite else frais
    total = subtotal + frais_final

    # Créer la commande en attente de paiement
    tel = request.form.get('telephone', customer.telephone or '').strip()
    if tel and not tel.startswith('+'):
        tel = '+225' + tel.replace(' ', '')

    ref = f"WEB-{t.id}-{datetime.utcnow().strftime('%y%m%d%H%M')}-{secrets.token_hex(2).upper()}"
    order = OnlineOrder(
        tenant_id=t.id,
        customer_id=customer.id,
        reference=ref,
        total_amount=total,
        total_ht=subtotal,
        total_tva=0,
        frais_livraison=frais_final,
        adresse_livraison=request.form.get('adresse', customer.adresse or ''),
        ville_livraison=request.form.get('ville', customer.ville or ''),
        telephone_contact=tel,
        note_client=request.form.get('note', ''),
        payment_method='carte',
        payment_status='en_attente',
    )
    db.session.add(order)
    db.session.flush()

    for item in cart:
        db.session.add(OnlineOrderItem(
            order_id=order.id,
            product_id=item['product_id'],
            variant_id=item.get('variant_id'),
            designation=item['name'],
            prix_vente=item['price'],
            quantity=item['qty'],
            subtotal=item['price'] * item['qty'],
        ))

    # Sauvegarder adresse
    addr_text = request.form.get('adresse', '').strip()
    addr_ville = request.form.get('ville', '').strip()
    if addr_text and addr_ville:
        existing_addr = CustomerAddress.query.filter_by(
            customer_id=customer.id, adresse=addr_text, ville=addr_ville
        ).first()
        if not existing_addr:
            db.session.add(CustomerAddress(
                customer_id=customer.id, adresse=addr_text,
                ville=addr_ville, telephone=tel,
            ))

    db.session.commit()

    # Construire les line_items Stripe
    line_items = []
    for item in cart:
        line_items.append({
            'price_data': {
                'currency': 'xof',
                'product_data': {'name': item['name'][:80]},
                'unit_amount': int(item['price']),  # XOF n'a pas de centimes
            },
            'quantity': item['qty'],
        })

    # Ajouter frais de livraison si applicable
    if frais_final > 0:
        line_items.append({
            'price_data': {
                'currency': 'xof',
                'product_data': {'name': '🚚 Frais de livraison'},
                'unit_amount': int(frais_final),
            },
            'quantity': 1,
        })

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=line_items,
            mode='payment',
            customer_email=customer.email,
            metadata={'order_id': str(order.id), 'reference': ref},
            success_url=request.host_url.rstrip('/') + url_for('shop.stripe_success', slug=slug, ref=ref),
            cancel_url=request.host_url.rstrip('/') + url_for('shop.stripe_cancel', slug=slug, ref=ref),
        )
        _save_cart(t.id, [])
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        print(f'[stripe] Erreur: {e}')
        flash(f'Erreur de paiement : {e}', 'danger')
        # Supprimer la commande échouée
        db.session.delete(order)
        db.session.commit()
        return redirect(url_for('shop.checkout', slug=slug))


@shop_bp.route('/<slug>/paiement/succes/<ref>')
def stripe_success(slug, ref):
    """Page de succès après paiement Stripe."""
    t = _get_tenant(slug)
    order = OnlineOrder.query.filter_by(tenant_id=t.id, reference=ref).first_or_404()
    order.payment_status = 'paye'
    db.session.commit()
    flash('Paiement réussi ! Votre commande est confirmée.', 'success')
    return redirect(url_for('shop.order_detail', slug=slug, ref=ref))


@shop_bp.route('/<slug>/paiement/annule/<ref>')
def stripe_cancel(slug, ref):
    """Page d'annulation de paiement Stripe."""
    t = _get_tenant(slug)
    order = OnlineOrder.query.filter_by(tenant_id=t.id, reference=ref).first()
    if order and order.payment_status == 'en_attente':
        order.status = 'cancelled'
        order.payment_status = 'annule'
        db.session.commit()
    flash('Paiement annulé. Votre commande n\'a pas été validée.', 'warning')
    return redirect(url_for('shop.cart_view', slug=slug))


@shop_bp.route('/<slug>/webhook/stripe', methods=['POST'])
def stripe_webhook(slug):
    """Webhook Stripe pour confirmer le paiement (optionnel mais recommandé)."""
    import stripe, os
    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
    webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
        else:
            event = stripe.Event.construct_from(
                request.get_json(), stripe.api_key
            )
    except Exception as e:
        print(f'[stripe webhook] Erreur: {e}')
        return jsonify({'error': str(e)}), 400

    if event['type'] == 'checkout.session.completed':
        session_data = event['data']['object']
        order_id = session_data.get('metadata', {}).get('order_id')
        if order_id:
            order = OnlineOrder.query.get(int(order_id))
            if order:
                order.payment_status = 'paye'
                db.session.commit()
                print(f'[stripe] Paiement confirmé pour {order.reference}')

    return jsonify({'status': 'ok'}), 200


# ── AVIS ───────────────────────────────────────────────────────────────────

@shop_bp.route('/<slug>/avis/<int:pid>', methods=['POST'])
def add_review(slug, pid):
    t = _get_tenant(slug)
    customer = _get_customer(t.id)
    if not customer:
        flash('Connectez-vous pour laisser un avis.', 'warning')
        return redirect(url_for('shop.login', slug=slug))

    review = ProductReview(
        tenant_id=t.id,
        product_id=pid,
        customer_id=customer.id,
        rating=min(5, max(1, request.form.get('rating', 5, type=int))),
        comment=request.form.get('comment', '').strip(),
    )
    db.session.add(review)
    db.session.commit()
    flash('Merci pour votre avis !', 'success')
    return redirect(url_for('shop.product_detail', slug=slug, pid=pid))


# ── SUIVI ──────────────────────────────────────────────────────────────────

@shop_bp.route('/<slug>/suivi', methods=['GET', 'POST'])
def tracking(slug):
    t = _get_tenant(slug)
    error = None
    ref = request.args.get('ref', '')
    if request.method == 'POST':
        ref = request.form.get('reference', '').strip().upper()
        order = OnlineOrder.query.filter_by(tenant_id=t.id, reference=ref).first()
        if order:
            return redirect(url_for('shop.order_detail', slug=slug, ref=ref))
        error = f'Aucune commande trouvée avec la référence "{ref}".'
    return render_template('shop/tracking.html', tenant=t, error=error, ref=ref,
                           cart=_get_cart(t.id), customer=_get_customer(t.id))


# ── FAVORIS ────────────────────────────────────────────────────────────────

@shop_bp.route('/<slug>/favoris/toggle/<int:pid>', methods=['POST'])
def toggle_favorite(slug, pid):
    t = _get_tenant(slug)
    customer = _get_customer(t.id)
    if not customer:
        flash('Connectez-vous pour ajouter aux favoris.', 'warning')
        return redirect(url_for('shop.login', slug=slug))
    existing = ProductFavorite.query.filter_by(
        tenant_id=t.id, product_id=pid, customer_id=customer.id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
    else:
        db.session.add(ProductFavorite(tenant_id=t.id, product_id=pid, customer_id=customer.id))
        db.session.commit()
    return redirect(request.referrer or url_for('shop.home', slug=slug))


@shop_bp.route('/<slug>/favoris')
def favorites(slug):
    t = _get_tenant(slug)
    customer = _get_customer(t.id)
    if not customer:
        return redirect(url_for('shop.login', slug=slug))
    favs = ProductFavorite.query.filter_by(tenant_id=t.id, customer_id=customer.id)\
           .order_by(ProductFavorite.created_at.desc()).all()
    products = [f.product for f in favs if f.product]
    cart = _get_cart(t.id)
    return render_template('shop/favorites.html', tenant=t, products=products,
                           cart=cart, customer=customer)


@shop_bp.route('/<slug>/api/cart/count')
def cart_count(slug):
    t = _get_tenant(slug)
    cart = _get_cart(t.id)
    return jsonify({'count': sum(i['qty'] for i in cart)})


#for google claude connexion automatique
# ── GOOGLE OAUTH ───────────────────────────────────────────────────────────
from authlib.integrations.flask_client import OAuth as _OAuth


_oauth = _OAuth()

def _get_google(app=None):
    from flask import current_app
    _oauth.init_app(current_app)
    google = _oauth.register(
        name='google',
        client_id=os.environ.get('GOOGLE_CLIENT_ID'),
        client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )
    return google

@shop_bp.route('/<slug>/login/google')
def google_login(slug):
    t = _get_tenant(slug)
    session['google_slug'] = slug
    callback_url = url_for('shop.google_callback', _external=True)
    return _get_google().authorize_redirect(callback_url)

@shop_bp.route('/google/callback')
def google_callback():
    slug = session.pop('google_slug', None)
    if not slug:
        abort(400)
    t = _get_tenant(slug)
    try:
        token = _get_google().authorize_access_token()
        info  = token.get('userinfo') or _get_google().userinfo()
    except Exception as e:
        flash(f'Erreur Google : {e}', 'danger')
        return redirect(url_for('shop.login', slug=slug))

    email  = info.get('email', '').lower()
    prenom = info.get('given_name', '')
    nom    = info.get('family_name', '')

    # Trouver ou créer le compte client
    c = OnlineCustomer.query.filter_by(tenant_id=t.id, email=email).first()
    if not c:
        c = OnlineCustomer(
            tenant_id=t.id,
            email=email,
            prenom=prenom,
            nom=nom,
        )
        c.set_password(secrets.token_hex(16))  # mot de passe aléatoire
        db.session.add(c)
        db.session.commit()
        flash(f'Compte créé avec succès, bienvenue {prenom} !', 'success')
    else:
        flash(f'Bienvenue {c.prenom} !', 'success')

    session[f'shop_customer_{t.id}'] = c.id
    return redirect(url_for('shop.home', slug=slug))