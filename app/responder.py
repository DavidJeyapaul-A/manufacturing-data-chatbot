"""Turns a result set into a short natural-language answer.

Deliberately templated, not generated. Two reasons:
  * it is instant, where a second LLM call would add 10-30s on CPU
  * it cannot hallucinate a number that is not in the result set

The LLM's job in this system is SQL, not prose. Phrasing numbers is the one
place where a template is strictly better than a model.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from db.connection import QueryResult


def _fmt(value: Any) -> str:
    """Human-friendly scalar formatting: thousands separators, tidy decimals."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return str(value)


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _label(context: dict[str, Any]) -> str:
    """' on Line A over the last 30 days' — the scope of whatever we counted."""
    bits = []
    if context.get("line"):
        bits.append(f"on {context['line']}")
    period = context.get("period")
    if period:
        # 'right now' is already a complete phrase; "over right now" is not.
        bits.append(period if period.startswith("right now") else f"over {period}")
    return (" " + " ".join(bits)) if bits else ""


# --------------------------------------------------------------------------- #
# Per-template summaries
# --------------------------------------------------------------------------- #

def _say_good_vs_bad(rows: list[dict], ctx: dict) -> str:
    r = rows[0]
    total = _num(r.get("total_parts"))
    if not total:
        return f"No parts were recorded{_label(ctx)}."
    return (
        f"{_fmt(r.get('good_parts'))} good parts and {_fmt(r.get('bad_parts'))} bad parts"
        f"{_label(ctx)} — {_fmt(r.get('total_parts'))} in total, a yield of "
        f"{_fmt(r.get('yield_pct'))}%."
    )


def _say_reject_breakdown(rows: list[dict], ctx: dict) -> str:
    total = sum(_num(r.get("parts")) for r in rows)
    if not total:
        return f"No rejects were recorded{_label(ctx)}."
    parts = ", ".join(
        f"{r['category'].replace('reject_', '')} {_fmt(r.get('parts'))} "
        f"({100 * _num(r.get('parts')) / total:.0f}%)"
        for r in rows
    )
    worst = rows[0]["category"].replace("reject_", "")
    return (
        f"{_fmt(total)} rejected parts{_label(ctx)}, split as: {parts}. "
        f"The biggest cause was {worst}."
    )


def _say_top_alarms_frequency(rows: list[dict], ctx: dict) -> str:
    if not rows:
        return f"No alarms were recorded{_label(ctx)}."
    top = rows[0]
    listed = "; ".join(
        f"{r['alarm_code']} {r['description']} ({_fmt(r.get('occurrences'))}x)" for r in rows
    )
    return (
        f"The most frequent alarm{_label(ctx)} was {top['alarm_code']} "
        f"({top['description']}, {top['severity']}) with {_fmt(top.get('occurrences'))} "
        f"occurrences. Full list: {listed}."
    )


def _say_top_alarms_downtime(rows: list[dict], ctx: dict) -> str:
    if not rows:
        return f"No completed alarms were recorded{_label(ctx)}, so there is no downtime to total."
    top = rows[0]
    total = sum(_num(r.get("downtime_minutes")) for r in rows)
    listed = "; ".join(
        f"{r['alarm_code']} {r['description']} ({_fmt(r.get('downtime_minutes'))} min "
        f"over {_fmt(r.get('occurrences'))}x)"
        for r in rows
    )
    return (
        f"{top['alarm_code']} ({top['description']}) caused the most downtime{_label(ctx)}: "
        f"{_fmt(top.get('downtime_minutes'))} minutes across {_fmt(top.get('occurrences'))} "
        f"occurrences. These {len(rows)} alarms account for {_fmt(total)} minutes in total. "
        f"Breakdown: {listed}."
    )


def _say_batch_count(rows: list[dict], ctx: dict) -> str:
    if not rows:
        return f"No batches were run{_label(ctx)}."
    total = sum(_num(r.get("batches")) for r in rows)
    by_status: dict[str, float] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + _num(r.get("batches"))
    status_text = ", ".join(f"{_fmt(v)} {k}" for k, v in sorted(by_status.items()))
    lines = {r["line_name"] for r in rows}
    scope = f" across {len(lines)} lines" if len(lines) > 1 else ""
    return f"{_fmt(total)} batches were run{_label(ctx)}{scope}: {status_text}."


def _say_production_totals(rows: list[dict], ctx: dict) -> str:
    r = rows[0]
    if not _num(r.get("batches")):
        return f"No production was recorded{_label(ctx)}."
    return (
        f"{_fmt(r.get('total_parts'))} parts were produced across {_fmt(r.get('batches'))} "
        f"batches{_label(ctx)}, of which {_fmt(r.get('good_parts'))} were good. "
        f"That averages {_fmt(r.get('avg_parts_per_batch'))} parts per batch."
    )


def _say_yield_by_line(rows: list[dict], ctx: dict) -> str:
    if not rows:
        return f"No production was recorded{_label(ctx)}."
    listed = ", ".join(
        f"{r['line_name']} {_fmt(r.get('yield_pct'))}% ({_fmt(r.get('good_parts'))} good "
        f"of {_fmt(r.get('total_parts'))})"
        for r in rows
    )
    if len(rows) == 1:
        return f"Yield{_label(ctx)}: {listed}."
    return (
        f"{rows[0]['line_name']} had the best yield{_label(ctx)} at "
        f"{_fmt(rows[0].get('yield_pct'))}%, {rows[-1]['line_name']} the worst at "
        f"{_fmt(rows[-1].get('yield_pct'))}%. Full picture: {listed}."
    )


