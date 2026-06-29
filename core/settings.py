import os
from datetime import timedelta
from pathlib import Path

"""Django settings for the Foundry-AI orchestrator backend."""

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-key-change-me")

DEBUG = os.environ.get("DEBUG", "true").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'corsheaders',
    'channels',
    'rest_framework',
    'rest_framework_simplejwt.token_blacklist',
    'apps.accounts',
    'apps.workspaces',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'
ASGI_APPLICATION = 'core.asgi.application'


# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': os.environ.get('DB_ENGINE', 'django.db.backends.sqlite3'),
        'NAME': os.environ.get('DB_NAME', BASE_DIR / 'db.sqlite3'),
        'USER': os.environ.get('DB_USER', ''),
        'PASSWORD': os.environ.get('DB_PASSWORD', ''),
        'HOST': os.environ.get('DB_HOST', ''),
        'PORT': os.environ.get('DB_PORT', ''),
    }
}


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

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
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.AnonRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'user': os.environ.get('THROTTLE_USER_RATE', '240/minute'),
        'anon': os.environ.get('THROTTLE_ANON_RATE', '60/minute'),
        'auth_ip': os.environ.get('THROTTLE_AUTH_IP_RATE', '20/minute'),
        'auth_user': os.environ.get('THROTTLE_AUTH_USER_RATE', '10/minute'),
        'auth_refresh': os.environ.get('THROTTLE_AUTH_REFRESH_RATE', '30/minute'),
        'auth_manage_ip': os.environ.get('THROTTLE_AUTH_MANAGE_IP_RATE', '60/minute'),
        'project_mutation': os.environ.get('THROTTLE_PROJECT_MUTATION_RATE', '60/minute'),
        'execute_prompt': os.environ.get('THROTTLE_EXECUTE_PROMPT_RATE', '12/minute'),
    },
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(
        minutes=int(os.environ.get('JWT_ACCESS_TOKEN_LIFETIME_MINUTES', '60')),
    ),
    'REFRESH_TOKEN_LIFETIME': timedelta(
        days=int(os.environ.get('JWT_REFRESH_TOKEN_LIFETIME_DAYS', '7')),
    ),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': os.environ.get('JWT_SIGNING_KEY', SECRET_KEY),
    'AUTH_HEADER_TYPES': ('Bearer',),
}

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        'CORS_ALLOWED_ORIGINS',
        'http://localhost:3000,http://127.0.0.1:3000',
    ).split(',')
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True
CORS_URLS_REGEX = r'^/api/.*$'
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS

REDIS_URL = os.environ.get('REDIS_URL', '').strip()
if REDIS_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                'hosts': [REDIS_URL],
            },
        },
    }
else:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        },
    }

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true'
CSRF_COOKIE_SECURE = os.environ.get('CSRF_COOKIE_SECURE', 'false').lower() == 'true'

