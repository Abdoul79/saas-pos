import json
from datetime import datetime, date
from flask_login import UserMixin
from app import db, bcrypt, login_manager


# ─────────────────────────────────────────
# ENUMS & CONSTANTS
# ─────────────────────────────────────────
class TenantStatus:
    PENDING   = 'pending'
    ACTIVE    = 'active'
    SUSPENDED = 'suspended'
    REJECTED  = 'rejected'

class UserRole:
    SUPER_ADMIN = 'super_admin'
    ACTIVATEUR  = 'activateur'   # Peut uniquement activer/rejeter les comptes
    MANAGER     = 'manager'
    CASHIER     = 'cashier'

class PaymentMethod:
    CASH         = 'especes'
    CARD         = 'carte'
    MOBILE_MONEY = 'mobile_money'

ACTIVITY_CHOICES = [
    'Supermarché', 'Supérette', 'Magasin de Cosmétiques',
    'Pharmacie', 'Prêt-à-porter', 'Quincaillerie', 'Autre'
]

# Couleurs pour les catégories
CATEGORY_COLORS = [
    '#f5a623', '#3b82f6', '#22c55e', '#ef4444', '#a78bfa',
    '#f59e0b', '#06b6d4', '#ec4899', '#84cc16', '#f97316'
]


# ─────────────────────────────────────────
# ORDER STATUS
# ─────────────────────────────────────────
class OrderStatus:
    DRAFT    = 'brouillon'      # Brouillon
    SENT     = 'envoyee'        # Envoyée au fournisseur
    PARTIAL  = 'recue_partielle'# Reçue partiellement
    RECEIVED = 'recue'          # Reçue totalement
    CANCELLED= 'annulee'        # Annulée

    LABELS = {
        'brouillon'       : ('Brouillon',     'badge-blue'),
        'envoyee'         : ('Envoyée',        'badge-purple'),
        'recue_partielle' : ('Partielle',      'badge-orange'),
        'recue'           : ('Reçue',          'badge-green'),
        'annulee'         : ('Annulée',        'badge-red'),
    }

    @classmethod
    def label(cls, status):
        return cls.LABELS.get(status, (status, 'badge-blue'))


