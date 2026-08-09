import os, uuid
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from datetime import datetime, date
from sqlalchemy import func, desc
from werkzeug.utils import secure_filename
from app import db
from app.utils.storage import upload_image, delete_image
from app.models import (User, UserRole, Product, Category, Supplier, StockTransfer,
                         Sale, SaleItem, LossFiche, LossFicheItem)
from app.utils.decorators import role_required, tenant_active_required
from app.utils.barcode_gen import generate_ean13_number, generate_barcode_b64

manager_bp = Blueprint('manager', __name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '..', '..', 'static', 'uploads', 'products')
ALLOWED_EXT   = {'png', 'jpg', 'jpeg', 'webp', 'gif'}


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


def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def _save_image(file_obj) -> str | None:
    """Upload image vers Supabase Storage (prod) ou local (dev). Retourne URL ou chemin."""
    if not file_obj or file_obj.filename == '':
        return None
    if not _allowed_file(file_obj.filename):
        return None
    # upload_image gere automatiquement Supabase ou local selon config
    url = upload_image(file_obj, folder='products')
    print(f"[_save_image] upload_image retourne: {url!r}")
    return url


def _delete_image(filename_or_url: str):
    """Supprimer image de Supabase ou localement."""
    if filename_or_url:
        try:
            delete_image(filename_or_url)
        except Exception as e:
            print(f"[_delete_image] warning: {e}")


def _calc_ttc(ht: float, tva: float) -> float:
    """Calculate TTC from HT + TVA rate (%)."""
    return round(ht * (1 + tva / 100), 2)


# ── DASHBOARD ──────────────────────────────────────────────────────────────
@manager_bp.route('/dashboard')
@_manager_access
def dashboard():
    tenant = current_user.tenant
    today  = date.today()

    today_sales = Sale.query.filter(
        Sale.tenant_id == _tid(),
        func.date(Sale.created_at) == today
    ).all()
    ca_today = sum(float(s.total_amount) for s in today_sales)
    tx_today = len(today_sales)

    month_start = today.replace(day=1)
    ca_month    = db.session.query(func.sum(Sale.total_amount)).filter(
        Sale.tenant_id == _tid(), Sale.created_at >= month_start
    ).scalar() or 0

    # Top produits : agrégation par product_id — couvre les variantes
    # (SaleItem.product_id est toujours le produit parent)
    top_products_raw = db.session.query(
        Product.id,
        Product.designation,
        Product.has_variants,
        func.sum(SaleItem.quantity).label('qty')
    ).join(SaleItem, SaleItem.product_id == Product.id).filter(
        Product.tenant_id == _tid()
    ).group_by(Product.id).order_by(desc('qty')).limit(5).all()

    # Enrichir avec le stock total (variantes incluses)
    top_products = []
    for row in top_products_raw:
        p = Product.query.get(row.id)
        top_products.append({
            'designation'     : row.designation,
            'qty'             : row.qty,
            'has_variants'    : row.has_variants,
            'stock_magasin'   : p.total_stock_magasin   if p else 0,
            'stock_entrepot'  : p.total_stock_entrepot  if p else 0,
        })

    active_cashiers = User.query.filter_by(
        tenant_id=_tid(), role=UserRole.CASHIER, is_active=True).all()

    # Alertes stock : utiliser total_stock_magasin (somme variantes incluse)
    all_products = Product.query.filter_by(tenant_id=_tid()).all()
    low_stock = []
    for p in all_products:
        sm = p.total_stock_magasin
        se = p.total_stock_entrepot
        if sm <= 5:
            # Pour les variantes, lister les variantes en rupture
            variants_low = []
            if p.has_variants:
                for v in p.variants:
                    if v.stock_magasin <= 5 and v.is_active:
                        variants_low.append({
                            'nom'           : v.attributs_display or v.nom,
                            'stock_magasin' : v.stock_magasin,
                            'stock_entrepot': v.stock_entrepot,
                        })
            low_stock.append({
                'designation'   : p.designation,
                'has_variants'  : p.has_variants,
                'stock_magasin' : sm,
                'stock_entrepot': se,
                'variants_low'  : variants_low,
                'product_id'    : p.id,
            })

    # Infos super admin (contact + montant mensuel)
    from app.models import Config
    saas_telephone = Config.get('saas_telephone', '')
    saas_email     = Config.get('saas_email', '')
    montant_mensuel= tenant.montant_mensuel or Config.get('montant_mensuel_defaut', '')

    return render_template('manager/dashboard.html',
        tenant=tenant, ca_today=ca_today, tx_today=tx_today,
        ca_month=float(ca_month), top_products=top_products,
        active_cashiers=active_cashiers, low_stock=low_stock,
        saas_telephone=saas_telephone,
        saas_email=saas_email,
        montant_mensuel=montant_mensuel)


# ── API : prochain SKU ────────────────────────────────────────────────────
@manager_bp.route('/api/next-sku')
@_manager_access
def api_next_sku():
    from flask import jsonify
    return jsonify({'sku': _next_sku()})


