# main.py
# Family Calendar — Pico 2 W
#
# Battery-first runtime: the board is fully powered OFF between refreshes
# (~µA — the VSYS hold latch is released). Wake sources:
#   - any front button: hardware powers the board on and latches which button
#     was pressed into the shift register (read at boot),
#   - the PCF85063A RTC alarm, armed for 00:01 local time (daily refresh).
# Each wake: act on the wake cause, fetch + draw once, re-arm the alarm,
# power off. On USB power VSYS can't be cut, so after "turn_off" execution
# simply continues — we then fall back to the old always-on polling loop,
# which keeps the desk/dev workflow identical.
#
# The PCF85063A keeps time across power-off (it's battery-backed) and is
# synced from NTP after each successful fetch. It runs on UTC; the 00:01
# local alarm is converted via localtime_helper (CET/CEST aware).
#
# Buttons:
#   A = Previous month
#   B = Home (this month)
#   C = Next month
#   D = Today's recipe
#   E = Tomorrow's recipe

import time
import inky_frame
from picographics import PicoGraphics, DISPLAY_INKY_FRAME_7
import network as net_mod

import network_fetch
import calendar_draw
import meals_draw
import localtime_helper


display = PicoGraphics(DISPLAY_INKY_FRAME_7)
WIDTH, HEIGHT = display.get_bounds()

STATE_FILE = "state.txt"


def make_pens(d):
    # Raw palette indices empirically confirmed for this device
    return {
        "black":  0,
        "white":  1,
        "yellow": 2,
        "red":    3,
        "blue":   5,
        "green":  6,
    }


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            parts = f.read().strip().split(",")
            screen = parts[0] if parts[0] in ("calendar","today","tomorrow") else "calendar"
            offset = int(parts[1]) if len(parts) > 1 else 0
            return screen, offset
    except Exception:
        return "calendar", 0


def save_state(screen, offset):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(screen + "," + str(offset))
    except Exception:
        pass


def disconnect_wifi():
    """Radio fully off — idle connected WiFi costs ~20 mA, a third of the
    battery budget. Called before every sleep/power-off."""
    try:
        wlan = net_mod.WLAN(net_mod.STA_IF)
        wlan.disconnect()
        wlan.active(False)
        try:
            wlan.deinit()
        except Exception:
            pass
        print("WiFi off")
    except Exception:
        pass


def draw_screen(data, screen, week_offset):
    display.set_pen(1)  # white
    display.clear()
    pens = make_pens(display)

    if screen == "calendar":
        calendar_draw.draw_calendar(display, data, pens, week_offset)
    elif screen == "today":
        meals_draw.draw_meals_full(display, data, pens, "today")
    elif screen == "tomorrow":
        meals_draw.draw_meals_full(display, data, pens, "tomorrow")

    display.set_font("bitmap8")
    # Separator above the button tags so screen content reads as its own block
    # (the meals screen especially ran right into the tag row).
    display.set_pen(pens["black"])
    display.line(0, HEIGHT - 17, WIDTH, HEIGHT - 17)
    labels = ["< 4 Weeks", "Home", "4 Weeks >", "Today's Recipe", "Tomorrow's Recipe"]
    slot = WIDTH // len(labels)
    # Which tag is the currently-open page?
    if screen == "today":
        active = 3
    elif screen == "tomorrow":
        active = 4
    else:  # calendar
        active = {-4: 0, 0: 1, 4: 2}.get(week_offset, 1)
    for i, label in enumerate(labels):
        tw = display.measure_text(label, 1)
        x  = i * slot + (slot - tw) // 2
        y  = HEIGHT - 10
        if i == active:
            display.set_pen(pens["black"])
            display.rectangle(x - 4, y - 3, tw + 8, 13)   # black background
            display.set_pen(pens["white"])
            display.text(label, x, y, scale=1)            # white text
        else:
            display.set_pen(pens["black"])
            display.text(label, x, y, scale=1)

    if data.get("_offline"):
        display.set_pen(pens["red"])
        display.rectangle(WIDTH - 72, 2, 70, 18)
        display.set_pen(pens["white"])
        display.text("OFFLINE", WIDTH - 70, 4, scale=1)

    print("Updating display...")
    display.update()
    print("Done")


def draw_no_data():
    display.set_pen(1)  # white
    display.clear()
    display.set_pen(3)  # red
    display.text("No data available", 20, 180, scale=3)
    display.set_pen(0)  # black
    display.text("WiFi failed and no cached data found.", 20, 230, scale=2)
    display.text("Check secrets.py and NAS connection.", 20, 265, scale=2)
    display.update()


def clock_valid():
    """True once the Pico RTC holds a real (NTP- or PCF-restored) date."""
    return time.localtime()[0] >= 2025


