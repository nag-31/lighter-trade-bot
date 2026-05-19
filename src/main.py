import asyncio
import logging
from datetime import datetime, timezone

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config as config_mod
from . import filters as filters_mod
from . import recap as recap_mod
from . import state as state_mod
from .chart_fetcher import ChartFetcher
from .formatter import format_event
from .healthz import Healthz
from .lighter_client import LighterClient
from .notifier import Notifier
from .position_tracker import PositionTracker
from .telegram_poster import TelegramPoster
from .twitter_poster import TwitterPoster
from .types import Trade

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("lighter-bot")


async def run() -> None:
    cfg = config_mod.load()
    state = state_mod.load(cfg.state_path)

    lighter = LighterClient(cfg.lighter_pool_id, cfg.lighter.rest_base, cfg.lighter.ws_url)
    chart = ChartFetcher(cfg.chart_api_key, cfg.chart)
    telegram = TelegramPoster(cfg.telegram_bot_token, cfg.telegram_channel_id, cfg.telegram_owner_user_id)
    twitter = TwitterPoster(
        cfg.twitter_api_key, cfg.twitter_api_secret,
        cfg.twitter_access_token, cfg.twitter_access_token_secret,
        cfg.twitter_daily_soft_cap,
    )
    notifier = Notifier(telegram)
    tracker = PositionTracker()
    healthz = Healthz(stale_after_seconds=180)
    pool_url = cfg.pool_url_template.format(pool_id=cfg.lighter_pool_id)

    # --- bootstrap ---
    await lighter.bootstrap_markets()
    initial_positions = await lighter.current_positions()
    tracker.seed(initial_positions)
    log.info("seeded tracker with %d open positions", len(initial_positions))

    # If we have no last_trade_id yet, anchor to the most recent trade so we
    # don't backflood Telegram with months of history on first run.
    if state.last_trade_id is None:
        latest = await lighter.fetch_trades_since(since_trade_id=None, limit=1)
        if latest:
            state.last_trade_id = latest[-1].trade_id
            state_mod.save(state, cfg.state_path)
            log.info("anchored last_trade_id to %d (first run)", state.last_trade_id)

    # --- command handlers ---
    async def on_pause() -> None:
        state.paused = True
        state_mod.save(state, cfg.state_path)

    async def on_resume() -> None:
        state.paused = False
        state_mod.save(state, cfg.state_path)

    async def on_status() -> str:
        return (
            f"paused={state.paused}  last_trade_id={state.last_trade_id}  "
            f"tw_today={state.twitter_count_for(datetime.now(timezone.utc).date())}/"
            f"{cfg.twitter_daily_soft_cap}  open_positions={len(tracker.snapshot())}"
        )

    # --- core processing ---
    queue: asyncio.Queue[Trade] = asyncio.Queue()

    async def process(trade: Trade) -> None:
        if state.paused:
            return
        events = tracker.apply(trade)
        for event in events:
            if not filters_mod.passes_min_notional(event, cfg.min_notional_usd):
                log.info("filtered (under min notional): %s %s", event.kind, event.trade.market_symbol)
                continue
            event.leverage = await lighter.fetch_leverage(event.trade.market_id)
            text = format_event(event, pool_url)
            image = await chart.get_chart(event.trade.market_symbol)
            try:
                await telegram.send_to_channel(text, image)
            except Exception:
                log.exception("telegram send failed")
            try:
                twitter.post(text, state, cfg.state_path, image)
            except Exception:
                log.exception("twitter send failed")

    # --- producers ---
    async def ws_producer() -> None:
        async for trade in lighter.stream_trades():
            await queue.put(trade)

    async def rest_safety_producer() -> None:
        while True:
            await asyncio.sleep(cfg.lighter.rest_safety_poll_seconds)
            try:
                trades = await lighter.fetch_trades_since(state.last_trade_id)
                for t in trades:
                    await queue.put(t)
                healthz.mark_alive()
            except Exception:
                log.exception("safety poll failed")

    # --- single consumer (serializes dedup + state writes) ---
    async def consumer() -> None:
        while True:
            trade = await queue.get()
            healthz.mark_alive()
            if state.last_trade_id is not None and trade.trade_id <= state.last_trade_id:
                continue
            try:
                await process(trade)
            except Exception as e:
                log.exception("process failed")
                await notifier.alert(f"process exception: {e!r}")
            state.last_trade_id = trade.trade_id
            state_mod.save(state, cfg.state_path)

    # --- recap ---
    async def post_recap(window: str) -> None:
        try:
            r = await recap_mod.compute(lighter, window)
            text = recap_mod.format_recap(r, pool_url)
            await telegram.send_to_channel(text)
            twitter.post(text, state, cfg.state_path)
        except NotImplementedError:
            log.info("recap not implemented yet (phase E)")
        except Exception:
            log.exception("recap failed")

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(post_recap, CronTrigger(hour=cfg.daily_recap_utc_hour, minute=0), args=["day"])
    scheduler.add_job(
        post_recap,
        CronTrigger(day_of_week=cfg.weekly_recap_day, hour=cfg.weekly_recap_utc_hour, minute=0),
        args=["week"],
    )
    scheduler.start()

    # --- healthz HTTP ---
    runner = web.AppRunner(healthz.build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.healthz_port).start()
    log.info("healthz listening on :%d", cfg.healthz_port)

    await telegram.dm_owner("lighter-bot started")

    await asyncio.gather(
        ws_producer(),
        rest_safety_producer(),
        consumer(),
        telegram.listen_commands(on_pause, on_resume, on_status),
    )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