# ── PRODUCTS PDF ──────────────────────────────────────────────────────────
@manager_bp.route('/products/pdf')
@_manager_access
def products_pdf():
    from flask import Response, current_app
    from datetime import date

    q           = request.args.get('q', '')
    category_id = request.args.get('category_id', 0, type=int)
    supplier_id = request.args.get('supplier_id', 0, type=int)

    query = Product.query.filter_by(tenant_id=_tid())
    if q:
        query = query.filter(Product.designation.ilike(f'%{q}%'))
    if category_id:
        query = query.filter_by(category_id=category_id)
    if supplier_id:
        query = query.filter_by(supplier_id=supplier_id)
    products = query.order_by(Product.designation).all()

    categories = Category.query.filter_by(tenant_id=_tid()).all()
    suppliers  = Supplier.query.filter_by(tenant_id=_tid()).all()

    html_str = render_template('manager/products_pdf.html',
                               products=products,
                               tenant=current_user.tenant,
                               today=date.today(),
                               q=q, category_id=category_id, supplier_id=supplier_id)
    try:
        from weasyprint import HTML
        pdf = HTML(string=html_str, base_url=current_app.root_path).write_pdf()
        fname = f'catalogue_produits_{date.today().strftime("%Y%m%d")}.pdf'
        return Response(pdf, mimetype='application/pdf',
                        headers={'Content-Disposition': f'attachment; filename="{fname}"'})
    except Exception as e:
        flash(f'Erreur PDF : {e}', 'danger')
        return redirect(url_for('manager.products'))


# ── ÉTIQUETTES PRIX (étiquettes rayonnage) ────────────────────────────────
@manager_bp.route('/products/price-tags')
@_manager_access
def price_tags_pdf():
    from flask import Response, current_app
    from datetime import date

    q           = request.args.get('q', '')
    category_id = request.args.get('category_id', 0, type=int)
    supplier_id = request.args.get('supplier_id', 0, type=int)

    query = Product.query.filter_by(tenant_id=_tid())
    if q:
        query = query.filter(Product.designation.ilike(f'%{q}%'))
    if category_id:
        query = query.filter_by(category_id=category_id)
    if supplier_id:
        query = query.filter_by(supplier_id=supplier_id)

    # Aplatir les variantes actives pour les produits avec variantes
    items = []
    for p in query.order_by(Product.designation).all():
        if p.has_variants:
            for v in p.variants.filter_by(is_active=True).all():
                items.append({
                    'nom'        : p.designation,
                    'variant'    : v.attributs_display or v.nom,
                    'prix'       : float(v.prix_vente_ttc),
                    'prix_gros'  : float(p.prix_gros) if p.prix_gros else None,
                    'sku'        : v.sku or p.sku or '',
                    'barcode'    : v.barcode or '',
                    'category'   : p.category,
                    'has_variant': True,
                })
        else:
            items.append({
                'nom'        : p.designation,
                'variant'    : '',
                'prix'       : float(p.prix_vente_ttc),
                'prix_gros'  : float(p.prix_gros) if p.prix_gros else None,
                'sku'        : p.sku or '',
                'barcode'    : p.barcode or '',
                'category'   : p.category,
                'has_variant': False,
            })

    html_str = render_template('manager/price_tags_pdf.html',
                               items=items,
                               tenant=current_user.tenant,
                               today=date.today())
    try:
        from weasyprint import HTML
        pdf = HTML(string=html_str, base_url=current_app.root_path).write_pdf()
        fname = f'etiquettes_prix_{date.today().strftime("%Y%m%d")}.pdf'
        return Response(pdf, mimetype='application/pdf',
                        headers={'Content-Disposition': f'attachment; filename="{fname}"'})
    except Exception as e:
        flash(f'Erreur PDF : {e}', 'danger')
        return redirect(url_for('manager.products'))


# ── PRODUCTS / STOCK CENTRAL ───────────────────────────────────────────────
@manager_bp.route('/products')
@_manager_access
def products():
    q           = request.args.get('q', '')
    supplier_id = request.args.get('supplier_id', 0, type=int)
    category_id = request.args.get('category_id', 0, type=int)
    query       = Product.query.filter_by(tenant_id=_tid())
    if q:
        query = query.filter(Product.designation.ilike(f'%{q}%'))
    if supplier_id:
        query = query.filter_by(supplier_id=supplier_id)
    if category_id:
        query = query.filter_by(category_id=category_id)
    prods      = query.order_by(Product.designation).all()
    suppliers  = Supplier.query.filter_by(tenant_id=_tid(), is_active=True).order_by(Supplier.nom).all()
    categories = Category.query.filter_by(tenant_id=_tid()).order_by(Category.ordre, Category.nom).all()
    return render_template('manager/products.html',
                           products=prods, q=q, suppliers=suppliers,
                           categories=categories,
                           current_supplier=supplier_id,
                           current_category=category_id)


