"""
Gunicorn settings, loaded automatically from the project root.

The code editor / customizer live-sync keeps a Server-Sent Events stream open per
browser tab (apps/themes/views.py ThemeEventsView) and its event bus lives in this
process's memory. So: ONE process (events must be shared), many threads (a stream
parks a thread, not the whole server). With the default sync worker a single open
stream blocked every other request, including sign-in, until it hit WORKER TIMEOUT.
"""
import os

worker_class = "gthread"
workers = int(os.getenv("WEB_CONCURRENCY", "1"))
threads = int(os.getenv("GUNICORN_THREADS", "32"))
# Longer than the 55s stream plus slow cold queries; threads keep the heartbeat alive
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5
