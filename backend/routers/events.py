"""
Server-Sent Events endpoint for real-time worklist push notifications.
When the folder watcher auto-ingests a new plan, it calls broadcast_new_plan()
which pushes the plan summary to all connected browser clients.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/events", tags=["events"])

_subscribers: list[asyncio.Queue] = []


async def broadcast_new_plan(plan_summary: dict) -> None:
    """Called after successful auto-ingestion. Pushes to all open SSE connections."""
    dead = []
    for q in _subscribers:
        try:
            await q.put(plan_summary)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def broadcast_new_plan_sync(plan_summary: dict) -> None:
    """
    Thread-safe wrapper for broadcast_new_plan().
    Called from the folder watcher background thread where no event loop is running.
    Uses asyncio.run_coroutine_threadsafe if a loop is available, otherwise schedules
    for the next available loop iteration.
    """
    import threading
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(broadcast_new_plan(plan_summary), loop)
        else:
            loop.run_until_complete(broadcast_new_plan(plan_summary))
    except RuntimeError:
        logger.warning("No event loop available for SSE broadcast — skipping push notification.")


@router.get("/worklist")
async def worklist_events():
    """
    SSE stream. Frontend connects once and receives push notifications when
    new plans are auto-ingested from the watch folder.
    """
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.append(queue)

    async def stream():
        try:
            while True:
                # Send keepalive comment every 15 s to prevent proxy timeouts
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                _subscribers.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
