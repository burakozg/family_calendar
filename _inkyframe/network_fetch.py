# network_fetch.py
# WiFi connect + fetch with local cache fallback
# If NAS is unreachable, returns last cached data so display still works

import network
import urequests
import json
import time
import secrets

NAS_URL       = secrets.NAS_URL
# ascii=1: fold the payload to ASCII server-side. PicoGraphics' bitmap8 font only
# has glyphs for ASCII 32-126, so Turkish (ı ş ğ ç ö ü) and Swedish (å ä ö) render
# as garbage. The web UI and phone still get proper Unicode — only we ask for this.
ASCII_ONLY    = True
WIFI_TIMEOUT  = 20
FETCH_RETRIES = 3
FETCH_DELAY   = 5
CACHE_FILE    = "display_cache.json"

_wlan = network.WLAN(network.STA_IF)


def connect_wifi():
    """Connect to WiFi and sync NTP. Returns True if connected."""
    _wlan.active(True)
    if _wlan.isconnected():
        print("WiFi already connected")
        return True
    print("Connecting to WiFi...")
    _wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
    for i in range(WIFI_TIMEOUT):
        if _wlan.isconnected():
            print("WiFi OK:", _wlan.ifconfig()[0])
            try:
                import ntptime
                ntptime.settime()
                print("NTP synced")
            except Exception as e:
                print("NTP failed:", e)
            return True
        time.sleep(1)
        print(".", end="")
    print("\nWiFi failed")
    return False


def fetch_data(week_offset=0):
    """
    Fetch display data with retries before falling back to cache.
    On success: saves to local cache and returns data.
    On failure after all retries: returns cached data if available, else None.
    """
    params = []
    if week_offset != 0:
        params.append("week_offset=" + str(week_offset))
    if ASCII_ONLY:
        params.append("ascii=1")
    url = NAS_URL + ("?" + "&".join(params) if params else "")
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            print("Fetching (attempt {}/{}):".format(attempt, FETCH_RETRIES), url)
            r = urequests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                r.close()
                print("Fetch OK")
                if week_offset == 0:
                    _save_cache(data)
                data["_offline"] = False
                return data
            print("HTTP error:", r.status_code)
            r.close()
        except Exception as e:
            print("Fetch error:", e)
        if attempt < FETCH_RETRIES:
            print("Retrying in {}s...".format(FETCH_DELAY))
            time.sleep(FETCH_DELAY)

    print("All attempts failed, using cache")
    return _load_cache()


def _save_cache(data):
    try:
        with open(CACHE_FILE, "w") as f:
            # Store a stripped version — drop large cells list detail
            json.dump(data, f)
        print("Cache saved")
    except Exception as e:
        print("Cache save failed:", e)


def _load_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        print("Using cached data")
        data["_offline"] = True
        return data
    except Exception:
        print("No cache available")
        return None


def cache_exists():
    try:
        with open(CACHE_FILE, "r") as f:
            pass
        return True
    except Exception:
        return False
