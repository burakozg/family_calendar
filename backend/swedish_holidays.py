"""Swedish public holidays ("röda dagar") computed locally — no API, no network.

Fixed calendar dates + Western Easter (Gauss/Computus algorithm) + the two
floating Saturdays (Midsummer, All Saints). The household runs on
Europe/Stockholm; names are in Swedish, the way a Swedish wall calendar prints
them. Includes the de-facto red eves (Midsommarafton, Julafton, Nyårsafton)
that Swedish calendars mark red alongside the statutory holidays."""
from datetime import date, timedelta


def _easter_sunday(year: int) -> date:
    """Gregorian (Western) Easter Sunday — Gauss/Anonymous algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month = (h + L - 7 * m + 114) // 31
    day = ((h + L - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _first_saturday_on_or_after(d: date) -> date:
    while d.weekday() != 5:                     # Mon=0 … Sat=5
        d += timedelta(days=1)
    return d


def swedish_holidays(year: int) -> dict:
    """Map of 'YYYY-MM-DD' → Swedish holiday name for the given year."""
    easter = _easter_sunday(year)
    midsummer_day = _first_saturday_on_or_after(date(year, 6, 20))   # Sat in Jun 20–26
    all_saints    = _first_saturday_on_or_after(date(year, 10, 31))  # Sat in Oct 31–Nov 6
    days = {
        date(year, 1, 1):                 "Nyårsdagen",
        date(year, 1, 6):                 "Trettondedag jul",
        easter - timedelta(days=2):       "Långfredagen",
        easter:                           "Påskdagen",
        easter + timedelta(days=1):       "Annandag påsk",
        date(year, 5, 1):                 "Första maj",
        easter + timedelta(days=39):      "Kristi himmelsfärdsdag",
        easter + timedelta(days=49):      "Pingstdagen",
        date(year, 6, 6):                 "Sveriges nationaldag",
        midsummer_day - timedelta(days=1): "Midsommarafton",
        midsummer_day:                    "Midsommardagen",
        all_saints:                       "Alla helgons dag",
        date(year, 12, 24):               "Julafton",
        date(year, 12, 25):               "Juldagen",
        date(year, 12, 26):               "Annandag jul",
        date(year, 12, 31):               "Nyårsafton",
    }
    return {d.isoformat(): name for d, name in days.items()}
