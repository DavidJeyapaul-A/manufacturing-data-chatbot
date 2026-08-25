"""Every few-shot example must itself survive the guard.

An example that the guard would reject teaches the model to write SQL that gets
rejected — so this is a correctness test of the prompt, not a style check.
"""

import pytest

import sql_guard
from db.dialect import get_dialect
from llm_query import load_examples

EXAMPLES = load_examples(get_dialect().fewshot_file)


def test_there_are_enough_examples():
    assert 8 <= len(EXAMPLES) <= 15


@pytest.mark.parametrize("question,sql", EXAMPLES, ids=[q[:40] for q, _ in EXAMPLES])
def test_example_sql_passes_the_guard(question, sql):
    guarded = sql_guard.validate(sql)
    assert guarded.sql.upper().startswith("SELECT")


def test_fewshot_examples_mostly_miss_templates():
    import templates
    covered = sum(1 for q, _ in EXAMPLES if templates.match_template(q) is not None)
    assert covered <= len(EXAMPLES) // 2, (
        f"{covered}/{len(EXAMPLES)} few-shot examples are already handled by a "
        "template — they should demonstrate the fallback path instead."
    )
