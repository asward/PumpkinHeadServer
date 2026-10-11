# Runs on a Raspberry Pi (arm64). Build on the Pi, or cross-build with
#   docker buildx build --platform linux/arm64 -t pumpkinhead .
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libportaudio2 alsa-utils openssl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 # lgpio ships arm wheels only; on other platforms GPIO falls back to the mock
 && (pip install --no-cache-dir lgpio || echo "lgpio not available on $(uname -m); GPIO will be mocked")

COPY pumpkin ./pumpkin
COPY web ./web
COPY docker/entrypoint.sh /entrypoint.sh

ENV PUMPKIN_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8443
ENTRYPOINT ["/entrypoint.sh"]
