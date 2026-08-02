"""Тесты чистых функций, каждая из которых однажды уже портила данные.

Здесь закреплены выученные уроки, а не абстрактное покрытие:
- collapse_repeats съедал нули из «1000000 рублей» (числа — не залипшая клавиша);
- верхняя граница --to молча теряла последний день/месяц;
- is_suspect принимал смех и пение за срыв Whisper в повтор;
- BOILERPLATE — «водяные знаки» Whisper, из-за которых транскрипт не пустой,
  но и не речь.

Ни БД, ни macOS, ни моделей не нужно: pytest -q.
"""
import datetime as dt
from types import SimpleNamespace

import build_chunks
import db_import
import transcribe_voice
from search import build_filters


# ---------- collapse_repeats: повторы схлопываются, числа целы ----------

def test_collapse_repeats_shrinks_stuck_keys():
    assert build_chunks.collapse_repeats("ХА" * 10) == "ХАХАХА"
    assert build_chunks.collapse_repeats("!" * 12) == "!!!"
    assert build_chunks.collapse_repeats("а" * 40) == "ааа"


def test_collapse_repeats_keeps_numbers_intact():
    for s in ("мне 1000000 рублей", "10000000 раз", "0,0000001",
              "код 111111", "год 2000"):
        assert build_chunks.collapse_repeats(s) == s


def test_collapse_repeats_leaves_normal_text_alone():
    s = "обычное предложение без залипаний, даже длинное"
    assert build_chunks.collapse_repeats(s) == s


# ---------- split_long: длинная реплика режется без потерь ----------

def test_split_long_short_line_untouched():
    line = "Я (20:49): короткая реплика"
    assert build_chunks.split_long(line) == [line]


def test_split_long_splits_and_keeps_every_word():
    line = "Я (20:49): " + "слово " * 400            # ~2 400 символов
    parts = build_chunks.split_long(line)
    assert len(parts) >= 2
    assert all(p.startswith("Я (20:49): ") for p in parts)
    total = sum(p.count("слово") for p in parts)
    assert total == 400                               # ни одно слово не потеряно


def test_split_long_handles_spaceless_monster():
    line = "Я (20:49): " + "Х" * 4000                 # смех без единого пробела
    parts = build_chunks.split_long(line)
    assert len(parts) >= 3
    prefix = "Я (20:49): "
    assert all(len(p) - len(prefix) <= build_chunks.TARGET_CHARS for p in parts)


# ---------- build_filters: включающая верхняя граница --to ----------

def args_for(date_from=None, date_to=None, peer=None):
    return SimpleNamespace(peer=peer, source=None,
                           date_from=date_from, date_to=date_to)


def test_to_year_includes_whole_year():
    _, params = build_filters(args_for(date_to="2016"))
    assert params == [dt.date(2017, 1, 1)]


def test_to_month_includes_whole_month():
    _, params = build_filters(args_for(date_to="2016-05"))
    assert params == [dt.date(2016, 6, 1)]


def test_to_december_rolls_over_year():
    _, params = build_filters(args_for(date_to="2016-12"))
    assert params == [dt.date(2017, 1, 1)]


def test_to_full_date_includes_that_day():
    # Регрессия: --to 2016-05-10 молча теряло весь день 10 мая.
    _, params = build_filters(args_for(date_to="2016-05-10"))
    assert params == [dt.date(2016, 5, 11)]


def test_from_is_inclusive_as_written():
    where, params = build_filters(args_for(date_from="2016-05-10"))
    assert "c.ts_from >= %s" in where
    assert params == [dt.date(2016, 5, 10)]


def test_single_day_window_is_not_empty():
    # --from X --to X должно давать непустой интервал [X, X+1)
    _, params = build_filters(args_for(date_from="2016-05-10",
                                       date_to="2016-05-10"))
    assert params == [dt.date(2016, 5, 10), dt.date(2016, 5, 11)]


def test_peer_filter_casts_to_int():
    where, params = build_filters(args_for(peer=["123456789"]))
    assert "c.peer_id = ANY(%s)" in where
    assert params == [[123456789]]


# ---------- is_suspect: срыв Whisper против живой речи ----------

def test_real_breakdown_is_suspect():
    assert transcribe_voice.is_suspect("недель " * 9)


def test_laughter_is_not_suspect():
    assert not transcribe_voice.is_suspect("ха " * 10)


def test_even_filler_43_times_is_suspect():
    assert transcribe_voice.is_suspect("ну " * 43)


def test_normal_speech_is_not_suspect():
    assert not transcribe_voice.is_suspect(
        "вот вот вот эти штуки мы вчера обсуждали и решили что берём")


def test_repetition_stats_counts_longest_run():
    best, word, share = transcribe_voice.repetition_stats("да да да нет")
    assert (best, word) == (3, "да")
    assert 0 < share <= 1


# ---------- db_import: дата, тип вложения, «водяные знаки» ----------

def test_parse_date_happy_path():
    ts = db_import.parse_date(", 10 мая 2016 в 21:03:04")
    assert ts == dt.datetime(2016, 5, 10, 21, 3, 4, tzinfo=db_import.MSK)


def test_parse_date_rejects_garbage():
    assert db_import.parse_date("без даты вовсе") is None
    assert db_import.parse_date(", 31 фев 2016 в 00:00:00") is None


def test_att_kind_voice_photo_other():
    assert db_import.att_kind(
        "Файл", "https://psv4.example/amsg/rec.ogg?extra=1") == "voice"
    assert db_import.att_kind("Фотография", "") == "photo"
    assert db_import.att_kind("Файл", "https://example.com/doc.pdf") == "other"


def test_boilerplate_catches_whisper_watermarks():
    for s in ("Субтитры сделал DimaTorzok", "Продолжение следует...",
              "Спасибо за просмотр!"):
        assert db_import.BOILERPLATE.search(s), s


def test_boilerplate_ignores_real_speech():
    assert not db_import.BOILERPLATE.search(
        "давай завтра созвонимся и всё обсудим")


def test_clean_text_strips_tags_and_nul():
    assert db_import.clean_text("<b>привет &amp; мир</b>\x00") == "привет & мир"
