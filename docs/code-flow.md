# Code flow

## What this is

A chatbot that answers questions like *"how many good vs bad parts did Line A
make last week?"* in plain English — with the SQL it ran always on show.

The database is the point. A Python data extractor already parses machine logs
from a three-line production plant into a database that Power BI reads. This
adds a conversational layer over that same data without changing anything
underneath it.

One constraint shapes everything else: **the target IT environment forbids
external AI calls.** The model is an open-weight one (`qwen2.5-coder:7b`) served
by Ollama in a container on the same host. Once it has been pulled, the whole
stack runs with the network disabled.

That constraint is visible in every diagram below:

- **A 7B model on CPU is slow** — 10 to 30 seconds a question. So the common
  questions never reach it: eleven hand-written templates answer them in ~15 ms
  with exact, reproducible SQL. The model is the fallback, not the front door.
- **A 7B model is also fallible**, so nothing it writes is trusted. Every
  statement, from either path, is validated with sqlglot and then executed by a
  database role that holds `SELECT` and nothing else.
- **Confidently wrong is the failure worth engineering against.** A template
  that cannot answer the exact question asked declines rather than answering a
  near-miss, and a rejected query is reported rather than repaired.

The POC runs on PostgreSQL; the client's production database is SQL Server.
Every vendor-specific concern sits behind a single module — which is what the
last diagram is about.

## A question, end to end

```mermaid
flowchart TD
    Q["POST /ask"] --> Ready{"readiness.ready?"}
    Ready -- no --> R503["503 — still warming up<br/>(names the failing check)"]
    Ready -- yes --> T["templates.match_template()"]

    T -- "hit — 11 templates" --> TSQL["parameterised SQL<br/>+ bound parameters"]
    T -- "miss" --> W["readiness.wait_for_warm_model()"]
    W --> LLM["llm_query.generate_sql()<br/>schema.yaml + few-shot"]

    LLM -- "LlmUnavailable" --> RErr["error — model not answering"]
    LLM --> Sent{"model said<br/>'cannot answer'?"}
    Sent -- yes --> RRef["refusal — not answerable<br/>from this schema"]
    Sent -- no --> G

    TSQL --> G["sql_guard.validate()"]
    G -- "SqlRejected" --> RRej["I couldn't safely answer that<br/>(never auto-repaired)"]
    G -- "ok" --> D["dialect.to_driver_sql()<br/>? → %s"]
    D --> X["run_select() as readonly_user<br/>statement_timeout"]
    X -- "QueryExecutionError" --> RDb["error — database refused it"]
    X --> S["responder.summarise()"]
    S --> A["Answer envelope<br/>sql · params · rows · timings"]

    style G fill:#8b3a3a,color:#fff
    style X fill:#2d5a2d,color:#fff
    style A fill:#2b5c99,color:#fff
```

The guard sits on the join of both routes, not on one branch — there is no path
to the database that skips it.

## Bring-up ordering

```mermaid
sequenceDiagram
    autonumber
    participant CO as docker compose
    participant DB as db
    participant OL as ollama
    participant IN as ollama-init
    participant SE as seed
    participant AP as app

    CO->>DB: start
    DB-->>CO: healthy (pg_isready)
    Note over DB: db/init/ runs ONLY on an<br/>empty volume — creates the role
    CO->>OL: start
    OL-->>CO: healthy (ollama list)

    CO->>IN: start (ollama healthy)
    IN->>OL: pull model if absent
    IN-->>CO: exit 0

    CO->>SE: start (db healthy)
    SE->>DB: CREATE TABLE, seed if empty
    SE->>DB: ensure role + re-apply password
    SE->>DB: GRANT SELECT
    SE-->>CO: exit 0

    CO->>AP: start (seed + init completed)
    AP->>DB: SELECT as readonly_user
    AP->>OL: POST /api/generate (real probe)
    Note over AP,OL: then warm the prompt prefix<br/>149s cold → 11s warm
```

## Layering

```mermaid
flowchart LR
    subgraph web["HTTP"]
        M["main.py"]
    end
    subgraph brain["Question handling"]
        P["pipeline.py"]
        TP["templates.py"]
        EN["entities.py"]
        LQ["llm_query.py"]
        RS["responder.py"]
    end
    subgraph safety["Safety"]
        SG["sql_guard.py"]
        SM["semantic.py<br/>schema.yaml"]
    end
    subgraph data["Vendor boundary"]
        DI["db/dialect.py"]
        CN["db/connection.py"]
    end

    M --> P
    P --> TP --> EN
    P --> LQ --> SM
    P --> SG --> SM
    P --> RS
    P --> CN --> DI
    TP --> DI
    SG --> DI

    style safety fill:#3a2b2b
    style data fill:#2b3340
```

Only `db/` imports a driver or writes vendor SQL — that is what makes the
SQL Server migration a one-module change.

---

*Diagrams are [Mermaid](https://mermaid.js.org/) — GitHub renders them natively,
so they need no tooling to read, and they diff line-by-line when the code they
describe changes.*