def _next_sku():
    """Génère le prochain SKU séquentiel pour le tenant courant."""
    from app.models import ProductVariant
    # Compter tous les produits + variantes pour un numéro global unique
    nb_products = Product.query.filter_by(tenant_id=_tid()).count()
    nb_variants = db.session.query(func.count(ProductVariant.id)).filter_by(tenant_id=_tid()).scalar() or 0
    next_num    = nb_products + nb_variants + 1
    return f'ART-{next_num:04d}'


# ── CREATE PRODUCT ───────────────────────────────────────────────────────
@manager_bp.route('/products/create', methods=['GET', 'POST'])
@_manager_access
def create_product():
    suppliers  = Supplier.query.filter_by(tenant_id=_tid(), is_active=True).order_by(Supplier.nom).all()
    categories = Category.query.filter_by(tenant_id=_tid()).order_by(Category.ordre, Category.nom).all()

    if request.method == 'POST':
        designation   = request.form.get('designation', '').strip()
        sku           = request.form.get('sku', '').strip() or None
        description   = request.form.get('description', '').strip() or None
        prix_achat    = float(request.form.get('prix_achat', 0) or 0)
        prix_gros_raw = request.form.get('prix_gros', '').strip()
        prix_gros     = float(prix_gros_raw) if prix_gros_raw else None
        prix_vente_ht = float(request.form.get('prix_vente_ht', 0) or 0)
        taux_tva      = float(request.form.get('taux_tva', 0) or 0)
        supplier_id   = request.form.get('supplier_id', None) or None
        category_id   = request.form.get('category_id', None) or None
        barcode_input = request.form.get('barcode', '').strip()
        stock_init    = int(request.form.get('stock_initial', 0) or 0)
        generate_bc   = request.form.get('generate_barcode') == '1'

        if not designation or prix_vente_ht <= 0:
            flash('Désignation et prix de vente HT sont obligatoires.', 'danger')
            return render_template('manager/product_form.html', suppliers=suppliers,
                                   categories=categories, next_sku=_next_sku())

        prix_vente_ttc = _calc_ttc(prix_vente_ht, taux_tva)
        image_file = request.files.get('image')
        image_fn   = _save_image(image_file)

        product = Product(
            tenant_id      = _tid(),
            supplier_id    = int(supplier_id)   if supplier_id   else None,
            category_id    = int(category_id)   if category_id   else None,
            sku            = sku,
            designation    = designation,
            description    = description,
            prix_achat     = prix_achat,
            prix_gros      = prix_gros,
            prix_vente_ht  = prix_vente_ht,
            taux_tva       = taux_tva,
            prix_vente_ttc = prix_vente_ttc,
            image_filename = image_fn,
            stock_entrepot = stock_init,
        )
        if generate_bc or not barcode_input:
            product.barcode = generate_ean13_number(_tid())
            product.barcode_generated = True
        else:
            product.barcode = barcode_input

        db.session.add(product)
        db.session.flush()  # pour avoir product.id

        # ── Images supplémentaires ──────────────────────────────────────
        from app.models import ProductImage
        extra_files = request.files.getlist('extra_images')
        for i, img_file in enumerate(extra_files):
            if img_file and img_file.filename:
                fname = _save_image(img_file)
                if fname:
                    db.session.add(ProductImage(
                        product_id=product.id,
                        filename=fname,
                        order_num=i
                    ))

        db.session.commit()
        # Supprimer images cochées
        delete_img_ids = request.form.getlist('delete_extra_image')
        for img_id in delete_img_ids:
            from app.models import ProductImage
            pi = ProductImage.query.get(int(img_id))
            if pi and pi.product.tenant_id == _tid():
                try: delete_image(pi.filename)
                except: pass
                db.session.delete(pi)
        db.session.commit()


        flash(f'Produit « {designation} » créé. Prix TTC : {prix_vente_ttc:,.0f} FCFA.', 'success')
        return redirect(url_for('manager.products'))

    return render_template('manager/product_form.html', suppliers=suppliers,
                           categories=categories, next_sku=_next_sku())


