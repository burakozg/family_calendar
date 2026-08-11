# meals_draw.py — meal plan, two modes:
#   draw_meals()      — compact footer strip on calendar
#   draw_meals_full() — full screen when button B pressed:
#                       left = week list (today highlighted), right = today's recipe

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _today_dow(today_str):
    try:
        import utime
        parts = today_str.split("-")
        t = utime.mktime((int(parts[0]), int(parts[1]), int(parts[2]), 0, 0, 0, 0, 0))
        return utime.localtime(t)[6]
    except Exception:
        return -1


def _wrap_lines(display, text, max_w, scale):
    """Greedy word-wrap using the display's own text measurement."""
    lines = []
    cur = ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if display.measure_text(trial, scale) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines if lines else [""]


def draw_meals(display, meal_plan, today_str, pens):
    """Compact footer strip at bottom of calendar (y=418..476)."""
    footer_y = 418
    footer_h = 54
    footer_x = 4
    footer_w = 792
    col_w    = footer_w // 7
    dow      = _today_dow(today_str)

    display.set_pen(pens["white"])
    display.rectangle(footer_x, footer_y, footer_w, footer_h)

    display.set_pen(pens["black"])
    display.line(footer_x, footer_y, footer_x + footer_w, footer_y)

    for i in range(7):
        x        = footer_x + i * col_w
        meal     = meal_plan[i] if i < len(meal_plan) else {}
        name     = meal.get("name", "") if isinstance(meal, dict) else ""
        is_today = (i == dow)
        max_chars = col_w // 8

        if i > 0:
            display.set_pen(pens["black"])
            display.line(x, footer_y + 4, x, footer_y + footer_h - 4)

        if is_today:
            display.set_pen(pens["red"])
            for t in range(2):
                display.line(x + t, footer_y + t, x + col_w - t, footer_y + t)
                display.line(x + t, footer_y + t, x + t, footer_y + footer_h - t)
                display.line(x + col_w - t, footer_y + t, x + col_w - t, footer_y + footer_h - t)
                display.line(x + t, footer_y + footer_h - t, x + col_w - t, footer_y + footer_h - t)

        display.set_pen(pens["red"] if is_today else pens["black"])
        display.text(DAY_NAMES[i], x + 3, footer_y + 2, scale=2)

        display.set_pen(pens["black"])
        if name:
            display.text(name[:max_chars], x + 3, footer_y + 22, scale=2)
        else:
            display.text("-", x + 3, footer_y + 22, scale=2)


