# localtime_helper.py — Stockholm local time with automatic CET/CEST (EU DST).
#
# The RTC is UTC (set via NTP). Europe/Stockholm is UTC+1 (CET) in winter and
# UTC+2 (CEST) in summer. EU DST switches at 01:00 UTC on the last Sunday of
# March (spring forward) and the last Sunday of October (fall back).

import utime


def _last_sunday(year, month):
    """Day-of-month of the last Sunday in the given month (March/October: 31 days)."""
    for day in range(31, 24, -1):
        # weekday index 6 == Sunday in MicroPython's localtime()
        if utime.localtime(utime.mktime((year, month, day, 12, 0, 0, 0, 0)))[6] == 6:
            return day
    return 25


def tz_offset_hours():
    """Current Stockholm UTC offset in hours: 2 (CEST summer) or 1 (CET winter)."""
    t = utime.localtime()  # RTC is UTC
    year, month, day, hour = t[0], t[1], t[2], t[3]
    if month < 3 or month > 10:
        return 1
    if 3 < month < 10:
        return 2
    if month == 3:                       # DST begins last Sunday, 01:00 UTC
        start = _last_sunday(year, 3)
        return 2 if (day > start or (day == start and hour >= 1)) else 1
    end = _last_sunday(year, 10)         # month == 10, DST ends last Sunday, 01:00 UTC
    return 1 if (day > end or (day == end and hour >= 1)) else 2


def local_time():
    """Broken-down local time tuple for Stockholm (DST-aware)."""
    return utime.localtime(utime.time() + tz_offset_hours() * 3600)
