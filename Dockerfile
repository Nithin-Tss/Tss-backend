FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --create-home app \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R app:app /app
USER app

# Settings need a secret key at import time; this build-only value is not used at runtime.
RUN DJANGO_SECRET_KEY=build-only DJANGO_DEBUG=True python manage.py collectstatic --noinput

EXPOSE 8000

# gunicorn.conf.py is picked up automatically from the working directory
CMD ["sh", "-c", "gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000}"]
