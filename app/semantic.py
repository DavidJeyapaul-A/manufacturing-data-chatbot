"""Loads schema.yaml — the semantic layer — and serves it to its two consumers.

sql_guard uses it as a whitelist; llm_query renders it into the prompt. Keeping
one loader means the guard can never disagree with what the model was told.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from config import settings


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    primary_key: str
    columns: dict[str, Column]


@dataclass(frozen=True)
class SemanticModel:
    database: str
    description: str
    tables: dict[str, Table]
    relationships: tuple[str, ...]
    business_rules: tuple[str, ...]
    alarm_catalogue: dict[str, dict[str, str]]

    # -- whitelist views used by sql_guard --------------------------------

    @functools.cached_property
    def table_names(self) -> frozenset[str]:
        return frozenset(self.tables)

    @functools.cached_property
    def all_column_names(self) -> frozenset[str]:
        return frozenset(c for t in self.tables.values() for c in t.columns)

    def columns_of(self, table: str) -> frozenset[str]:
        spec = self.tables.get(table.lower())
        return frozenset(spec.columns) if spec else frozenset()

    # -- prompt rendering used by llm_query -------------------------------

    def render_for_prompt(self) -> str:
        """Compact, plain-English schema description for the LLM."""
        out: list[str] = [f"Database: {self.database}", self.description.strip(), ""]
        for table in self.tables.values():
            out.append(f"TABLE {table.name} — {_one_line(table.description)}")
            for col in table.columns.values():
                line = f"  - {col.name} ({col.type}): {_one_line(col.description)}"
                if col.values:
                    line += f" Allowed values: {', '.join(repr(v) for v in col.values)}."
                out.append(line)
            out.append("")

        out.append("RELATIONSHIPS:")
        out += [f"  - {r}" for r in self.relationships]
        out.append("")
        out.append("RULES OF THIS DOMAIN:")
        out += [f"  - {r}" for r in self.business_rules]
        return "\n".join(out)


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _parse(raw: dict[str, Any]) -> SemanticModel:
    tables: dict[str, Table] = {}
    for tname, tspec in (raw.get("tables") or {}).items():
        columns = {
            cname: Column(
                name=cname,
                type=str(cspec.get("type", "")),
                description=_one_line(cspec.get("description", "")),
                values=tuple(cspec.get("values") or ()),
            )
            for cname, cspec in (tspec.get("columns") or {}).items()
        }
        tables[tname] = Table(
            name=tname,
            description=_one_line(tspec.get("description", "")),
            primary_key=str(tspec.get("primary_key", "")),
            columns=columns,
        )

    return SemanticModel(
        database=str(raw.get("database", "")),
        description=_one_line(raw.get("description", "")),
        tables=tables,
        relationships=tuple(raw.get("relationships") or ()),
        business_rules=tuple(raw.get("business_rules") or ()),
        alarm_catalogue=dict(raw.get("alarm_catalogue") or {}),
    )


@functools.lru_cache(maxsize=4)
def load_semantic_model(path: str | Path | None = None) -> SemanticModel:
    target = Path(path) if path else settings.schema_path
    if not target.exists():
        raise FileNotFoundError(
            f"Semantic layer not found at {target}. schema.yaml must sit next to the app modules."
        )
    with target.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not raw or not raw.get("tables"):
        raise ValueError(f"{target} has no 'tables' section — the whitelist would be empty.")
    return _parse(raw)
