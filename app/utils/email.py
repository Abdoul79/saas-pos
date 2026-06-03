"""Utilitaire email — SendGrid / Brevo / Resend via API HTTPS."""
import os
import requests as req
import threading


def send_email(to_email, subject, html_content, attachment=None, attachment_name=None):
    """
    Envoie un email via SendGrid → Brevo → Resend.
    attachment: bytes du fichier PDF (optionnel)
    attachment_name: nom du fichier joint (ex: facture.pdf)
    """
    sendgrid = os.environ.get('SENDGRID_API_KEY', '').strip()
    brevo    = os.environ.get('BREVO_API_KEY', '').strip()
    resend   = os.environ.get('RESEND_API_KEY', '').strip()
    sender   = (os.environ.get('MAIL_DEFAULT_SENDER') or
                os.environ.get('MAIL_USERNAME') or '').strip()

    import base64

    if sendgrid:
        payload = {
            'personalizations': [{'to': [{'email': to_email}]}],
            'from'   : {'email': sender or 'noreply@saaspos.com', 'name': 'SaaS POS'},
            'subject': subject,
            'content': [{'type': 'text/html', 'value': html_content}],
        }
        if attachment and attachment_name:
            payload['attachments'] = [{
                'content': base64.b64encode(attachment).decode(),
                'type': 'application/pdf',
                'filename': attachment_name,
            }]
        r = req.post('https://api.sendgrid.com/v3/mail/send',
            headers={'Authorization': f'Bearer {sendgrid}', 'Content-Type': 'application/json'},
            json=payload, timeout=20)
        print(f'[sendgrid] {r.status_code} → {to_email}' if r.status_code == 202
              else f'[sendgrid] Err {r.status_code}: {r.text[:200]}')

    elif brevo and sender:
        payload = {
            'sender': {'name': 'SaaS POS', 'email': sender},
            'to': [{'email': to_email}],
            'subject': subject,
            'htmlContent': html_content,
        }
        if attachment and attachment_name:
            payload['attachment'] = [{
                'content': base64.b64encode(attachment).decode(),
                'name': attachment_name,
            }]
        r = req.post('https://api.brevo.com/v3/smtp/email',
            headers={'api-key': brevo, 'Content-Type': 'application/json'},
            json=payload, timeout=20)
        print(f'[brevo] {r.status_code} → {to_email}' if r.ok
              else f'[brevo] Err {r.status_code}: {r.text[:150]}')

    elif resend:
        payload = {
            'from': f'SaaS POS <{sender or "onboarding@resend.dev"}>',
            'to': [to_email],
            'subject': subject,
            'html': html_content,
        }
        if attachment and attachment_name:
            payload['attachments'] = [{
                'content': list(attachment),
                'filename': attachment_name,
            }]
        r = req.post('https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {resend}', 'Content-Type': 'application/json'},
            json=payload, timeout=20)
        print(f'[resend] {r.status_code} → {to_email}' if r.ok
              else f'[resend] Err {r.status_code}: {r.text[:150]}')

    else:
        print('[email] Aucun service email configuré')


def send_email_async(to_email, subject, html_content, attachment=None, attachment_name=None):
    """Envoie l'email dans un thread background (évite timeout worker)."""
    t = threading.Thread(target=send_email,
                         args=(to_email, subject, html_content, attachment, attachment_name))
    t.daemon = True
    t.start()
