from functools import wraps
from flask import abort, flash, redirect, url_for
from flask_login import current_user
from app.models import UserRole, TenantStatus
from datetime import date


def role_required(*roles):
    """Restrict access to users with specific roles."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def tenant_active_required(f):
    """Ensure the tenant account is active and licence not expired."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        if current_user.is_super_admin:
            return f(*args, **kwargs)
        tenant = current_user.tenant
        if not tenant or tenant.status != TenantStatus.ACTIVE:
            flash('Votre compte est suspendu ou inactif. Contactez l\'administrateur.', 'danger')
            return redirect(url_for('auth.login'))
        if tenant.licence_expiry and tenant.licence_expiry < date.today():
            flash('Votre abonnement a expiré. Veuillez contacter l\'administrateur.', 'danger')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


def same_tenant_required(f):
    """Ensure operations stay within the user's own tenant scope."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        if not current_user.tenant_id and not current_user.is_super_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


# Shorthand combined decorators
def manager_required(f):
    @wraps(f)
    @role_required(UserRole.MANAGER, UserRole.SUPER_ADMIN)
    @tenant_active_required
    def decorated_function(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated_function


def cashier_or_manager_required(f):
    @wraps(f)
    @role_required(UserRole.CASHIER, UserRole.MANAGER, UserRole.SUPER_ADMIN)
    @tenant_active_required
    def decorated_function(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated_function
