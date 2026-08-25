# Manufacturing Data Chatbot (POC)

Ask natural-language questions about production data and get an answer, the SQL
that produced it, and how long it took.

Everything runs in containers on your machine. The only AI is an open-weight
model served by a local Ollama container — **no OpenAI, no Anthropic, no cloud
API of any kind**. After the model is pulled once, the whole stack runs with the
network disabled.

```
┌──────────┐   HTTP    ┌─────────────┐  SQL (readonly_user)  ┌────────────┐
│ browser  │ ────────▶ │  app        │ ────────────────────▶ │ db         │
│ chat UI  │           │  FastAPI    │                       │ PostgreSQL │
└──────────┘           └──────┬──────┘                       └────────────┘
                              │ http://ollama:11434
                              ▼
                       ┌─────────────┐
                       │ ollama      │  qwen2.5-coder:7b
                       └─────────────┘
```

---

## 1. Bring it up on a fresh machine

Prerequisites: Docker with Compose v2. Nothing else — no local Python, no pip.

```bash
git clone <this-repo> manufacturing-data-chatbot
cd manufacturing-data-chatbot

cp .env.example .env          # required: compose reads .env, which is git-ignored

docker compose up --build
```

Then open **http://localhost:8000**.

The first bring-up downloads the model (~4.7 GB for `qwen2.5-coder:7b`), so it
takes a while. The page comes up immediately and shows a "warming up" banner
naming exactly what it is still waiting for. Watch progress with:

```bash
docker compose logs -f ollama-init    # the model download
docker compose logs -f app            # readiness checks
```

When `/health` returns 200 and the banner clears, the chatbot is live. Try:

> How many good vs bad parts did Line A make last week?

See [example_questions.md](example_questions.md) for a full demo script.

### Everyday commands

```bash
docker compose up -d               # start in the background
docker compose down                # stop, KEEP data and model
docker compose down -v             # stop and WIPE EVERYTHING — including the
                                   # model volume, forcing a ~4.7 GB re-download
docker compose down && docker volume rm mfg_pgdata   # reset ONLY the database
docker compose logs -f app         # follow the app
docker compose restart app         # after changing something uvicorn missed
docker compose ps                  # what is healthy
```

Source is bind-mounted and uvicorn runs with `--reload`: edit anything under
`app/` and the server restarts by itself. No rebuild. Rebuild only when
`requirements.txt` changes:

```bash
docker compose up -d --build app
```

### Running the tests

199 tests, none of which need a database or a model:

```bash
docker compose run --rm --no-deps app python -m pytest tests/ -q
```

---

## 2. How the model pull works

The `ollama/ollama` container starts **empty** — this is the classic first
gotcha. A one-shot `ollama-init` service handles it:

1. It waits for the `ollama` service to answer its API.
2. It runs `ollama pull $OLLAMA_MODEL` **as a client**: its `OLLAMA_HOST` points
   at `http://ollama:11434`, so the *server* does the downloading and the blobs
   land in the server's `ollama_models` named volume — not inside the throwaway
   init container.
3. It verifies with `ollama show`, because appearing in `ollama list` is not
   proof the file is intact.
4. It exits 0. `app` has `depends_on: ollama-init: service_completed_successfully`,
   so the app does not start until the model is really on disk.

Because the volume is named and persistent, **the download happens exactly once,
ever**. Every later `docker compose up` finds the model already present and
`ollama-init` exits in under a second. `docker compose down -v` is the only
thing that discards it — which is exactly why `-v` is the wrong way to reset the
database. Remove `mfg_pgdata` by name instead.

To use a different model:

```bash
# in .env
OLLAMA_MODEL=qwen2.5-coder:1.5b     # much faster on CPU, weaker SQL
docker compose up -d                # ollama-init pulls the new one
```

`qwen2.5-coder:7b` is the default because it is genuinely good at SQL for its
size. On a CPU-only laptop expect **10-60 s** for an LLM-path answer. The
template path is unaffected — it never calls the model at all.

---

## 3. Running offline

Once the model is in the volume, nothing in the running system reaches the
internet. To prove it:

```bash
docker compose up -d                   # confirm it works
docker compose down

# disconnect the machine from the network, then:
docker compose up -d
```

It comes up and answers questions normally. Verify there is no egress:

```bash
docker compose exec app python -c "import socket; socket.create_connection(('1.1.1.1',53),3)"
# fails when the host is offline — and the chatbot still works
```

What this means in practice:

| Needs the internet | Never needs the internet |
|---|---|
| `docker compose build` (pip install) | answering questions |
| the one-time `ollama pull` | the database |
| pulling the base images | the UI (no CDN, no external font) |

The frontend deliberately has no CDN link, no web font and no build step — a
page that fetched a stylesheet would render broken on an air-gapped network.

