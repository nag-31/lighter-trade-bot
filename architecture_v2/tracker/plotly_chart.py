"""Optional Plotly/Kaleido renderer for exchange-backed lifecycle charts.

Plotly is deliberately optional: Telegram delivery must continue to work in
minimal deployments, so callers can fall back to ``static_chart`` when Plotly
or a headless Chrome runtime is unavailable.
"""

from __future__ import annotations

from architecture_v2.domain.charts import TradeChartSpec


class PlotlyUnavailable(RuntimeError):
    """Raised when optional Plotly/Kaleido export is not installed or usable."""


def _format_price(value) -> str:
    """Keep the header compact while preserving precision for micro-assets."""
    magnitude = abs(float(value))
    if magnitude >= 1000:
        pattern = ",.2f"
    elif magnitude >= 1:
        pattern = ",.4f"
    elif magnitude >= 0.01:
        pattern = ".5f"
    else:
        pattern = ".8f"
    return format(value, pattern).rstrip("0").rstrip(".")


def render_plotly_chart_png(
    spec: TradeChartSpec,
    *,
    width: int = 1200,
    height: int = 700,
) -> bytes:
    try:
        import plotly.graph_objects as go
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise PlotlyUnavailable("plotly is not installed") from exc

    from plotly.subplots import make_subplots

    has_volume = any(candle.volume is not None for candle in spec.candles)
    fig = make_subplots(
        rows=2 if has_volume else 1,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.78, 0.22] if has_volume else [1.0],
        vertical_spacing=0.035,
        specs=[[{"secondary_y": False}], [{"secondary_y": False}]]
        if has_volume
        else [[{"secondary_y": False}]],
    )

    candle_times = [candle.opened_at for candle in spec.candles]
    if spec.candles:
        fig.add_trace(
            go.Candlestick(
                x=candle_times,
                open=[float(candle.open) for candle in spec.candles],
                high=[float(candle.high) for candle in spec.candles],
                low=[float(candle.low) for candle in spec.candles],
                close=[float(candle.close) for candle in spec.candles],
                increasing_line_color="#58d6a7",
                increasing_fillcolor="#58d6a7",
                decreasing_line_color="#fb7185",
                decreasing_fillcolor="#fb7185",
                name="OHLC",
                whiskerwidth=0.55,
                hovertemplate=(
                    "<b>%{x|%b %d %H:%M}</b><br>"
                    "Open %{open:.8f}<br>High %{high:.8f}<br>"
                    "Low %{low:.8f}<br>Close %{close:.8f}<extra></extra>"
                ),
            ),
            row=1,
            col=1,
        )

        if has_volume:
            volume_times = [candle.opened_at for candle in spec.candles if candle.volume is not None]
            volume_values = [float(candle.volume or 0) for candle in spec.candles if candle.volume is not None]
            volume_colors = [
                "#2f9e7a" if candle.close >= candle.open else "#b94b63"
                for candle in spec.candles
                if candle.volume is not None
            ]
            fig.add_trace(
                go.Bar(
                    x=volume_times,
                    y=volume_values,
                    marker_color=volume_colors,
                    opacity=0.72,
                    name="Volume",
                    hovertemplate="Volume %{y:,.4f}<extra></extra>",
                ),
                row=2,
                col=1,
            )

    for marker in spec.markers:
        color = "#22c55e" if marker.side.value == "BUY" else "#ef4444"
        symbol = "triangle-up" if marker.side.value == "BUY" else "triangle-down"
        label = marker.action.value.replace("_", " ")
        if marker.raw_fill_count > 1:
            label += f" x{marker.raw_fill_count}"
        fig.add_trace(
            go.Scatter(
                x=[marker.first_at],
                y=[float(marker.price_vwap)],
                mode="markers+text",
                text=[label.replace("_", " ")],
                textposition="top center" if marker.side.value == "BUY" else "bottom center",
                textfont={"color": color, "size": 10, "family": "Arial, sans-serif"},
                marker={"color": color, "symbol": symbol, "size": 11, "line": {"color": "#0b1017", "width": 1}},
                name=label,
                hovertemplate=(
                    f"<b>{label.replace('_', ' ')}</b><br>Price %{{y:.8f}}<br>"
                    f"Qty {marker.quantity}<br>Fills {marker.raw_fill_count}<extra></extra>"
                ),
                showlegend=False,
            ),
            row=1,
            col=1,
        )

    shapes = [
        {
            "type": "line",
            "x0": spec.opened_at,
            "x1": spec.closed_at or spec.opened_at,
            "y0": float(spec.entry_vwap),
            "y1": float(spec.entry_vwap),
            "line": {"color": "#60a5fa", "dash": "dash"},
        }
    ]
    if spec.exit_vwap is not None:
        shapes.append(
            {
                "type": "line",
                "x0": spec.opened_at,
                "x1": spec.closed_at or spec.opened_at,
                "y0": float(spec.exit_vwap),
                "y1": float(spec.exit_vwap),
                "line": {"color": "#facc15", "dash": "dash"},
            }
        )

    price_points = [candle.close for candle in spec.candles]
    price_points.extend(marker.price_vwap for marker in spec.markers)
    last_price = price_points[-1] if price_points else spec.entry_vwap
    first_price = price_points[0] if price_points else spec.entry_vwap
    move_pct = ((last_price - first_price) / first_price * 100) if first_price else 0
    move_color = "#58d6a7" if move_pct >= 0 else "#fb7185"
    candle_note = "OHLCV" if has_volume else "OHLC"
    if not spec.candles:
        candle_note = "EXECUTION-ONLY"
    active_interval = f"{max(1, spec.interval_seconds // 60)}m"
    if spec.interval_seconds >= 3600:
        active_interval = f"{spec.interval_seconds // 3600}h"

    shapes = [
        *shapes,
        {
            "type": "line",
            "xref": "x",
            "yref": "paper",
            "x0": spec.opened_at,
            "x1": spec.opened_at,
            "y0": 0,
            "y1": 1,
            "line": {"color": "#334155", "dash": "dot", "width": 1},
        },
    ]
    if spec.closed_at is not None:
        shapes.append(
            {
                "type": "line",
                "xref": "x",
                "yref": "paper",
                "x0": spec.closed_at,
                "x1": spec.closed_at,
                "y0": 0,
                "y1": 1,
                "line": {"color": "#334155", "dash": "dot", "width": 1},
            }
        )

    fig.update_layout(
        template="plotly_dark",
        width=width,
        height=height,
        margin={"l": 52, "r": 30, "t": 128, "b": 48},
        paper_bgcolor="#080c12",
        plot_bgcolor="#0b1017",
        font={"family": "Arial, sans-serif", "color": "#d7e0ea", "size": 10},
        title={"text": f"<b>{spec.market_key.rsplit(':', 1)[-1]}</b>  <span style='font-size:11px;color:#718096'>{spec.market_key.split(':', 1)[0].upper()}</span>", "x": 0.035, "y": 0.975, "xanchor": "left", "yanchor": "top", "font": {"size": 20, "color": "#f1f5f9"}},
        xaxis={"rangeslider": {"visible": False}, "showgrid": True, "gridcolor": "#1b2634", "showline": False, "zeroline": False, "tickfont": {"color": "#718096", "size": 9}},
        yaxis={"title": "", "fixedrange": False, "showgrid": True, "gridcolor": "#1b2634", "side": "right", "tickfont": {"color": "#94a3b8", "size": 9}, "zeroline": False},
        shapes=shapes,
        annotations=[
            {
                "text": f"{candle_note} · {active_interval}   <span style='color:{move_color}'>{move_pct:+.2f}%</span>",
                "xref": "paper", "yref": "paper", "x": 0.035, "y": 1.045,
                "showarrow": False, "xanchor": "left", "font": {"color": "#94a3b8", "size": 10},
            },
            {
                "text": f"<b>LAST</b> {_format_price(last_price)}   <b>PnL</b> <span style='color:{'#58d6a7' if spec.realized_pnl >= 0 else '#fb7185'}'>{'+' if spec.realized_pnl >= 0 else '-'}${abs(spec.realized_pnl):,.2f}</span>",
                "xref": "paper", "yref": "paper", "x": 0.965, "y": 1.045,
                "showarrow": False, "xanchor": "right", "font": {"color": "#d7e0ea", "size": 10},
            },
            {
                "text": "<b>1m</b>  5m  15m  1h",
                "xref": "paper", "yref": "paper", "x": 0.965, "y": 1.095,
                "showarrow": False, "xanchor": "right", "font": {"color": "#718096", "size": 9},
            },
        ],
        showlegend=False,
    )
    if has_volume:
        fig.update_yaxes(
            title={"text": "VOL", "font": {"color": "#64748b", "size": 8}},
            tickfont={"color": "#64748b", "size": 8},
            showgrid=False,
            row=2,
            col=1,
        )
    fig.update_xaxes(showticklabels=True, row=1, col=1)
    if has_volume:
        fig.update_xaxes(showticklabels=True, row=2, col=1)
    fig.add_annotation(
        text=f"{spec.candle_provenance} · {spec.completeness}",
        xref="paper", yref="paper", x=0.03, y=-0.07,
        showarrow=False, xanchor="left", font={"color": "#64748b", "size": 9},
    )
    try:
        return bytes(fig.to_image(format="png", width=width, height=height, scale=1))
    except Exception as exc:  # pragma: no cover - depends on Chrome/Kaleido
        raise PlotlyUnavailable("Plotly static export is unavailable") from exc
