from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_mail import Mail
from flask_bcrypt import Bcrypt
from config import config
from datetime import date, datetime

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
bcrypt = Bcrypt()
mail = Mail()


def create_app(config_name='default'):
    app = Flask(__name__, template_folder='../templates', static_folder='../static')

    # Charger la config de base
    cfg_obj = config.get(config_name) or config['default']
    app.config.from_object(cfg_obj)

    # Appeler init_app de la config (permet d'override SQLALCHEMY_DATABASE_URI)
    if hasattr(cfg_obj, 'init_app'):
        cfg_obj.init_app(app)

    # Vérification finale : si DATABASE_URL toujours vide, fallback SQLite
    if not app.config.get('SQLALCHEMY_DATABASE_URI'):
        import os, sys
        url = os.environ.get('DATABASE_URL', '').strip()
        url = url.replace('postgres://', 'postgresql://', 1) if url else ''
        app.config['SQLALCHEMY_DATABASE_URI'] = url or 'sqlite:///saas_pos.db'

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    bcrypt.init_app(app)
    # Re-lire les variables mail depuis l'env au runtime (Railway injecte après import)
    @app.context_processor
    def inject_date():
           return {'date': date, 'datetime': datetime}


    import os
    for key in ('MAIL_SERVER','MAIL_PORT','MAIL_USE_TLS','MAIL_USE_SSL',
                'MAIL_USERNAME','MAIL_PASSWORD','MAIL_DEFAULT_SENDER'):
        val = os.environ.get(key)
        if val is not None:
            if key == 'MAIL_PORT':
                app.config[key] = int(val)
            elif key in ('MAIL_USE_TLS', 'MAIL_USE_SSL'):
                app.config[key] = val.lower() == 'true'
            else:
                app.config[key] = val
    mail.init_app(app)

    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Veuillez vous connecter.'
    login_manager.login_message_category = 'warning'

    from app.routes.auth        import auth_bp
    from app.routes.super_admin import super_admin_bp
    from app.routes.manager     import manager_bp
    from app.routes.cashier     import cashier_bp
    from app.routes.pos         import pos_bp
    from app.routes.suppliers   import suppliers_bp
    from app.routes.categories  import categories_bp
    from app.routes.variants    import variants_bp
    from app.routes.orders      import orders_bp

    app.register_blueprint(auth_bp,        url_prefix='/auth')
    app.register_blueprint(super_admin_bp, url_prefix='/superadmin')
    app.register_blueprint(manager_bp,     url_prefix='/manager')
    app.register_blueprint(cashier_bp,     url_prefix='/cashier')
    app.register_blueprint(pos_bp,         url_prefix='/pos')
    app.register_blueprint(suppliers_bp,   url_prefix='/manager/suppliers')
    app.register_blueprint(categories_bp,  url_prefix='/manager/categories')
    app.register_blueprint(variants_bp,    url_prefix='/manager/variants')
    app.register_blueprint(orders_bp,      url_prefix='/manager/orders')

    from app.routes.shop import shop_bp
    from app.routes.online import online_bp
    app.register_blueprint(shop_bp,        url_prefix='/shop')
    app.register_blueprint(online_bp,      url_prefix='/manager/online')

    @app.route('/')
    def index():
        from flask import redirect, url_for
        return redirect(url_for('auth.login'))

    return app
