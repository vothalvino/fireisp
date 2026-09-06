FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock .
RUN pip install --require-hashes --requirement requirements.lock && groupadd --gid 1000 fireisp && useradd --uid 1000 --gid 1000 --create-home fireisp
COPY --chown=1000:1000 . .
RUN DEBUG=true python manage.py collectstatic --noinput && mkdir -p /data/documents && chown -R fireisp:fireisp /data /app/staticfiles
USER 1000:1000
EXPOSE 8000
CMD ["gunicorn", "fireisp.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--timeout", "100", "--max-requests", "1000", "--max-requests-jitter", "100"]
