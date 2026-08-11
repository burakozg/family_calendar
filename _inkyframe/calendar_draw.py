# calendar_draw.py — monthly calendar grid
# Uses ACeP 6-color palette: black, white, red, green, blue, yellow

_HEX_TO_PEN = {
    "#000000": "black",
    "#ffffff": "white",
    "#ff0000": "red",
    "#00ff00": "green",
    "#0000ff": "blue",
    "#ffff00": "yellow",
}

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def _draw_icon(display, icon, x, y):
    """Draw a ~9x9 pixel icon at (x,y) using the current pen."""
    if icon == "trash":
        display.line(x+3, y,   x+5, y)        # handle top
        display.line(x+3, y,   x+3, y+2)
        display.line(x+5, y,   x+5, y+2)
        display.line(x,   y+2, x+8, y+2)      # lid
        display.rectangle(x+1, y+3, 7, 6)     # body
        display.line(x+3, y+4, x+3, y+8)      # rib left
        display.line(x+5, y+4, x+5, y+8)      # rib right
    elif icon == "cake":
        display.line(x+4, y,   x+4, y+2)      # candle
        display.pixel(x+4, y)
        display.rectangle(x+2, y+3, 5, 2)     # top tier
        display.rectangle(x+1, y+5, 7, 4)     # base
    elif icon == "swim":
        for row, yo in enumerate([2, 5]):
            display.line(x,   y+yo,   x+2, y+yo-2)
            display.line(x+2, y+yo-2, x+4, y+yo)
            display.line(x+4, y+yo,   x+6, y+yo-2)
            display.line(x+6, y+yo-2, x+8, y+yo)
    elif icon == "gym":
        display.rectangle(x,   y+2, 2, 5)     # left weight
        display.rectangle(x+7, y+2, 2, 5)     # right weight
        display.line(x+2, y+4, x+7, y+4)      # bar
    elif icon == "football":
        display.rectangle(x+2, y,   5, 9)
        display.rectangle(x,   y+2, 9, 5)
        display.pixel(x+1, y+1); display.pixel(x+7, y+1)
        display.pixel(x+1, y+7); display.pixel(x+7, y+7)
    elif icon == "meeting":
        display.rectangle(x,   y,   9, 6)     # bubble
        display.line(x+2, y+6, x+2, y+8)      # tail
        display.line(x+2, y+8, x+5, y+6)
    elif icon == "doctor":
        display.line(x+4, y,   x+4, y+8)      # cross
        display.line(x+1, y+4, x+7, y+4)
    elif icon == "school":
        display.rectangle(x+1, y+3, 7, 6)     # building
        display.line(x,   y+3, x+4, y)        # roof left
        display.line(x+4, y,   x+8, y+3)      # roof right
        display.rectangle(x+3, y+5, 3, 4)     # door
    elif icon == "yoga":
        display.pixel(x+4, y)                  # head
        display.line(x+4, y+1, x+4, y+4)      # body
        display.line(x+1, y+3, x+7, y+3)      # arms
        display.line(x+4, y+4, x+2, y+8)      # left leg
        display.line(x+4, y+4, x+6, y+8)      # right leg
    elif icon == "travel":
        display.line(x,   y+4, x+8, y+4)      # fuselage
        display.line(x+6, y+4, x+8, y+2)      # nose up
        display.line(x+6, y+4, x+8, y+6)      # nose down
        display.line(x+2, y+1, x+5, y+4)      # wing up
        display.line(x+2, y+7, x+5, y+4)      # wing down
    elif icon == "bbq":
        display.line(x+3, y,   x+4, y+2)
        display.line(x+4, y+2, x+3, y+4)
        display.line(x+3, y+4, x+5, y+4)
        display.line(x+5, y+4, x+4, y+2)
        display.rectangle(x+1, y+5, 7, 2)     # grill
        display.line(x+2, y+7, x+2, y+9)
        display.line(x+6, y+7, x+6, y+9)
    elif icon == "date":
        display.pixel(x+1, y+2); display.pixel(x+2, y+1)
        display.pixel(x+3, y+1); display.pixel(x+4, y+2)
        display.pixel(x+5, y+1); display.pixel(x+6, y+1)
        display.pixel(x+7, y+2)
        display.line(x+1, y+3, x+7, y+3)
        display.line(x+2, y+4, x+6, y+4)
        display.line(x+3, y+5, x+5, y+5)
        display.pixel(x+4, y+6)
    elif icon == "star":
        display.pixel(x+4, y)
        display.line(x+3, y+2, x+5, y+2)
        display.line(x,   y+3, x+8, y+3)
        display.line(x+2, y+5, x+6, y+5)
        display.pixel(x+1, y+7); display.pixel(x+7, y+7)
        display.line(x+3, y+6, x+5, y+8)
    elif icon == "music":
        display.line(x+3, y,   x+3, y+6)      # left stem
        display.line(x+6, y+2, x+6, y+7)      # right stem
        display.line(x+3, y,   x+6, y+2)      # beam
        display.rectangle(x+1, y+6, 3, 2)     # left note
        display.rectangle(x+4, y+7, 3, 2)     # right note
    elif icon == "run":
        display.pixel(x+5, y)                  # head
        display.line(x+3, y+2, x+5, y+4)
        display.line(x+5, y+2, x+3, y+4)      # arms
        display.line(x+4, y+4, x+6, y+7)      # right leg
        display.line(x+4, y+4, x+2, y+7)      # left leg
    elif icon in ("volleyball", "beachvolley"):
        display.rectangle(x+2, y,   5, 9)
        display.rectangle(x,   y+2, 9, 5)
        display.pixel(x+1, y+1); display.pixel(x+7, y+1)
        display.pixel(x+1, y+7); display.pixel(x+7, y+7)
        display.line(x+4, y+1, x+4, y+7)      # vertical seam
        display.line(x+1, y+4, x+7, y+4)      # horizontal seam
        display.line(x+2, y+2, x+4, y+4)      # top-left curve
        display.line(x+4, y+4, x+6, y+6)      # bottom-right curve
    elif icon == "pizza":
        display.pixel(x+4, y)
        display.line(x+3, y+2, x+5, y+2)
        display.line(x+2, y+4, x+6, y+4)
        display.line(x+1, y+6, x+7, y+6)
        display.line(x,   y+8, x+8, y+8)
        display.pixel(x+4, y+3)               # toppings
        display.pixel(x+3, y+5); display.pixel(x+5, y+5)
    elif icon == "beer":
        display.pixel(x+2, y); display.pixel(x+4, y); display.pixel(x+6, y)  # foam
        display.line(x+1, y+1, x+7, y+1)
        display.line(x+1, y+1, x+1, y+8)      # left wall
        display.line(x+7, y+1, x+7, y+8)      # right wall
        display.line(x+1, y+8, x+7, y+8)      # bottom
        display.line(x+7, y+3, x+8, y+3)      # handle top
        display.line(x+8, y+3, x+8, y+6)
        display.line(x+7, y+6, x+8, y+6)      # handle bottom
    elif icon == "whisky":
        display.line(x+1, y+1, x+7, y+1)      # top rim
        display.line(x+1, y+1, x+2, y+8)      # left wall
        display.line(x+7, y+1, x+6, y+8)      # right wall
        display.line(x+2, y+8, x+6, y+8)      # bottom
        display.line(x+2, y+5, x+6, y+5)      # liquid level
        display.pixel(x+3, y+4); display.pixel(x+5, y+4)  # ice
    elif icon == "coffee":
        display.pixel(x+3, y); display.pixel(x+5, y)      # steam
        display.line(x+1, y+2, x+7, y+2)      # cup top
        display.line(x+1, y+2, x+2, y+7)      # left wall
        display.line(x+7, y+2, x+6, y+7)      # right wall
        display.line(x+2, y+7, x+6, y+7)      # bottom
        display.line(x+7, y+3, x+8, y+3)      # handle top
        display.line(x+8, y+3, x+8, y+6)
        display.line(x+7, y+6, x+8, y+6)      # handle bottom
        display.line(x+2, y+8, x+6, y+8)      # saucer
    elif icon == "cocktail":
        display.line(x+1, y+1, x+7, y+1)      # top rim
        display.line(x+1, y+1, x+4, y+5)      # left side
        display.line(x+7, y+1, x+4, y+5)      # right side
        display.line(x+4, y+5, x+4, y+7)      # stem
        display.line(x+2, y+8, x+6, y+8)      # base
        display.pixel(x+3, y+3); display.pixel(x+5, y+3)  # liquid
    elif icon == "car":
        display.line(x+2, y+1, x+6, y+1)      # roof
        display.line(x+1, y+2, x+7, y+2)      # upper body
        display.line(x,   y+3, x+8, y+3)      # body
        display.line(x,   y+4, x+8, y+4)
        display.line(x,   y+2, x,   y+4)      # front
        display.line(x+8, y+2, x+8, y+4)      # rear
        display.line(x+1, y+6, x+3, y+6)      # left wheel
        display.line(x+5, y+6, x+7, y+6)      # right wheel
    elif icon == "broom":
        display.line(x+8, y,   x+3, y+5)      # handle
        display.line(x+3, y+5, x+1, y+9)      # brush left edge
        display.line(x+3, y+5, x+6, y+9)      # brush right edge
        display.line(x+1, y+9, x+6, y+9)      # brush bottom
        display.line(x+3, y+6, x+3, y+9)      # centre bristle
    else:
        display.rectangle(x+2, y+2, 5, 5)     # fallback square