# ─────────────────────────────────────────────────────────────────────────────
@manager_bp.route('/products/<int:product_id>/edit', methods=['GET', 'POST'])
@_manager_access
def edit_product(product_id):
    product    = Product.query.filter_by(id=product_id, tenant_id=_tid()).first_or_404()
    suppliers  = Supplier.query.filter_by(tenant_id=_tid(), is_active=True).order_by(Supplier.nom).all()
    categories = Category.query.filter_by(tenant_id=_tid()).order_by(Category.ordre, Category.nom).all()

    if request.method == 'POST':
        product.designation   = request.form.get('designation', product.designation).strip()
        product.sku           = request.form.get('sku', '').strip() or None
        product.description   = request.form.get('description', '').strip() or None
        product.prix_achat    = float(request.form.get('prix_achat', product.prix_achat) or 0)
        pg_raw = request.form.get('prix_gros', '').strip()
        product.prix_gros     = float(pg_raw) if pg_raw else None
        product.prix_vente_ht = float(request.form.get('prix_vente_ht', product.prix_vente_ht) or 0)
        product.taux_tva      = float(request.form.get('taux_tva', product.taux_tva) or 0)
        product.prix_vente_ttc= _calc_ttc(float(product.prix_vente_ht), float(product.taux_tva))
        sid = request.form.get('supplier_id', None) or None
        cid = request.form.get('category_id', None) or None
        product.supplier_id   = int(sid) if sid else None
        product.category_id   = int(cid) if cid else None

        # Image principale — update
        image_file = request.files.get('image')
        if image_file and image_file.filename:
            _delete_image(product.image_filename)
            product.image_filename = _save_image(image_file)

        # Image principale — suppression
        if request.form.get('delete_image') == '1':
            _delete_image(product.image_filename)
            product.image_filename = None

        # ── Images supplémentaires — suppression ───────────────────────
        from app.models import ProductImage
        delete_extra_ids = request.form.getlist('delete_extra_image')
        for img_id in delete_extra_ids:
            try:
                pi = ProductImage.query.get(int(img_id))
                if pi and pi.product_id == product_id:
                    _delete_image(pi.filename)
                    db.session.delete(pi)
            except Exception:
                pass

        # ── Images supplémentaires — ajout ─────────────────────────────
        extra_files = request.files.getlist('extra_images')
        existing_count = ProductImage.query.filter_by(product_id=product_id).count()
        for i, img_file in enumerate(extra_files):
            if img_file and img_file.filename:
                fname = _save_image(img_file)
                if fname:
                    db.session.add(ProductImage(
                        product_id=product_id,
                        filename=fname,
                        order_num=existing_count + i
                    ))

        db.session.commit()
        flash('Produit mis à jour.', 'success')
        return redirect(url_for('manager.products'))

    return render_template('manager/product_form.html', product=product,
                           suppliers=suppliers, categories=categories)



@manager_bp.route('/products/<int:product_id>/barcode')
@_manager_access
def view_barcode(product_id):
    product = Product.query.filter_by(id=product_id, tenant_id=_tid()).first_or_404()
    bc_b64  = generate_barcode_b64(product.barcode) if product.barcode else ''
    return render_template('manager/barcode_sheet.html', product=product, bc_b64=bc_b64)


# ── AJOUT DE STOCK ENTREPÔT (réception marchandises) ─────────────────────
@manager_bp.route('/products/<int:product_id>/receive', methods=['GET'])
@_manager_access
def receive_stock(product_id):
    """Page de réception de stock - affiche les références du produit."""
    product = Product.query.filter_by(id=product_id, tenant_id=_tid()).first_or_404()
    variants = product.variants.filter_by(is_active=True).all() if product.has_variants else []
    return render_template('manager/receive_stock.html', product=product, variants=variants)


@manager_bp.route('/products/<int:product_id>/add-stock', methods=['POST'])
@_manager_access
def add_stock(product_id):
    from app.models import ProductVariant
    product    = Product.query.filter_by(id=product_id, tenant_id=_tid()).first_or_404()
    variant_id = request.form.get('variant_id', '') or None
    quantity   = int(request.form.get('quantity', 0) or 0)
    note       = request.form.get('note', '').strip()

    if quantity <= 0:
        flash('La quantité doit être positive.', 'danger')
        return redirect(url_for('manager.products'))

    if product.has_variants and variant_id:
        variant = ProductVariant.query.filter_by(
            id=int(variant_id), product_id=product_id, tenant_id=_tid()
        ).first_or_404()
        variant.stock_entrepot += quantity
        db.session.commit()
        flash(f'{quantity} unite(s) ajoutee(s) a l\'entrepot : {product.designation} / {variant.nom}.', 'success')
    elif product.has_variants and not variant_id:
        flash('Veuillez sélectionner une variante.', 'danger')
    else:
        product.stock_entrepot += quantity
        db.session.commit()
        flash(f'{quantity} unite(s) ajoutee(s) a l\'entrepot : {product.designation}.', 'success')

    return redirect(url_for('manager.products'))


