# Design proposal — Manufacturing Data Chatbot POC

This is the proposal that precedes the code: file layout, the exact template list,
and the compose service layout (model pull, startup ordering, dialect isolation).

---

## 1. File / folder structure

```
manufacturing-data-chatbot/
├── docker-compose.yml            # 3 long-running services + 2 one-shots
├── .env.example                  # OLLAMA_*, DB vars for BOTH roles
├── README.md                     # bring-up, offline, GPU, MSSQL migration
├── example_questions.md          # template-handled vs LLM-handled demo questions
├── docs/
│   └── DESIGN.md                 # this file
├── docker/
│   ├── app.Dockerfile            # python:3.12-slim, deps, uvicorn
│   └── pull-model.sh             # one-shot: wait for ollama, pull model, verify
├── db/
│   └── init/
│       └── 01-create-readonly-role.sh   # runs once on empty volume: creates readonly_user
└── app/
    ├── requirements.txt
    ├── main.py                   # FastAPI: /ask, /health, /examples, static mount
    ├── config.py                 # env -> typed settings (single source)
    ├── readiness.py              # backoff wait: DB reachable + model actually responds
    ├── pipeline.py               # orchestrates: template -> else LLM -> guard -> execute -> answer
    ├── entities.py               # line / date-range / top-N / category / severity extraction
    ├── templates.py              # the 11 deterministic question templates
    ├── llm_query.py              # Ollama HTTP client + prompt assembly (schema + few-shot)
    ├── sql_guard.py              # sqlglot validation, whitelist, row cap, timeout
    ├── responder.py              # result set -> short natural-language answer
    ├── schema.yaml               # SEMANTIC LAYER: tables/columns/types/descriptions/enums
    ├── seed_data.py              # idempotent: schema + fixed-seed data + grants
    ├── fewshot/
    │   └── postgres.yaml         # few-shot question->SQL pairs (ONE file, swap per dialect)
    ├── db/
    │   ├── __init__.py
    │   ├── dialect.py            # ALL dialect-specific concerns live here
    │   └── connection.py         # admin + readonly connections, timeouts
    ├── static/
    │   └── index.html            # vanilla JS chat UI, no build step
    └── tests/
        ├── test_sql_guard.py      # adversarial: injection, DDL, catalogue access
        ├── test_entities.py       # date-range parsing, line/top-N extraction
        ├── test_templates.py      # routing + every template's SQL through the guard
        ├── test_fewshot.py        # every few-shot example must survive the guard
        ├── test_pipeline.py       # routing, rejection and outage envelopes
        ├── test_api.py            # readiness gate, /ask, /health, /examples
        └── test_seed_shape.py     # dataset shape and determinism
```

Nothing outside `app/db/` imports `psycopg` or writes Postgres-only syntax.

---

## 2. Question templates (deterministic path, no LLM)

Eleven templates, matched in priority order by regex + entity extraction. Every one
is parameterised SQL — user text never reaches the SQL string, only bound parameters.

| # | id | Example question | Returns |
|---|----|------------------|---------|
| 1 | `good_vs_bad` | "How many good vs bad parts on Line A last week?" | good, reject, total, yield % |
| 2 | `reject_breakdown` | "Reject breakdown by category for Line B in July" | one row per reject category + share |
| 3 | `top_alarms_frequency` | "Top 5 alarms on Line C in the last 30 days" | code, description, severity, occurrences |
| 4 | `top_alarms_downtime` | "Which alarms caused the most downtime last month?" | code, total downtime minutes, occurrences |
| 5 | `batch_count` | "How many batches did Line A run last week?" | count, split by status |
| 6 | `production_totals` | "Total parts produced yesterday" / "average parts per batch in July" | total + average per batch |
| 7 | `yield_by_line` | "What was the yield per line last month?" | per-line good/total and yield % |
| 8 | `batch_status_summary` | "How many batches were aborted last month?" | count per status |
| 9 | `daily_production` | "Daily production for Line A over the last 14 days" | one row per day (uses dialect date-trunc) |
| 10 | `alarm_list` | "Show critical alarms on Line B this week" / "any ongoing alarms?" | alarm rows with duration |
| 11 | `worst_batches` | "Which batches missed target by the most last month?" | batch, target, actual, shortfall |

