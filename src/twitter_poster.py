import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tweepy

from . import state as state_mod
from .state import State

log = logging.getLogger(__name__)


class TwitterPoster:
    """Free-tier-aware Twitter client. Stops posting when daily soft cap is hit."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_token_secret: str,
        daily_soft_cap: int,
    ):
        # v2 Client for tweets
        self._client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
        )
        # v1.1 API needed for media upload
        auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_token_secret)
        self._api_v1 = tweepy.API(auth)
        self._cap = daily_soft_cap

    def post(self, text: str, state: State, state_path: Path, image_bytes: Optional[bytes] = None) -> bool:
        today = datetime.now(timezone.utc).date()
        if state.twitter_count_for(today) >= self._cap:
            log.warning("twitter daily soft cap reached (%d) — skipping post", self._cap)
            return False

        media_ids = None
        if image_bytes is not None:
            try:
                media = self._api_v1.media_upload(filename="chart.png", file=_BytesIO(image_bytes))
                media_ids = [media.media_id]
            except Exception:
                log.exception("media upload failed — posting without image")

        try:
            self._client.create_tweet(text=text, media_ids=media_ids)
        except Exception:
            log.exception("tweet failed")
            return False

        state.bump_twitter_count(today)
        state_mod.save(state, state_path)
        return True


class _BytesIO:
    """tweepy.media_upload wants a file-like object with a .name attribute."""

    def __init__(self, data: bytes):
        import io

        self._buf = io.BytesIO(data)
        self.name = "chart.png"

    def read(self, *args, **kwargs):
        return self._buf.read(*args, **kwargs)

    def seek(self, *args, **kwargs):
        return self._buf.seek(*args, **kwargs)
