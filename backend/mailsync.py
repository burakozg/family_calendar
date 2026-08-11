"""Bi-directional mail/calendar sync with mailbox.org — see MAILSYNC_DESIGN.md.

Inbound:  iMIP invitations emailed to the alias (IMAP poll) become local
          events/recurring items; CANCELs remove them; updates (higher
          SEQUENCE) replace them.
Outbound: local events/recurring items are mirrored to a dedicated mailbox.org
          calendar (CalDAV PUT, NO attendees — the OX server would send its own
          invitations otherwise) and invitations are sent from the alias via
          SMTP (iMIP REQUEST/CANCEL). Sync state maps local id ↔ iCalendar UID.

Imports the shared storage/calendar helpers directly (the module split made
the old configure() injection unnecessary). All events.json read-modify-writes
go through the shared helpers/lock.
"""
import asyncio
import email
import email.policy
import email.utils
import hashlib
import imaplib
import json
import os
import re
import smtplib
import uuid
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dateutil.rrule import rrulestr
from icalendar import Calendar, Event as ICalEvent, vCalAddress, vText

import config  # noqa: F401  (loads .env before the env reads below)
from activity_log import log_event
from bus import broadcast
from calendar_store import _add_event, _add_recurring
from display_cache import build_display_cache
from fsatomic import _atomic_write_text
from storage import (F_EVENTS, _sanitize_event, _sanitize_recurring,
                     _write_lock, read_events, read_settings, write)

TZ = ZoneInfo("Europe/Stockholm")

def _app_name() -> str:
    return read_settings().get("appName") or "Family Calendar"

# Icon → emoji for outbound titles (mirror of the frontend ICON_EMOJI maps).
# The emoji is prepended to the event name in both the calendar SUMMARY and the
# email subject, so recipients see e.g. "🎂 Grandma's birthday".
ICON_EMOJI = {
    "trash": "🗑", "cake": "🎂", "football": "⚽", "swim": "🏊", "gym": "🏋",
    "meeting": "💬", "doctor": "🏥", "school": "🎒", "yoga": "🧘", "travel": "✈️",
    "bbq": "🍖", "date": "❤️", "star": "⭐", "music": "🎵", "run": "🏃",
    "beachvolley": "🏐", "pizza": "🍕", "beer": "🍺", "whisky": "🥃", "coffee": "☕",
    "cocktail": "🍹", "car": "🚗", "broom": "🧹",
}

def _titled(body: dict, suffix: str = "") -> str:
    """Event name prefixed with its icon's emoji (falls back to the bare name),
    with an optional owner suffix appended at build time (F6)."""
    label = (body.get("label") or "").strip()
    emoji = ICON_EMOJI.get(body.get("icon", ""))
    base = f"{emoji} {label}".strip() if emoji else label
    return base + suffix

def _owner_suffix(body: dict, members: list) -> str:
    """" (Name)" for a known member id, else "" — for who in ("", "family") or an
    unknown id. Applied only when building outbound copies (F6); never stored and
    never part of _content_hash, so renaming a member won't retro-send updates."""
    who = (body.get("who") or "").strip()
    if who in ("", "family"):
        return ""
    m = next((x for x in (members or []) if x.get("id") == who), None)
    label = (m.get("label") or "").strip() if m else ""
    return f" ({label})" if label else ""

MAILSYNC_ADDRESS      = os.getenv("MAILSYNC_ADDRESS", "").strip().lower()
MAILSYNC_LOGIN        = os.getenv("MAILSYNC_LOGIN", "").strip()
MAILSYNC_PASSWORD     = os.getenv("MAILSYNC_PASSWORD", "")
MAILSYNC_CALDAV_URL   = os.getenv("MAILSYNC_CALDAV_URL", "").strip()
# mailbox.org app passwords are scoped: a "Mail" password won't authenticate
# against DAV. These default to the mail credentials when unset.
MAILSYNC_CALDAV_LOGIN    = os.getenv("MAILSYNC_CALDAV_LOGIN", "").strip() or MAILSYNC_LOGIN
MAILSYNC_CALDAV_PASSWORD = os.getenv("MAILSYNC_CALDAV_PASSWORD", "") or MAILSYNC_PASSWORD
MAILSYNC_IMAP_HOST    = os.getenv("MAILSYNC_IMAP_HOST", "imap.mailbox.org")
MAILSYNC_SMTP_HOST    = os.getenv("MAILSYNC_SMTP_HOST", "smtp.mailbox.org")
MAILSYNC_POLL_SECONDS = int(os.getenv("MAILSYNC_POLL_SECONDS", "120"))

F_STATE = Path(os.getenv("DATA_DIR", "/data")) / "mailsync_state.json"

