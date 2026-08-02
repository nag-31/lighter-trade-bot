# Crypto Scientist — Simple Storybook

This is the short, plain-English explanation of what we are building.

## The big idea

When a trade closes, Telegram should show:

1. a clear PnL card: “How much money was made or lost?”;
2. a trade chart: “Where were the buys and sells?”

Both pictures must describe the same trade.

## What works today

- The bot watches trading activity.
- It records opens, adds, partial closes, and full closes.
- It calculates realized PnL.
- It already sends a PnL card for a full close.
- The new V2 chart engine can draw BUY and SELL markers.
- V2 source is installed on the VM and its 54 tests pass.

## What we just added

We connected the chart engine to the existing full-close PnL-card message.

The goal is one Telegram post containing two images:

- the familiar PnL card;
- the new trade chart.

If the chart cannot be made, the old PnL card must still be sent. A chart
problem must never hide a real trade alert.

The local test suite passes: **968 tests**.

## What is not done yet

- Live candle data is not connected yet.
- The first chart may show the trade timeline and markers without candles.
- The interactive Journal chart is still future work.
- V2 is not replacing the old accounting system.

## Safety promise

The chart is for explanation. It does not change orders, balances, PnL, or
accounting. The old alert remains the safety net.

## Latest milestone

On 2026-07-31, V2 was deployed and all four application services were restarted
successfully. On 2026-08-02, the chart was attached locally to the PnL-card
message. The next step is deploying this new integration and checking one real
close alert.
