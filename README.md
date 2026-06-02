# SaaS POS — Plateforme de Gestion de Points de Vente

Application Flask multi-tenant pour la gestion de caisses enregistreuses, stocks, fournisseurs et abonnements.

## Stack technique
- **Backend** : Python 3.11 + Flask + SQLAlchemy (SQLite/PostgreSQL)
- **Auth** : Flask-Login + Flask-Bcrypt
- **PDF** : WeasyPrint
- **Codes-barres** : python-barcode + qrcode

## Fonctionnalités
- ✅ Multi-tenant (commerçants isolés)
- ✅ Caisse détail + Caisse en gros
- ✅ Gestion stocks (entrepôt ↔ rayon)
- ✅ Variantes de produits (taille, couleur, pointure…)
- ✅ Fournisseurs & commandes
- ✅ Codes-barres EAN-13 + QR codes
- ✅ Tickets de caisse + Factures en gros (PDF)
- ✅ Rapports PDF (ventes, produits, étiquettes prix)
- ✅ Abonnements & paiements (super admin)
- ✅ Taux de change en temps réel (USD/EUR/TL → FCFA)

## Installation

```bash
git clone <repo-url>
cd saas_pos
pip install -r requirements.txt
python migrate_db.py   # Migrer la DB existante
python run.py
```

## Comptes par défaut (développement)
| Rôle | Email | Mot de passe |
|------|-------|--------------|
| Super Admin | admin@saaspos.local | Admin@1234! |
| Manager | manager@test.com | Test@1234! |

## Structure
```
saas_pos/
├── app/
│   ├── models.py          # Tous les modèles SQLAlchemy
│   ├── routes/            # Blueprints Flask
│   └── utils/             # Décorateurs, barcode gen
├── static/                # CSS, JS, images
├── templates/             # Jinja2
│   ├── admin/             # Super admin
│   ├── manager/           # Gérant
│   ├── cashier/           # Caissier
│   └── pos/               # Interface caisse
├── migrate_db.py          # Migration sans perte de données
├── run.py                 # Point d'entrée
└── requirements.txt
```
