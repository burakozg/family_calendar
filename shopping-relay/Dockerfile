FROM python:3.11-slim

# gosu lets the entrypoint fix the volume's ownership as root, then drop to a
# non-root user to actually run the app (defence-in-depth if the app is exploited).
RUN pip install --no-cache-dir fastapi==0.139.0 uvicorn==0.50.0 \
 && apt-get update && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10001 relay && useradd -u 10001 -g relay -M -r relay

WORKDIR /app
COPY main.py shop.html app.js manifest.webmanifest sw.js entrypoint.sh ./
COPY icon-192.png icon-512.png apple-touch-icon.png ./
RUN chmod +x entrypoint.sh

ENV RELAY_DATA=/data/relay.json
EXPOSE 8080
# The app writes ONLY to the data volume, so this image is compatible with a
# read-only root filesystem (e.g. `docker run --read-only -v relay:/data ...`).
ENTRYPOINT ["/app/entrypoint.sh"]
