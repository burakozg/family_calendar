"""Projects recipes and calendar events into the hobby Obsidian vault.

The sixth writer into a shared vault (after taster, podcast-digest,
security-digest, clippings-topics, video-digest) but the first into the
*hobby* one — see `~/.claude/skills/obsidian-vault-writer` and
`homelab/README.md` for the split. Whole-file ownership only, two folders,
no shared sections: nothing else writes `Recipes/` or `Calendar/`.

    Recipes/<id>.md          one note per recipe, id is already a filesystem-
                              safe slug (storage.py's recipe id)
    Calendar/Events.md       one-off events + recurring rules, rebuilt whole
    Calendar/Birthdays.md    birthdays, rebuilt whole

Rebuilt whole every cycle, never appended to — same convention
clippings-topics uses: an edited/deleted recipe or event has to be able to
disappear from the vault, and only a full rebuild makes that happen for
free. `LiveSyncVault.project()` already no-ops on unchanged content (a GET
+ compare, not a write), so a quiet cycle costs reads, not writes.

Config mirrors the other vault-writing apps' `.env` convention
(`VAULT_COUCHDB_URL`, `VAULT_DB`, `VAULT_USER`, `VAULT_COUCHDB_PASSWORD`).
Not configured -> the loop never starts, same as mailsync/relay when their
env is missing.
"""

from __future__ import annotations

import asyncio
import os
import time

from activity_log import log_event
from storage import RECIPES_DIR, read_events
from vault import LiveSyncVault, VaultConfig

VAULT_SYNC_POLL_SECONDS = int(os.getenv("VAULT_SYNC_POLL_SECONDS", "120"))

_MONTHS = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def configured_env() -> bool:
    return bool(_env("VAULT_COUCHDB_URL"))


def missing_env() -> list[str]:
    required = ["VAULT_COUCHDB_URL", "VAULT_USER", "VAULT_COUCHDB_PASSWORD"]
    return [k for k in required if not _env(k)]


def _vault() -> LiveSyncVault:
    return LiveSyncVault(
        VaultConfig(
            couchdb_url=_env("VAULT_COUCHDB_URL"),
            db=_env("VAULT_DB") or "hobby",
            user=_env("VAULT_USER"),
        ),
        _env("VAULT_COUCHDB_PASSWORD") or None,
    )


# ── formatting ────────────────────────────────────────────────────────────────

def _fmt_time(prep: int, cook: int, inactive: int) -> str:
    parts = []
    if prep:
        parts.append(f"**Prep:** {prep} min")
    if cook:
        parts.append(f"**Cook:** {cook} min")
    if inactive:
        parts.append(f"**Inactive:** {inactive} min")
    return " · ".join(parts)


def _fmt_ingredient(i: dict) -> str:
    bits = " ".join(x for x in (i.get("amount"), i.get("unit"), i.get("item")) if x)
    notes = i.get("notes")
    return f"- {bits} — {notes}" if notes else f"- {bits}"


def _fmt_step(n: int, s: dict) -> str:
    text = s.get("text") or ""
    dur = s.get("duration_min")
    return f"{n}. {text} ({dur} min)" if dur else f"{n}. {text}"


