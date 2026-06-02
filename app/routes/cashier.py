from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from datetime import datetime, date
from sqlalchemy import func
from app import db
from app.models import (Product, Sale, SaleItem, LossFiche, LossFicheItem, UserRole)
from app.utils.decorators import role_required, tenant_active_required

cashier_bp = Blueprint('cashier', __name__)


def _cashier_access(f):
    from functools import wraps
    @wraps(f)
    @login_required
    @role_required(UserRole.CASHIER, UserRole.MANAGER)
    @tenant_active_required
    def wrapped(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapped


def _tid():
    return current_user.tenant_id


# ── STOCK MAGASIN (read-only for cashier) ──────────────────────────────────
@cashier_bp.route('/stock')
@_cashier_access
def stock():
    q        = request.args.get('q', '')
    query    = Product.query.filter_by(tenant_id=_tid())
    if q:
        query = query.filter(Product.designation.ilike(f'%{q}%'))
    products = query.order_by(Product.designation).all()
    return render_template('cashier/stock.html', products=products, q=q)


# ── LOSS FICHE (Casse) ─────────────────────────────────────────────────────
@cashier_bp.route('/casse', methods=['GET', 'POST'])
@_cashier_access
def casse():
    if request.method == 'POST':
        motif      = request.form.get('motif', '').strip()
        note       = request.form.get('note', '').strip()
        product_ids = request.form.getlist('product_id[]')
        quantities  = request.form.getlist('quantity[]')

        if not motif or not product_ids:
            flash('Motif et au moins un produit requis.', 'danger')
            return redirect(url_for('cashier.casse'))

        fiche = LossFiche(
            tenant_id=_tid(),
            recorded_by_id=current_user.id,
            motif=motif,
            note=note
        )
        db.session.add(fiche)
        db.session.flush()

        for pid, qty_str in zip(product_ids, quantities):
            qty = int(qty_str)
            if qty <= 0:
                continue
            product = Product.query.filter_by(id=int(pid), tenant_id=_tid()).first()
            if not product:
                continue
            if product.total_stock_magasin < qty:
                flash(f'Stock insuffisant pour "{product.designation}".', 'warning')
                db.session.rollback()
                return redirect(url_for('cashier.casse'))

            product.stock_magasin -= qty  # produit simple; pour variantes gérer séparément
            item = LossFicheItem(
                fiche_id=fiche.id,
                product_id=product.id,
                designation=product.designation,
                quantity=qty,
                prix_vente=product.prix_vente_ttc
            )
            db.session.add(item)

        db.session.commit()
        flash('Fiche de casse enregistrée avec succès.', 'success')
        return redirect(url_for('cashier.my_history'))

    all_prods = Product.query.filter_by(tenant_id=_tid()).order_by(Product.designation).all()
    products  = [p for p in all_prods if p.total_stock_magasin > 0]
    return render_template('cashier/casse.html', products=products)


# ── HISTORY PDF ─────────────────────────────────────────────────────────────
@cashier_bp.route('/history/pdf')
@_cashier_access
def history_pdf():
    from flask import Response, current_app
    from collections import Counter
    from datetime import timedelta

    today    = date.today()
    date_str = request.args.get('date', today.strftime('%Y-%m-%d'))
    try:
        filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        filter_date = today

    q = Sale.query.filter_by(tenant_id=_tid())
    if current_user.role == UserRole.CASHIER:
        q = q.filter_by(cashier_id=current_user.id)
    q = q.filter(func.date(Sale.created_at) == filter_date)
    sales = q.order_by(Sale.created_at.asc()).all()

    total     = sum(float(s.total_amount) for s in sales)
    total_ht  = sum(float(s.total_ht)     for s in sales)
    total_tva = sum(float(s.total_tva)    for s in sales)
    by_method = {}
    for s in sales:
        m = s.payment_method
        if m not in by_method:
            by_method[m] = {'count': 0, 'total': 0.0}
        by_method[m]['count'] += 1
        by_method[m]['total'] += float(s.total_amount)

    ticket_moyen   = total / len(sales) if sales else 0
    total_articles = sum(sum(it.quantity for it in s.items) for s in sales)
    item_counter   = Counter()
    for s in sales:
        for it in s.items:
            item_counter[it.designation] += it.quantity
    top_produits = item_counter.most_common(5)

    # Détail vs Gros
    total_detail = sum(float(s.total_amount) for s in sales if getattr(s, 'sale_type', 'detail') != 'engros')
    total_engros = sum(float(s.total_amount) for s in sales if getattr(s, 'sale_type', 'detail') == 'engros')
    nb_detail    = sum(1 for s in sales if getattr(s, 'sale_type', 'detail') != 'engros')
    nb_engros    = sum(1 for s in sales if getattr(s, 'sale_type', 'detail') == 'engros')

    html_str = render_template('cashier/history_pdf.html',
                               sales=sales,
                               total=total, total_ht=total_ht, total_tva=total_tva,
                               by_method=by_method, ticket_moyen=ticket_moyen,
                               total_articles=total_articles, top_produits=top_produits,
                               total_detail=total_detail, total_engros=total_engros,
                               nb_detail=nb_detail, nb_engros=nb_engros,
                               filter_date=filter_date, date_filter=date_str,
                               tenant=current_user.tenant,
                               cashier=current_user,
                               today=today)
    try:
        from weasyprint import HTML
        pdf = HTML(string=html_str, base_url=current_app.root_path).write_pdf()
        fname = f'rapport_caisse_{date_str}.pdf'
        return Response(pdf, mimetype='application/pdf',
                        headers={'Content-Disposition': f'attachment; filename="{fname}"'})
    except Exception as e:
        flash(f'Erreur PDF : {e}', 'danger')
        return redirect(url_for('cashier.my_history', date=date_str))


# ── MY DAILY HISTORY ───────────────────────────────────────────────────────
@cashier_bp.route('/history')
@_cashier_access
def my_history():
    from app.models import PaymentMethod
    today    = date.today()
    date_str = request.args.get('date', today.strftime('%Y-%m-%d'))
    try:
        filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        filter_date = today

    q = Sale.query.filter_by(tenant_id=_tid())
    if current_user.role == UserRole.CASHIER:
        q = q.filter_by(cashier_id=current_user.id)
    q = q.filter(func.date(Sale.created_at) == filter_date)
    sales = q.order_by(Sale.created_at.asc()).all()

    # Stats globales
    total     = sum(float(s.total_amount) for s in sales)
    total_ht  = sum(float(s.total_ht)     for s in sales)
    total_tva = sum(float(s.total_tva)    for s in sales)

    # Stats par mode de paiement
    by_method = {}
    for s in sales:
        m = s.payment_method
        if m not in by_method:
            by_method[m] = {'count': 0, 'total': 0.0}
        by_method[m]['count'] += 1
        by_method[m]['total'] += float(s.total_amount)

    ticket_moyen    = total / len(sales) if sales else 0
    total_articles = sum(sum(it.quantity for it in s.items) for s in sales)

    # Top 5 produits vendus
    from collections import Counter
    item_counter = Counter()
    for s in sales:
        for it in s.items:
            item_counter[it.designation] += it.quantity
    top_produits = item_counter.most_common(5)

    from datetime import timedelta
    prev_date = (filter_date - timedelta(days=1)).strftime('%Y-%m-%d')
    next_date = (filter_date + timedelta(days=1)).strftime('%Y-%m-%d')

    return render_template('cashier/history.html',
                           sales=sales,
                           total=total,
                           total_ht=total_ht,
                           total_tva=total_tva,
                           by_method=by_method,
                           ticket_moyen=ticket_moyen,
                           top_produits=top_produits,
                           date_filter=date_str,
                           filter_date=filter_date,
                           today=today.strftime('%Y-%m-%d'),
                           prev_date=prev_date,
                           next_date=next_date,
                           total_articles=total_articles,
                           PaymentMethod=PaymentMethod)
