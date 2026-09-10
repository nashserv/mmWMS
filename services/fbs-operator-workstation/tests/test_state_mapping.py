"""Таблица соответствия состояний — копия docs/state-mapping.md.

Таблица заморожена потоком 0 и на неё ссылаются все три потока. Если копия в
коде разойдётся с документом, экран начнёт показывать не тот статус, а
биллинг — считать не то событие. Поэтому здесь она сверяется дословно.
"""
from __future__ import annotations

from pathlib import Path

from app.domain import (STATE_TO_CANONICAL, STATE_TO_SCREEN, ScreenStatus, TaskState,
                        canonical_status, parse_state, screen_status)

DOC = Path(__file__).resolve().parents[3] / "docs" / "state-mapping.md"


def _rows_from_doc() -> dict[str, tuple[str, str]]:
    """Первая таблица документа: состояние wms → статус экрана → канонический."""
    rows: dict[str, tuple[str, str]] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        if len(cells) < 4 or cells[0] not in {state.value for state in TaskState}:
            continue
        rows[cells[0]] = (cells[1], cells[2])
    return rows


def test_every_task_state_of_the_contract_is_mapped():
    """Пустых ячеек в таблице нет — кроме diverged, у него правило отдельное."""
    for state in TaskState:
        if state is TaskState.DIVERGED:
            continue
        assert state in STATE_TO_SCREEN, f"состояние {state.value} не отображается на экран"
        assert state in STATE_TO_CANONICAL


def test_code_matches_the_frozen_document():
    doc_rows = _rows_from_doc()
    assert len(doc_rows) >= 13, "таблица в документе не прочиталась"
    for raw_state, (screen, canonical) in doc_rows.items():
        state = TaskState(raw_state)
        if state is TaskState.DIVERGED:
            continue
        assert STATE_TO_SCREEN[state].value == screen, (
            f"{raw_state}: в коде {STATE_TO_SCREEN[state].value}, в документе {screen}")
        assert STATE_TO_CANONICAL[state] == canonical, (
            f"{raw_state}: в коде {STATE_TO_CANONICAL[state]}, в документе {canonical}")


def test_diverged_keeps_the_last_known_status():
    """Расхождение не подменяет статус выдуманным — оно будит человека."""
    assert screen_status(TaskState.DIVERGED, ScreenStatus.PACKED) is ScreenStatus.PACKED
    assert canonical_status(TaskState.DIVERGED, "PACKED") == "PACKED"


def test_shipped_handed_and_accepted_stay_three_different_facts():
    """На смешении первых двух держалась иллюзия, что 6072 задания доехали."""
    assert STATE_TO_CANONICAL[TaskState.SHIPPED] == "OUT_FOR_DELIVERY"
    assert STATE_TO_CANONICAL[TaskState.HANDED] == "HANDED_TO_WB"
    assert STATE_TO_CANONICAL[TaskState.ACCEPTED] == "ACCEPTED_BY_WB"


def test_an_unknown_state_is_not_guessed():
    """Придумать состояние за сервис нельзя: экран покажет вымысел."""
    assert parse_state("совершенно новое") is None
    assert screen_status(None) is ScreenStatus.QUEUED
