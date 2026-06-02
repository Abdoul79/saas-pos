import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # Sur Railway, les variables viennent directement de l'environnement

# Déterminer l'environnement
env = os.environ.get('FLASK_ENV', '').strip()
if not env or env not in ('development', 'production'):
    env = 'production'

from app import create_app, db

app = create_app(env)

if __name__ == '__main__':
    with app.app_context():
        try:
            db.create_all()
            print(f"✓ DB prête ({env})")
        except Exception as e:
            print(f"⚠ DB warning : {e}")

    # Railway injecte PORT — obligatoire d'écouter sur ce port
    port = int(os.environ.get('PORT', 5000))
    print(f"✓ Démarrage sur 0.0.0.0:{port}")

    app.run(
        host  = '0.0.0.0',
        port  = port,
        debug = False          # toujours False en production
    )
