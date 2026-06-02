"""
Script de migration : rend product_id nullable dans sale_items.
Lance UNE SEULE FOIS depuis votre PC avec l'URL Supabase dans .env.

Usage : python alter_sale_items.py
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault('FLASK_ENV', 'production')
from app import create_app, db

app = create_app('production')
with app.app_context():
    try:
        db.engine.execute(
            "ALTER TABLE sale_items ALTER COLUMN product_id DROP NOT NULL;"
        )
        print("✅ sale_items.product_id est maintenant nullable.")
    except Exception as e:
        # SQLAlchemy 2.x
        with db.engine.connect() as conn:
            conn.execute(db.text(
                "ALTER TABLE sale_items ALTER COLUMN product_id DROP NOT NULL;"
            ))
            conn.commit()
        print("✅ sale_items.product_id est maintenant nullable.")
