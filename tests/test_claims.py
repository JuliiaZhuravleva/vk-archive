"""Правила журнала уточнений claims.py: без БД, на выдуманных утверждениях.

Главное, что здесь закреплено: более слабая проверка не перебивает более
сильную, «не поняли» ничего не ломает, а противоречивый журнал не пишется
вовсе — ни частично.
"""
import datetime as dt
import json

import pytest

import claims

CLAIM = dict(key="run-x:abcdef12", text="Исходная заметка модели.",
             period_from="2010-05-01", period_to="2010-05-03")


def rev(verdict, level, **kw):
    return dict(verdict=verdict, level=level, by="проверка", **kw)


def test_fresh_claim_is_open_draft():
    state, rows = claims.fold(CLAIM, [])
    assert (state["level"], state["status"], state["text"]) == (1, "open", CLAIM["text"])
    assert rows == []


def test_correction_replaces_text_and_logs_both_versions():
    state, rows = claims.fold(CLAIM, [rev("corrected", 3, text="Исправлено.")])
    assert (state["level"], state["status"], state["text"]) == (3, "corrected", "Исправлено.")
    assert rows[0]["old_text"] == CLAIM["text"] and rows[0]["new_text"] == "Исправлено."
    assert (rows[0]["old_level"], rows[0]["new_level"]) == (1, 3)


def test_confirmation_keeps_text_and_logs_no_text_change():
    state, rows = claims.fold(CLAIM, [rev("confirmed", 2)])
    assert state["text"] == CLAIM["text"] and state["status"] == "confirmed"
    assert rows[0]["old_text"] is None and rows[0]["new_text"] is None


def test_weaker_check_cannot_override_stronger():
    with pytest.raises(ValueError, match="уровня 2 не может перебить уровень 3"):
        claims.fold(CLAIM, [rev("refuted", 3), rev("confirmed", 2)])


def test_same_level_can_correct_itself():
    state, _ = claims.fold(CLAIM, [rev("corrected", 3, text="Первая."),
                                   rev("corrected", 3, text="Вторая.")])
    assert state["text"] == "Вторая."


def test_unclear_from_weaker_check_is_logged_but_changes_nothing():
    state, rows = claims.fold(CLAIM, [rev("confirmed", 3), rev("unclear", 2)])
    assert (state["level"], state["status"]) == (3, "confirmed")
    assert rows[1]["verdict"] == "unclear"


def test_owner_word_overrides_research():
    state, _ = claims.fold(CLAIM, [rev("confirmed", 3), rev("refuted", 4)])
    assert (state["level"], state["status"]) == (4, "refuted")


@pytest.mark.parametrize("bad, message", [
    (rev("corrected", 3), "corrected без новой редакции"),
    (rev("confirmed", 1), "проверка — это 2, 3 или 4"),
    (rev("maybe", 2), "verdict='maybe'"),
    (dict(verdict="confirmed", level=2), "не указано, кто проверял"),
])
def test_malformed_revision_is_rejected(bad, message):
    with pytest.raises(ValueError, match=message):
        claims.fold(CLAIM, [bad])


def test_key_depends_on_content_not_order():
    a = claims.claim_key("run-x", 1, "Текст.")
    assert a == claims.claim_key("run-x", 1, "Текст.")
    assert a != claims.claim_key("run-x", 2, "Текст.")
    assert a != claims.claim_key("run-y", 1, "Текст.")
    assert a.startswith("run-x:")


@pytest.mark.parametrize("lo, hi, expected", [
    (dt.date(2010, 5, 3), dt.date(2010, 5, 9), True),    # касается последним днём
    (dt.date(2010, 4, 1), dt.date(2010, 5, 1), True),    # касается первым днём
    (dt.date(2010, 5, 4), None, False),
    (None, dt.date(2010, 4, 30), False),
    (None, None, True),
])
def test_overlap_bounds_are_inclusive(lo, hi, expected):
    assert claims.overlaps(CLAIM, lo, hi) is expected


def test_undated_claim_always_shows_up():
    assert claims.overlaps(dict(CLAIM, period_from=None), dt.date(1990, 1, 1),
                           dt.date(1990, 1, 2))


def test_resolve_accepts_hash_alone():
    keys = {"run-x:abcdef12": 1, "run-y:abc99999": 2}
    assert claims.resolve(keys, "abcdef") == "run-x:abcdef12"
    with pytest.raises(SystemExit):
        claims.resolve(keys, "abc")  # двусмысленно


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def test_load_all_folds_journal_from_disk(tmp_path):
    write_jsonl(tmp_path / "runs" / "r.jsonl", [dict(CLAIM, run="r", origin="m")])
    write_jsonl(tmp_path / "revisions.jsonl",
                [dict(rev("refuted", 3), claim=CLAIM["key"])])
    loaded = claims.load_all(tmp_path)
    assert loaded[CLAIM["key"]]["state"]["status"] == "refuted"


def test_load_all_rejects_revision_of_unknown_claim(tmp_path):
    write_jsonl(tmp_path / "runs" / "r.jsonl", [dict(CLAIM, run="r", origin="m")])
    write_jsonl(tmp_path / "revisions.jsonl", [dict(rev("refuted", 3), claim="run-x:nope")])
    with pytest.raises(ValueError, match="несуществующему"):
        claims.load_all(tmp_path)


def test_append_revisions_writes_whole_batch(tmp_path):
    write_jsonl(tmp_path / "runs" / "r.jsonl", [dict(CLAIM, run="r", origin="m")])
    loaded = claims.load_all(tmp_path)
    claims.append_revisions([dict(rev("confirmed", 2), claim=CLAIM["key"]),
                             dict(rev("refuted", 3), claim=CLAIM["key"])], loaded, tmp_path)
    assert loaded[CLAIM["key"]]["state"]["status"] == "refuted"
    assert claims.load_all(tmp_path)[CLAIM["key"]]["state"]["level"] == 3


def test_append_revisions_contradiction_leaves_file_and_state_untouched(tmp_path):
    write_jsonl(tmp_path / "runs" / "r.jsonl", [dict(CLAIM, run="r", origin="m")])
    write_jsonl(tmp_path / "revisions.jsonl", [dict(rev("confirmed", 3), claim=CLAIM["key"])])
    before = (tmp_path / "revisions.jsonl").read_text()
    loaded = claims.load_all(tmp_path)
    with pytest.raises(ValueError, match="не может перебить"):
        claims.append_revisions([dict(rev("unclear", 2), claim=CLAIM["key"]),
                                 dict(rev("refuted", 2), claim=CLAIM["key"])], loaded, tmp_path)
    assert (tmp_path / "revisions.jsonl").read_text() == before
    assert len(loaded[CLAIM["key"]]["revisions"]) == 1
