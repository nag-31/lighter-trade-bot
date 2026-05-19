import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional


@dataclass
class State:
    last_trade_id: Optional[str] = None
    paused: bool = False
    twitter_post_date: str = ""  # ISO date string
    twitter_posts_today: int = 0
    open_positions: dict = field(default_factory=dict)  # market -> dict (Position serialized)

    def bump_twitter_count(self, today: date) -> None:
        iso = today.isoformat()
        if self.twitter_post_date != iso:
            self.twitter_post_date = iso
            self.twitter_posts_today = 0
        self.twitter_posts_today += 1

    def twitter_count_for(self, today: date) -> int:
        return self.twitter_posts_today if self.twitter_post_date == today.isoformat() else 0


def load(path: Path) -> State:
    if not path.exists():
        return State()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return State(**data)


def save(state: State, path: Path) -> None:
    """Atomic write — temp file + os.replace so a crash mid-write can't corrupt state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name, dir=str(path.parent or "."))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
