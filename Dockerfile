FROM python:3.12-slim

# Optional: mutagen lets Crate read tags and bitrates when checking your library.
RUN pip install --no-cache-dir mutagen

WORKDIR /app
COPY crate ./crate

ENV CRATE_DB=/data/crate.db \
    CRATE_SLSKD_DOWNLOADS_DIR=/downloads \
    CRATE_LIBRARY_DIR=/music \
    PYTHONUNBUFFERED=1

# /data holds the SQLite database. Make it writable for any user the container
# is started as (compose sets user: PUID:PGID).
RUN mkdir -p /data && chmod 777 /data

EXPOSE 9325
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9325/api/status', timeout=4)"
CMD ["python", "-m", "crate", "--port", "9325"]