# ─────────────────────────────────────────
# TENANT
# ─────────────────────────────────────────
class Tenant(db.Model):
    __tablename__ = 'tenants'

    id             = db.Column(db.Integer, primary_key=True)
    nom            = db.Column(db.String(100), nullable=False)
    prenom         = db.Column(db.String(100), nullable=False)
    email          = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password_hash  = db.Column(db.String(256), nullable=False)
    activite       = db.Column(db.String(80),  nullable=False)
    ville          = db.Column(db.String(100), nullable=False)
    adresse        = db.Column(db.Text,        nullable=False)
    status         = db.Column(db.String(20),  default=TenantStatus.PENDING, nullable=False, index=True)
    licence_expiry = db.Column(db.Date, nullable=True)

    # Nouveaux champs inscription
    nom_boutique           = db.Column(db.String(150), nullable=True)
    telephone_personnel    = db.Column(db.String(30),  nullable=True)
    telephone_entreprise   = db.Column(db.String(30),  nullable=True)
    logo_filename          = db.Column(db.String(255), nullable=True)
    vente_engros_active    = db.Column(db.Boolean, default=False, nullable=False)
    montant_mensuel        = db.Column(db.Numeric(10, 2), nullable=True, default=0)

    # Boutique en ligne
    boutique_en_ligne_active = db.Column(db.Boolean, default=False, nullable=False)
    shop_mode = db.Column(db.String(20), nullable=True, default='boutique')  # 'boutique' ou 'restaurant'
    shop_slug                = db.Column(db.String(100), nullable=True, unique=True, index=True)
    shop_description         = db.Column(db.Text, nullable=True)
    shop_banner_filename     = db.Column(db.String(255), nullable=True)
    frais_livraison          = db.Column(db.Numeric(10, 2), nullable=True, default=0)
    seuil_livraison_gratuite = db.Column(db.Numeric(10, 2), nullable=True, default=0)
    shop_heure_ouverture     = db.Column(db.String(5), nullable=True, default='08:00')
    shop_heure_fermeture     = db.Column(db.String(5), nullable=True, default='22:00')
    shop_jours_fermes        = db.Column(db.String(50), nullable=True, default='')  # ex: "0,6" = dim,sam
    stripe_secret_key      = db.Column(db.String(255), nullable=True)
    stripe_publishable_key = db.Column(db.String(255), nullable=True)


    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    users       = db.relationship('User',       back_populates='tenant', lazy='dynamic', cascade='all, delete-orphan')
    products    = db.relationship('Product',    back_populates='tenant', lazy='dynamic', cascade='all, delete-orphan')
    sales       = db.relationship('Sale',       back_populates='tenant', lazy='dynamic', cascade='all, delete-orphan')
    losses      = db.relationship('LossFiche',  back_populates='tenant', lazy='dynamic', cascade='all, delete-orphan')
    suppliers   = db.relationship('Supplier',   back_populates='tenant', lazy='dynamic', cascade='all, delete-orphan')
    categories  = db.relationship('Category',   back_populates='tenant', lazy='dynamic', cascade='all, delete-orphan')



    def set_password(self, p):   self.password_hash = bcrypt.generate_password_hash(p).decode('utf-8')
    def check_password(self, p): return bcrypt.check_password_hash(self.password_hash, p)

    @property
    def logo_url(self):
        if not self.logo_filename:
            return None
        if self.logo_filename.startswith('http') or self.logo_filename.startswith('/static/'):
            return self.logo_filename
        return f'/static/uploads/logos/{self.logo_filename}'

    @property
    def days_remaining(self):
        if not self.licence_expiry: return 0
        return max(0, (self.licence_expiry - date.today()).days)

    @property
    def licence_badge(self):
        d = self.days_remaining
        return 'green' if d > 15 else ('orange' if d >= 7 else 'red')

    def __repr__(self): return f'<Tenant {self.email}>'

    @property
    def shop_is_open(self):
        """Vérifie si la boutique est ouverte maintenant."""
        from datetime import datetime
        now = datetime.utcnow()
        # Jours fermés (0=lundi, 6=dimanche)
        if self.shop_jours_fermes:
            closed_days = [int(d.strip()) for d in self.shop_jours_fermes.split(',') if d.strip().isdigit()]
            if now.weekday() in closed_days:
                return False
        # Heures
        h_open  = self.shop_heure_ouverture or '00:00'
        h_close = self.shop_heure_fermeture or '23:59'
        current = now.strftime('%H:%M')
        return h_open <= current <= h_close


# ─────────────────────────────────────────
# CATEGORY
# ─────────────────────────────────────────
class Category(db.Model):
    __tablename__ = 'categories'

    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    nom         = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    couleur     = db.Column(db.String(10), nullable=False, default='#f5a623')  # hex color
    icone       = db.Column(db.String(10), nullable=True, default='📦')        # emoji
    ordre       = db.Column(db.Integer, default=0)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    tenant   = db.relationship('Tenant',  back_populates='categories')
    products = db.relationship('Product', back_populates='category', lazy='dynamic')

    @property
    def product_count(self): return self.products.count()

    def __repr__(self): return f'<Category {self.nom}>'


# ─────────────────────────────────────────
# SUPPLIER
# ─────────────────────────────────────────
class Supplier(db.Model):
    __tablename__ = 'suppliers'

    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    nom         = db.Column(db.String(150), nullable=False)
    contact     = db.Column(db.String(100), nullable=True)
    telephone   = db.Column(db.String(30),  nullable=True)
    email       = db.Column(db.String(150), nullable=True)
    adresse     = db.Column(db.Text,        nullable=True)
    ville       = db.Column(db.String(100), nullable=True)
    notes       = db.Column(db.Text,        nullable=True)
    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant   = db.relationship('Tenant',   back_populates='suppliers')
    products = db.relationship('Product',  back_populates='supplier', lazy='dynamic')

    @property
    def product_count(self): return self.products.count()

    def __repr__(self): return f'<Supplier {self.nom}>'