def fetch_and_draw(screen, week_offset):
    inky_frame.button_a.led_on()
    network_fetch.connect_wifi()
    data = network_fetch.fetch_data(week_offset)
    inky_frame.button_a.led_off()

    # NTP (in connect_wifi) set the Pico RTC; mirror it into the battery-backed
    # PCF85063A so the midnight alarm and post-wake clock stay correct.
    if clock_valid():
        try:
            inky_frame.pico_rtc_to_pcf()
        except Exception as e:
            print("PCF sync failed:", e)

    if data is None:
        draw_no_data()
        return False

    draw_screen(data, screen, week_offset)
    return True


def secs_until_0001():
    """Seconds until 00:01 local time (Stockholm), DST-aware."""
    t = localtime_helper.local_time()
    now_secs = t[3] * 3600 + t[4] * 60 + t[5]
    target   = 60  # 00:01:00
    if now_secs < target:
        return target - now_secs
    return 86400 - now_secs + target


def arm_wake_alarm():
    """Arm the PCF85063A to wake the board at 00:01 local time (daily refresh).
    The PCF runs on UTC, so 00:01 local is (24 - tz_offset):01 UTC. If the clock
    was never synced (no NTP yet), fall back to a 60-minute timer wake to retry."""
    rtc = inky_frame.rtc
    try:
        rtc.clear_timer_flag()
        rtc.unset_timer()
        rtc.clear_alarm_flag()
        if not clock_valid():
            print("Clock unsynced — timer wake in 60 min")
            rtc.set_timer(60, ttp=rtc.TIMER_TICK_1_OVER_60HZ)
            rtc.enable_timer_interrupt(True)
            return
        utc_hour = (24 - localtime_helper.tz_offset_hours()) % 24
        rtc.set_alarm(0, 1, utc_hour)          # daily at hh:01 UTC == 00:01 local
        rtc.enable_alarm_interrupt(True)
        print("RTC alarm armed for {:02d}:01 UTC".format(utc_hour))
    except Exception as e:
        print("Alarm arm failed:", e)


def wake_button():
    """Which front button caused this power-on (latched in the shift register),
    or None for an RTC-alarm wake / cold boot / USB reset."""
    for name, btn in (("a", inky_frame.button_a), ("b", inky_frame.button_b),
                      ("c", inky_frame.button_c), ("d", inky_frame.button_d),
                      ("e", inky_frame.button_e)):
        try:
            if btn.read():
                return name
        except Exception:
            pass
    return None


# Button → (screen, week_offset). The calendar is a rolling 4-week window anchored
# on the current week, so these slide it a whole screenful at a time. Absolute
# offsets from the real current week, not relative to what's displayed.
ACTIONS = {
    "a": ("calendar", -4),
    "b": ("calendar", 0),
    "c": ("calendar", 4),
    "d": ("today", 0),
    "e": ("tomorrow", 0),
}


def usb_loop(current_screen, week_offset):
    """USB power only: VSYS can't be cut, so keep the old polling loop
    (buttons + midnight refresh). Never reached on battery."""
    while True:
        secs = secs_until_0001() if clock_valid() else 3600
        print("USB loop: waiting for button or midnight ({:.0f}s)...".format(secs))
        deadline = time.time() + secs
        btn = None
        while time.time() < deadline:
            for name, b in (("a", inky_frame.button_a), ("b", inky_frame.button_b),
                            ("c", inky_frame.button_c), ("d", inky_frame.button_d),
                            ("e", inky_frame.button_e)):
                if b.is_pressed:
                    btn = name
                    break
            if btn:
                break
            time.sleep(0.1)

        if btn is None:
            print("Midnight refresh")
        else:
            print("Button", btn.upper())
            current_screen, week_offset = ACTIONS[btn]
        fetch_and_draw(current_screen, week_offset)
        save_state(current_screen, week_offset)
        disconnect_wifi()
        time.sleep(0.5)   # button release


def run():
    print("Family Calendar starting...")

    # Restore real time from the battery-backed PCF (the Pico RTC resets to
    # 2021 on every power-on). Skipped while the PCF itself is unsynced.
    try:
        if inky_frame.rtc.datetime()[0] >= 2025:
            inky_frame.pcf_to_pico_rtc()
    except Exception:
        pass

    current_screen, week_offset = load_state()
    btn = wake_button()
    if btn:
        print("Woken by button", btn.upper())
        current_screen, week_offset = ACTIONS[btn]
    elif inky_frame.woken_by_rtc():
        print("Woken by RTC alarm — daily refresh")
    else:
        print("Cold boot / USB start")

    fetch_and_draw(current_screen, week_offset)
    save_state(current_screen, week_offset)

    # Everything below is battery hygiene: radio off, wake alarm armed,
    # then release the power latch. On battery, execution ENDS here.
    disconnect_wifi()
    arm_wake_alarm()
    print("Powering off (battery) / falling back to USB loop...")
    time.sleep(0.1)
    inky_frame.turn_off()

    # Still running → we're on USB power.
    usb_loop(current_screen, week_offset)


run()
