FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATTENDQR_ENV=production

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 attendqr \
    && mkdir -p /app/uploads \
    && chown -R attendqr:attendqr /app

COPY --chown=attendqr:attendqr . ./
RUN chown -R attendqr:attendqr /app
USER attendqr
EXPOSE 5001
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
