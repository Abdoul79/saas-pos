import os, uuid, json
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from app import db
from app.utils.storage import upload_image, delete_image
from app.models import Product, ProductVariant, UserRole, StockTransfer  
from app.utils.decorators import role_required, tenant_active_required
from app.utils.barcode_gen import generate_ean13_number, generate_barcode_b64

variants_bp = Blueprint('variants', __name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '..', '..', 'static', 'uploads', 'products')
ALLOWED_EXT   = {'png', 'jpg', 'jpeg', 'webp'}


def _mgr(f):
    from functools import wraps
    @wraps(f)
    @login_required
    @role_required(UserRole.MANAGER)
    @tenant_active_required
    def w(*a, **kw): return f(*a, **kw)
    return w


def _tid(): return current_user.tenant_id


def _save_img(f):
    """Upload image variante vers Supabase Storage (prod) ou local (dev)."""
    if not f or not f.filename:
        return None
    return upload_image(f, folder='products')


def _calc_ttc(ht, tva): return round(float(ht) * (1 + float(tva) / 100), 2)


# ── List variants of a product ─────────────────────────────────────────────
@variants_bp.route('/product/<int:pid>')
@_mgr
def index(pid):
    product  = Product.query.filter_by(id=pid, tenant_id=_tid()).first_or_404()
    variants = product.variants.all()
    return render_template('manager/variants/index.html', product=product, variants=variants)


# ── Create variant ─────────────────────────────────────────────────────────
@variants_bp.route('/product/<int:pid>/create', methods=['GET', 'POST'])
@_mgr
def create(pid):
    product = Product.query.filter_by(id=pid, tenant_id=_tid()).first_or_404()

    if request.method == 'POST':
        # Collect attribute keys/values
        attr_keys   = request.form.getlist('attr_key[]')
        attr_values = request.form.getlist('attr_val[]')
        attributs   = {k.strip(): v.strip() for k, v in zip(attr_keys, attr_values) if k.strip() and v.strip()}

        nom = request.form.get('nom', '').strip()
        if not nom:
            nom = ' / '.join(attributs.values()) or 'Variante'

        prix_achat    = float(request.form.get('prix_achat', product.prix_achat) or 0)
        prix_vente_ht = float(request.form.get('prix_vente_ht', product.prix_vente_ht) or 0)
        taux_tva      = float(request.form.get('taux_tva', product.taux_tva) or 0)
        prix_ttc      = _calc_ttc(prix_vente_ht, taux_tva)

        barcode = request.form.get('barcode', '').strip()
        gen_bc  = request.form.get('generate_barcode') == '1'
        if gen_bc or not barcode:
            barcode = generate_ean13_number(_tid())

        v = ProductVariant(
            product_id     = product.id,
            tenant_id      = _tid(),
            nom            = nom,
            sku            = request.form.get('sku', '').strip() or None,
            barcode        = barcode,
            barcode_generated = gen_bc or not request.form.get('barcode', '').strip(),
            prix_achat     = prix_achat,
            prix_vente_ht  = prix_vente_ht,
            taux_tva       = taux_tva,
            prix_vente_ttc = prix_ttc,
            stock_entrepot = int(request.form.get('stock_initial', 0) or 0),
            ordre          = int(request.form.get('ordre', 0) or 0),
        )
        v.attributs    = attributs
        v.image_filename = _save_img(request.files.get('image'))

        db.session.add(v)

        # Activer has_variants sur le parent
        product.has_variants = True
        db.session.commit()

        flash(f'Variante « {v.nom} » créée.', 'success')
        return redirect(url_for('variants.index', pid=pid))

    return render_template('manager/variants/form.html', product=product)


