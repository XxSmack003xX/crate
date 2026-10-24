FROM python:3.12-slim

ARG CRATE_REPOSITORY=XxSmack003xX/crate

# Optional: mutagen lets Crate read tags and bitrates when checking your library.
RUN pip install --no-cache-dir mutagen

WORKDIR /app
COPY crate ./crate

# CRATE_RUNTIME/CRATE_UPDATE_REPO tell the in-app updater it's the official image and
# which GitHub releases (and ghcr.io image) to follow.
ENV CRATE_DB=/data/crate.db \
    CRATE_SLSKD_DOWNLOADS_DIR=/downloads \
    CRATE_LIBRARY_DIR=/music \
    CRATE_RUNTIME=docker \
    CRATE_UPDATE_REPO=${CRATE_REPOSITORY} \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data && chmod 777 /data

EXPOSE 9325
VOLUME ["/data"]
# /api/version needs no sign-in.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9325/api/version', timeout=4)"
# Starts as root just long enough to join the Docker socket's group (one-click updates)
# and fix /data ownership, then runs Crate as PUID:PGID.
ENTRYPOINT ["python", "-m", "crate.entrypoint"]
CMD ["python", "-m", "crate", "--port", "9325"]
