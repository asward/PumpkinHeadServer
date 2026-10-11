# Runs on a Raspberry Pi (arm64). Build on the Pi, or cross-build with
#   docker buildx build --platform linux/arm64 -t pumpkinhead .
FROM python:3.12-slim-bookworm

# On arm64, add Raspberry Pi's apt repo for rpicam-apps (libcamera), which the Pi CSI
# camera needs (PUMPKIN_CAMERA_SOURCE=rpicam). Other platforms skip it.
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gpg \
 && if [ "${TARGETARCH:-$(dpkg --print-architecture)}" = "arm64" ]; then \
      curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/raspberrypi.gpg \
   && echo "deb [signed-by=/usr/share/keyrings/raspberrypi.gpg] https://archive.raspberrypi.com/debian bookworm main" \
        > /etc/apt/sources.list.d/raspi.list \
   && apt-get update \
   && apt-get install -y --no-install-recommends rpicam-apps-lite; \
    fi \
 && apt-get install -y --no-install-recommends ffmpeg libportaudio2 alsa-utils openssl \
 && apt-get purge -y --auto-remove curl gpg \
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