# ── Edit variant ───────────────────────────────────────────────────────────
@variants_bp.route('/<int:vid>/edit', methods=['GET', 'POST'])
@_mgr
def edit(vid):
    v = ProductVariant.query.filter_by(id=vid, tenant_id=_tid()).first_or_404()
    product = v.product

    if request.method == 'POST':
        attr_keys   = request.form.getlist('attr_key[]')
        attr_values = request.form.getlist('attr_val[]')
        attributs   = {k.strip(): v_val.strip() for k, v_val in zip(attr_keys, attr_values) if k.strip()}
        v.attributs = attributs

        v.nom            = request.form.get('nom', v.nom).strip() or ' / '.join(attributs.values())
        v.sku            = request.form.get('sku', '').strip() or None
        v.prix_achat     = float(request.form.get('prix_achat', v.prix_achat) or 0)
        v.prix_vente_ht  = float(request.form.get('prix_vente_ht', v.prix_vente_ht) or 0)
        v.taux_tva       = float(request.form.get('taux_tva', v.taux_tva) or 0)
        v.prix_vente_ttc = _calc_ttc(v.prix_vente_ht, v.taux_tva)
        # getlist car hidden+checkbox envoient 2 valeurs : '0' puis '1' si cochée
        v.is_active      = '1' in request.form.getlist('is_active')
        v.ordre          = int(request.form.get('ordre', v.ordre) or 0)

        img = request.files.get('image')
        if img and img.filename:
            v.image_filename = _save_img(img)
        if request.form.get('delete_image') == '1':
            v.image_filename = None

        db.session.commit()
        flash(f'Variante « {v.nom} » mise à jour.', 'success')
        return redirect(url_for('variants.index', pid=product.id))

    return render_template('manager/variants/form.html', product=product, variant=v)


# ── Delete variant ─────────────────────────────────────────────────────────
@variants_bp.route('/<int:vid>/delete', methods=['POST'])
@_mgr
def delete(vid):
    v = ProductVariant.query.filter_by(id=vid, tenant_id=_tid()).first_or_404()
    pid = v.product_id
    product = v.product

    # Empêche le crash : vérifie s'il existe des transferts de stock liés à cette variante
    has_transfers = StockTransfer.query.filter_by(variant_id=vid).first() is not None
    if has_transfers:
        flash('Impossible de supprimer cette variante : elle a des transferts de stock associés.', 'danger')
        return redirect(url_for('variants.index', pid=pid))

    # Compte les variantes restantes AVANT de supprimer, pour éviter le bug d'autoflush
    remaining = product.variants.filter(ProductVariant.id != vid).count()

    db.session.delete(v)
    if remaining == 0:
        product.has_variants = False
    db.session.commit()
    flash('Variante supprimée.', 'info')
    return redirect(url_for('variants.index', pid=pid))


# ── Barcode : une variante ────────────────────────────────────────────────
@variants_bp.route('/<int:vid>/barcode')
@_mgr
def barcode(vid):
    v = ProductVariant.query.filter_by(id=vid, tenant_id=_tid()).first_or_404()
    bc_b64 = generate_barcode_b64(v.barcode) if v.barcode else ''
    return render_template('manager/variants/barcode_single.html', variant=v, bc_b64=bc_b64)


# ── Barcode : planche de toutes les variantes d'un produit ────────────────
@variants_bp.route('/product/<int:pid>/barcodes')
@_mgr
def barcodes_all(pid):
    product  = Product.query.filter_by(id=pid, tenant_id=_tid()).first_or_404()
    variants = product.variants.filter_by(is_active=True).all()
    # Générer les images base64 pour chaque variante
    items = []
    for v in variants:
        if v.barcode:
            items.append({
                'variant' : v,
                'bc_b64'  : generate_barcode_b64(v.barcode),
            })
    copies = request.args.get('copies', 1, type=int)
    copies = max(1, min(copies, 20))
    return render_template('manager/variants/barcode_sheet.html',
                           product=product, items=items, copies=copies)


# ── API: variantes d'un produit (pour le POS) ─────────────────────────────
@variants_bp.route('/api/product/<int:pid>')
@login_required
def api_variants(pid):
    product  = Product.query.filter_by(id=pid, tenant_id=current_user.tenant_id).first_or_404()
    variants = product.variants.filter_by(is_active=True).all()
    return jsonify([_variant_json(v) for v in variants])


def _variant_json(v):
    # Convention identique à _variant_pos_json dans pos.py :
    #   'id'         = product_id parent  (pour regrouper dans le panier)
    #   'variant_id' = v.id réel          (envoyé au backend lors de la vente)
    #   'is_variant' = True               (pour cartKey() et validateSale())
    return {
        'id'           : v.product_id,
        'variant_id'   : v.id,
        'is_variant'   : True,
        'nom'          : v.nom,
        'variant_label': v.attributs_display,
        'sku'          : v.sku,
        'barcode'      : v.barcode,
        'attributs'    : v.attributs,
        'attributs_str': v.attributs_display,
        'prix_vente'   : v.prix_vente_ttc_f,
        'prix_ht'      : v.prix_vente_ht_f,
        'taux_tva'     : v.taux_tva_f,
        'stock_magasin': v.stock_magasin,
        'image_url'    : v.image_url or '',
    }