def draw_meals_full(display, data, pens, which="today"):
    """Full-screen: left = week list (target day boxed), right = that day's recipe.
    which = 'today' or 'tomorrow'. Does NOT call display.update() — the caller does."""
    W, H = display.get_bounds()
    meal_plan = data.get("meal_plan", [])
    today_dow = _today_dow(data.get("today", ""))
    if which == "tomorrow":
        tm     = data.get("tomorrow_meal")
        prefix = "(Tomorrow) "
        if today_dow == 6:                          # Sunday -> tomorrow is next week's Monday
            week_plan = data.get("meal_plan_next", [])
            dow       = 0                           # highlight Monday
            past_cut  = 0                           # next week: nothing is in the past
        else:
            week_plan = meal_plan
            dow       = today_dow + 1               # highlight tomorrow within this week
            past_cut  = today_dow
    else:
        tm        = data.get("today_meal")
        prefix    = ""
        week_plan = meal_plan
        dow       = today_dow
        past_cut  = today_dow

    display.set_pen(pens["white"])
    display.clear()
    display.set_font("bitmap8")

    # ---------- Left: week overview ----------
    left_w = 180
    row_h  = (H - 24) // 7   # reserve space for the separator + button legend
    display.set_pen(pens["black"])
    display.line(left_w, 0, left_w, H - 17)          # frame divider (stops at the separator)
    for i in range(1, 7):
        display.line(0, i * row_h, left_w, i * row_h)  # horizontal day dividers

    for i in range(7):
        y        = i * row_h
        is_today = (i == dow)

        display.set_pen(pens["red"] if is_today else pens["black"])
        display.text(DAY_NAMES[i], 8, y + 6, -1, 1)   # smaller day label

        name = ""
        if i < len(week_plan) and isinstance(week_plan[i], dict):
            name = week_plan[i].get("name", "") or ""
        display.set_pen(pens["black"])
        yy = y + 22
        for ln in _wrap_lines(display, name if name else "-", left_w - 14, 2)[:2]:
            display.text(ln, 8, yy, -1, 2)             # meal name unchanged (bitmap8 x2)
            yy += 18

        # Past days — dim with a white stipple to simulate grey
        if i < past_cut:
            display.set_pen(pens["white"])
            py = y + 3
            while py < y + row_h - 3:
                px = 6 + (py & 1)
                while px < left_w - 6:
                    display.pixel(px, py)
                    px += 2
                py += 1

        if is_today:
            display.set_pen(pens["red"])
            x2 = left_w - 3
            for t in range(3):
                display.line(2 + t, y + 2 + t, x2 - t, y + 2 + t)
                display.line(2 + t, y + row_h - 2 - t, x2 - t, y + row_h - 2 - t)
                display.line(2 + t, y + 2 + t, 2 + t, y + row_h - 2 - t)
                display.line(x2 - t, y + 2 + t, x2 - t, y + row_h - 2 - t)

    # ---------- Right: today's recipe ----------
    rx      = left_w + 14
    rw      = W - rx - 8
    col_gap = 18
    col_w   = (rw - col_gap) // 2
    col2_x  = rx + col_w + col_gap
    bottom  = H - 24   # stop above the separator line + button legend drawn by the caller
    state   = {"y": 8}

    def block(text, scale, lh, pen="black", gap=4):
        display.set_pen(pens[pen])
        for ln in _wrap_lines(display, text, rw, scale):
            if state["y"] + lh > bottom:
                return False
            display.text(ln, rx, state["y"], -1, scale)
            state["y"] += lh
        state["y"] += gap
        return True

    def draw_col(items, x, cw, start_y, scale, lh):
        """Draw a column of pre-wrapped items; returns (end_y, truncated)."""
        yy = start_y
        for it in items:
            for ln in _wrap_lines(display, it, cw, scale):
                if yy + lh > bottom:
                    return yy, True
                display.text(ln, x, yy, -1, scale)
                yy += lh
        return yy, False

    if not tm:
        display.set_font("bitmap8")
        display.set_pen(pens["black"])
        display.text(prefix + "No dinner planned", rx, state["y"], rw, 3)
        return

    # Title (bitmap8 x3) — tomorrow view is prefixed with "(Tomorrow) "
    display.set_font("bitmap8")
    block(prefix + tm.get("name", ""), 3, 28, gap=4)

    # Meta bar: white text on a black background
    meta = []
    if tm.get("cuisine"):
        meta.append(tm["cuisine"])
    tot = (tm.get("prep_time_min", 0) or 0) + (tm.get("cook_time_min", 0) or 0)
    if tot:
        meta.append("{} min".format(tot))
    if tm.get("difficulty"):
        meta.append("difficulty {}/5".format(tm["difficulty"]))
    if tm.get("servings"):
        meta.append("serves {}".format(tm["servings"]))
    if meta:
        bar_h = 26
        display.set_pen(pens["black"])
        display.rectangle(rx - 4, state["y"], rw + 8, bar_h)
        display.set_pen(pens["white"])
        display.text(" | ".join(meta), rx, state["y"] + 5, -1, 2)
        state["y"] += bar_h + 10

    ingredients = tm.get("ingredients", [])
    steps       = tm.get("steps", [])

    if not ingredients and not steps:
        if tm.get("notes"):
            block(tm["notes"], 2, 18)
        return

    # Dynamically pick the body text size so long recipes still fit.
    # Preferred middle size is bitmap6 x2 (~12px); shrink to x1 (~6px) if needed.
    display.set_font("bitmap6")
    avail = bottom - state["y"]
    b_scale, b_lh = 1, 9
    for scale, lh in ((2, 15), (1, 9)):
        need = 0
        if ingredients:
            mid = (len(ingredients) + 1) // 2
            l1 = sum(len(_wrap_lines(display, "- " + g, col_w, scale)) for g in ingredients[:mid])
            l2 = sum(len(_wrap_lines(display, "- " + g, col_w, scale)) for g in ingredients[mid:])
            need += 24 + max(l1, l2) * lh + 8
        if steps:
            need += 24
            for i in range(len(steps)):
                need += len(_wrap_lines(display, "{}. {}".format(i + 1, steps[i]), rw, scale)) * lh + 5
        if need <= avail:
            b_scale, b_lh = scale, lh
            break

    truncated = False

    if ingredients:
        display.set_font("bitmap8")
        display.set_pen(pens["red"])
        display.text("INGREDIENTS", rx, state["y"], -1, 2)
        state["y"] += 24
        display.set_font("bitmap6")
        display.set_pen(pens["black"])
        mid = (len(ingredients) + 1) // 2
        c1  = ["- " + g for g in ingredients[:mid]]
        c2  = ["- " + g for g in ingredients[mid:]]
        y1, t1 = draw_col(c1, rx,     col_w, state["y"], b_scale, b_lh)
        y2, t2 = draw_col(c2, col2_x, col_w, state["y"], b_scale, b_lh)
        if t1 or t2:
            truncated = True
        state["y"] = max(y1, y2) + 8

    if steps and not truncated:
        display.set_font("bitmap8")
        if state["y"] + 22 <= bottom:
            display.set_pen(pens["red"])
            display.text("METHOD", rx, state["y"], -1, 2)
            state["y"] += 24
        display.set_font("bitmap6")
        for i in range(len(steps)):
            if not block("{}. {}".format(i + 1, steps[i]), b_scale, b_lh, gap=5):
                truncated = True
                break

    if truncated:
        display.set_font("bitmap6")
        display.set_pen(pens["red"])
        display.text("... full recipe in the app", rx, bottom - 10, -1, 1)

    display.set_font("bitmap8")
