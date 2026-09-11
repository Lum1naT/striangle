import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or "test" in __import__("sys").argv:
        SECRET_KEY = "local-development-only-not-for-hosting"
    else:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY, or DJANGO_DEBUG=true for local development.")
ALLOWED_HOSTS = [x.strip() for x in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if x.strip()]
if os.getenv("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])
CSRF_TRUSTED_ORIGINS = [x for x in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if x]
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "django.contrib.staticfiles", "trading"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware", "whitenoise.middleware.WhiteNoiseMiddleware", "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware", "trading.middleware.PrivateAPI"]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": dj_database_url.config(default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=60, conn_health_checks=True)}
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    DATABASES["default"]["OPTIONS"] = {"timeout": 30}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"trading": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR.parent / "dist"]
WHITENOISE_ROOT = BASE_DIR.parent / "dist"
WHITENOISE_MAX_AGE = 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_AGE = 60 * 60 * 12
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.getenv("DJANGO_SSL_REDIRECT", "false" if DEBUG else "true").lower() == "true"
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
DATA_UPLOAD_MAX_MEMORY_SIZE = 100_000
SYMBOLS = tuple(os.getenv("TRADING_SYMBOLS", "BTCUSDT,ETHUSDT").split(","))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "")
AI_DAILY_ATTEMPT_LIMIT = int(os.getenv("AI_DAILY_ATTEMPT_LIMIT", "600"))
if not 1 <= AI_DAILY_ATTEMPT_LIMIT <= 10000:
    raise ImproperlyConfigured("AI_DAILY_ATTEMPT_LIMIT must be between 1 and 10,000")
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")
NEWS_FEEDS = tuple(x.strip() for x in os.getenv("NEWS_FEEDS", "https://www.coindesk.com/arc/outboundfeeds/rss/").split(",") if x.strip())
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
# This process owns exactly one dedicated exchange account. Never use a shared account.
LIVE_OWNER_ID = os.getenv("LIVE_OWNER_ID", "")
LIVE_ACCOUNT_DEDICATED = os.getenv("LIVE_ACCOUNT_DEDICATED", "false").lower() == "true"
LIVE_WITHDRAWALS_DISABLED_CONFIRMED = os.getenv("LIVE_WITHDRAWALS_DISABLED_CONFIRMED", "false").lower() == "true"
LIVE_MAX_CAPITAL = os.getenv("LIVE_MAX_CAPITAL", "0")
LIVE_MIN_PAPER_DAYS = 30
LIVE_MIN_CLOSED_TRADES = 20
