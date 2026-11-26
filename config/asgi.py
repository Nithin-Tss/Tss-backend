"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

Django serves everything, except the AI assistant's FastAPI app, which is mounted under /fastapi/ai-assistant
(apps/ai_assistant/fastapi_app.py). Run this with an ASGI server, e.g. `uvicorn config.asgi:application`.
Production runs gunicorn + config.wsgi (Procfile, Dockerfile), which maps the same paths onto the DRF views instead,
so the endpoints work either way; only FastAPI's /docs and /openapi.json need this ASGI entry point.

Don't switch production to this file without testing the theme live-sync stream (apps/themes/views.py
ThemeEventsView): under ASGI, Django reads a sync StreamingHttpResponse to the end before sending any of it.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

django_application = get_asgi_application()  # also sets Django up, which the FastAPI app's imports need

from apps.ai_assistant.fastapi_app import PREFIX as AI_PREFIX, app as ai_fastapi_app  # noqa: E402


async def application(scope, receive, send):
    if scope['type'] in ('http', 'websocket'):
        path = scope['path']
        if path == AI_PREFIX or path.startswith(AI_PREFIX + '/'):
            # Starlette strips root_path from the path to find its route
            scope = {**scope, 'root_path': scope.get('root_path', '').rstrip('/') + AI_PREFIX}
            return await ai_fastapi_app(scope, receive, send)
    return await django_application(scope, receive, send)