# ── DELETE PRODUCT ──────────────────────────────────────────────────────────
@manager_bp.route('/products/<int:product_id>/delete', methods=['POST'])
@_manager_access
def delete_product(product_id):
    p = Product.query.filter_by(id=product_id, tenant_id=_tid()).first_or_404()
    name = p.designation
    try:
        from app.models import (SaleItem, StockTransfer, ProductVariant,
                                 SupplierOrderItem, LossFicheItem,
                                 ProductFavorite, ShopVisit, ProductReview,
                                 OnlineOrderItem, ProductImage)
        from app.utils.storage import delete_image

        # 1. Supprimer images variantes
        for v in p.variants.all():
            try: delete_image(v.image_filename)
            except Exception: pass

        # 2. Supprimer image principale
        try: delete_image(p.image_filename)
        except Exception: pass

        # 2b. Supprimer images supplémentaires
        for pi in ProductImage.query.filter_by(product_id=product_id).all():
            try: delete_image(pi.filename)
            except Exception: pass
        ProductImage.query.filter_by(product_id=product_id).delete(synchronize_session=False)

        # 3. Détacher les ventes
        SaleItem.query.filter_by(product_id=product_id).update(
            {'product_id': None, 'variant_id': None}, synchronize_session=False
        )

        # 3b. Nettoyer les dépendances boutique en ligne
        ProductFavorite.query.filter_by(product_id=product_id).delete(synchronize_session=False)
        ShopVisit.query.filter_by(product_id=product_id).update(
            {'product_id': None}, synchronize_session=False
        )
        ProductReview.query.filter_by(product_id=product_id).update(
            {'product_id': None}, synchronize_session=False
        )
        OnlineOrderItem.query.filter_by(product_id=product_id).update(
            {'product_id': None}, synchronize_session=False
        )

        # Supprimer les données opérationnelles
        StockTransfer.query.filter_by(product_id=product_id).delete()
        SupplierOrderItem.query.filter_by(product_id=product_id).delete()
        try:
            LossFicheItem.query.filter_by(product_id=product_id).delete()
        except Exception:
            pass

        # Variantes
        for v in ProductVariant.query.filter_by(product_id=product_id).all():
            SaleItem.query.filter_by(variant_id=v.id).update(
                {'variant_id': None}, synchronize_session=False
            )
            db.session.delete(v)

        db.session.flush()
        db.session.delete(p)
        db.session.commit()
        flash(f'Produit "{name}" supprimé avec succès.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Erreur lors de la suppression : {str(e)[:120]}', 'danger')

    return redirect(url_for('manager.products'))
# ── DELETE PRODUCT ──────────────────────────────────────────────────────────


# ── STOCK TRANSFER (Entrepôt → Magasin) ───────────────────────────────────
@manager_bp.route('/transfers')
@_manager_access
def transfers():
    import json
    transfers = StockTransfer.query.filter_by(tenant_id=_tid())\
                .order_by(StockTransfer.transferred_at.desc()).limit(50).all()
    products  = Product.query.filter_by(tenant_id=_tid()).order_by(Product.designation).all()
    # Sérialiser les variantes pour le JS du template
    variants_map = {}
    for p in products:
        if p.has_variants:
            variants_map[p.id] = [
                {
                    'id': v.id,
                    'nom': v.nom,
                    'attributs_str': v.attributs_display,
                    'sku': v.sku or '',
                    'stock_entrepot': v.stock_entrepot,
                    'stock_magasin': v.stock_magasin,
                }
                for v in p.variants.filter_by(is_active=True).all()
            ]
    return render_template('manager/transfers.html',
                           transfers=transfers, products=products,
                           variants_map_json=json.dumps(variants_map))


@manager_bp.route('/transfers/create', methods=['POST'])
@_manager_access
def create_transfer():
    from app.models import ProductVariant
    product_id = int(request.form.get('product_id'))
    variant_id = request.form.get('variant_id', '') or None
    quantity   = int(request.form.get('quantity', 0) or 0)
    note       = request.form.get('note', '')
    product    = Product.query.filter_by(id=product_id, tenant_id=_tid()).first_or_404()

    if quantity <= 0:
        flash('La quantité doit être positive.', 'danger')
        return redirect(url_for('manager.transfers'))

    if product.has_variants:
        if not variant_id:
            flash('Veuillez sélectionner une variante.', 'danger')
            return redirect(url_for('manager.transfers'))
        variant = ProductVariant.query.filter_by(id=int(variant_id), product_id=product_id, tenant_id=_tid()).first_or_404()
        if variant.stock_entrepot < quantity:
            flash(f'Stock entrepôt insuffisant pour « {variant.nom} » ({variant.stock_entrepot} dispo).', 'danger')
            return redirect(url_for('manager.transfers'))
        variant.stock_entrepot -= quantity
        variant.stock_magasin  += quantity
        db.session.add(StockTransfer(
            tenant_id=_tid(), product_id=product_id, variant_id=int(variant_id),
            manager_id=current_user.id, quantity=quantity, note=note))
        db.session.commit()
        flash(f'{quantity} × « {product.designation} / {variant.nom} » transféré(s) en rayon.', 'success')
    else:
        if product.stock_entrepot < quantity:
            flash(f'Stock entrepôt insuffisant ({product.stock_entrepot} dispo).', 'danger')
            return redirect(url_for('manager.transfers'))
        product.stock_entrepot -= quantity
        product.stock_magasin  += quantity
        db.session.add(StockTransfer(
            tenant_id=_tid(), product_id=product_id, variant_id=None,
            manager_id=current_user.id, quantity=quantity, note=note))
        db.session.commit()
        flash(f'{quantity} × « {product.designation} » transféré(s) en rayon.', 'success')

    return redirect(url_for('manager.transfers'))


# ── USERS ──────────────────────────────────────────────────────────────────
from app.models import User

