#!/bin/sh
# Browsers only allow microphone access over HTTPS (or on localhost), so by default
# serve TLS with a self-signed certificate created on first start.
set -e
DATA="${PUMPKIN_DATA_DIR:-/data}"
if [ "${PUMPKIN_TLS:-1}" = "1" ]; then
  CERTS="$DATA/certs"
  mkdir -p "$CERTS"
  if [ ! -f "$CERTS/cert.pem" ]; then
    HOST="${PUMPKIN_HOSTNAME:-pumpkin.local}"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$HOST" \
      -addext "subjectAltName=DNS:$HOST,DNS:localhost,IP:127.0.0.1" \
      -keyout "$CERTS/key.pem" -out "$CERTS/cert.pem" 2>/dev/null
    echo "created self-signed certificate for $HOST in $CERTS"
  fi
  exec uvicorn pumpkin.main:app --host 0.0.0.0 --port "${PUMPKIN_PORT:-8443}" \
    --ssl-keyfile "$CERTS/key.pem" --ssl-certfile "$CERTS/cert.pem"
fi
exec uvicorn pumpkin.main:app --host 0.0.0.0 --port "${PUMPKIN_PORT:-8080}"