To pre-stage a truly air-gapped machine, build and pull on a connected one, then
move the images and the model volume:

```bash
docker save postgres:16.4-alpine ollama/ollama:0.9.0 mfg-chatbot-app:latest -o images.tar
docker run --rm -v mfg_ollama_models:/data -v "$PWD":/backup alpine \
  tar czf /backup/ollama_models.tar.gz -C /data .
# on the target machine:
docker load -i images.tar
docker volume create mfg_ollama_models
docker run --rm -v mfg_ollama_models:/data -v "$PWD":/backup alpine \
  tar xzf /backup/ollama_models.tar.gz -C /data
```

---

## 4. Enabling GPU (NVIDIA hosts)

CPU-only by default so it runs anywhere. On a machine with an NVIDIA GPU:

1. Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host.
2. Uncomment the `deploy:` block in the `ollama` service in `docker-compose.yml`
   (it is already written, just commented out).
3. Recreate the container:

```bash
docker compose up -d --force-recreate ollama
docker compose exec ollama nvidia-smi     # should list your GPU
```

Then lower `OLLAMA_TIMEOUT` in `.env` — GPU answers land in 1-3 s rather than
30-60 s.

---

## 5. How a question is answered

```
question
   │
   ├─▶ templates.py — 11 regex + entity patterns ──hit──▶ parameterised SQL
   │                                                            │
   └─miss─▶ llm_query.py — schema.yaml + few-shot ──▶ model-written SQL
                                                                │
                              ┌─────────────────────────────────┘
                              ▼
                        sql_guard.py     ← BOTH paths, no exceptions
                              ▼
                     run_select() as readonly_user
                              ▼
                        responder.py → plain-English answer
```

**Template path.** Eleven hand-written patterns cover the common questions.
No model involved, so they are instant, exact and identical on every machine —
lead with these in a demo. Extracted entities (line, date range, top-N,
severity) become **bound parameters**; user text is never concatenated into SQL.

A template that cannot group the way a question asks **declines** and lets the
LLM handle it. "Reject rate by product" will not be answered by the per-line
yield template. Confidently wrong is the one outcome worth engineering against.

**LLM path.** Anything unmatched goes to the local model with the semantic layer
(`schema.yaml`), ten worked question → SQL examples, and a strict instruction to
emit a single SELECT. Temperature 0 and a fixed seed, so the same question gives
the same SQL.

**The guard sits on the join, not on a branch** — there is no route to the
database that skips it. `sql_guard.py`:

1. parses with sqlglot (unparseable → rejected)
2. requires exactly one statement, and it must be a `SELECT`
3. rejects any write/DDL/admin node anywhere in the tree
4. rejects any table or column not in the `schema.yaml` whitelist — which also
   blocks `pg_catalog` and `information_schema`
5. allows only whitelisted functions (no `pg_sleep`, `pg_read_file`, `dblink`)
6. injects a row cap, or clamps one that is too large
7. **regenerates the SQL from the parse tree**, so anything the parser did not
   understand cannot survive to execution

If validation fails the answer is *"I couldn't safely answer that"* plus the
reason. There is **no auto-repair**: a rejected query is reported, never
rewritten into something that probably meant the same thing.

### Two database roles

| Role | Used by | Privileges |
|---|---|---|
| `mfg_admin` | the one-shot `seed` service, and nothing else | owner |
| `readonly_user` | **every chatbot query** | `CONNECT`, `USAGE`, `SELECT` |

`db/init/01-create-readonly-role.sh` creates the role on first bring-up.
Because Postgres runs init scripts **only against an empty data volume**, that
script never runs on a volume that already exists — so `seed_data.py` also
creates the role if it is missing, and re-applies its password from `.env`,
every time it runs. That makes a stale volume self-healing and means a password
change no longer requires `down -v`. `seed_data.py` then creates the tables and
grants `SELECT` on them. The query path
opens `READONLY_DATABASE_URL` and no other connection, so the chatbot is
*physically* incapable of writing — the guard is the friendly error message, the
role is the actual guarantee. Belt and braces: the session is also set
`read_only`, with a server-side `statement_timeout`.

---

## 6. The data

`seed_data.py` is **idempotent** (it only generates when `batches` is empty) and
uses a **fixed RNG seed**, so every machine gets the same dataset:

- **3 lines**, **~790 batches** over 60 days, 3-6 per line per day, 1-4 h each
- **~3,170 part_count rows** — four per batch, tidy/long format
- **~1,570 alarms**, 0-5 per batch plus ~9% standalone (`batch_id IS NULL`)

Deliberate awkwardness, so the demo is not artificially easy:

- ~5% of batches start at 22:00-23:30 and **end after midnight** — these break
  naive date filtering
- ~1% are still `running` (`end_time IS NULL`)
- ~2% were `aborted`, ending early with lower output
- ~1% of alarms are ongoing (`end_time IS NULL`), only ever on running batches
- output is often *under* target, not always over

