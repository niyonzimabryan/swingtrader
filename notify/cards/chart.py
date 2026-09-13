"""The price chart, as a PNG.

matplotlib on the ``Agg`` backend — no display, no interactivity, deterministic
output for the same input. The bars are read out of the **stored card payload**,
not queried at render time: a card is a record of what was said at a moment, and
a chart that silently redrew itself against today's prices would make the page
disagree with the email it was linked from. That also means the chart costs no
database read and cannot 500 because the price plane moved.

PNG by URL rather than a ``data:`` URI, because Gmail strips data URIs from
``<img src>`` and proxies remote images instead. The route that serves it is
``workspace/app.py``'s ``/cards/<uid>/chart.png``.

**No arithmetic on a reported number.** The bars, the entry, the stop and the
target are drawn where the payload says they are. Matplotlib's own axis scaling
is layout, not a statistic.
"""

from __future__ import annotations

from io import BytesIO

#: Rendered at 2x for retina, displayed at 584 CSS px.
FIGURE_WIDTH_INCHES = 7.3
FIGURE_HEIGHT_INCHES = 3.3
DPI = 160

#: Light palette only. An email image cannot follow the reader's colour scheme —
#: `prefers-color-scheme` does not reach an `<img>` — so the chart commits to the
#: light one and the card's dark mode keeps it on a light plate rather than
#: shipping two images and guessing which to link.
INK = "#11151c"
MUTED = "#5c6673"
HAIRLINE = "#e2e6ec"
GROUND = "#ffffff"
LINE = "#1f4fd8"
ENTRY = "#0f7a4d"
STOP = "#b3261e"
TARGET = "#9a6200"


class ChartUnavailable(Exception):
    """No bars, or matplotlib is not installed. The caller serves a 404."""


def _levels(chart: dict) -> list[tuple[str, float, str]]:
    levels = chart.get("levels") or {}
    out = []
    for name, colour in (("entry", ENTRY), ("stop", STOP), ("target", TARGET)):
        value = levels.get(name)
        if value is None:
            continue
        try:
            out.append((name, float(value), colour))
        except (TypeError, ValueError):
            continue
    return out


def render_png(chart: dict) -> bytes:
    """``chart`` is ``payload["chart"]``. Returns PNG bytes.

    ``chart["bars"]`` is a list of ``{"date": "YYYY-MM-DD", "close": float}``;
    ``open``/``high``/``low`` are accepted and ignored, so the stored payload can
    carry the whole bar without this deciding to draw candles one day and lines
    the next.
    """
    bars = [bar for bar in (chart or {}).get("bars") or [] if bar.get("close") is not None]
    if not bars:
        raise ChartUnavailable("this card carries no price bars.")

    try:
        import matplotlib

        matplotlib.use("Agg", force=False)
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MaxNLocator
    except Exception as exc:  # pragma: no cover - import-environment specific
        raise ChartUnavailable(f"matplotlib unavailable: {exc}") from exc

    labels = [str(bar.get("date") or "") for bar in bars]
    try:
        closes = [float(bar["close"]) for bar in bars]
    except (TypeError, ValueError) as exc:
        raise ChartUnavailable(f"a bar carries a non-numeric close: {exc}") from exc

    positions = list(range(len(closes)))

    figure, axes = plt.subplots(
        figsize=(FIGURE_WIDTH_INCHES, FIGURE_HEIGHT_INCHES), dpi=DPI
    )
    figure.patch.set_facecolor(GROUND)
    axes.set_facecolor(GROUND)

    axes.plot(positions, closes, color=LINE, linewidth=1.9, solid_joinstyle="round")
    axes.fill_between(positions, closes, min(closes), color=LINE, alpha=0.07, linewidth=0)

    for name, value, colour in _levels(chart):
        axes.axhline(value, color=colour, linewidth=1.2, linestyle=(0, (5, 4)))
        axes.annotate(
            f"{name} {value:,.2f}",
            xy=(positions[-1], value),
            xytext=(4, 3),
            textcoords="offset points",
            color=colour,
            fontsize=8,
            fontweight="bold",
            ha="right",
            va="bottom",
        )

    axes.set_title(
        str(chart.get("title") or chart.get("ticker") or ""),
        loc="left",
        color=INK,
        fontsize=11,
        fontweight="bold",
        pad=10,
    )
    axes.tick_params(colors=MUTED, labelsize=8, length=0)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(HAIRLINE)
    axes.grid(axis="y", color=HAIRLINE, linewidth=0.8)
    axes.set_axisbelow(True)
    axes.yaxis.set_major_locator(MaxNLocator(nbins=5))

    step = max(1, len(labels) // 6)
    ticks = positions[::step]
    axes.set_xticks(ticks)
    axes.set_xticklabels([labels[index] for index in ticks], rotation=0)
    axes.set_xlim(positions[0], positions[-1] if len(positions) > 1 else positions[0] + 1)

    caption = str(chart.get("as_of_note") or "")
    if caption:
        figure.text(0.012, 0.02, caption, color=MUTED, fontsize=7.5, ha="left", va="bottom")

    figure.tight_layout(rect=(0, 0.05 if caption else 0, 1, 1))
    buffer = BytesIO()
    figure.savefig(buffer, format="png", facecolor=GROUND, edgecolor="none")
    plt.close(figure)
    return buffer.getvalue()
