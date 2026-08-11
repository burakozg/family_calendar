"""In-process SSE fan-out: web clients subscribe via /stream (main.py); every
mutation broadcasts an update message to all connected queues."""
import asyncio
import json

_subscribers: list[asyncio.Queue] = []


async def broadcast(event: str, payload: dict):
    msg = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
    dead = []
    for q in _subscribers:
        try: q.put_nowait(msg)
        except asyncio.QueueFull: dead.append(q)
    for q in dead: _subscribers.remove(q)