By default the 60-day window ends **today**, so "yesterday" and "last week"
always have data. Set `SEED_END_DATE=2026-08-24` in `.env` to pin the calendar
too and get byte-identical rows.

Re-seed from scratch (database only — the model volume is left alone):

```bash
docker compose down
docker volume rm mfg_pgdata
docker compose up -d
```

Re-running `seed` on a populated database is safe — it re-applies the grants and
touches nothing else:

```bash
docker compose run --rm seed
```

---

## 7. Layout

```
docker-compose.yml          db + ollama + app, plus ollama-init and seed one-shots
.env.example                every setting, commented
docker/
  app.Dockerfile            python:3.12-slim + deps
  pull-model.sh             the one-shot model pull
db/init/
  01-create-readonly-role.sh   creates readonly_user (roles only — no tables yet)
app/
  main.py                   FastAPI: /ask, /health, /examples, static UI
  pipeline.py               question → template-or-LLM → guard → execute → answer
  readiness.py              backoff wait for DB + model; drives the UI banner
  templates.py              the 11 deterministic templates
  entities.py               line / date range / top-N / severity extraction
  llm_query.py              Ollama HTTP client + prompt assembly
  sql_guard.py              sqlglot validation, whitelist, row cap
  responder.py              result set → plain-English summary
  semantic.py               loads schema.yaml
  schema.yaml               SEMANTIC LAYER — whitelist and prompt schema, one file
  seed_data.py              schema + fixed-seed data + grants (idempotent)
  config.py                 env → typed settings
  fewshot/postgres.yaml     question → SQL examples (ONE file, swappable per dialect)
  db/dialect.py             ALL vendor-specific concerns live here
  db/connection.py          the only module that opens a connection
  static/index.html         chat UI, vanilla JS, no build step
  tests/                    199 tests, no DB or model required
```

`schema.yaml` is the single source of truth for both the guard's whitelist and
the prompt's schema description, so the model can never be told about a column
the guard would reject.

---

## 8. Migrating to Microsoft SQL Server

Every vendor-specific concern is behind one abstraction, `app/db/dialect.py`.
`SqlServerDialect` is already there as a documented stub next to
`PostgresDialect`. Nothing outside `app/db/` writes vendor SQL or imports a
driver — templates, guard, responder and UI are untouched by the migration.

### What actually has to change

| Concern | Postgres (now) | SQL Server (later) | Where |
|---|---|---|---|
| Driver | `psycopg[binary]` | `pyodbc` + `msodbcsql18` | `requirements.txt`, `app.Dockerfile` |
| sqlglot dialect | `"postgres"` | `"tsql"` | `dialect.py` — `sqlglot_dialect` |
| Row cap | `LIMIT n` | `SELECT TOP n` | nothing — sqlglot regenerates it from the tree |
| Driver placeholder | `%s` | `?` | `dialect.py` — `driver_placeholder` |
| Duration in seconds | `EXTRACT(EPOCH FROM (e - s))` | `DATEDIFF(SECOND, s, e)` | `dialect.py` — `duration_seconds()` |
| Truncate to day | `DATE_TRUNC('day', x)` | `CAST(x AS DATE)` | `dialect.py` — `truncate_to_day()` |
| Identifier quoting | `"col"` | `[col]` | `dialect.py` — `quote_identifier()` |
| Statement timeout | `-c statement_timeout=…` | driver `timeout=` | `dialect.py` — `connect_readonly()` |
| Read-only role | `GRANT SELECT` | `ALTER ROLE db_datareader ADD MEMBER` | `dialect.py` — `grant_readonly_statements()` |
| Few-shot SQL | `fewshot/postgres.yaml` | `fewshot/tsql.yaml` | copy the file, adjust the SQL |

### Steps

1. Add `pyodbc` to `app/requirements.txt` and the `msodbcsql18` driver to
   `docker/app.Dockerfile` (the only image change).
2. Fill in the four `NotImplementedError` bodies in `SqlServerDialect`.
3. `cp app/fewshot/postgres.yaml app/fewshot/tsql.yaml` and convert the example
   SQL to T-SQL (`TOP` instead of `LIMIT`, `DATEDIFF`, `GETDATE()`).
4. In `.env`: set `DB_DIALECT=mssql` and point both DSNs at SQL Server.
5. Run the tests. `test_templates.py` validates every template against the
   active dialect, so a missed T-SQL detail fails there rather than on stage.

Two things worth knowing:

- **Row caps and placeholders need no work.** The guard applies the cap to the
  *parse tree* and sqlglot renders `TOP n` for `tsql` automatically. Templates
  author parameters as a neutral `?`, which `to_driver_sql()` converts to the
  driver's marker at execution time — that conversion is done on the parse tree,
  so a literal `?` inside a quoted string is left alone.
