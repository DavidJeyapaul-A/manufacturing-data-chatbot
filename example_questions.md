# Demo questions

Split by which path answers them. The UI labels every answer with the route it
took (`template · no LLM` or `LLM · qwen2.5-coder:7b`), so you can point at the
badge while demoing.

Every question below was checked against the actual router — the template
assignments are generated from the code, not from memory.

---

## Handled by templates (no LLM — instant, exact, identical every time)

These are the ones to lead with. They return in milliseconds and give the same
answer on every machine, so they are safe for a live demo.

### Good vs bad parts — `good_vs_bad`
- How many good vs bad parts did Line A make last week?
- Good vs bad parts on Line B yesterday
- How many good parts on 2026-07-14?

### Reject breakdown by category — `reject_breakdown`
- Reject breakdown by category for Line B last month
- What are the reject reasons on Line C in July?

### Top alarms by frequency — `top_alarms_frequency`
- Top 5 alarms on Line C in the last 30 days
- Which alarms happen most often on Line A?
- Top 3 critical alarms this month

### Top alarms by downtime — `top_alarms_downtime`
- Which alarms caused the most downtime last month?
- Top 3 alarms by downtime on Line A

### Batch counts — `batch_count`
- How many batches did Line A run last week?
- Number of batches in July
- How many batches did Line A run between 2026-07-01 and 2026-07-10?

### Production totals and averages — `production_totals`
- Total parts produced yesterday
- Average parts per batch in July
- How much did we produce on Line B this month?

### Yield per line — `yield_by_line`
- What was the yield per line last month?
- Which line had the worst scrap rate?

### Batch status — `batch_status_summary`
- How many batches were aborted last month?
- Are any batches still running?

### Daily production trend — `daily_production`
- Daily production for Line A over the last 14 days
- Show me the production trend for Line C

### Alarm listings — `alarm_list`
- Show critical alarms on Line B this week
- Any ongoing alarms?
- List the latest alarms on Line A

### Batches that missed target — `worst_batches`
- Which batches missed target by the most last month?
- Worst performing batches on Line C

---

## Handled by the LLM fallback (local model writes the SQL)

Nothing here matches a template, so the question goes to the model with the
schema and the few-shot examples. Expect **10-60 seconds on CPU** for the first
one — say so before you run it, or run one early to warm the model.

These are the ten shipped in `app/fewshot/postgres.yaml`, so the model has seen
this *shape* of question and answers them reliably:

- Which product had the worst reject rate last month?
- How many alarms fired when no batch was running?
- What is the average batch duration in hours for each line?
- Show me the 5 longest alarms in the last 7 days.
- Which batches ran overnight, starting one day and finishing the next?
- How many critical alarms did each line have this month?
- What was the total good output per product code in July 2026?
- Which batches last week had no alarms at all?
- How does each line's yield compare against its target quantity?
- Which alarm code causes the most downtime per occurrence?

Genuinely unseen questions — riskier, but the interesting demo:

- Which day of the week has the worst yield?
- What is the average alarm duration on Line B?
- How many alarms fired on each line this month?
- Compare good output per product code in July
- Which line has the longest gap between batches?
- Do longer batches have worse yield?

### Why these are not templates

Several look close to a template but ask for a **different grouping**. The
templates deliberately *decline* those rather than answer the wrong question:

| Question | Nearby template | Why it declines |
|---|---|---|
| total good output **per product code** | `production_totals` | groups by nothing, not by product |
| critical alarms **for each line** | `top_alarms_frequency` | groups by alarm code, not by line |
| **average** downtime per occurrence | `top_alarms_downtime` | sums downtime, never averages |
| the 5 **longest** alarms | `alarm_list` | orders by time, not by duration |
| yield vs **target quantity** | `yield_by_line` | has no notion of target attainment |
| batches with **no alarms at all** | `alarm_list` | needs an anti-join |
| worst yield **by day of the week** | `daily_production` | truncates to a date, not a weekday |

This is the design point worth making in the demo: a template answers exactly or
not at all. Confidently wrong is the one outcome that is not acceptable.

---

## Questions that get refused

Useful for showing the guardrails. Type these into the same box:

- `Delete all the batches` — the model is asked for SQL; anything that is not a
  single SELECT is rejected by `sql_guard` before it can reach the database.
- `Show me the database users` — `pg_shadow` / `pg_catalog` are not in the
  whitelist, so the query is rejected even though it is a valid SELECT.
- `Drop the alarms table` — rejected.

All three return **"I couldn't safely answer that"** plus the reason. Expand the
panel to show the SQL that was blocked. Nothing is auto-repaired: a rejected
query is reported, never rewritten into something that "probably" meant the same.

And the belt-and-braces point: even if the guard were bypassed entirely, the
query connection belongs to `readonly_user`, which holds `SELECT` and nothing
else. The guard turns a bad query into a clear message; the role is what makes a
write impossible.