# ─────────────────────────────────────────
# USER
# ─────────────────────────────────────────
class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id            = db.Column(db.Integer, primary_key=True)
    tenant_id     = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=True, index=True)
    nom           = db.Column(db.String(100), nullable=False)
    prenom        = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role          = db.Column(db.String(20),  nullable=False, index=True)
    is_active     = db.Column(db.Boolean, default=True)
    last_login    = db.Column(db.DateTime, nullable=True)
    last_seen     = db.Column(db.DateTime, nullable=True)
    pos_state     = db.Column(db.Text, nullable=True)  # JSON état caisse en temps réel
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    tenant = db.relationship('Tenant', back_populates='users')
    sales  = db.relationship('Sale',   back_populates='cashier', lazy='dynamic')
    losses = db.relationship('LossFiche', back_populates='recorded_by', lazy='dynamic')

    def set_password(self, p):   self.password_hash = bcrypt.generate_password_hash(p).decode('utf-8')
    def check_password(self, p): return bcrypt.check_password_hash(self.password_hash, p)

    @property
    def logo_url(self):
        if not self.logo_filename:
            return None
        if self.logo_filename.startswith('http') or self.logo_filename.startswith('/static/'):
            return self.logo_filename
        return f'/static/uploads/logos/{self.logo_filename}'

    @property
    def is_super_admin(self): return self.role == UserRole.SUPER_ADMIN

    @property
    def is_activateur(self): return self.role == UserRole.ACTIVATEUR
    @property
    def is_manager(self):     return self.role == UserRole.MANAGER
    @property
    def is_cashier(self):     return self.role == UserRole.CASHIER
    @property
    def is_online(self):
        """En ligne si ping il y a moins de 3 minutes."""
        if not self.last_seen:
            return False
        from datetime import datetime, timedelta
        # Forcer naive datetime pour eviter erreurs timezone
        last = self.last_seen.replace(tzinfo=None) if hasattr(self.last_seen, 'tzinfo') else self.last_seen
        return (datetime.utcnow() - last) < timedelta(minutes=3)

    @property
    def full_name(self):      return f'{self.prenom} {self.nom}'

    def __repr__(self): return f'<User {self.email} [{self.role}]>'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ─────────────────────────────────────────
# PRODUCT
# ─────────────────────────────────────────
class Product(db.Model):
    __tablename__ = 'products'

    id                = db.Column(db.Integer, primary_key=True)
    tenant_id         = db.Column(db.Integer, db.ForeignKey('tenants.id'),   nullable=False, index=True)
    supplier_id       = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True,  index=True)
    category_id       = db.Column(db.Integer, db.ForeignKey('categories.id'),nullable=True,  index=True)

    # Identification
    sku               = db.Column(db.String(80),  nullable=True, index=True)   # Code interne
    designation       = db.Column(db.String(200), nullable=False)
    description       = db.Column(db.Text, nullable=True)
    barcode           = db.Column(db.String(50),  nullable=True, index=True)
    barcode_generated = db.Column(db.Boolean, default=False)

    # Prix (produit simple — ignorés si has_variants=True)
    prix_achat        = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    prix_vente_ht     = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    taux_tva          = db.Column(db.Numeric(5,  2), nullable=False, default=0)
    prix_vente_ttc    = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    # Image
    image_filename    = db.Column(db.String(255), nullable=True)

    # Stock (produit simple — ignorés si has_variants=True)
    stock_entrepot    = db.Column(db.Integer, default=0, nullable=False)
    stock_magasin     = db.Column(db.Integer, default=0, nullable=False)

    # Variants
    has_variants      = db.Column(db.Boolean, default=False, nullable=False)
    prix_gros         = db.Column(db.Numeric(10, 2), nullable=True)  # Prix vente gros (optionnel)

    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant     = db.relationship('Tenant',    back_populates='products')
    supplier   = db.relationship('Supplier',  back_populates='products')
    category   = db.relationship('Category',  back_populates='products')
    variants   = db.relationship('ProductVariant', back_populates='product',
                                  lazy='dynamic', cascade='all, delete-orphan',
                                  order_by='ProductVariant.ordre')
    sale_items = db.relationship('SaleItem',      back_populates='product', lazy='dynamic')
    loss_items = db.relationship('LossFicheItem', back_populates='product', lazy='dynamic')
    transfers  = db.relationship('StockTransfer', back_populates='product', lazy='dynamic')

    # ── Helpers ──
    @property
    def prix_achat_f(self):     return float(self.prix_achat)
    @property
    def prix_vente_ht_f(self):  return float(self.prix_vente_ht)
    @property
    def prix_vente_ttc_f(self): return float(self.prix_vente_ttc)
    @property
    def taux_tva_f(self):       return float(self.taux_tva)

    @property
    def marge(self):
        return self.prix_vente_ht_f - self.prix_achat_f

    @property
    def taux_marge(self):
        if self.prix_achat_f == 0: return 0
        return (self.marge / self.prix_achat_f) * 100

    @property
    def prix_gros_f(self): return float(self.prix_gros) if self.prix_gros else 0.0

    @property
    def image_url(self):
        if not self.image_filename:
            return None
        if self.image_filename.startswith('http') or self.image_filename.startswith('/static/'):
            return self.image_filename
        return f'/static/uploads/products/{self.image_filename}'

    @property
    def total_stock_magasin(self):
        """Stock en rayon : somme des variantes ou stock direct."""
        if self.has_variants:
            return sum(v.stock_magasin for v in self.variants)
        return self.stock_magasin

    @property
    def total_stock_entrepot(self):
        if self.has_variants:
            return sum(v.stock_entrepot for v in self.variants)
        return self.stock_entrepot

    @property
    def variant_count(self):
        return self.variants.count()

    def __repr__(self): return f'<Product {self.designation}>'