- **The seed step is POC-only.** In production the tables already exist and are
  filled by the real data extractor, so `create_schema_statements()` on the
  SQL Server side stays unimplemented on purpose. You will need the DBA to
  create a `db_datareader` login for the chatbot.

---

## 9. Configuration

All of `.env`, with the ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `OLLAMA_URL` | `http://ollama:11434` | **Service name, not localhost** — inside the app container `localhost` is the app itself. |
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | Pulled once into the named volume. |
| `OLLAMA_TIMEOUT` | `180` | Seconds. CPU inference is slow; lower it for a GPU or a small model. |
| `POSTGRES_ADMIN_USER` / `_PASSWORD` | `mfg_admin` | Seed step only. The DSN is built from these. |
| `APP_READONLY_USER` / `_PASSWORD` | `readonly_user` | Every chatbot query. The DSN is built from these. |
| `ADMIN_DATABASE_URL` / `READONLY_DATABASE_URL` | *(unset)* | Optional override, for a database this compose file did not create. Must agree with the variables above or the app refuses to start. |
| `MAX_ROWS` | `200` | Hard row cap; the guard injects or clamps `LIMIT`. |
| `STATEMENT_TIMEOUT_MS` | `8000` | Server-side kill switch for a runaway query. |
| `SEED_RANDOM_SEED` | `20260101` | Same data on every machine. |
| `SEED_END_DATE` | *(blank)* | Blank anchors the window to today. Set it to pin the calendar. |
| `DB_DIALECT` | `postgres` | See section 8. |

Passwords in `.env.example` are placeholders and fine for a local POC. Changing
the readonly password just means editing `.env` and running `docker compose up`:
`db/init/` only runs against an empty volume, so the seed step re-applies the
role's password on every bring-up.

---

## 10. Troubleshooting

**The banner says "warming up" and never clears.**
`docker compose logs app` names the failing check. Almost always the model is
still downloading — `docker compose logs -f ollama-init`.

**`connection refused` to Ollama.** `OLLAMA_URL` must be `http://ollama:11434`.
`localhost` inside the app container is the app, not Ollama.

**LLM answers take 30-60 seconds.** Expected on CPU for a 7B model. Use
`qwen2.5-coder:1.5b`, or enable the GPU (section 4). Template questions are
unaffected. `OLLAMA_KEEP_ALIVE=24h` keeps the model resident between questions,
and the readiness probe warms it at startup so the first demo question is not
the cold-load one.

**"I couldn't safely answer that."** Working as designed — expand the panel for
the reason. Either the model wrote something outside the whitelist, or the
question needs a column that does not exist. Rephrase; the SQL is never
auto-repaired.

**Port already in use.** Change `APP_PORT`, `DB_PORT_HOST` or `OLLAMA_PORT_HOST`
in `.env`.

**The seed ran but there is no data.** It is idempotent — it skips when
`batches` is non-empty. Force a rebuild with
`docker compose down && docker volume rm mfg_pgdata && docker compose up`.

**`password authentication failed for user "readonly_user"`.** Note that
Postgres returns this same message when the role **does not exist at all** — it
deliberately does not reveal which usernames are valid. So treat it as "the role
is missing or its password is stale", not as proof the role exists.

The seed step re-creates the role and re-applies its password from `.env` on
every bring-up, so this normally fixes itself:

```bash
docker compose up          # the seed repairs the role
```

If it persists, the log line to look for is in the `db` output:

```
PostgreSQL Database directory appears to contain a database; Skipping initialization
```

That means the data volume predates the current `db/init/` script. The seed
repairs the role anyway, but to start genuinely clean:

```bash
docker compose down
docker volume rm mfg_pgdata
docker compose up
```

**Do not reach for `docker compose down -v` here.** `-v` removes *every* named
volume in the compose file — including `mfg_ollama_models`, which costs you the
whole ~4.7 GB model download again. Removing `mfg_pgdata` by name resets the
database and leaves the model alone. The seed data is regenerated identically
from the fixed RNG seed.

---

## 11. Known limits

This is a POC, and these are deliberate:

- **Single-turn.** No conversation memory; "and for Line B?" will not work.
- **Date parsing is heuristic.** Common English forms are covered
  (`yesterday`, `last week`, `in July`, `between X and Y`); an unrecognised form
  silently falls back to the last 30 days. The date range used is always stated
  in the answer, so a misread is visible rather than hidden.
- **The LLM path is as good as a 7B model.** It gets most schema-shaped
  questions right and occasionally writes something the guard rejects. That is
  the safe failure — it says so rather than answering wrongly.
- **Column checking is by name**, not full resolution. The table whitelist is
  what bounds reachable data; no name can reach outside the four tables.
- **No auth on the app.** Anyone who can reach port 8000 can query. Fine on a
  laptop, not for a shared network.
