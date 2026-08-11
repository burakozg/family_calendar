"""Shared calendar mutation helpers: add/delete events, birthdays, and
recurring items with the full refresh contract (write under the global lock,
rebuild the display cache, broadcast). Used by the HTTP routes (main.py), the
relay drain (relay_client.py), and mailsync."""
from fastapi import HTTPException

from activity_log import log_event
from bus import broadcast
from display_cache import build_display_cache
from storage import F_EVENTS, _new_id, _write_lock, read_events, write


async def _add_event(body: dict):
    """Append a one-off event and refresh. Shared by the route and the relay drain."""
    async with _write_lock:
        if not body.get("id"):
            body["id"] = _new_id()
        ev = read_events()
        ev.setdefault("events", []).append(body)
        ev["events"].sort(key=lambda e: e["date"])
        write(F_EVENTS, ev)
        build_display_cache()
    await broadcast("update", {"section": "events"})


async def _add_recurring(body: dict):
    """Append a recurring item and refresh. Shared by the route and the relay drain."""
    async with _write_lock:
        if not body.get("id"):
            body["id"] = _new_id()
        ev = read_events()
        ev.setdefault("recurring", []).append(body)
        write(F_EVENTS, ev)
        build_display_cache()
    await broadcast("update", {"section": "events"})


async def _delete_event(p: dict) -> bool:
    """Remove one event: by stable id when the command carries one, else the
    first exact {date, who, label} content match (legacy phone clients)."""
    async with _write_lock:
        ev = read_events()
        tid = p.get("id")
        d, who, label = p.get("date"), p.get("who"), (p.get("label") or "")
        kept, removed = [], False
        for e in ev.get("events", []):
            match = (e.get("id") == tid) if tid else (
                e.get("date") == d and e.get("who") == who and (e.get("label") or "") == label)
            if not removed and match:
                removed = True
                continue
            kept.append(e)
        if removed:
            ev["events"] = kept
            write(F_EVENTS, ev)
            build_display_cache()
    if removed:
        await broadcast("update", {"section": "events"})
    return removed


async def _delete_recurring(p: dict) -> bool:
    """Remove the first recurring item matching id or {label, startDate, step}."""
    async with _write_lock:
        ev = read_events()
        tid = p.get("id")
        label, start, step = (p.get("label") or ""), p.get("startDate"), p.get("step")
        kept, removed = [], False
        for r in ev.get("recurring", []):
            match = (r.get("id") == tid) if tid else (
                (r.get("label") or "") == label
                and r.get("startDate") == start and str(r.get("step")) == str(step))
            if not removed and match:
                removed = True
                continue
            kept.append(r)
        if removed:
            ev["recurring"] = kept
            write(F_EVENTS, ev)
            build_display_cache()
    if removed:
        await broadcast("update", {"section": "events"})
    return removed


async def _delete_by_id(section: str, item_id: str) -> dict:
    """Race-free delete by stable id (A7); 404 when the id is unknown."""
    async with _write_lock:
        ev = read_events()
        items = ev.get(section, []) or []
        gone = next((x for x in items if isinstance(x, dict) and x.get("id") == item_id), None)
        if gone is None:
            raise HTTPException(404, f"No {section} item with id {item_id}")
        ev[section] = [x for x in items if x is not gone]
        write(F_EVENTS, ev)
        build_display_cache()
    await broadcast("update", {"section": "events"})
    log_event("data", f"{section.rstrip('s')}.delete",
              f"Deleted {section.rstrip('s')} '{gone.get('label') or gone.get('name', '')}' (by id)",
              detail={"id": item_id})
    return {"ok": True}
