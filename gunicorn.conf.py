"""Production Gunicorn configuration for AttendQR."""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5001')}"
workers = max(1, int(os.environ.get("WEB_CONCURRENCY", "4")))
threads = max(1, int(os.environ.get("GUNICORN_THREADS", "2")))
worker_class = "gthread"
timeout = 60
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
capture_output = True
