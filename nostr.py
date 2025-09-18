import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
from httpx_ws import aconnect_ws, AsyncWebSocketSession


async def stream_nostr(
    relays: str | list[str],
    filters: list[dict[str, Any]] | None = None,
    timeout: float | None = None,
    since_seconds: int | None = None,
    stop_after_idle: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    subscription_filters = (
        [
            {**f, "since": int(time.time()) - max(0, since_seconds)}
            for f in (filters or [{}])
        ]
        if since_seconds is not None
        else filters or [{}]
    )
    relay_urls = [relays] if isinstance(relays, str) else relays

    out_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    seen_event_ids: set[str] = set()
    seen_lock = asyncio.Lock()
    tasks: list[asyncio.Task[None]] = []

    async def _relay_worker(relay_url: str) -> None:
        client = httpx.AsyncClient(http2=False, timeout=None)
        try:
            async with aconnect_ws(relay_url, client) as ws_raw:  # type: AsyncWebSocketSession
                ws = cast(AsyncWebSocketSession, ws_raw)
                subscription_id = f"sub_{int(time.time() * 1000)}"
                req_message = ["REQ", subscription_id, *subscription_filters]
                await ws.send_text(json.dumps(req_message))

                while True:
                    message = await ws.receive_text()
                    try:
                        data = json.loads(message)
                        if (
                            isinstance(data, list)
                            and len(data) >= 3
                            and data[0] == "EVENT"
                            and isinstance(data[2], dict)
                            and "id" in data[2]
                        ):
                            event = data[2]
                            if (
                                since_seconds is not None
                                and isinstance(event.get("created_at"), int)
                                and event["created_at"]
                                < int(time.time()) - max(0, since_seconds)
                            ):
                                continue
                            event_id = event["id"]
                            async with seen_lock:
                                if event_id in seen_event_ids:
                                    continue
                                seen_event_ids.add(event_id)
                            event["relay"] = relay_url
                            await out_queue.put(event)
                    except json.JSONDecodeError:
                        continue
        finally:
            await client.aclose()

    try:
        for relay_url in relay_urls:
            tasks.append(asyncio.create_task(_relay_worker(relay_url)))

        start_time = time.time() if timeout else None
        last_message_time = time.time()

        while True:
            if (
                timeout is not None
                and start_time is not None
                and (time.time() - start_time) > timeout
            ):
                break

            if (
                stop_after_idle is not None
                and (time.time() - last_message_time) > stop_after_idle
            ):
                break

            if tasks and all(t.done() for t in tasks) and out_queue.empty():
                break

            try:
                event = await asyncio.wait_for(out_queue.get(), timeout=0.05)
                last_message_time = time.time()
                yield event
            except asyncio.TimeoutError:
                continue
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
