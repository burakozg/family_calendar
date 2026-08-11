#!/bin/sh
# Run the backend test suite. Needs requirements.txt + requirements-dev.txt
# installed (e.g. in a venv):  pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cd "$(dirname "$0")/backend" && exec python3 -m pytest tests -q "$@"
