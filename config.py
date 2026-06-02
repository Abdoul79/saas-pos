import os
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY                     = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    SUPABASE_URL                   = os.environ.get('SUPABASE_URL', '')
    # Flask-Mail (SMTP)
    MAIL_SERVER        = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT          = int(os.environ.get('MAIL_PORT', 465))
    MAIL_USE_TLS       = os.environ.get('MAIL_USE_TLS', 'false').lower() == 'true'
    MAIL_USE_SSL       = os.environ.get('MAIL_USE_SSL', 'true').lower() == 'true'
    MAIL_USERNAME      = os.environ.get('MAIL_USERNAME', '')
    MAIL_PASSWORD      = os.environ.get('MAIL_PASSWORD', '')
    MAIL_DEFAULT_SENDER= os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@saaspos.com')
    SUPABASE_KEY                   = os.environ.get('SUPABASE_KEY', '')
    SUPABASE_BUCKET                = os.environ.get('SUPABASE_BUCKET', 'product-images')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PERMANENT_SESSION_LIFETIME     = timedelta(hours=8)
    MAX_CONTENT_LENGTH             = 16 * 1024 * 1024
    BARCODE_OUTPUT_DIR             = os.path.join(basedir, 'static', 'barcodes')

    @staticmethod
    def init_app(app):
        os.makedirs(os.path.join(basedir, 'static', 'barcodes'),           exist_ok=True)
        os.makedirs(os.path.join(basedir, 'static', 'uploads', 'logos'),   exist_ok=True)
        os.makedirs(os.path.join(basedir, 'static', 'uploads', 'products'),exist_ok=True)


def _build_db_url():
    """Construit l'URL de la base — appelé à l'import du module."""
    url = os.environ.get('DATABASE_URL', '').strip()
    if url:
        return url.replace('postgres://', 'postgresql://', 1)
    return None


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = (
        _build_db_url() or
        f"sqlite:///{os.path.join(basedir, 'saas_pos_dev.db')}"
    )


class ProductionConfig(Config):
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = (
        _build_db_url() or
        'sqlite:///saas_pos_prod.db'      # fallback si DATABASE_URL absent
    )
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle' : 300,
    }

    @staticmethod
    def init_app(app):
        Config.init_app(app)


config = {
    'development': DevelopmentConfig,
    'production' : ProductionConfig,
    'default'    : DevelopmentConfig,
}
