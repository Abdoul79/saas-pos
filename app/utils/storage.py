"""
Gestion du stockage des images — Supabase Storage en production, local en dev.
"""
import os
import uuid
import requests
from flask import current_app
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def _allowed(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def upload_image(file, folder='products'):
    """
    Upload une image.
    - Si SUPABASE_URL est défini → Supabase Storage → retourne URL publique
    - Sinon → sauvegarde locale → retourne chemin relatif
    """
    if not file or not _allowed(file.filename):
        return None

    ext      = file.filename.rsplit('.', 1)[1].lower()
    filename = f"{folder}/{uuid.uuid4().hex}.{ext}"

    supabase_url = current_app.config.get('SUPABASE_URL', '')
    supabase_key = current_app.config.get('SUPABASE_KEY', '')
    bucket       = current_app.config.get('SUPABASE_BUCKET', 'product-images')

    if supabase_url and supabase_key:
        # ── Upload vers Supabase Storage ──────────────────────────────────
        try:
            supabase_url = supabase_url.rstrip('/')   # enlever slash final
            file.seek(0)                               # rembobiner le stream
            file_data    = file.read()
            if not file_data:
                print("Supabase upload error: fichier vide (stream déjà lu)")
                return None
            content_type = f"image/{ext}" if ext != 'jpg' else 'image/jpeg'
            upload_url   = f"{supabase_url}/storage/v1/object/{bucket}/{filename}"
            headers = {
                'Authorization': f'Bearer {supabase_key}',
                'Content-Type' : content_type,
                'x-upsert'     : 'true',
            }
            print(f"[Storage] upload → {upload_url} ({len(file_data)} bytes)")
            r = requests.post(upload_url, headers=headers, data=file_data, timeout=30)
            print(f"[Storage] status={r.status_code} resp={r.text[:120]}")
            if r.status_code in (200, 201):
                public_url = f"{supabase_url}/storage/v1/object/public/{bucket}/{filename}"
                return public_url
            else:
                print(f"Supabase upload error {r.status_code}: {r.text}")
                return None
        except Exception as e:
            print(f"Supabase upload exception: {e}")
            return None
    else:
        # ── Sauvegarde locale (développement) ─────────────────────────────
        upload_dir = os.path.join(current_app.static_folder, 'uploads', folder)
        os.makedirs(upload_dir, exist_ok=True)
        local_name = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(upload_dir, local_name))
        return f"/static/uploads/{folder}/{local_name}"


def delete_image(url_or_path):
    """Supprimer une image de Supabase ou localement."""
    if not url_or_path:
        return
    supabase_url = current_app.config.get('SUPABASE_URL', '')
    supabase_key = current_app.config.get('SUPABASE_KEY', '')
    bucket       = current_app.config.get('SUPABASE_BUCKET', 'product-images')

    if supabase_url and url_or_path.startswith(supabase_url):
        try:
            # Extraire le path depuis l'URL publique
            path = url_or_path.split(f'/object/public/{bucket}/')[-1]
            del_url = f"{supabase_url}/storage/v1/object/{bucket}/{path}"
            requests.delete(del_url,
                headers={'Authorization': f'Bearer {supabase_key}'},
                timeout=10)
        except Exception as e:
            print(f"Supabase delete warning: {e}")
    elif url_or_path.startswith('/static/'):
        # Suppression locale
        try:
            local_path = os.path.join(current_app.static_folder,
                                      url_or_path.replace('/static/', ''))
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception as e:
            print(f"Local delete warning: {e}")
