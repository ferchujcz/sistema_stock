"""
Django settings for control_stock project.
"""

import os
from pathlib import Path
import dj_database_url
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# <--- NUEVO 2: Le decimos que cargue los datos secretos AHORA MISMO
load_dotenv(os.path.join(BASE_DIR, '.env'))

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-p0t@(j!*c2i+nnh7g0l3@xwr0ggh3=j#_9egtqdg0*3%+0(ud2')

# SECURITY WARNING: don't run with debug turned on in production!
# Lo ponemos en True para desarrollo local
DEBUG = os.environ.get('DJANGO_DEBUG', '') != 'False'

ALLOWED_HOSTS = ['178.105.42.34', 'kioscos-saas.duckdns.org', 'localhost', '127.0.0.1']




# Configuración para Ngrok y conexiones externas
CSRF_TRUSTED_ORIGINS = [
    'http://178.105.42.34', 
    'https://kioscos-saas.duckdns.org'
    'https://*.ngrok-free.app',
    'https://*.ngrok-free.dev',  # <--- ESTA ES LA QUE TE FALTABA
    'https://*.ngrok.io',
    'http://127.0.0.1',
    'http://localhost',
]
# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core.apps.CoreConfig', # Tu aplicación principal
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware', # Manejo de archivos estáticos
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'control_stock.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                # Tus procesadores de contexto personalizados
                'core.context_processors.info_global',      # <--- ESTE NUEVO
                'core.context_processors.alertas_globales', # <--- ESTE
            ],
        },
    },
]

WSGI_APPLICATION = 'control_stock.wsgi.application'

# Database
# Por defecto usamos SQLite para trabajar en local (PC)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Solo si existe una variable de entorno DATABASE_URL (ej. en Render), usamos PostgreSQL
# Esto no afectará tu trabajo local
database_url = os.environ.get("DATABASE_URL")
if database_url:
    DATABASES['default'] = dj_database_url.parse(database_url)

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'es-ar' # Español Argentina

TIME_ZONE = 'America/Argentina/Buenos_Aires' # Tu hora local

USE_I18N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# Habilitamos compresión y caché de archivos estáticos
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Redirecciones de Login/Logout
LOGIN_REDIRECT_URL = '/' 
LOGOUT_REDIRECT_URL = '/'