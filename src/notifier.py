import logging

from .telegram_poster import TelegramPoster

log = logging.getLogger(__name__)


class Notifier:
    """Best-effort owner DM for crashes / loss-of-connection."""

    def __init__(self, telegram: TelegramPoster):
        self._tg = telegram

    async def alert(self, reason: str) -> None:
        log.error("alerting owner: %s", reason)
        try:
            await self._tg.dm_owner(f"⚠️ lighter-bot: {reason}")
        except Exception:
            log.exception("notifier dm failed")