@manager_bp.route('/users')
@_manager_access
def users():
    users = (
        User.query
        .filter_by(tenant_id=_tid(), is_manager=False)
        .order_by(User.prenom)
        .all()
    )
    return render_template('manager/users.html', users=users)
    



@manager_bp.route('/users/create', methods=['GET', 'POST'])
@_manager_access
def create_user():
    if request.method == 'POST':
        nom      = request.form.get('nom', '').strip()
        prenom   = request.form.get('prenom', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if User.query.filter_by(email=email).first():
            flash('Email déjà utilisé.', 'danger')
            return render_template('manager/user_form.html')
        u = User(tenant_id=_tid(), nom=nom, prenom=prenom,
                 email=email, role=UserRole.CASHIER)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash(f'Caissier « {u.full_name} » créé.', 'success')
        return redirect(url_for('manager.users'))
    return render_template('manager/user_form.html')


@manager_bp.route('/users/<int:user_id>/toggle', methods=['POST'])
@_manager_access
def toggle_user(user_id):
    u = User.query.filter_by(id=user_id, tenant_id=_tid()).first_or_404()
    u.is_active = not u.is_active
    db.session.commit()
    flash(f'{u.full_name} {"activé" if u.is_active else "désactivé"}.', 'info')
    return redirect(url_for('manager.users'))


# ── SALES ──────────────────────────────────────────────────────────────────
@manager_bp.route('/sales')
@_manager_access
def sales():
    from app.models import PaymentMethod
    from datetime import date as date_today
    page     = request.args.get('page', 1, type=int)
    # Par défaut : aujourd'hui
    date_str = request.args.get('date', date_today.today().strftime('%Y-%m-%d'))
    cashier_id = request.args.get('cashier_id', 0, type=int)

    q = Sale.query.filter_by(tenant_id=_tid())
    filter_date = None
    if date_str:
        try:
            filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            q = q.filter(func.date(Sale.created_at) == filter_date)
        except ValueError:
            pass
    if cashier_id:
        q = q.filter_by(cashier_id=cashier_id)

    all_sales  = q.order_by(Sale.created_at.asc()).all()
    total_ttc  = sum(float(s.total_amount) for s in all_sales)
    total_ht   = sum(float(s.total_ht)     for s in all_sales)
    total_tva  = sum(float(s.total_tva)    for s in all_sales)
    by_method  = {}
    for s in all_sales:
        m = s.payment_method
        if m not in by_method:
            by_method[m] = {'count': 0, 'total': 0.0}
        by_method[m]['count'] += 1
        by_method[m]['total'] += float(s.total_amount)

    # Top produits vendus aujourd'hui
    from collections import defaultdict
    product_counts = defaultdict(lambda: {'designation': '', 'qty': 0, 'total': 0.0})
    for s in all_sales:
        for item in s.items:
            key = item.designation or 'Article supprimé'
            product_counts[key]['designation'] = key
            product_counts[key]['qty']   += item.quantity
            product_counts[key]['total'] += float(item.subtotal or 0)
    top_products = sorted(product_counts.values(), key=lambda x: x['qty'], reverse=True)[:8]

    sales_page = q.order_by(Sale.created_at.desc()).paginate(page=page, per_page=50)
    cashiers   = User.query.filter_by(tenant_id=_tid(), role=UserRole.CASHIER).all()

    # Grouper par caissier + séparer detail/engros
    from collections import OrderedDict
    sales_by_cashier = OrderedDict()
    total_detail = total_engros = 0.0
    nb_detail    = nb_engros    = 0
    for s in all_sales:
        cid = s.cashier_id or 0
        if cid not in sales_by_cashier:
            sales_by_cashier[cid] = {
                'cashier'    : s.cashier,
                'sales'      : [],
                'total'      : 0.0,
                'nb_items'   : 0,
                'total_detail': 0.0,
                'total_engros': 0.0,
                'nb_detail'   : 0,
                'nb_engros'   : 0,
            }
        sales_by_cashier[cid]['sales'].append(s)
        sales_by_cashier[cid]['total']    += float(s.total_amount or 0)
        sales_by_cashier[cid]['nb_items'] += sum(i.quantity for i in s.items)
        if getattr(s, 'sale_type', 'detail') == 'engros':
            sales_by_cashier[cid]['total_engros'] += float(s.total_amount or 0)
            sales_by_cashier[cid]['nb_engros']    += 1
            total_engros += float(s.total_amount or 0)
            nb_engros    += 1
        else:
            sales_by_cashier[cid]['total_detail'] += float(s.total_amount or 0)
            sales_by_cashier[cid]['nb_detail']    += 1
            total_detail += float(s.total_amount or 0)
            nb_detail    += 1

    # Commandes en ligne du jour
    from app.models import OnlineOrder, OnlineOrderStatus
    online_q = OnlineOrder.query.filter_by(tenant_id=_tid())
    if date_str:
        online_q = online_q.filter(func.date(OnlineOrder.created_at) == date_str)
    online_orders = online_q.order_by(OnlineOrder.created_at.desc()).all()
    online_total  = sum(float(o.total_amount) for o in online_orders if o.status != OnlineOrderStatus.CANCELLED)
    online_pending = sum(1 for o in online_orders if o.status == OnlineOrderStatus.PENDING)

    return render_template('manager/sales.html',
                           sales=sales_page,
                           sales_by_cashier=sales_by_cashier,
                           top_products=top_products,
                           date_filter=date_str,
                           filter_date=filter_date,
                           total_ttc=total_ttc,
                           total_ht=total_ht,
                           total_tva=total_tva,
                           total_detail=total_detail,
                           total_engros=total_engros,
                           nb_detail=nb_detail,
                           nb_engros=nb_engros,
                           by_method=by_method,
                           nb_transactions=len(all_sales),
                           ticket_moyen=total_ttc/len(all_sales) if all_sales else 0,
                           cashiers=cashiers,
                           current_cashier=cashier_id,
                           online_orders=online_orders,
                           online_total=online_total,
                           online_pending=online_pending,
                           OnlineOrderStatus=OnlineOrderStatus)


@manager_bp.route('/sales/pdf')
@_manager_access
def sales_pdf():
    from app.models import PaymentMethod
    from collections import OrderedDict, defaultdict
    date_str   = request.args.get('date', '')
    cashier_id = request.args.get('cashier_id', 0, type=int)

    q = Sale.query.filter_by(tenant_id=_tid())
    filter_date = None
    if date_str:
        try:
            filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            q = q.filter(func.date(Sale.created_at) == filter_date)
        except ValueError:
            pass
    if cashier_id:
        q = q.filter_by(cashier_id=cashier_id)

    all_sales = q.order_by(Sale.created_at.asc()).all()
    total_ttc = sum(float(s.total_amount) for s in all_sales)
    total_ht  = sum(float(s.total_ht)     for s in all_sales)
    total_tva = sum(float(s.total_tva)    for s in all_sales)
    by_method = {}
    for s in all_sales:
        m = s.payment_method
        if m not in by_method:
            by_method[m] = {'count': 0, 'total': 0.0}
        by_method[m]['count'] += 1
        by_method[m]['total'] += float(s.total_amount)

    # Grouper par caissier
    sales_by_cashier = OrderedDict()
    for s in all_sales:
        cid = s.cashier_id or 0
        if cid not in sales_by_cashier:
            sales_by_cashier[cid] = {'cashier': s.cashier, 'sales': [], 'total': 0.0, 'nb_items': 0}
        sales_by_cashier[cid]['sales'].append(s)
        sales_by_cashier[cid]['total']    += float(s.total_amount or 0)
        sales_by_cashier[cid]['nb_items'] += sum(i.quantity for i in s.items)

    # Top produits
    product_counts = defaultdict(lambda: {'designation': '', 'qty': 0, 'total': 0.0})
    for s in all_sales:
        for item in s.items:
            key = item.designation or 'Article supprimé'
            product_counts[key]['designation'] = key
            product_counts[key]['qty']   += item.quantity
            product_counts[key]['total'] += float(item.subtotal or 0)
    top_products = sorted(product_counts.values(), key=lambda x: x['qty'], reverse=True)[:8]

    # Détail vs Gros
    total_detail = sum(float(s.total_amount) for s in all_sales if getattr(s,'sale_type','detail') != 'engros')
    total_engros = sum(float(s.total_amount) for s in all_sales if getattr(s,'sale_type','detail') == 'engros')
    nb_detail    = sum(1 for s in all_sales if getattr(s,'sale_type','detail') != 'engros')
    nb_engros    = sum(1 for s in all_sales if getattr(s,'sale_type','detail') == 'engros')

    html_str = render_template('manager/sales_pdf.html',
                               sales=all_sales,
                               sales_by_cashier=sales_by_cashier,
                               top_products=top_products,
                               filter_date=filter_date,
                               date_filter=date_str,
                               total_ttc=total_ttc,
                               total_ht=total_ht,
                               total_tva=total_tva,
                               by_method=by_method,
                               nb_transactions=len(all_sales),
                               ticket_moyen=total_ttc/len(all_sales) if all_sales else 0,
                               total_detail=total_detail,
                               total_engros=total_engros,
                               nb_detail=nb_detail,
                               nb_engros=nb_engros,
                               tenant=current_user.tenant,
                               generated_by=current_user.full_name,
                               today=date.today())
    try:
        from weasyprint import HTML
        from flask import Response, current_app
        pdf = HTML(string=html_str, base_url=current_app.root_path).write_pdf()
        fname = f"ventes_{date_str or 'global'}.pdf"
        return Response(pdf, mimetype='application/pdf',
                        headers={'Content-Disposition': f'attachment; filename="{fname}"'})
    except Exception as e:
        flash(f'Erreur PDF : {e}', 'danger')
        return redirect(url_for('manager.sales', date=date_str))


# ── LOSSES ─────────────────────────────────────────────────────────────────
@manager_bp.route('/losses')
@_manager_access
def losses():
    fiches = LossFiche.query.filter_by(tenant_id=_tid())\
                            .order_by(LossFiche.created_at.desc()).all()
    return render_template('manager/losses.html', fiches=fiches)


# ── SETTINGS ───────────────────────────────────────────────────────────────
@manager_bp.route('/settings', methods=['GET', 'POST'])
@_manager_access
def settings():
    tenant = current_user.tenant
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update_profile':
            tenant.nom_boutique         = request.form.get('nom_boutique', tenant.nom_boutique or '').strip()
            tenant.activite             = request.form.get('activite', tenant.activite).strip()
            tenant.nom                  = request.form.get('nom', tenant.nom).strip()
            tenant.prenom               = request.form.get('prenom', tenant.prenom).strip()
            tenant.ville                = request.form.get('ville', tenant.ville).strip()
            tenant.adresse              = request.form.get('adresse', tenant.adresse).strip()
            tenant.telephone_entreprise = request.form.get('telephone_entreprise', tenant.telephone_entreprise or '').strip()
            current_user.nom    = tenant.nom
            current_user.prenom = tenant.prenom
            # Upload logo vers Supabase Storage
            logo_file = request.files.get('logo')
            if logo_file and logo_file.filename:
                url = upload_image(logo_file, folder='logos')
                if url:
                    tenant.logo_filename = url
                    flash('Logo mis a jour avec succes.', 'success')
            db.session.commit()
            flash('Profil mis a jour.', 'success')
        elif action == 'change_password':
            old_pw = request.form.get('old_password', '')
            new_pw = request.form.get('new_password', '')
            if not current_user.check_password(old_pw):
                flash('Ancien mot de passe incorrect.', 'danger')
            elif len(new_pw) < 8:
                flash('Nouveau mot de passe trop court.', 'danger')
            else:
                current_user.set_password(new_pw)
                db.session.commit()
                flash('Mot de passe changé.', 'success')

    return render_template('manager/settings.html', tenant=tenant)



@manager_bp.route('/api/cashiers/presence')
@_manager_access
def cashiers_presence():
    """API JSON — statut en ligne (SQL direct, pas de cache ORM)."""
    from datetime import datetime, timedelta
    from sqlalchemy import text
    now = datetime.utcnow()
    import json as _json
    rows = db.session.execute(text(
        """SELECT id, nom, prenom, last_seen, pos_state
           FROM users
           WHERE tenant_id=:tid AND is_active=true
             AND role='cashier'"""
    ), {'tid': _tid()}).fetchall()
    result = []
    for r in rows:
        last_seen = r[3]
        pos_state_raw = r[4]
        if last_seen:
            if hasattr(last_seen, 'tzinfo') and last_seen.tzinfo:
                from datetime import timezone
                last_seen = last_seen.astimezone(timezone.utc).replace(tzinfo=None)
            diff = (now - last_seen).total_seconds()
            is_online = diff < 180
        else:
            diff = None
            is_online = False
        # Parser l'état du panier
        pos_state = None
        if pos_state_raw and is_online:
            try:
                pos_state = _json.loads(pos_state_raw)
            except Exception:
                pos_state = None
        result.append({
            'id'       : r[0],
            'name'     : f"{r[2]} {r[1]}",
            'is_online': is_online,
            'last_seen': last_seen.isoformat() if last_seen else None,
            'diff_sec' : round(diff) if diff else None,
            'pos_state': pos_state,
        })
    return {'cashiers': result}


@manager_bp.route('/settings/pin', methods=['POST'])
@_manager_access
def save_pin():
    from flask_login import current_user
    pin = request.form.get('pin', '').strip()
    if not pin.isdigit() or len(pin) != 4:
        flash('Code PIN invalide. 4 chiffres requis.', 'danger')
        return redirect(url_for('manager.settings'))
    current_user.set_pin(pin)
    db.session.commit()
    flash('Code PIN enregistré.', 'success')
    return redirect(url_for('manager.settings'))


@manager_bp.route('/settings/pin/employee/<int:uid>', methods=['POST'])
@_manager_access
def save_employee_pin(uid):
    from app.models import User
    u = User.query.filter_by(id=uid, tenant_id=_tid()).first_or_404()
    pin = request.form.get('pin', '').strip()
    if not pin.isdigit() or len(pin) != 4:
        flash('Code PIN invalide.', 'danger')
        return redirect(url_for('manager.settings'))
    u.set_pin(pin)
    db.session.commit()
    flash(f'Code PIN de {u.prenom} enregistré.', 'success')
    return redirect(url_for('manager.settings'))


@manager_bp.route('/api/verify-pin', methods=['POST'])
@login_required
def verify_pin():
    from flask import jsonify
    data = request.get_json() or {}
    pin  = str(data.get('pin', ''))
    if current_user.check_pin(pin):
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 401