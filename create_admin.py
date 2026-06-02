"""
Script de création du Super Admin dans Supabase.
Usage : python create_admin.py
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Vérifier DATABASE_URL
db_url = os.environ.get('DATABASE_URL', '').strip()
if not db_url:
    print("❌ DATABASE_URL non défini dans .env")
    print("   Ajoutez l'URL Supabase pooler dans votre .env")
    sys.exit(1)

os.environ['FLASK_ENV'] = 'production'

from app import create_app, db
from app.models import User, UserRole

app = create_app('production')

with app.app_context():
    # Créer toutes les tables si elles n'existent pas encore
    print("⏳ Création des tables...")
    db.create_all()
    print("✓ Tables créées")

    # Vérifier si un super admin existe déjà
    existing = User.query.filter_by(role=UserRole.SUPER_ADMIN).first()
    if existing:
        print(f"\n⚠️  Super admin existant : {existing.email}")
        overwrite = input("   Créer un nouveau super admin quand même ? (o/N) : ").strip().lower()
        if overwrite != 'o':
            print("Annulé.")
            sys.exit(0)

    print("\n── Création du Super Administrateur ──────────────────")
    nom    = input("Nom        : ").strip()
    prenom = input("Prénom     : ").strip()
    email  = input("Email      : ").strip().lower()
    
    # Vérifier que l'email n'est pas déjà utilisé
    if User.query.filter_by(email=email).first():
        print(f"❌ L'email {email} est déjà utilisé.")
        sys.exit(1)

    import getpass
    password = getpass.getpass("Mot de passe (min. 8 car.) : ")
    if len(password) < 8:
        print("❌ Mot de passe trop court (minimum 8 caractères).")
        sys.exit(1)

    confirm = getpass.getpass("Confirmer le mot de passe  : ")
    if password != confirm:
        print("❌ Les mots de passe ne correspondent pas.")
        sys.exit(1)

    # Créer le super admin
    admin = User(
        nom    = nom,
        prenom = prenom,
        email  = email,
        role   = UserRole.SUPER_ADMIN,
    )
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()

    print(f"\n✅ Super Admin créé avec succès !")
    print(f"   Nom   : {prenom} {nom}")
    print(f"   Email : {email}")
    print(f"   Rôle  : Super Administrateur")
    print(f"\n   Connectez-vous sur votre app Railway avec ces identifiants.")