INBOX_BATCH  = 50    # max messages processed per cycle
EMAIL_FUSE   = 20    # max outbound email messages per cycle (misconfiguration fuse)
EXPAND_DAYS  = 180   # horizon for expanding non-mappable RRULEs
EXPAND_LIMIT = 60    # max instances per expanded series

_REQUIRED_ENV = ("MAILSYNC_ADDRESS", "MAILSYNC_LOGIN", "MAILSYNC_PASSWORD", "MAILSYNC_CALDAV_URL")

def missing_env() -> list:
    vals = {"MAILSYNC_ADDRESS": MAILSYNC_ADDRESS, "MAILSYNC_LOGIN": MAILSYNC_LOGIN,
            "MAILSYNC_PASSWORD": MAILSYNC_PASSWORD, "MAILSYNC_CALDAV_URL": MAILSYNC_CALDAV_URL}
    return [k for k in _REQUIRED_ENV if not vals[k]]

def configured_env() -> bool:
    return not missing_env()

def _log(action, message, level="info", detail=None):
    log_event("mailsync", action, message, level=level, detail=detail)

# ── Settings ────────────────────────────────────────────────────────────────
DEFAULT_MAILSYNC = {"enabled": False, "invitees": [], "defaultWho": "family",
                    "autoAccept": False, "syncEvents": True, "syncRecurring": True}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _ms_settings(settings: dict) -> dict:
    return {**DEFAULT_MAILSYNC, **(settings.get("mailSync") or {})}

def _clean_invitees(ms: dict) -> list:
    own = {MAILSYNC_ADDRESS, MAILSYNC_LOGIN.lower()}
    out = []
    for a in ms.get("invitees") or []:
        a = str(a).strip().lower()
        if _EMAIL_RE.match(a) and a not in own and a not in out:
            out.append(a)
    return out[:10]

# ── State file ──────────────────────────────────────────────────────────────
def _blank_state() -> dict:
    return {"bootstrapped": False,
            "imap": {"uidvalidity": 0, "last_uid": 0, "processed_msgids": []},
            "inbound": {}, "outbound": {}}

def _read_state() -> dict:
    if F_STATE.exists():
        try:
            st = json.loads(F_STATE.read_text())
            base = _blank_state()
            base.update({k: st[k] for k in base if k in st})
            return base
        except Exception as e:
            _log("state", f"Could not parse {F_STATE.name}, starting fresh: {e}", level="error")
    return _blank_state()

def _write_state(st: dict):
    _atomic_write_text(F_STATE, json.dumps(st, indent=2, ensure_ascii=False))

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

# ── Identity: content hash (edit detection). Includes icon because the emoji is
# now part of the outbound title, so an icon change must re-send an update. ──
def _content_hash(body: dict, kind: str) -> str:
    keys = (("date", "endDate", "time", "who", "label", "icon") if kind == "event" else
            ("startDate", "time", "freq", "interval", "byday", "until", "exdates", "step", "label", "icon"))
    return hashlib.sha1(json.dumps(
        {k: body.get(k) for k in keys}, sort_keys=True, default=str
    ).encode()).hexdigest()[:16]