# ─────────────────────────────────────────
# PRODUCT VARIANT
# ─────────────────────────────────────────
class ProductVariant(db.Model):
    __tablename__ = 'product_variants'

    id          = db.Column(db.Integer, primary_key=True)
    product_id  = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False, index=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey('tenants.id'),  nullable=False, index=True)

    # Identification de la variante
    nom         = db.Column(db.String(150), nullable=False)   # ex: "Rouge / M"
    sku         = db.Column(db.String(80),  nullable=True)    # ex: "TSHIRT-RG-M"
    barcode     = db.Column(db.String(50),  nullable=True, index=True)
    barcode_generated = db.Column(db.Boolean, default=False)

    # Attributs : JSON {"Taille": "M", "Couleur": "Rouge"}
    _attributs  = db.Column('attributs', db.Text, nullable=True, default='{}')

    # Image propre à la variante (optionnel)
    image_filename = db.Column(db.String(255), nullable=True)

    # Prix (peut surcharger le parent)
    prix_achat      = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    prix_vente_ht   = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    taux_tva        = db.Column(db.Numeric(5,  2), nullable=False, default=0)
    prix_vente_ttc  = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    # Stock
    stock_entrepot  = db.Column(db.Integer, default=0, nullable=False)
    stock_magasin   = db.Column(db.Integer, default=0, nullable=False)

    is_active  = db.Column(db.Boolean, default=True)
    ordre      = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship('Product', back_populates='variants')

    # ── Attributs JSON helpers ──
    @property
    def attributs(self):
        try:   return json.loads(self._attributs or '{}')
        except: return {}

    @attributs.setter
    def attributs(self, val):
        self._attributs = json.dumps(val, ensure_ascii=False)

    @property
    def attributs_display(self):
        """'Rouge / M' from {'Couleur':'Rouge','Taille':'M'}"""
        return ' / '.join(self.attributs.values()) or self.nom

    @property
    def prix_vente_ttc_f(self): return float(self.prix_vente_ttc)
    @property
    def prix_vente_ht_f(self):  return float(self.prix_vente_ht)
    @property
    def prix_achat_f(self):     return float(self.prix_achat)
    @property
    def taux_tva_f(self):       return float(self.taux_tva)

    @property
    def marge(self):
        return self.prix_vente_ht_f - self.prix_achat_f

    @property
    def prix_gros_f(self): return float(self.prix_gros) if self.prix_gros else 0.0

    @property
    def image_url(self):
        if self.image_filename:
            if self.image_filename.startswith('http') or self.image_filename.startswith('/static/'):
                return self.image_filename
            return f'/static/uploads/products/{self.image_filename}'
        return self.product.image_url  # fallback sur l'image parent

    def __repr__(self): return f'<Variant {self.nom} of {self.product_id}>'