def _say_batch_status_summary(rows: list[dict], ctx: dict) -> str:
    status = ctx.get("status", "matching")
    if not rows:
        return f"There are no {status} batches{_label(ctx)}."
    lines = ", ".join(sorted({r["line_name"] for r in rows}))
    newest = rows[0]
    return (
        f"{_fmt(len(rows))} {status} batches{_label(ctx)} ({lines}). Most recent: "
        f"{newest['batch_id']} on {newest['line_name']} making {newest['product_code']}, "
        f"started {_fmt(newest.get('start_time'))}."
    )


def _say_daily_production(rows: list[dict], ctx: dict) -> str:
    if not rows:
        return f"No production was recorded{_label(ctx)}."
    best = max(rows, key=lambda r: _num(r.get("total_parts")))
    total = sum(_num(r.get("total_parts")) for r in rows)
    return (
        f"{len(rows)} days of production{_label(ctx)}, {_fmt(total)} parts in total "
        f"({_fmt(total / len(rows))} per day on average). The best day was "
        f"{_fmt(best.get('production_day'))} with {_fmt(best.get('total_parts'))} parts. "
        f"The table below has the day-by-day detail."
    )


def _say_alarm_list(rows: list[dict], ctx: dict) -> str:
    if not rows:
        what = "ongoing alarms" if ctx.get("ongoing") else "alarms"
        return f"There are no {what}{_label(ctx)}."
    codes: dict[str, int] = {}
    for r in rows:
        codes[r["alarm_code"]] = codes.get(r["alarm_code"], 0) + 1
    common = max(codes.items(), key=lambda kv: kv[1])
    newest = rows[0]
    return (
        f"{_fmt(len(rows))} alarms{_label(ctx)}. The most common was {common[0]} "
        f"({common[1]}x). Most recent: {newest['alarm_code']} — {newest['description']} "
        f"({newest['severity']}) on {newest['line_name']} at {_fmt(newest.get('start_time'))}."
    )


def _say_worst_batches(rows: list[dict], ctx: dict) -> str:
    if not rows:
        return f"No completed batches were found{_label(ctx)}."
    missed = [r for r in rows if _num(r.get("shortfall")) > 0]
    if not missed:
        return f"Every completed batch{_label(ctx)} met or beat its target."
    worst = missed[0]
    return (
        f"{_fmt(len(missed))} of the {_fmt(len(rows))} batches shown missed target"
        f"{_label(ctx)}. The worst was {worst['batch_id']} on {worst['line_name']} "
        f"({worst['product_code']}): {_fmt(worst.get('actual_parts'))} parts against a "
        f"target of {_fmt(worst.get('target_quantity'))}, short by "
        f"{_fmt(worst.get('shortfall'))}."
    )


_SUMMARISERS = {
    "good_vs_bad": _say_good_vs_bad,
    "reject_breakdown": _say_reject_breakdown,
    "top_alarms_frequency": _say_top_alarms_frequency,
    "top_alarms_downtime": _say_top_alarms_downtime,
    "batch_count": _say_batch_count,
    "production_totals": _say_production_totals,
    "yield_by_line": _say_yield_by_line,
    "batch_status_summary": _say_batch_status_summary,
    "daily_production": _say_daily_production,
    "alarm_list": _say_alarm_list,
    "worst_batches": _say_worst_batches,
}


# --------------------------------------------------------------------------- #
# Generic summary — the LLM path, where the shape is not known in advance
# --------------------------------------------------------------------------- #

def _say_generic(result: QueryResult) -> str:
    rows = result.dicts()
    if not rows:
        return "That query ran successfully but matched no rows."

    # Single scalar: answer it as a sentence rather than a one-cell table.
    if len(rows) == 1 and len(result.columns) == 1:
        col = result.columns[0]
        return f"{_readable(col)}: {_fmt(rows[0][col])}."

    if len(rows) == 1:
        pairs = ", ".join(f"{_readable(c)} {_fmt(rows[0][c])}" for c in result.columns)
        return f"One row: {pairs}."

    lead = rows[0]
    # Enough columns to include the one the query ordered by — on the LLM path
    # that is usually the column the question was actually about.
    lead_text = ", ".join(f"{_readable(c)} {_fmt(lead[c])}" for c in result.columns[:4])
    suffix = " (capped at the row limit)" if result.truncated else ""
    return (
        f"{_fmt(result.row_count)} rows{suffix}. The first is {lead_text}. "
        f"The full result is in the table below."
    )


def _readable(column: str) -> str:
    return column.replace("_", " ")


# --------------------------------------------------------------------------- #

def summarise(
    result: QueryResult,
    template_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Best available natural-language summary of a result set."""
    context = context or {}
    rows = result.dicts()

    summariser = _SUMMARISERS.get(template_id or "")
    if summariser and rows:
        try:
            return summariser(rows, context)
        except (KeyError, IndexError, TypeError):
            # A template summary must never turn a good answer into an error —
            # the numbers are still right, so fall back to the generic phrasing.
            pass
    if summariser and not rows:
        try:
            return summariser([], context)
        except (KeyError, IndexError, TypeError):
            pass
    return _say_generic(result)


__all__ = ["summarise"]
