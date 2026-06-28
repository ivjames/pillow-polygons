# Pillow Polygons — production image.
# Hardening that lives here: slim base, non-root user, no build toolchain in the
# final layer. The rest (read-only rootfs, dropped caps, seccomp, resource limits)
# is applied at run time — see docker-compose.yml and DEPLOY.md.

FROM python:3.12-slim AS base

# Fonts referenced by the scene generator (it falls back gracefully if absent,
# but LiberationMono is one of the paths in the system prompt).
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user. The writable paths (DB, renders, uploads) are
# supplied as mounted volumes/tmpfs at run time, so the image itself stays read-only.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser \
 && mkdir -p /data /app/static/renders /app/static/uploads /jobs/incoming /jobs/done \
 && chown -R appuser:appuser /data /app/static/renders /app/static/uploads /jobs
USER appuser

ENV POLY_DB_PATH=/data/poly.db \
    PORT=8040 \
    HOME=/tmp

EXPOSE 8040

# One worker keeps the in-process rate-limit/spam state coherent; threads give
# concurrency. Scale out only with a shared store (Redis) — see README.
# --worker-tmp-dir /tmp keeps gunicorn's heartbeat file on tmpfs so the rest of
# the filesystem can stay read-only. Bind to all interfaces inside the container;
# publish to 127.0.0.1 in compose.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 60 --worker-tmp-dir /tmp --bind 0.0.0.0:${PORT} app:app"]