# ─────────────────────────────────────────
# STOCK TRANSFER
# ─────────────────────────────────────────
class StockTransfer(db.Model):
    __tablename__ = 'stock_transfers'

    id             = db.Column(db.Integer, primary_key=True)
    tenant_id      = db.Column(db.Integer, db.ForeignKey('tenants.id'),  nullable=False, index=True)
    product_id     = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    variant_id     = db.Column(db.Integer, db.ForeignKey('product_variants.id'), nullable=True)
    manager_id     = db.Column(db.Integer, db.ForeignKey('users.id'),    nullable=False)
    quantity       = db.Column(db.Integer, nullable=False)
    transferred_at = db.Column(db.DateTime, default=datetime.utcnow)
    note           = db.Column(db.Text, nullable=True)

    product = db.relationship('Product', back_populates='transfers')
    variant = db.relationship('ProductVariant')
    manager = db.relationship('User')


# ─────────────────────────────────────────
# SALE
# ─────────────────────────────────────────
class Sale(db.Model):
    __tablename__ = 'sales'

    id             = db.Column(db.Integer, primary_key=True)
    tenant_id      = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    cashier_id     = db.Column(db.Integer, db.ForeignKey('users.id'),   nullable=False)
    total_ht       = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_tva      = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_amount   = db.Column(db.Numeric(12, 2), nullable=False)
    amount_given   = db.Column(db.Numeric(12, 2), nullable=True)
    change_given   = db.Column(db.Numeric(12, 2), nullable=True)
    payment_method = db.Column(db.String(20), nullable=False, default=PaymentMethod.CASH)
    sale_type      = db.Column(db.String(10), nullable=False, default='detail')  # detail | engros
    created_at     = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    tenant  = db.relationship('Tenant', back_populates='sales')
    cashier = db.relationship('User', back_populates='sales')
    items   = db.relationship('SaleItem', back_populates='sale', cascade='all, delete-orphan')

    def __repr__(self): return f'<Sale #{self.id}>'


class SaleItem(db.Model):
    __tablename__ = 'sale_items'

    id          = db.Column(db.Integer, primary_key=True)
    sale_id     = db.Column(db.Integer, db.ForeignKey('sales.id'),    nullable=False, index=True)
    product_id  = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    variant_id  = db.Column(db.Integer, db.ForeignKey('product_variants.id'), nullable=True)
    designation = db.Column(db.String(200), nullable=False)   # snapshot
    prix_vente  = db.Column(db.Numeric(10, 2), nullable=False)
    taux_tva    = db.Column(db.Numeric(5,  2), nullable=False, default=0)
    quantity    = db.Column(db.Integer, nullable=False)
    subtotal    = db.Column(db.Numeric(12, 2), nullable=False)

    sale    = db.relationship('Sale',           back_populates='items')
    product = db.relationship('Product',        back_populates='sale_items')
    variant = db.relationship('ProductVariant')