def format_recipe(recipe: dict) -> str:
    name = recipe.get("name") or recipe.get("id") or "Untitled"
    tags = recipe.get("tags") or []
    meal_type = recipe.get("meal_type") or []
    dietary = recipe.get("dietary") or []

    front = ["---"]
    front.append(f'title: "{name}"')
    if recipe.get("course"):
        front.append(f"course: {recipe['course']}")
    if recipe.get("cuisine"):
        front.append(f"cuisine: {recipe['cuisine']}")
    if tags:
        front.append(f"tags: [{', '.join(tags)}]")
    if meal_type:
        front.append(f"meal_type: [{', '.join(meal_type)}]")
    if dietary:
        front.append(f"dietary: [{', '.join(dietary)}]")
    if recipe.get("servings"):
        front.append(f"servings: {recipe['servings']}")
    if recipe.get("rating") is not None:
        front.append(f"rating: {recipe['rating']}")
    front.append("---")

    lines = front + ["", f"# {name}"]
    if recipe.get("description"):
        lines += ["", recipe["description"]]

    meta = _fmt_time(
        recipe.get("prep_time_min") or 0,
        recipe.get("cook_time_min") or 0,
        recipe.get("inactive_time_min") or 0,
    )
    if recipe.get("servings"):
        meta = f"{meta} · **Servings:** {recipe['servings']}" if meta else f"**Servings:** {recipe['servings']}"
    if meta:
        lines += ["", meta]

    ingredients = recipe.get("ingredients") or []
    if ingredients:
        lines += ["", "## Ingredients", *[_fmt_ingredient(i) for i in ingredients]]

    equipment = recipe.get("equipment") or []
    if equipment:
        lines += ["", "## Equipment", *[f"- {e}" for e in equipment]]

    steps = recipe.get("steps") or []
    if steps:
        lines += ["", "## Steps", *[_fmt_step(n, s) for n, s in enumerate(steps, 1)]]

    if recipe.get("notes"):
        lines += ["", "## Notes", recipe["notes"]]

    variations = recipe.get("variations") or []
    if variations:
        lines += ["", "## Variations", *[f"- {v}" for v in variations]]

    source = recipe.get("source") or {}
    log = recipe.get("log") or {}
    footer = []
    if source.get("type"):
        footer.append(f"Source: {source['type']} — {source.get('value', '')}".rstrip(" —"))
    if log.get("entered_by") or log.get("entered_at"):
        who = log.get("entered_by", "")
        when = log.get("entered_at", "")
        footer.append(f"Logged by {who} on {when}".strip())
    if footer:
        lines += ["", "---", *footer]

    return "\n".join(lines) + "\n"


def format_events(events: list, recurring: list) -> str:
    lines = ["# Events", ""]
    dated = sorted((e for e in events if e.get("date")), key=lambda e: e["date"])
    if dated:
        lines.append("## Upcoming")
        for e in dated:
            who = f" ({e['who']})" if e.get("who") and e["who"] not in ("", "family") else ""
            lines.append(f"- {e['date']} · {e.get('label', '')}{who}")
        lines.append("")
    if recurring:
        lines.append("## Recurring")
        for r in recurring:
            step = r.get("step")
            cadence = f"every {step} days from {r.get('startDate', '?')}" if step else "recurring"
            lines.append(f"- {r.get('label', '')} — {cadence}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_birthdays(birthdays: list) -> str:
    lines = ["# Birthdays", ""]
    ordered = sorted(birthdays, key=lambda b: (b.get("month") or 0, b.get("day") or 0))
    for b in ordered:
        month = _MONTHS[b["month"]] if b.get("month") and 1 <= b["month"] <= 12 else "?"
        lines.append(f"- {month} {b.get('day', '?')} — {b.get('name', '')}")
    return "\n".join(lines).rstrip() + "\n"


# ── sync ──────────────────────────────────────────────────────────────────────

async def sync_all() -> None:
    """Rebuild every note this app owns and project whatever changed."""
    vault = _vault()
    now_ms = int(time.time() * 1000)
    try:
        written = 0
        for path in sorted(RECIPES_DIR.glob("*.json")):
            if path.name == "index.json":
                continue
            try:
                import json
                recipe = json.loads(path.read_text())
            except Exception:
                continue
            recipe_id = recipe.get("id") or path.stem
            markdown = format_recipe(recipe)
            if await vault.project(f"Recipes/{recipe_id}.md", markdown, mtime_ms=now_ms):
                written += 1

        data = read_events()
        events_md = format_events(data.get("events") or [], data.get("recurring") or [])
        birthdays_md = format_birthdays(data.get("birthdays") or [])
        if await vault.project("Calendar/Events.md", events_md, mtime_ms=now_ms):
            written += 1
        if await vault.project("Calendar/Birthdays.md", birthdays_md, mtime_ms=now_ms):
            written += 1

        if written:
            log_event("data", "vault.sync", f"Vault sync wrote {written} note(s)")
    finally:
        await vault.close()


async def vault_sync_loop() -> None:
    """Background loop, started from main.py's startup() like relay/mailsync."""
    while True:
        try:
            await sync_all()
        except Exception as e:
            log_event("data", "vault.sync", f"Vault sync crashed: {e}", level="error")
        await asyncio.sleep(max(30, VAULT_SYNC_POLL_SECONDS))
