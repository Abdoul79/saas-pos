import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app import create_app, db

env = os.environ.get('FLASK_ENV', 'production').strip()
if env not in ('development', 'production'):
    env = 'production'

application = create_app(env)
app = application

# Créer les tables au démarrage
with application.app_context():
    try:
        db.create_all()
        print(f"✓ DB prête ({env})")
    except Exception as e:
        print(f"⚠ DB warning : {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    application.run(host='0.0.0.0', port=port, debug=False)