# ─────────────────────────────────────────
# LOSS FICHE
# ─────────────────────────────────────────
class LossFiche(db.Model):
    __tablename__ = 'loss_fiches'

    id             = db.Column(db.Integer, primary_key=True)
    tenant_id      = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    recorded_by_id = db.Column(db.Integer, db.ForeignKey('users.id'),   nullable=False)
    motif          = db.Column(db.String(100), nullable=False)
    note           = db.Column(db.Text,        nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    tenant      = db.relationship('Tenant',  back_populates='losses')
    recorded_by = db.relationship('User',    back_populates='losses')
    items       = db.relationship('LossFicheItem', back_populates='fiche', cascade='all, delete-orphan')


class LossFicheItem(db.Model):
    __tablename__ = 'loss_fiche_items'

    id          = db.Column(db.Integer, primary_key=True)
    fiche_id    = db.Column(db.Integer, db.ForeignKey('loss_fiches.id'), nullable=False, index=True)
    product_id  = db.Column(db.Integer, db.ForeignKey('products.id'),    nullable=False)
    designation = db.Column(db.String(200), nullable=False)
    quantity    = db.Column(db.Integer, nullable=False)
    prix_vente  = db.Column(db.Numeric(10, 2), nullable=False)

    fiche   = db.relationship('LossFiche', back_populates='items')
    product = db.relationship('Product',   back_populates='loss_items')


# ─────────────────────────────────────────
# SUPPLIER ORDER (Commande fournisseur)
# ─────────────────────────────────────────
class SupplierOrder(db.Model):
    __tablename__ = 'supplier_orders'

    id                   = db.Column(db.Integer, primary_key=True)
    tenant_id            = db.Column(db.Integer, db.ForeignKey('tenants.id'),   nullable=False, index=True)
    supplier_id          = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False, index=True)
    created_by_id        = db.Column(db.Integer, db.ForeignKey('users.id'),     nullable=False)

    reference            = db.Column(db.String(80),  nullable=True)   # Numéro de commande interne
    ref_fournisseur      = db.Column(db.String(80),  nullable=True)   # Réf. attribuée par le fournisseur
    statut               = db.Column(db.String(20),  nullable=False, default=OrderStatus.DRAFT, index=True)
    notes                = db.Column(db.Text,        nullable=True)
    date_livraison_prevue= db.Column(db.Date,        nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    supplier   = db.relationship('Supplier',  backref=db.backref('orders',     lazy='dynamic'))
    created_by = db.relationship('User',      backref=db.backref('sup_orders', lazy='dynamic'))
    items      = db.relationship('SupplierOrderItem', back_populates='order',
                                  cascade='all, delete-orphan', lazy='dynamic')

    @property
    def total_amount(self):
        return sum(float(i.total) for i in self.items)

    @property
    def total_items(self):
        return sum(i.quantite_commandee for i in self.items)

    @property
    def status_label(self):
        return OrderStatus.label(self.statut)

    @property
    def can_receive(self):
        return self.statut in (OrderStatus.SENT, OrderStatus.PARTIAL)

    @property
    def is_editable(self):
        return self.statut == OrderStatus.DRAFT

    def __repr__(self):
        return f'<SupplierOrder #{self.id} [{self.statut}]>'


class SupplierOrderItem(db.Model):
    __tablename__ = 'supplier_order_items'

    id                  = db.Column(db.Integer, primary_key=True)
    order_id            = db.Column(db.Integer, db.ForeignKey('supplier_orders.id'), nullable=False, index=True)
    product_id          = db.Column(db.Integer, db.ForeignKey('products.id'),        nullable=False)
    variant_id          = db.Column(db.Integer, db.ForeignKey('product_variants.id'),nullable=True)
    designation         = db.Column(db.String(200), nullable=False)   # snapshot
    sku                 = db.Column(db.String(80),  nullable=True)
    quantite_commandee  = db.Column(db.Integer, nullable=False, default=0)
    quantite_recue      = db.Column(db.Integer, nullable=False, default=0)
    prix_achat_unitaire = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    total               = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    order   = db.relationship('SupplierOrder',  back_populates='items')
    product = db.relationship('Product')
    variant = db.relationship('ProductVariant')

    @property
    def quantite_restante(self):
        return max(0, self.quantite_commandee - self.quantite_recue)

    @property
    def is_fully_received(self):
        return self.quantite_recue >= self.quantite_commandee

    def __repr__(self):
        return f'<OrderItem {self.designation} x{self.quantite_commandee}>'


# ─────────────────────────────────────────
# PAIEMENTS ABONNEMENTS (SaaS)
# ─────────────────────────────────────────
class StatutPaiement:
    EN_ATTENTE = 'en_attente'
    PAYE       = 'paye'
    RETARD     = 'retard'

    LABELS = {
        'en_attente': ('En attente', 'badge-orange'),
        'paye'      : ('Payé',       'badge-green'),
        'retard'    : ('En retard',  'badge-red'),
    }

    @classmethod
    def label(cls, s):
        return cls.LABELS.get(s, (s, 'badge-blue'))


class Paiement(db.Model):
    __tablename__ = 'paiements'

    id             = db.Column(db.Integer, primary_key=True)
    tenant_id      = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    mois           = db.Column(db.String(7),  nullable=False)        # YYYY-MM
    montant        = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    statut         = db.Column(db.String(20), nullable=False, default=StatutPaiement.EN_ATTENTE)
    date_paiement  = db.Column(db.Date, nullable=True)
    reference      = db.Column(db.String(80), nullable=True)
    notes          = db.Column(db.Text, nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    tenant = db.relationship('Tenant', backref=db.backref('paiements', lazy='dynamic'))

    @property
    def status_label(self):
        return StatutPaiement.label(self.statut)

    @property
    def mois_display(self):
        from datetime import datetime as dt
        try:
            return dt.strptime(self.mois, '%Y-%m').strftime('%B %Y')
        except Exception:
            return self.mois

    def __repr__(self):
        return f'<Paiement {self.mois} [{self.statut}]>'


# ─────────────────────────────────────────
# CONFIG GLOBALE (paramètres SaaS)
# ─────────────────────────────────────────
class Config(db.Model):
    __tablename__ = 'config'
    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)

    @classmethod
    def get(cls, key, default=None):
        obj = cls.query.get(key)
        return obj.value if obj else default

    @classmethod
    def set(cls, key, value):
        obj = cls.query.get(key)
        if obj:
            obj.value = str(value) if value is not None else None
        else:
            db.session.add(cls(key=key, value=str(value) if value is not None else None))


# ─────────────────────────────────────────
# BOUTIQUE EN LIGNE — MODÈLES
# ─────────────────────────────────────────

class OnlineCustomer(db.Model):
    __tablename__ = 'online_customers'

    id            = db.Column(db.Integer, primary_key=True)
    tenant_id     = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    nom           = db.Column(db.String(100), nullable=False)
    prenom        = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(150), nullable=False, index=True)
    telephone     = db.Column(db.String(30), nullable=True)
    adresse       = db.Column(db.Text, nullable=True)
    ville         = db.Column(db.String(100), nullable=True)
    password_hash = db.Column(db.String(256), nullable=False)
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    tenant  = db.relationship('Tenant', backref=db.backref('online_customers', lazy='dynamic'))
    orders  = db.relationship('OnlineOrder', back_populates='customer', lazy='dynamic')
    reviews = db.relationship('ProductReview', back_populates='customer', lazy='dynamic')

    def set_password(self, p):   self.password_hash = bcrypt.generate_password_hash(p).decode('utf-8')
    def check_password(self, p): return bcrypt.check_password_hash(self.password_hash, p)

    @property
    def full_name(self): return f"{self.prenom} {self.nom}"


