import math

import pytest

from data_agent_baseline.benchmark.schema import AnswerTable
from data_agent_baseline.verification import AnswerVerifier


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (AnswerTable(columns=[], rows=[]), "EMPTY_COLUMN_NAME"),
        (AnswerTable(columns=[" "], rows=[["value"]]), "EMPTY_COLUMN_NAME"),
        (AnswerTable(columns=["name", " name "], rows=[["a", "b"]]), "DUPLICATE_COLUMN_NAME"),
        (AnswerTable(columns=["na\nme"], rows=[["value"]]), "CONTROL_CHARACTER"),
        (AnswerTable(columns=["one", "two"], rows=[["only-one"]]), "ROW_WIDTH_MISMATCH"),
        (AnswerTable(columns=["value"], rows=[[{"nested": "object"}]]), "UNSUPPORTED_CELL_TYPE"),
        (AnswerTable(columns=["value"], rows=[[math.inf]]), "NONFINITE_NUMBER"),
        (AnswerTable(columns=["value"], rows=[[None], [""]]), "ALL_NULL_COLUMN"),
    ],
)
def test_rejects_invalid_answer_tables(answer, code):
    failure = AnswerVerifier().verify(answer)

    assert failure is not None
    assert failure.code == code


def test_accepts_csv_round_trip_safe_answer_table():
    answer = AnswerTable(
        columns=["name", "score", "active"],
        rows=[["Alice, Jr.", 1.5, True], ['Bob said "hi"', None, False]],
    )

    assert AnswerVerifier().verify(answer) is None


def test_allows_a_zero_row_answer_without_semantic_guessing():
    answer = AnswerTable(columns=["name"], rows=[])

    assert AnswerVerifier().verify(answer) is None
