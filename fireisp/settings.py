import os
from pathlib import Path
import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'
TESTING = 'test' in __import__('sys').argv or 'pytest' in __import__('sys').argv[0]
SECRET_KEY = os.getenv('SECRET_KEY', 'development-only-change-before-deploy')
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', '')
if not (DEBUG or TESTING) and (SECRET_KEY.startswith('development-') or not ENCRYPTION_KEY):
    raise ImproperlyConfigured('SECRET_KEY and ENCRYPTION_KEY must be configured.')
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',')
CSRF_TRUSTED_ORIGINS = [v for v in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if v]
INSTALLED_APPS = [
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'core', 'billing', 'fiscal', 'network', 'operations', 'compliance',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware', 'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware', 'core.middleware.AccessMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'fireisp.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
              'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
              'OPTIONS': {'context_processors': [
                  'django.template.context_processors.request', 'django.contrib.auth.context_processors.auth',
                  'django.contrib.messages.context_processors.messages', 'core.context.application',
              ]}}]
WSGI_APPLICATION = 'fireisp.wsgi.application'
DATABASES = {'default': dj_database_url.config(default=f'sqlite:///{BASE_DIR}/db.sqlite3', conn_max_age=60)}
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'es-mx'
LANGUAGES = [('es-mx', 'Español (México)')]
TIME_ZONE = 'America/Chihuahua'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'}}
MEDIA_ROOT = Path(os.getenv('DOCUMENT_ROOT', str(BASE_DIR / 'private_documents')))
MEDIA_URL = '/protected-files/'
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
FORM_RENDERER = 'core.form_renderer.FireISPFormRenderer'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = not (DEBUG or TESTING)
SECURE_REDIRECT_EXEMPT = [r'^network/radius/']
SESSION_COOKIE_SECURE = not (DEBUG or TESTING)
CSRF_COOKIE_SECURE = not (DEBUG or TESTING)
SECURE_HSTS_SECONDS = 2592000 if not DEBUG else 0
SECURE_REFERRER_POLICY = 'same-origin'
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
DEFAULT_FROM_EMAIL = 'FireISP <noreply@localhost>'
REDIS_URL = os.getenv('REDIS_URL', '')
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache', 'LOCATION': REDIS_URL}} if REDIS_URL and not TESTING else {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'fireisp'}}
CELERY_BROKER_URL = REDIS_URL if REDIS_URL and not TESTING else 'memory://'
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 120
CELERY_TASK_SOFT_TIME_LIMIT = 100
CELERY_TASK_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_BEAT_SCHEDULE = {'outbox': {'task': 'core.tasks.deliver_outbox', 'schedule': 15.0},
                        'renewal-preview': {'task': 'billing.tasks.renewal_preview', 'schedule': 3600.0},
                        'suspension-evaluation': {'task': 'billing.tasks.evaluate_suspensions', 'schedule': 60.0}}
FIREISP_VERSION = os.getenv('FIREISP_VERSION', '0.1.0')
NETWORK_AGENT_SOCKET = os.getenv('NETWORK_AGENT_SOCKET', '/run/fireisp-network/agent.sock')
NETWORK_RADIUS_TOKEN = os.getenv('NETWORK_RADIUS_TOKEN', '')
LOGGING = {'version': 1, 'disable_existing_loggers': False,
           'handlers': {'console': {'class': 'logging.StreamHandler'}},
           'root': {'handlers': ['console'], 'level': 'WARNING'}}