MANAGED_PROJECTS_ROOT = os.environ.get('MANAGED_PROJECTS_ROOT', '')
MANAGED_PROJECTS_HOST_ROOT = os.environ.get('MANAGED_PROJECTS_HOST_ROOT', '')
MANAGED_PROJECTS_STORAGE_MODE = os.environ.get('MANAGED_PROJECTS_STORAGE_MODE', '')
GLOBAL_TEMPLATE_PATH = os.environ.get('GLOBAL_TEMPLATE_PATH', '')
OPENCODE_BINARY_PATH = os.environ.get('OPENCODE_BINARY_PATH', 'opencode')
OPENCODE_WEB_SUBCOMMAND = os.environ.get('OPENCODE_WEB_SUBCOMMAND', 'serve')
OPENCODE_SERVER_PASSWORD = os.environ.get('OPENCODE_SERVER_PASSWORD', '')
OPENCODE_REQUEST_TIMEOUT_SECONDS = int(os.environ.get('OPENCODE_REQUEST_TIMEOUT_SECONDS', '120'))
OPENCODE_CLIENT_MAX_RETRIES = int(os.environ.get('OPENCODE_CLIENT_MAX_RETRIES', '3'))
OPENCODE_CLIENT_BACKOFF_BASE_SECONDS = float(os.environ.get('OPENCODE_CLIENT_BACKOFF_BASE_SECONDS', '0.5'))
OPENCODE_CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get('OPENCODE_CIRCUIT_BREAKER_THRESHOLD', '5'))
OPENCODE_CIRCUIT_BREAKER_COOLDOWN_SECONDS = int(os.environ.get('OPENCODE_CIRCUIT_BREAKER_COOLDOWN_SECONDS', '15'))
PROJECT_COMPILE_COMMAND = os.environ.get('PROJECT_COMPILE_COMMAND', 'python -m compileall .')
PLAN_AGENT_NAME = os.environ.get('PLAN_AGENT_NAME', 'plan')
SUPERVISOR_AGENT_NAME = os.environ.get('SUPERVISOR_AGENT_NAME', 'supervisor')
ORCHESTRATION_ALLOWED_WORKER_AGENTS = os.environ.get(
    'ORCHESTRATION_ALLOWED_WORKER_AGENTS',
    'build,frontend-wizard,db-expert,code-reviewer,explore,scout,general',
)
ORCHESTRATION_FALLBACK_WORKER_AGENT = os.environ.get('ORCHESTRATION_FALLBACK_WORKER_AGENT', 'general')
OPENCODE_DAEMON_SANDBOX_MODE = os.environ.get('OPENCODE_DAEMON_SANDBOX_MODE', 'host').strip().lower()
OPENCODE_DAEMON_DOCKER_IMAGE = os.environ.get('OPENCODE_DAEMON_DOCKER_IMAGE', '').strip()
OPENCODE_DAEMON_CONTAINER_WORKDIR = os.environ.get('OPENCODE_DAEMON_CONTAINER_WORKDIR', '/workspace').strip() or '/workspace'
DAEMON_WATCHDOG_ENABLED = os.environ.get('DAEMON_WATCHDOG_ENABLED', 'true').lower() == 'true'
DAEMON_WATCHDOG_INTERVAL_SECONDS = int(os.environ.get('DAEMON_WATCHDOG_INTERVAL_SECONDS', '20'))
DAEMON_HEALTHCHECK_TIMEOUT_SECONDS = float(os.environ.get('DAEMON_HEALTHCHECK_TIMEOUT_SECONDS', '15'))
DAEMON_WATCHDOG_CONSECUTIVE_FAILURE_THRESHOLD = int(os.environ.get('DAEMON_WATCHDOG_CONSECUTIVE_FAILURE_THRESHOLD', '3'))
STUCK_RUN_THRESHOLD_SECONDS = int(os.environ.get('STUCK_RUN_THRESHOLD_SECONDS', '180'))
STUCK_RUN_SCAN_INTERVAL_SECONDS = int(os.environ.get('STUCK_RUN_SCAN_INTERVAL_SECONDS', '30'))
STUCK_RUN_MAX_RECOVERY_ATTEMPTS = int(os.environ.get('STUCK_RUN_MAX_RECOVERY_ATTEMPTS', '3'))

GITHUB_AUTOMATION_ENABLED = os.environ.get('GITHUB_AUTOMATION_ENABLED', 'false').lower() == 'true'
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_API_BASE_URL = os.environ.get('GITHUB_API_BASE_URL', 'https://api.github.com')
GITHUB_PR_BASE_BRANCH = os.environ.get('GITHUB_PR_BASE_BRANCH', 'dev')
GITHUB_COMMIT_PREFIX = os.environ.get('GITHUB_COMMIT_PREFIX', 'chore(foundry):')
GITHUB_REPO_PRIVATE = os.environ.get('GITHUB_REPO_PRIVATE', 'true').lower() == 'true'
GITHUB_MERGE_METHOD = os.environ.get('GITHUB_MERGE_METHOD', 'squash')
GITHUB_COMMITTER_NAME = os.environ.get('GITHUB_COMMITTER_NAME', 'Foundry AI')
GITHUB_COMMITTER_EMAIL = os.environ.get('GITHUB_COMMITTER_EMAIL', 'foundry-ai@users.noreply.github.com')

CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', REDIS_URL or 'redis://127.0.0.1:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', CELERY_BROKER_URL)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_BEAT_SCHEDULE = {
    'scan-stuck-runs': {
        'task': 'apps.workspaces.tasks.scan_stuck_runs_task',
        'schedule': timedelta(seconds=max(10, STUCK_RUN_SCAN_INTERVAL_SECONDS)),
    },
}

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