class OnlineOrderStatus:
    PENDING    = 'pending'      # En attente
    CONFIRMED  = 'confirmed'    # Confirmée
    PREPARING  = 'preparing'    # En préparation
    SHIPPED    = 'shipped'      # Expédiée
    DELIVERED  = 'delivered'    # Livrée
    CANCELLED  = 'cancelled'   # Annulée

    @classmethod
    def all(cls):
        return [cls.PENDING, cls.CONFIRMED, cls.PREPARING, cls.SHIPPED, cls.DELIVERED, cls.CANCELLED]

    @classmethod
    def label(cls, s):
        return {'pending':'En attente','confirmed':'Confirmée','preparing':'En préparation',
                'shipped':'Expédiée','delivered':'Livrée','cancelled':'Annulée'}.get(s, s)

    @classmethod
    def color(cls, s):
        return {'pending':'#f59e0b','confirmed':'#3b82f6','preparing':'#a78bfa',
                'shipped':'#06b6d4','delivered':'#22c55e','cancelled':'#ef4444'}.get(s, '#7a8099')

    @classmethod
    def icon(cls, s):
        return {'pending':'⏳','confirmed':'✅','preparing':'📦',
                'shipped':'🚚','delivered':'🏠','cancelled':'❌'}.get(s, '•')


