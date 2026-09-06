import base64
import hashlib
from cryptography.fernet import Fernet
from django.conf import settings

def _fernet():
    key = settings.ENCRYPTION_KEY
    if not key and (settings.DEBUG or settings.TESTING):
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)

def encrypt(value):
    if not value: return ''
    if not isinstance(value, str): raise TypeError('Secret must be text')
    return _fernet().encrypt(value.encode()).decode()

def decrypt(value):
    return _fernet().decrypt(value.encode()).decode() if value else ''
