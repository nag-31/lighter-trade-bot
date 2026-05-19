import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

log = logging.getLogger(__name__)


class TelegramPoster:
    def __init__(self, bot_token: str, channel_id: str, owner_user_id: int):
        self._token = bot_token
        self._channel = channel_id
        self._owner = owner_user_id
        self._api = f"https://api.telegram.org/bot{bot_token}"
        self._client = httpx.AsyncClient(timeout=20.0)
        self._last_update_id: int = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def send_to_channel(self, text: str, image_bytes: Optional[bytes] = None) -> None:
        if image_bytes is not None:
            files = {"photo": ("chart.png", image_bytes, "image/png")}
            data = {"chat_id": self._channel, "caption": text}
            r = await self._client.post(f"{self._api}/sendPhoto", data=data, files=files)
        else:
            data = {"chat_id": self._channel, "text": text, "disable_web_page_preview": "true"}
            r = await self._client.post(f"{self._api}/sendMessage", data=data)
        r.raise_for_status()

    async def dm_owner(self, text: str) -> None:
        data = {"chat_id": self._owner, "text": text}
        try:
            r = await self._client.post(f"{self._api}/sendMessage", data=data)
            r.raise_for_status()
        except Exception:
            log.exception("failed to DM owner")

    async def listen_commands(
        self,
        on_pause: Callable[[], Awaitable[None]],
        on_resume: Callable[[], Awaitable[None]],
        on_status: Callable[[], Awaitable[str]],
    ) -> None:
        """Long-poll Telegram for DMs from the owner. Calls handlers on /pause /resume /status."""
        while True:
            try:
                r = await self._client.get(
                    f"{self._api}/getUpdates",
                    params={"offset": self._last_update_id + 1, "timeout": 25},
                    timeout=35.0,
                )
                r.raise_for_status()
                updates = r.json().get("result", [])
                for u in updates:
                    self._last_update_id = u["update_id"]
                    msg = u.get("message") or {}
                    if msg.get("from", {}).get("id") != self._owner:
                        continue
                    text = (msg.get("text") or "").strip().lower()
                    if text == "/pause":
                        await on_pause()
                        await self.dm_owner("paused")
                    elif text == "/resume":
                        await on_resume()
                        await self.dm_owner("resumed")
                    elif text == "/status":
                        await self.dm_owner(await on_status())
            except Exception:
                log.exception("getUpdates failed; backing off 10s")
                await asyncio.sleep(10)
