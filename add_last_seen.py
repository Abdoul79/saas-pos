"""Migration: ajoute last_seen à la table users."""
import os; os.environ.setdefault('FLASK_ENV','production')
from app import create_app, db
from sqlalchemy import text
app = create_app('production')
with app.app_context():
    try:
        db.session.execute(text("ALTER TABLE users ADD COLUMN last_seen TIMESTAMP"))
        db.session.commit()
        print("✅ Colonne last_seen ajoutée.")
    except Exception as e:
        print(f"Info: {e}")
