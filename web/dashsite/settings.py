"""Minimal settings: no database, no auth. The dashboard reads the healer's own SQLite DB."""
import os
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = WEB_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))        # makes `import healer` work

SECRET_KEY = "dev-only-local-demo"           # local demo; never expose this server
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

INSTALLED_APPS = ["monitor"]
MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
ROOT_URLCONF = "dashsite.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
}]
DATABASES = {}                               # no Django DB: nothing to migrate

# Path to healer.yaml; None = the project's default config/healer.yaml
HEALER_CONFIG_PATH = os.environ.get("HEALER_CONFIG")