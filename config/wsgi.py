"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

This is what production runs (Procfile, Dockerfile, docker-compose: gunicorn config.wsgi:application). The AI
assistant's FastAPI app (apps/ai_assistant/fastapi_app.py) can't run here, so its paths are mapped onto the DRF
views that serve the same endpoints with the same code:

    /fastapi/ai-assistant/<rest>   ->   /api/v1/ai-assistant/<rest>/

Everything else, including the /api/ middleware (auth, audit log, request validation, security headers), then
applies as for any API call. config/asgi.py keeps sending those paths to FastAPI under an ASGI server.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Same value as apps.ai_assistant.fastapi_app.PREFIX (not imported, so gunicorn doesn't load FastAPI)
AI_FASTAPI_PREFIX = '/fastapi/ai-assistant'
AI_DRF_PREFIX = '/api/v1/ai-assistant'


def ai_assistant_path(path):
    """The DRF path for a /fastapi/ai-assistant/... path, or None when the path isn't one of those."""
    if path != AI_FASTAPI_PREFIX and not path.startswith(AI_FASTAPI_PREFIX + '/'):
        return None
    rest = path[len(AI_FASTAPI_PREFIX):].strip('/')
    return f'{AI_DRF_PREFIX}/{rest}/' if rest else f'{AI_DRF_PREFIX}/'


django_application = get_wsgi_application()


def application(environ, start_response):
    mapped = ai_assistant_path(environ.get('PATH_INFO', ''))
    if mapped is not None:
        environ = {**environ, 'PATH_INFO': mapped}
    return django_application(environ, start_response)