# ── ICS building ────────────────────────────────────────────────────────────
def build_vevent(uid: str, body: dict, kind: str, *, sequence: int = 0,
                 method: str | None = None, invitees=(), cancelled: bool = False,
                 owner: str = "") -> bytes:
    """One VEVENT as a VCALENDAR. The CalDAV copy has NO method/organizer/
    attendees (the OX server would email invitations itself otherwise); the
    emailed copy (method set) carries organizer=alias + attendees."""
    cal = Calendar()
    cal.add("prodid", f"-//{_app_name()}//mailsync//EN")
    cal.add("version", "2.0")
    if method:
        cal.add("method", method)
    ev = ICalEvent()
    ev.add("uid", uid)
    ev.add("dtstamp", datetime.now(timezone.utc))
    ev.add("summary", _titled(body, owner))
    ev.add("sequence", sequence)

    t = body.get("time")
    d0 = date.fromisoformat(str(body.get("date") or body.get("startDate")))
    if t:
        hh, mm = (int(x) for x in t.split(":"))
        dt0 = datetime(d0.year, d0.month, d0.day, hh, mm, tzinfo=TZ)
        ev.add("dtstart", dt0)
        if kind == "event" and body.get("endDate"):
            d1 = date.fromisoformat(body["endDate"])
            ev.add("dtend", datetime(d1.year, d1.month, d1.day, hh, mm, tzinfo=TZ) + timedelta(hours=1))
        else:
            ev.add("dtend", dt0 + timedelta(hours=1))   # no stored duration: +1 h convention
    else:
        ev.add("dtstart", d0)
        if kind == "event" and body.get("endDate"):
            ev.add("dtend", date.fromisoformat(body["endDate"]) + timedelta(days=1))  # exclusive
        else:
            ev.add("dtend", d0 + timedelta(days=1))

    if kind == "recurring":
        freq = body.get("freq")
        if freq == "weekly":
            rr = {"freq": "weekly", "interval": int(body.get("interval") or 1)}
            if body.get("byday"):
                rr["byday"] = list(body["byday"])
        elif freq == "daily":
            rr = {"freq": "daily", "interval": int(body.get("interval") or 1)}
        else:
            step = max(1, int(body.get("step") or 1))
            rr = ({"freq": "weekly", "interval": step // 7} if step % 7 == 0 else
                  {"freq": "daily", "interval": step})
        if body.get("until"):
            u = date.fromisoformat(body["until"])
            rr["until"] = (datetime(u.year, u.month, u.day, 23, 59, 59, tzinfo=TZ)
                           .astimezone(timezone.utc)) if t else u
        ev.add("rrule", rr)
        for x in body.get("exdates") or []:
            xd = date.fromisoformat(x)
            ev.add("exdate", datetime(xd.year, xd.month, xd.day, hh, mm, tzinfo=TZ) if t else xd)

    if cancelled:
        ev.add("status", "CANCELLED")
    if method:
        org = vCalAddress(f"mailto:{MAILSYNC_ADDRESS}")
        org.params["CN"] = vText(_app_name())
        ev["organizer"] = org
        for a in invitees:
            att = vCalAddress(f"mailto:{a}")
            att.params["ROLE"] = vText("REQ-PARTICIPANT")
            att.params["PARTSTAT"] = vText("NEEDS-ACTION")
            att.params["RSVP"] = vText("TRUE")
            ev.add("attendee", att, encode=0)

    cal.add_component(ev)
    try:
        cal.add_missing_timezones()   # emits VTIMEZONE for TZID references
    except Exception:
        pass
    return cal.to_ical()

def _build_reply(inbound_vevent, organizer_email: str) -> bytes:
    """iMIP REPLY (auto-accept): original UID/SEQUENCE/DTSTART, alias ACCEPTED."""
    cal = Calendar()
    cal.add("prodid", f"-//{_app_name()}//mailsync//EN")
    cal.add("version", "2.0")
    cal.add("method", "REPLY")
    ev = ICalEvent()
    ev.add("uid", str(inbound_vevent.get("UID")))
    ev.add("dtstamp", datetime.now(timezone.utc))
    ev.add("sequence", int(inbound_vevent.get("SEQUENCE", 0) or 0))
    ev["dtstart"] = inbound_vevent["DTSTART"]
    if inbound_vevent.get("SUMMARY") is not None:
        ev.add("summary", str(inbound_vevent.get("SUMMARY")))
    org = vCalAddress(f"mailto:{organizer_email}")
    ev["organizer"] = org
    att = vCalAddress(f"mailto:{MAILSYNC_ADDRESS}")
    att.params["PARTSTAT"] = vText("ACCEPTED")
    ev.add("attendee", att, encode=0)
    cal.add_component(ev)
    return cal.to_ical()

# ── CalDAV client ───────────────────────────────────────────────────────────
async def caldav_put(uid: str, ics: bytes) -> bool:
    url = MAILSYNC_CALDAV_URL.rstrip("/") + f"/{uid}.ics"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.put(url, content=ics,
                                 headers={"Content-Type": "text/calendar; charset=utf-8"},
                                 auth=(MAILSYNC_CALDAV_LOGIN, MAILSYNC_CALDAV_PASSWORD))
        ok = r.status_code in (200, 201, 204)
        if not ok:
            _log("caldav.put", f"CalDAV PUT {uid} failed: {r.status_code}",
                 level="error", detail={"body": r.text[:200]})
        return ok
    except Exception as e:
        _log("caldav.put", f"CalDAV unreachable: {e}", level="error")
        return False

async def caldav_delete(uid: str) -> bool:
    url = MAILSYNC_CALDAV_URL.rstrip("/") + f"/{uid}.ics"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.delete(url, auth=(MAILSYNC_CALDAV_LOGIN, MAILSYNC_CALDAV_PASSWORD))
        ok = r.status_code in (200, 204, 404)   # 404 = already gone
        if not ok:
            _log("caldav.delete", f"CalDAV DELETE {uid} failed: {r.status_code}", level="error")
        return ok
    except Exception as e:
        _log("caldav.delete", f"CalDAV unreachable: {e}", level="error")
        return False

# ── SMTP sender (blocking; call via asyncio.to_thread) ─────────────────────
def _smtp_send(to_addrs: list, subject: str, body_text: str, ics_text: str, method: str):
    msg = EmailMessage()
    msg["From"] = f"{_app_name()} <{MAILSYNC_ADDRESS}>"
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content(body_text)
    msg.add_alternative(ics_text, subtype="calendar")
    for p in msg.walk():                      # text/calendar needs method= param
        if p.get_content_type() == "text/calendar":
            p.set_param("method", method)
    msg.add_attachment(ics_text.encode(), maintype="application", subtype="ics",
                       filename="invite.ics")
    with smtplib.SMTP(MAILSYNC_SMTP_HOST, 587, timeout=30) as s:
        s.starttls()
        s.login(MAILSYNC_LOGIN, MAILSYNC_PASSWORD)
        s.send_message(msg, from_addr=MAILSYNC_ADDRESS)   # envelope sender = alias

def _invite_body_text(body: dict, kind: str, owner: str = "") -> str:
    when = body.get("date") or body.get("startDate", "")
    if body.get("endDate"):
        when += f" to {body['endDate']}"
    if body.get("time"):
        when += f" at {body['time']} (about 1 hour)"
    return f"{body.get('label', '')}{owner} on {when}. This invitation was sent by the family calendar."

# ── Inbound: IMAP poll + iMIP apply ─────────────────────────────────────────
def _imap_fetch_batch(last_uid: int, uidvalidity: int):
    """Blocking. Returns (uidvalidity, [(uid, raw_bytes), ...]) for up to
    INBOX_BATCH messages after last_uid. Messages are flagged \\Seen; mail is
    never deleted or moved."""
    M = imaplib.IMAP4_SSL(MAILSYNC_IMAP_HOST, 993, timeout=30)
    try:
        M.login(MAILSYNC_LOGIN, MAILSYNC_PASSWORD)
        M.select("INBOX")
        uv_raw = M.response("UIDVALIDITY")[1]
        uv = int(uv_raw[0]) if uv_raw and uv_raw[0] else 0
        if uv != uidvalidity:
            last_uid = 0
        typ, data = M.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        uids = sorted(int(x) for x in (data[0].split() if data and data[0] else [])
                      if int(x) > last_uid)
        out = []
        for u in uids[:INBOX_BATCH]:
            typ, msgdata = M.uid("FETCH", str(u), "(RFC822)")
            raw = next((p[1] for p in (msgdata or []) if isinstance(p, tuple) and p[1]), None)
            out.append((u, raw))
            M.uid("STORE", str(u), "+FLAGS", "(\\Seen)")
        return uv, out
    finally:
        try:
            M.logout()
        except Exception:
            pass

def _find_ics(msg) -> tuple:
    """(ics_text, mime_method_param) from a parsed email, or (None, None)."""
    for part in msg.walk():
        ctype = part.get_content_type()
        fname = (part.get_filename() or "").lower()
        if ctype == "text/calendar" or (fname.endswith(".ics") and
                                        ctype in ("application/ics", "application/octet-stream")):
            try:
                payload = part.get_payload(decode=True)
                return payload.decode(part.get_content_charset() or "utf-8", "replace"), \
                    part.get_param("method")
            except Exception:
                continue
    return None, None

def _email_of(caladdress) -> str:
    if not caladdress:
        return ""
    s = str(caladdress)
    return s[7:].strip().lower() if s.lower().startswith("mailto:") else s.strip().lower()

def _who_from_emails(members: list, *emails: str) -> str:
    """Attribute an inbound event to a family member by name: return the member
    id whose label (e.g. "Alex", "Sam") appears in the local-part of any of the
    given sender/organizer addresses, else "". Local-part only, so an org domain
    can't false-match; longest label first so a short name can't shadow a longer
    one that contains it; labels under 3 chars are ignored as too collision-prone."""
    blob = " ".join((e or "").split("@", 1)[0].lower() for e in emails if e)
    if not blob:
        return ""
    for m in sorted(members, key=lambda x: len(x.get("label") or ""), reverse=True):
        label = (m.get("label") or "").strip().lower()
        if len(label) >= 3 and label in blob:
            return m.get("id", "")
    return ""

def _map_dtstart(dt):
    """DTSTART → (iso date, HH:MM or None), Europe/Stockholm local."""
    if isinstance(dt, datetime):
        local = dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
        return local.date().isoformat(), local.strftime("%H:%M")
    return dt.isoformat(), None

def _map_end(v, dtstart, body_date: str):
    """DTEND/DURATION → inclusive endDate string, or None for single-day."""
    dtend = None
    try:
        dtend = v.decoded("DTEND", None)
        if dtend is None and v.get("DURATION") is not None:
            dtend = dtstart + v.decoded("DURATION")
    except Exception:
        return None
    if dtend is None:
        return None
    if isinstance(dtend, datetime):
        local = dtend.astimezone(TZ) if dtend.tzinfo else dtend.replace(tzinfo=TZ)
        end_d = local.date()
    else:
        end_d = dtend - timedelta(days=1)   # all-day DTEND is exclusive
    s = end_d.isoformat()
    return s if s > body_date else None

def _exdate_strs(v) -> set:
    out = set()
    ex = v.get("EXDATE")
    if ex is None:
        return out
    for chunk in (ex if isinstance(ex, list) else [ex]):
        for d in getattr(chunk, "dts", []):
            val = d.dt
            if isinstance(val, datetime):
                val = (val.astimezone(TZ) if val.tzinfo else val).date()
            out.add(val.isoformat())
    return out

def _local_ids_exist(rec: dict) -> bool:
    ids = set(rec.get("local_ids") or [])
    ev = read_events()
    return any(x.get("id") in ids
               for sec in ("events", "recurring") for x in ev.get(sec, []) or [])

async def _remove_local_ids(rec: dict):
    ids = set(rec.get("local_ids") or [])
    if not ids:
        return
    changed = False
    async with _write_lock:
        ev = read_events()
        for sec in ("events", "recurring"):
            kept = [x for x in ev.get(sec, []) or [] if x.get("id") not in ids]
            if len(kept) != len(ev.get(sec, []) or []):
                ev[sec] = kept
                changed = True
        if changed:
            write(F_EVENTS, ev)
            build_display_cache()
    if changed:
        await broadcast("update", {"section": "events"})

async def _apply_request(cal, st: dict, is_request: bool, sender: str = ""):
    settings = read_settings()
    ms = _ms_settings(settings)
    members = settings.get("members", [])
    vevents = list(cal.walk("VEVENT"))
    masters = [v for v in vevents if v.get("RECURRENCE-ID") is None]
    if len(vevents) > len(masters):
        _log("inbound.request", "Ignoring recurrence exception instances (v1 limitation)",
             level="warn")
    if not masters:
        return
    v = masters[0]
    uid = str(v.get("UID", "") or "").strip()
    if not uid:
        _log("inbound.request", "Invitation without UID skipped", level="warn")
        return
    seq = int(v.get("SEQUENCE", 0) or 0)
    summary = str(v.get("SUMMARY", "") or "(no title)")
    organizer = _email_of(v.get("ORGANIZER"))
    if organizer and organizer in (MAILSYNC_ADDRESS, MAILSYNC_LOGIN.lower()):
        return                                            # our own traffic bouncing back

    rec = st["inbound"].get(uid)
    if rec:
        if seq < rec.get("sequence", 0):
            return                                        # stale update
        if seq == rec.get("sequence", 0) and _local_ids_exist(rec):
            return                                        # idempotent resend
        await _remove_local_ids(rec)                      # update: replace below

    try:
        dtstart = v.decoded("DTSTART")
    except Exception:
        _log("inbound.request", f"Invitation '{summary}' has no usable DTSTART", level="warn")
        return
    body_date, body_time = _map_dtstart(dtstart)
    end_date = _map_end(v, dtstart, body_date)
    rrule = v.get("RRULE")
    if not rrule and (end_date or body_date) < date.today().isoformat():
        return                                            # entirely in the past

    # Attribute to the family member whose name is in the sender's (or organizer's)
    # email address; fall back to the configured default when nobody matches.
    who = _who_from_emails(members, sender, organizer) or ms["defaultWho"]
    base = {"date": body_date, "who": who, "icon": "meeting", "label": summary}
    if body_time:
        base["time"] = body_time
    if end_date:
        base["endDate"] = end_date

    local_ids, kind, expanded = [], "event", False
    if not rrule:
        clean = _sanitize_event(base)
        if not clean:
            return
        await _add_event(clean)
        local_ids = [clean["id"]]
    else:
        kind, local_ids, expanded = await _apply_rrule(v, rrule, base, body_date, body_time, summary)
        if not local_ids:
            return

    st["inbound"][uid] = {"kind": kind, "sequence": seq, "local_ids": local_ids,
                          "expanded": expanded, "organizer": organizer, "summary": summary}
    # Persist the inbound mapping right after the events are written, so a crash
    # before poll_inbox's per-message flush can't leave inbound-created events
    # unrecorded (which the next outbound cycle would echo back to the sender).
    _write_state(st)
    if is_request and ms["autoAccept"] and organizer:
        try:
            ics = _build_reply(v, organizer).decode()
            await asyncio.to_thread(_smtp_send, [organizer], f"Accepted: {summary}",
                                    f"{summary} — accepted by the family calendar.", ics, "REPLY")
        except Exception as e:
            _log("inbound.reply", f"Auto-accept reply failed: {e}", level="warn")
    _log("inbound.request", f"Added '{summary}' from {organizer or 'unknown sender'}",
         detail={"uid": uid, "kind": kind, "items": len(local_ids)})

async def _apply_rrule(v, rrule, base: dict, body_date: str, body_time, summary: str):
    """§7.3: map daily/weekly natively onto the v2 recurring model; expand
    everything else into concrete one-off events over the next EXPAND_DAYS."""
    freq = str((rrule.get("FREQ") or [""])[0]).upper()
    complex_parts = any(k in rrule for k in
                        ("BYMONTHDAY", "BYMONTH", "BYSETPOS", "BYWEEKNO", "BYYEARDAY"))
    exdates = _exdate_strs(v)

    if freq in ("DAILY", "WEEKLY") and not complex_parts:
        item = {"label": summary, "icon": "meeting", "startDate": body_date,
                "freq": freq.lower(), "interval": int((rrule.get("INTERVAL") or [1])[0]),
                "iconOnly": False}
        if body_time:
            item["time"] = body_time
        if freq == "WEEKLY" and rrule.get("BYDAY"):
            item["byday"] = [str(b)[-2:].upper() for b in rrule.get("BYDAY")]
        until = rrule.get("UNTIL")
        if until:
            u = until[0]
            item["until"] = ((u.astimezone(TZ) if u.tzinfo else u).date().isoformat()
                             if isinstance(u, datetime) else u.isoformat())
        elif rrule.get("COUNT"):
            last = _expand_dates(v, limit=int((rrule.get("COUNT") or [1])[0]), horizon_days=3650)
            if last:
                item["until"] = last[-1]
        if exdates:
            item["exdates"] = sorted(exdates)
        clean = _sanitize_recurring(item)
        if not clean:
            return "recurring", [], False
        await _add_recurring(clean)
        return "recurring", [clean["id"]], False

    # Not representable natively — expand instances (monthly/yearly/BYSETPOS…).
    dates = [d for d in _expand_dates(v, limit=EXPAND_LIMIT, horizon_days=EXPAND_DAYS)
             if d not in exdates]
    ids = []
    for d in dates:
        instance = {k: v2 for k, v2 in base.items() if k != "endDate"}   # each instance is one day
        clean = _sanitize_event({**instance, "date": d})
        if clean:
            await _add_event(clean)
            ids.append(clean["id"])
    _log("inbound.expand",
         f"Expanded non-mappable recurrence '{summary}' into {len(ids)} events "
         f"(next {EXPAND_DAYS} days; refreshes only on organizer updates)", level="warn")
    return "event", ids, True

def _expand_dates(v, limit: int, horizon_days: int) -> list:
    """Occurrence dates (ISO strings) for a VEVENT's RRULE from today forward."""
    try:
        dtstart = v.decoded("DTSTART")
        dt = dtstart if isinstance(dtstart, datetime) else \
            datetime.combine(dtstart, datetime.min.time())
        rule = rrulestr(v["RRULE"].to_ical().decode(), dtstart=dt)
        lo = (datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()) \
            .replace(hour=0, minute=0, second=0, microsecond=0)
        hi = lo + timedelta(days=horizon_days)
        out = []
        for occ in rule.between(lo - timedelta(seconds=1), hi, inc=True):
            local = occ.astimezone(TZ) if occ.tzinfo else occ
            out.append(local.date().isoformat())
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        _log("inbound.expand", f"RRULE expansion failed: {e}", level="warn")
        return []

async def _apply_cancel(cal, st: dict):
    for v in cal.walk("VEVENT"):
        uid = str(v.get("UID", "") or "").strip()
        rec = st["inbound"].get(uid)
        if not rec:
            continue
        await _remove_local_ids(rec)
        del st["inbound"][uid]
        _log("inbound.cancel", f"Cancelled '{rec.get('summary', '')}'", detail={"uid": uid})

async def _process_message(raw: bytes, st: dict):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    msgid = (msg.get("Message-ID") or "").strip()

    def mark_done():
        if msgid:
            st["imap"]["processed_msgids"] = (st["imap"]["processed_msgids"] + [msgid])[-500:]

    if msgid and msgid in st["imap"]["processed_msgids"]:
        return
    addrs = {a.lower() for _, a in email.utils.getaddresses(
        (msg.get_all("To") or []) + (msg.get_all("Cc") or []) +
        (msg.get_all("Delivered-To") or []) + (msg.get_all("X-Original-To") or [])) if a}
    if MAILSYNC_ADDRESS not in addrs:
        mark_done()
        return
    ics_text, mime_method = _find_ics(msg)
    if not ics_text:
        mark_done()                                       # ordinary mail to the alias
        return
    try:
        cal = Calendar.from_ical(ics_text)
    except Exception as e:
        _log("inbound.parse", f"Unparseable iCalendar from {msg.get('From', '?')}: {e}",
             level="error")
        mark_done()
        return
    method = str(cal.get("METHOD") or mime_method or "").upper()
    if method == "CANCEL":
        await _apply_cancel(cal, st)
    elif method == "REPLY":
        _log("inbound.reply", f"Attendee reply received (not tracked in v1): "
                              f"{msg.get('From', '?')}")
    else:                                                 # REQUEST, or PUBLISH-like
        sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
        await _apply_request(cal, st, is_request=(method == "REQUEST"), sender=sender)
    mark_done()

async def poll_inbox():
    st = _read_state()
    uv, batch = await asyncio.to_thread(
        _imap_fetch_batch, st["imap"]["last_uid"], st["imap"]["uidvalidity"])
    if uv != st["imap"]["uidvalidity"]:
        st["imap"]["uidvalidity"] = uv
        st["imap"]["last_uid"] = 0
        _write_state(st)
    for u, raw in batch:
        st["imap"]["last_uid"] = max(st["imap"]["last_uid"], u)
        if raw:
            try:
                await _process_message(raw, st)
            except Exception as e:
                _log("inbound.process", f"Message {u} failed: {e}", level="error")
        _write_state(st)                                  # per-message crash safety

# ── Outbound: diff by local id ──────────────────────────────────────────────
async def _sync_one(rec: dict, kind: str, body: dict, invitees: list, budget: list,
                    members: list = ()):
    """Retry-aware push of one item: CalDAV leg + email leg independently.
    The owner suffix (F6) decorates the SUMMARY, subject, and body at build time."""
    owner = _owner_suffix(body, members)
    if not rec.get("caldav_ok"):
        ics = build_vevent(rec["uid"], body, kind, sequence=rec["sequence"], owner=owner)
        rec["caldav_ok"] = await caldav_put(rec["uid"], ics)
    if not rec.get("invited_ok"):
        if not invitees:
            rec["invited_ok"] = True
        elif budget[0] > 0:
            budget[0] -= 1
            ics = build_vevent(rec["uid"], body, kind, sequence=rec["sequence"],
                               method="REQUEST", invitees=invitees, owner=owner).decode()
            prefix = "Updated: " if rec["sequence"] > 0 else "Invitation: "
            subject = prefix + f"{_titled(body, owner)} — {body.get('date') or body.get('startDate')}"
            try:
                await asyncio.to_thread(_smtp_send, invitees, subject,
                                        _invite_body_text(body, kind, owner), ics, "REQUEST")
                rec["invited_ok"] = True
                _log("outbound.invite", f"Sent {'update' if rec['sequence'] else 'invitation'} "
                                        f"'{body.get('label', '')}' to {len(invitees)} recipient(s)",
                     detail={"uid": rec["uid"]})
            except Exception as e:
                _log("outbound.invite", f"SMTP send failed: {e}", level="error")

async def sync_outbound():
    settings = read_settings()
    ms = _ms_settings(settings)
    members = settings.get("members", [])   # F6: resolve the owner-name suffix
    st = _read_state()
    today = date.today().isoformat()
    ev = read_events()

    current: dict = {}
    if ms["syncEvents"]:
        for e in ev.get("events", []) or []:
            if isinstance(e, dict) and e.get("id") and \
                    (e.get("endDate") or e.get("date") or "") >= today:
                current[e["id"]] = ("event", e)
    if ms["syncRecurring"]:
        for r in ev.get("recurring", []) or []:
            if isinstance(r, dict) and r.get("id") and (r.get("until") or "9999") >= today:
                current[r["id"]] = ("recurring", r)
    inbound_ids = {i for rec in st["inbound"].values() for i in rec.get("local_ids", [])}
    current = {k: v for k, v in current.items() if k not in inbound_ids}   # loop guard
    invitees = _clean_invitees(ms)

    if not st.get("bootstrapped"):
        # First run ever: register everything WITHOUT emailing the backlog;
        # CalDAV mirroring happens over the following iterations (caldav_ok=False).
        for lid, (kind, body) in current.items():
            st["outbound"][lid] = {"uid": f"fc-{uuid.uuid4().hex}@familycalendar",
                                   "kind": kind, "sequence": 0,
                                   "hash": _content_hash(body, kind),
                                   "caldav_ok": False, "invited_ok": True,
                                   "created": _now_iso()}
        st["bootstrapped"] = True
        _write_state(st)
        _log("outbound.bootstrap",
             f"Registered {len(current)} pre-existing item(s) — mirrored to CalDAV, no emails")

    new     = [k for k in current if k not in st["outbound"]]
    gone    = [k for k in st["outbound"] if k not in current]
    changed = [k for k in current if k in st["outbound"]
               and _content_hash(current[k][1], current[k][0]) != st["outbound"][k]["hash"]]
    retry   = [k for k in current if k in st["outbound"] and k not in changed
               and not (st["outbound"][k].get("caldav_ok") and st["outbound"][k].get("invited_ok"))]

    planned_emails = (len([k for k in new + changed if invitees])
                      + len([k for k in gone
                             if invitees and not st["outbound"][k].get("cancel_sent")]))
    if planned_emails > EMAIL_FUSE:
        _log("outbound.fuse", f"Refusing to send {planned_emails} emails in one cycle "
                              f"(fuse={EMAIL_FUSE}) — check settings/state", level="error")
        invitees = []                                     # CalDAV still proceeds

    budget = [EMAIL_FUSE]
    for lid in new:
        kind, body = current[lid]
        st["outbound"][lid] = {"uid": f"fc-{uuid.uuid4().hex}@familycalendar",
                               "kind": kind, "sequence": 0,
                               "hash": _content_hash(body, kind),
                               "caldav_ok": False, "invited_ok": not invitees,
                               "created": _now_iso()}
        await _sync_one(st["outbound"][lid], kind, body, invitees, budget, members)
        _write_state(st)
    for lid in changed:
        kind, body = current[lid]
        rec = st["outbound"][lid]
        rec["sequence"] += 1
        rec["hash"] = _content_hash(body, kind)
        rec["caldav_ok"] = False
        rec["invited_ok"] = not invitees
        await _sync_one(rec, kind, body, invitees, budget, members)
        _write_state(st)
    for lid in retry:
        kind, body = current[lid]
        await _sync_one(st["outbound"][lid], kind, body, invitees, budget, members)
        _write_state(st)
    for lid in gone:
        rec = st["outbound"][lid]
        if invitees and not rec.get("cancel_sent") and budget[0] > 0:
            budget[0] -= 1
            ics = build_vevent(rec["uid"], {"label": "Cancelled event", "date": today},
                               "event", sequence=rec["sequence"] + 1,
                               method="CANCEL", invitees=invitees, cancelled=True).decode()
            try:
                await asyncio.to_thread(_smtp_send, invitees, "Cancelled: family calendar event",
                                        "This event was removed from the family calendar.",
                                        ics, "CANCEL")
                rec["cancel_sent"] = True
            except Exception as e:
                _log("outbound.cancel", f"SMTP cancel failed: {e}", level="error")
        dav_ok = await caldav_delete(rec["uid"])
        if dav_ok and (rec.get("cancel_sent") or not invitees):
            del st["outbound"][lid]
            _log("outbound.delete", "Removed item from CalDAV (+ cancellation sent)"
                 if invitees else "Removed item from CalDAV", detail={"uid": rec["uid"]})
        _write_state(st)

# ── Loop, manual run, status ────────────────────────────────────────────────
STATUS = {"lastRun": "", "lastError": ""}
_run_lock = asyncio.Lock()

async def run_cycle():
    async with _run_lock:                                  # manual run can't overlap the loop
        STATUS["lastError"] = ""
        try:
            await poll_inbox()
        except Exception as e:
            STATUS["lastError"] = f"inbound: {e}"
            _log("loop", f"Inbound cycle failed: {e}", level="error")
        try:
            await sync_outbound()
        except Exception as e:
            STATUS["lastError"] = (STATUS["lastError"] + " | " if STATUS["lastError"] else "") \
                + f"outbound: {e}"
            _log("loop", f"Outbound cycle failed: {e}", level="error")
        STATUS["lastRun"] = _now_iso()

async def mailsync_loop():
    while True:
        try:
            if configured_env() and _ms_settings(read_settings())["enabled"]:
                await run_cycle()
        except Exception as e:
            _log("loop", f"Cycle crashed: {e}", level="error")
        await asyncio.sleep(MAILSYNC_POLL_SECONDS)

def status() -> dict:
    ms = _ms_settings(read_settings())
    st = _read_state()
    return {"configured": configured_env(), "missing_env": missing_env(),
            "enabled": bool(ms["enabled"]), "address": MAILSYNC_ADDRESS,
            "lastRun": STATUS["lastRun"], "lastError": STATUS["lastError"],
            "counts": {"inbound": len(st["inbound"]), "outbound": len(st["outbound"])},
            "invitees": _clean_invitees(ms), "pollSeconds": MAILSYNC_POLL_SECONDS}
