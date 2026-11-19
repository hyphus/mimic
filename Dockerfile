FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV MIMIC_DATA_DIR=/data
ENV MIMIC_PUBLIC_BIND=0.0.0.0:8080
ENV MIMIC_ADMIN_BIND=0.0.0.0:8443

# After admin module toggles, signal Gunicorn master so all workers reload config (set 0 to disable).
ENV MIMIC_GUNICORN_RELOAD_HUP=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gunicorn.conf.py .
COPY mimic/ ./mimic/

RUN useradd --system --uid 10001 mimic && mkdir -p /data && chown mimic /data

VOLUME ["/data"]

USER mimic

CMD ["gunicorn"]
