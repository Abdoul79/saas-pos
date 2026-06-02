import os
import random
import string
from io import BytesIO
import base64

try:
    import barcode
    from barcode.writer import ImageWriter
    BARCODE_AVAILABLE = True
except ImportError:
    BARCODE_AVAILABLE = False


BARCODE_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'static', 'barcodes')


def generate_ean13_number(tenant_id: int) -> str:
    """Generate a unique EAN-13 compatible barcode number.

    EAN-13 = 12 payload digits + 1 check digit = 13 total.
    """
    # 2-digit tenant prefix + 10 random digits = 12-digit payload
    tenant_prefix = str(tenant_id % 99).zfill(2)
    random_digits = ''.join(random.choices(string.digits, k=10))
    payload = tenant_prefix + random_digits  # exactly 12 digits

    # EAN-13 check digit: odd positions x1, even positions x3
    odds  = sum(int(payload[i]) for i in range(0, 12, 2))   # indices 0,2,4,6,8,10
    evens = sum(int(payload[i]) for i in range(1, 12, 2))   # indices 1,3,5,7,9,11
    check = (10 - ((odds + evens * 3) % 10)) % 10
    return payload + str(check)  # 13 digits total


def generate_barcode_image(barcode_value: str, product_id: int, fmt='EAN13') -> str:
    """
    Generate a barcode image and save to static/barcodes/.
    Returns the relative URL path or None if barcode lib not available.
    """
    if not BARCODE_AVAILABLE:
        return None

    os.makedirs(BARCODE_DIR, exist_ok=True)
    filename = f'barcode_{product_id}_{barcode_value}'
    filepath = os.path.join(BARCODE_DIR, filename)

    try:
        if fmt == 'EAN13' and len(barcode_value) == 13:
            bc_class = barcode.get_barcode_class('ean13')
            bc = bc_class(barcode_value[:-1], writer=ImageWriter())
        else:
            bc_class = barcode.get_barcode_class('code128')
            bc = bc_class(barcode_value, writer=ImageWriter())

        bc.save(filepath)
        return f'/static/barcodes/{filename}.png'
    except Exception as e:
        print(f'Barcode generation error: {e}')
        return None


def generate_barcode_b64(barcode_value: str) -> str:
    """Return barcode as base64 PNG for inline HTML embedding."""
    if not BARCODE_AVAILABLE:
        return ''
    try:
        buffer = BytesIO()
        bc_class = barcode.get_barcode_class('code128')
        bc = bc_class(barcode_value, writer=ImageWriter())
        bc.write(buffer)
        buffer.seek(0)
        return base64.b64encode(buffer.read()).decode('utf-8')
    except Exception:
        return ''
