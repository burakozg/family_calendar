"""Test bootstrap: point the app at a throwaway data dir BEFORE importing main
(main reads env at import time), keep the AI disabled, and allow the
TestClient's default Host ("testserver") through the allowlist."""
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="famcal-test-")
# Never call the AI from tests, even if the host environment happens to have a
# key set (e.g. an ambient MISTRAL_API_KEY/OPENROUTER_API_KEY on the machine).
os.environ["MISTRAL_API_KEY"] = ""
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["ALLOWED_HOSTS"] = "localhost,testserver,family-calendar.servers.zou"
os.environ.pop("SHOP_RELAY_URL", None)          # never talk to a relay
os.environ.pop("SHOP_RELAY_PUBLISH_TOKEN", None)
# Mailsync: configured (so unit tests exercise it) but disabled in settings, so
# the loop never talks to the network. Hosts point at invalid domains anyway.
os.environ["MAILSYNC_ADDRESS"] = "family-cal@test.invalid"
os.environ["MAILSYNC_LOGIN"] = "account@test.invalid"
os.environ["MAILSYNC_PASSWORD"] = "test-password"
os.environ["MAILSYNC_CALDAV_URL"] = "https://dav.test.invalid/caldav/CAL/"
os.environ["MAILSYNC_IMAP_HOST"] = "imap.test.invalid"
os.environ["MAILSYNC_SMTP_HOST"] = "smtp.test.invalid"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture()
def client():
    # Context manager runs startup (seeds defaults, builds the cache).
    with TestClient(main.app) as c:
        yield c