class OnlineOrder(db.Model):
    __tablename__ = 'online_orders'

    id             = db.Column(db.Integer, primary_key=True)
    tenant_id      = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    customer_id    = db.Column(db.Integer, db.ForeignKey('online_customers.id'), nullable=False, index=True)
    reference      = db.Column(db.String(30), nullable=False, unique=True)
    status         = db.Column(db.String(20), default=OnlineOrderStatus.PENDING, nullable=False, index=True)

    total_ht       = db.Column(db.Numeric(10, 2), default=0)
    total_tva      = db.Column(db.Numeric(10, 2), default=0)
    total_amount   = db.Column(db.Numeric(10, 2), default=0)
    frais_livraison = db.Column(db.Numeric(10, 2), default=0)

    adresse_livraison = db.Column(db.Text, nullable=True)
    ville_livraison   = db.Column(db.String(100), nullable=True)
    telephone_contact = db.Column(db.String(30), nullable=True)
    note_client       = db.Column(db.Text, nullable=True)
    note_manager      = db.Column(db.Text, nullable=True)

    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant   = db.relationship('Tenant', backref=db.backref('online_orders', lazy='dynamic'))
    customer = db.relationship('OnlineCustomer', back_populates='orders')
    items    = db.relationship('OnlineOrderItem', back_populates='order', cascade='all, delete-orphan')

    payment_method  = db.Column(db.String(30), default='livraison')  # livraison, orange_money, mtn_money, moov_money, wave
    payment_status  = db.Column(db.String(20), default='en_attente')  # en_attente, paye

class OnlineOrderItem(db.Model):
    __tablename__ = 'online_order_items'

    id          = db.Column(db.Integer, primary_key=True)
    order_id    = db.Column(db.Integer, db.ForeignKey('online_orders.id'), nullable=False)
    product_id  = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    variant_id  = db.Column(db.Integer, db.ForeignKey('product_variants.id'), nullable=True)
    designation = db.Column(db.String(200), nullable=False)
    prix_vente  = db.Column(db.Numeric(10, 2), nullable=False)
    quantity    = db.Column(db.Integer, nullable=False, default=1)
    subtotal    = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    order   = db.relationship('OnlineOrder', back_populates='items')
    product = db.relationship('Product')
    variant = db.relationship('ProductVariant')


class ProductReview(db.Model):
    __tablename__ = 'product_reviews'

    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    product_id  = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('online_customers.id'), nullable=False)
    rating      = db.Column(db.Integer, nullable=False)  # 1-5 étoiles
    comment     = db.Column(db.Text, nullable=True)
    is_approved = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    tenant   = db.relationship('Tenant', backref=db.backref('reviews', lazy='dynamic'))
    product  = db.relationship('Product', backref=db.backref('reviews', lazy='dynamic'))
    customer = db.relationship('OnlineCustomer', back_populates='reviews')


class ShopVisit(db.Model):
    __tablename__ = 'shop_visits'
    id        = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    visited_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    ip_hash    = db.Column(db.String(64), nullable=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True, index=True)  # si visite produit


class ProductFavorite(db.Model):
    __tablename__ = 'product_favorites'
    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    product_id  = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('online_customers.id'), nullable=False, index=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    product  = db.relationship('Product', backref=db.backref('favorites', lazy='dynamic'))
    customer = db.relationship('OnlineCustomer', backref=db.backref('favorites', lazy='dynamic'))


#adress registrements
class CustomerAddress(db.Model):
    __tablename__ = 'customer_addresses'
    id          = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('online_customers.id'), nullable=False, index=True)
    label       = db.Column(db.String(50), default='Maison')  # Maison, Bureau, etc.
    adresse     = db.Column(db.Text, nullable=False)
    ville       = db.Column(db.String(100), nullable=False)
    telephone   = db.Column(db.String(30), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship('OnlineCustomer', backref=db.backref('addresses', lazy='dynamic', order_by='CustomerAddress.created_at.desc()'))