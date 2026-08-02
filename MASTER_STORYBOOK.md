# Crypto Scientist - Simple Storybook

Short, plain-English explanation of what we are building.

## The big idea

When a trade closes, Telegram should show:

1. a clear PnL card: how much money was made or lost;
2. a trade chart: where the buys and sells happened.

Both pictures must describe the same trade.

## What works now

- The bot watches trading activity.
- It records opens, adds, partial closes, and full closes.
- It calculates realized PnL.
- It sends a PnL card for a full close.
- It now sends the PnL card and execution chart together as a Telegram album.
- The chart shows real execution timing and BUY/SELL markers.
- If chart creation or delivery fails, the PnL card still goes out.
- The deployed version passed the local 968-test suite.

## What is not done yet

- Live market candle data is not connected, so the first chart is execution-only.
- The interactive Journal chart is future work.
- V2 is not replacing the old accounting system.

## Safety promise

The chart explains a trade. It does not change orders, balances, PnL, or
accounting. The old PnL-card path remains the safety net.

## Latest milestone

On 2026-08-02, chart integration commit
`2bf745a6b23a52f4357e7ee8c07dc5c335767c8b` was pushed and deployed. All four
application services restarted successfully, all four health endpoints returned
HTTP 200, and all three databases remained intact.