Shared entity extraction: **line** (`Line A`/`line a`/`A`), **date range**
(today, yesterday, this/last week, this/last month, last N days/hours, `in July`,
`in July 2026`, `on 2026-07-14`, `between X and Y`, this year; default = last 30 days),
**top-N** (`top 5`, `top three`, default 5), **severity**, **reject category**,
**product code**.

Anything that matches none of the eleven falls through to the LLM.

---

## 3. Docker compose service layout

Three long-running services, exactly as specified, plus two one-shot helpers
(explicitly allowed for the model pull; the second one seeds the DB):

| Service | Image | Kind | Purpose |
|---------|-------|------|---------|
| `db` | `postgres:16.4-alpine` (pinned) | long-running | Data. Named volume `pgdata`. Healthcheck `pg_isready`. Init script creates `readonly_user`. |
| `ollama` | `ollama/ollama:<pinned>` | long-running | Serves the model on 11434. Named volume `ollama_models` so the pull happens once, ever. Healthcheck `ollama list`. |
| `ollama-init` | same ollama image | **one-shot** | Waits for the server, `ollama pull $OLLAMA_MODEL` if absent, verifies, exits 0. Reuses the already-present image so no extra download. |
| `seed` | app image | **one-shot** | Connects as the **admin** role: creates tables, seeds fixed-seed data, `GRANT SELECT` to `readonly_user`. Idempotent — no-ops if `batches` is non-empty. |
| `app` | app image (built) | long-running | FastAPI + static UI on 8000. Bind-mounts `./app`, runs `uvicorn --reload`. |

### Model pull

The `ollama-init` container sets `OLLAMA_HOST=http://ollama:11434` and runs the
CLI against the **server**, so the download lands in the server's named volume,
not the init container. On every later `up`, the script sees the model already
in `ollama list` and exits immediately. No mounted volume needed on the init
container, which keeps the ownership story simple.

### Startup ordering

```
db ──(service_healthy)──> seed ──(service_completed_successfully)──┐
                                                                   ├──> app
ollama ──(service_healthy)──> ollama-init ──(completed_successfully)┘
```

So on a fresh clone `docker compose up` blocks `app` until the model is on disk
and the tables are populated. Belt and braces, `app` *also* runs its own
readiness loop (`readiness.py`) with exponential backoff:

1. DB: connect as readonly, `SELECT 1`, confirm the four tables exist.
2. Model: real `POST /api/generate` with a 1-token prompt — proves the model
   *responds*, not merely that the port is open, and warms the weights so the
   first demo question isn't the cold-load one.

Until both pass, `/ask` returns HTTP 503 with a plain-English "still warming up"
message and `/health` reports which check is outstanding; the UI shows a banner.
After the retry budget is exhausted it flips to a hard `failed` state with the
underlying error — a clear failure, never a silent hang.

### Dialect isolation

`app/db/dialect.py` defines a `Dialect` protocol; `PostgresDialect` implements it
today and a commented `SqlServerDialect` sketch sits beside it. It owns:

| Concern | Postgres today | SQL Server later |
|---|---|---|
| driver / connect | `psycopg` | `pyodbc` |
| sqlglot dialect string | `postgres` | `tsql` |
| row cap | `LIMIT n` | `TOP n` (sqlglot rewrites this on generate) |
| param placeholder | authored as `?`, sent as `%s` | authored as `?`, sent as `?` |
| duration in seconds | `EXTRACT(EPOCH FROM (e - s))` | `DATEDIFF(SECOND, s, e)` |
| truncate to day | `DATE_TRUNC('day', x)` | `CAST(x AS DATE)` |
| statement timeout | `-c statement_timeout=…` | driver `timeout=` |
| identifier quoting | `"x"` | `[x]` |
| few-shot file | `fewshot/postgres.yaml` | `fewshot/tsql.yaml` |

Because the guard must parse SQL *before* values are bound, templates author
parameters with a neutral `?` — the one marker sqlglot parses in both dialects.
`Dialect.to_driver_sql()` rewrites it to the driver's marker (`%s` for psycopg)
at execution time, working on the parse tree so a `?` inside a string literal is
untouched.

Templates never write date functions: **all date ranges are resolved to concrete
datetimes in Python and passed as bound parameters**, so the only dialect-specific
SQL in the template path is the day-truncation in template 9 and the duration
maths in templates 4 and 10 — both behind dialect methods.
