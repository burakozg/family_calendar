#!/bin/sh
# Make the data volume writable by the non-root 'relay' user, then drop privileges.
set -e
DIR="$(dirname "${RELAY_DATA:-/data/relay.json}")"
mkdir -p "$DIR" 2>/dev/null || true
chown -R relay:relay "$DIR" 2>/dev/null || true   # best-effort; ignore on read-only mounts
exec gosu relay uvicorn main:app --host 0.0.0.0 --port 8080