def _dot_hline(display, x, y, w, pen):
    display.set_pen(pen)
    for i in range(0, w, 3):
        display.pixel(x + i, y)

def _dot_vline(display, x, y, h, pen):
    display.set_pen(pen)
    for i in range(0, h, 3):
        display.pixel(x, y + i)


def _truncate(text, max_len):
    return text if len(text) <= max_len else text[:max_len - 1] + "~"


def _get_month_data(data, month_offset):
    """Adjust cells for month offset if needed."""
    # For now offset is handled server-side in future;
    # offset=0 always uses today's month from backend
    return data


def draw_calendar(display, data, pens, month_offset=0):
    cells  = data.get("cells", [])
    month  = data.get("month", 1)
    year   = data.get("year", 2026)
    legend = data.get("legend", [])

    # Use RTC for today's date so stale cache never shows the wrong day highlighted
    real_day = None
    real_month = month
    real_year  = year
    if month_offset == 0:
        try:
            import localtime_helper
            _t = localtime_helper.local_time()
            real_year, real_month, real_day = _t[0], _t[1], _t[2]
        except Exception:
            pass

    disp_month = real_month
    disp_year  = real_year

    # Layout
    grid_x = 4
    grid_y = 50
    grid_w = 792
    grid_h = 402
    rows   = max(len(cells) // 7, 1)
    col_w  = grid_w // 7
    row_h  = grid_h // rows

    # ── Title ─────────────────────────────────────────────────
    display.set_pen(pens["black"])
    display.text(MONTH_NAMES[disp_month] + " " + str(disp_year), grid_x, 2, scale=3)

    # ── Color lookup — direct palette dict, no heuristic ─────
    def color_pen(hex_color):
        key = (hex_color or "#000000").lower()
        pen_name = _HEX_TO_PEN.get(key, "black")
        return pens[pen_name]

    event_colors = data.get("event_colors", {"birthday": "#ffff00", "recurring": "#000000"})

    # Right-align legend flush with right edge of screen
    leg_items = data.get("legend", [])
    leg_total_w = sum(len(leg.get("label", "")) * 12 + 14 for leg in leg_items) - (6 if leg_items else 0)
    lx = max(300, grid_x + grid_w - leg_total_w)
    ly = 8
    for leg in leg_items:
        label = leg.get("label", "")
        bg = leg.get("color", "#000000")
        fg = leg.get("text_color", "#ffffff")
        # Birthday/holiday chips mirror the cells' colors straight from event_colors,
        # so the legend matches even if the cached legend text_color is stale.
        if label == "Birthday":
            bg = event_colors.get("birthday", bg)
            fg = event_colors.get("birthdayText", fg)
        elif label == "Holiday":
            bg = event_colors.get("holiday", bg)
            fg = event_colors.get("holidayText", fg)
        w = len(label) * 12 + 8
        display.set_pen(color_pen(bg))
        # 18 tall: 1px above the glyph cell, full 16px text (incl. descenders), 1px below
        display.rectangle(lx, ly - 1, w, 18)
        display.set_pen(color_pen(fg))
        display.text(label, lx + 3, ly, scale=2)
        lx += w + 6

    # ── Weekday headers ───────────────────────────────────────
    for i, name in enumerate(DAY_NAMES):
        display.set_pen(pens["black"])
        display.text(name, grid_x + i * col_w + 4, grid_y - 16, scale=2)

    # ── Cells ─────────────────────────────────────────────────
    for idx, cell in enumerate(cells):
        col = idx % 7
        row = idx // 7
        x = grid_x + col * col_w
        y = grid_y + row * row_h
        in_month = cell.get("current_month", True)
        if real_day is not None and in_month:
            is_today = (cell.get("day") == real_day and real_month == month and real_year == year)
        else:
            is_today = cell.get("today", False)
        # Passed days of THIS month (or any day of an earlier month), but not today
        is_past = in_month and not is_today and (
            month_offset < 0 or (real_day is not None and cell.get("day", 0) < real_day)
        )
        events   = cell.get("events", [])

        # Background
        display.set_pen(pens["white"])
        display.rectangle(x + 1, y + 1, col_w - 2, row_h - 2)

        # Off-month shading — diagonal hatch
        if not in_month:
            display.set_pen(pens["black"])
            dy = y + 2
            while dy < y + row_h - 2:
                dx = x + 2
                while (dx + dy) % 4 != 0:
                    dx += 1
                while dx < x + col_w - 2:
                    display.pixel(dx, dy)
                    dx += 4
                dy += 1

        # Passed-day shading — staggered dot pattern every 3px
        if is_past:
            display.set_pen(pens["black"])
            pr = 0
            dy = y + 1
            while dy < y + row_h - 1:
                dx = x + 1 + (pr % 3)
                while dx < x + col_w - 1:
                    display.pixel(dx, dy)
                    dx += 3
                dy += 3
                pr += 1

        # Border — dotted lines for grey effect
        _dot_hline(display, x, y, col_w, pens["black"])
        _dot_vline(display, x, y, row_h, pens["black"])

        # Today — red frame (3 px thick)
        if is_today:
            display.set_pen(pens["red"])
            for t in range(2):
                display.line(x + t, y + t, x + col_w - t, y + t)
                display.line(x + t, y + t, x + t, y + row_h - t)
                display.line(x + col_w - t, y + t, x + col_w - t, y + row_h - t)
                display.line(x + t, y + row_h - t, x + col_w - t, y + row_h - t)

        icon_only = [e for e in events if e.get("icon_only")]
        text_evs  = [e for e in events if not e.get("icon_only")]

        # Narrow white patch behind day number only — rest of row stays shaded
        # (digits at scale 2 end at y+15, so the patch runs 1px past, to y+16)
        if not in_month or is_past:
            display.set_pen(pens["white"])
            display.rectangle(x + 1, y + 1, 22, 16)
        display.set_pen(pens["red"] if cell.get("holiday") else pens["black"])
        day_str = str(cell.get("day", ""))
        display.text(day_str, x + 3, y + 2, scale=2)

        # Icons right-aligned on same line as day number
        if icon_only:
            ix = x + col_w + 1 - len(icon_only) * 12
            for e in icon_only:
                display.set_pen(color_pen(e.get("bg", "#000000")))
                _draw_icon(display, e.get("icon", ""), ix, y + 3)
                ix += 12

        # Events
        ev_y     = y + 20
        max_ev_y = y + row_h - 2
        max_chars = max(col_w // 8, 5)

        # Text events
        for ev in text_evs:
            if ev_y >= max_ev_y:
                display.set_pen(pens["black"])
                display.text("+", x + col_w - 12, max_ev_y - 14, scale=2)
                break

            ev_type = ev.get("type", "personal")
            label = _truncate(ev.get("label", ""), max_chars)
            if ev_type == "birthday":
                bg_pen   = color_pen(event_colors.get("birthday", "#ffff00"))
                text_pen = color_pen(event_colors.get("birthdayText", "#000000"))
            elif ev_type == "holiday":
                bg_pen   = color_pen(event_colors.get("holiday", "#ff0000"))
                text_pen = color_pen(event_colors.get("holidayText", "#ffffff"))
            else:
                bg_pen   = color_pen(ev.get("bg", "#000000"))
                text_pen = color_pen(ev.get("text_color", "#ffffff"))
            # Chip is 15 tall: caps at scale 2 end at ev_y+13, so the background
            # runs 1px past the text; pitch 16 keeps a 1px gap between chips.
            if is_past:
                # passed day: 50% background (white base + colour checkerboard), original text colour
                display.set_pen(pens["white"])
                display.rectangle(x + 2, ev_y, col_w - 4, 15)
                display.set_pen(bg_pen)
                ry = ev_y
                while ry < ev_y + 15:
                    rx = x + 2 + ((ry - ev_y) & 1)
                    while rx < x + col_w - 2:
                        display.pixel(rx, ry)
                        rx += 2
                    ry += 1
                display.set_pen(text_pen)
            else:
                # today / upcoming: regular solid background
                display.set_pen(bg_pen)
                display.rectangle(x + 2, ev_y, col_w - 4, 15)
                display.set_pen(text_pen)
            display.text(label, x + 3, ev_y, scale=2)
            ev_y += 16

    # Close grid — right column edge and bottom row edge
    _dot_vline(display, grid_x + 7 * col_w, grid_y, rows * row_h, pens["black"])
    _dot_hline(display, grid_x, grid_y + rows * row_h, 7 * col_w + 1, pens["black"])
