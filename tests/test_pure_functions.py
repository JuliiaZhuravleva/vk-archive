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
import importlib
from types import SimpleNamespace

import build_chunks
import collect_photos
import common
import merge_media_manifest
import db_import
import life_events
import pytest
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


# ---------- url_key: одно вложение из двух выгрузок — один ключ ----------
#
# VK отдаёт тот же файл с другого шарда CDN и сменил домен vk.com → vk.ru.
# Имя скачанного файла — sha1(url), поэтому «новый» URL означает повторную
# выкачку и файл-близнец. На прогоне 05.08.2026 нормализация отсекла 638
# таких строк из 1344. Если регулярка перестанет матчить, слияние манифеста
# молча пропустит их как новые — ошибка без единого сообщения.

def _rec(url):
    return {"url": url}


def test_url_key_collapses_cdn_shard():
    a = _rec("https://sun9-78.userapi.com/impg/AbCd/x.jpg?size=850x533&sign=ff")
    b = _rec("https://sun9-9.userapi.com/impg/AbCd/x.jpg?size=850x533&sign=ff")
    assert merge_media_manifest.url_key(a) == merge_media_manifest.url_key(b)


def test_url_key_collapses_vk_com_and_vk_ru():
    a = _rec("https://vk.com/video-111111_222222")
    b = _rec("https://vk.ru/video-111111_222222")
    assert merge_media_manifest.url_key(a) == merge_media_manifest.url_key(b)


def test_url_key_keeps_different_files_apart():
    """Негативный контроль: перематчить опаснее, чем недоматчить.

    Слишком жадная нормализация схлопнет разные вложения в один ключ, и
    действительно новые фотографии не попадут в манифест вообще.
    """
    a = _rec("https://sun9-78.userapi.com/impg/AbCd/x.jpg")
    b = _rec("https://sun9-78.userapi.com/impg/ZzZz/y.jpg")
    assert merge_media_manifest.url_key(a) != merge_media_manifest.url_key(b)


def test_url_key_does_not_touch_voice_hosts():
    """Голосовые лежат на psv4, а не на шардах sun*, и их URL устойчив.

    Подпись у /amsg/ вшита в путь: два разных голосовых обязаны остаться
    разными ключами, иначе одно перекроет другое.
    """
    a = _rec("https://psv4.userapi.com/s/v1/amsg/AAAA.ogg")
    b = _rec("https://psv4.userapi.com/s/v1/amsg/BBBB.ogg")
    assert merge_media_manifest.url_key(a) != merge_media_manifest.url_key(b)
    assert merge_media_manifest.url_key(a) == merge_media_manifest.url_key(_rec(a["url"]))


# ---------- RAW: вторая выгрузка подключается переменной, а не подменой ----------

def test_archive_raw_env_switches_tree(monkeypatch):
    monkeypatch.setenv("VK_ARCHIVE_RAW", "data/raw-20260731")
    reloaded = importlib.reload(common)
    try:
        assert reloaded.RAW.parts[-2:] == ("data/raw-20260731".split("/")[-1],
                                           "messages")
        assert reloaded.RAW.is_absolute()   # относительный путь — от корня репо
    finally:
        monkeypatch.delenv("VK_ARCHIVE_RAW", raising=False)
        importlib.reload(common)


def test_archive_raw_defaults_to_data_raw():
    assert common.RAW.parts[-3:] == ("data", "raw", "messages")


# ---------- life_events: точность даты и границы фильтра ----------
# Дата события живёт вместе со своей точностью, и печатать её без неё нельзя:
# 2018-01-01/'year' — это «когда-то в 2018», а не первое января. Каждая
# проверка ниже закрывает способ выдать догадку за наблюдение.

def test_fmt_date_hides_precision_it_does_not_have():
    d = dt.date(2018, 10, 1)
    assert life_events.fmt_date(d, "day") == "01.10.2018"
    assert life_events.fmt_date(d, "month") == "10.2018"
    assert life_events.fmt_date(d, "year") == "2018"
    assert life_events.fmt_date(None, "day") == "дата неизвестна"


def test_fmt_span_keeps_each_end_at_its_own_precision():
    """Типичная находка: начало известно с точностью до месяца, конец — точно.
    Общая точность огрубила бы твёрдый конец или приукрасила мягкое начало."""
    ev = {"started_on": dt.date(2018, 10, 1), "precision": "month",
          "ended_on": dt.date(2019, 10, 16), "end_precision": "day"}
    assert life_events.fmt_span(ev) == "10.2018 — 16.10.2019"


def test_fmt_span_falls_back_to_start_precision():
    ev = {"started_on": dt.date(2020, 1, 1), "precision": "year",
          "ended_on": dt.date(2021, 1, 1), "end_precision": None}
    assert life_events.fmt_span(ev) == "2020 — 2021"


def test_fmt_span_collapses_single_date():
    ev = {"started_on": dt.date(2019, 5, 4), "precision": "day",
          "ended_on": None, "end_precision": None}
    assert life_events.fmt_span(ev) == "04.05.2019"


def test_parse_bound_includes_whole_period():
    """Та же семантика, что у --from/--to в search.py: верхняя граница
    включающая на любой гранулярности."""
    assert life_events.parse_bound("2018", False) == dt.date(2018, 1, 1)
    assert life_events.parse_bound("2018", True) == dt.date(2018, 12, 31)
    assert life_events.parse_bound("2018-05", False) == dt.date(2018, 5, 1)
    assert life_events.parse_bound("2018-05", True) == dt.date(2018, 5, 31)
    assert life_events.parse_bound("2018-12", True) == dt.date(2018, 12, 31)
    assert life_events.parse_bound("2020-02", True) == dt.date(2020, 2, 29)
    assert life_events.parse_bound("2018-05-10", True) == dt.date(2018, 5, 10)
    assert life_events.parse_bound(None, True) is None


def test_parse_ts_accepts_the_shapes_people_actually_paste():
    assert life_events.parse_ts("2018-10-12") == dt.datetime(2018, 10, 12)
    assert life_events.parse_ts("2018-10-12 10:09") == dt.datetime(2018, 10, 12, 10, 9)
    assert life_events.parse_ts("2018-10-12T10:09:31") == dt.datetime(2018, 10, 12, 10, 9, 31)
    assert life_events.parse_ts(None) is None


def test_parse_ts_refuses_garbage_instead_of_guessing():
    with pytest.raises(ValueError):
        life_events.parse_ts("12 октября")


# ---------- life_events.validate: не пустить в базу полуфабрикат ----------

def _event(**over):
    ev = {"slug": "s", "title": "t", "kind": "media", "summary": "x"}
    ev.update(over)
    return ev


def test_validate_passes_minimal_event():
    assert life_events.validate(_event()) == []


def test_validate_requires_the_four_fields():
    assert len(life_events.validate({})) == 4


def test_validate_rejects_unknown_vocabulary():
    assert life_events.validate(_event(kind="еда"))
    assert life_events.validate(_event(precision="примерно"))
    assert life_events.validate(_event(end_precision="примерно"))
    assert life_events.validate(_event(confidence="наверное"))


def test_validate_allows_absent_end_precision():
    assert life_events.validate(_event(end_precision=None)) == []


def test_validate_rejects_backwards_range():
    """Перевёрнутый диапазон молча ломает и сортировку, и смысл."""
    assert life_events.validate(
        _event(started_on="2020-01-01", ended_on="2019-01-01"))
    assert life_events.validate(
        _event(started_on="2019-01-01", ended_on="2020-01-01")) == []


def test_validate_forbids_verified_without_sources():
    """verified означает «видно в архиве» — видно в конкретном сообщении."""
    assert life_events.validate(_event(confidence="verified"))
    assert life_events.validate(
        _event(confidence="verified", sources=[{"quote": "q"}])) == []


# ---------- collect_photos.safe: имя диалога в имя файла ----------

def test_safe_strips_what_a_filename_cannot_hold():
    assert collect_photos.safe("Иван Петров") == "Иван-Петров"
    assert collect_photos.safe("Беседа 🌸") == "Беседа"
    assert "/" not in collect_photos.safe("a/b/c")
    assert collect_photos.safe("🌸🌸") == "без-имени"


def test_safe_keeps_cyrillic_rather_than_erasing_it():
    """\\w без re.ASCII оставляет кириллицу: иначе все русские имена диалогов
    схлопнулись бы в «без-имени» и файлы стали бы неразличимы."""
    assert collect_photos.safe("Пётр Сидоров") == "Пётр-Сидоров"


def test_safe_cannot_walk_out_of_the_target_directory():
    """Название беседы задаёт любой её участник, а оно идёт в путь файла.
    Точки и слэши обязаны срезаться здесь."""
    for hostile in ("../../etc/passwd", "..", "../..", "a/../../b"):
        got = collect_photos.safe(hostile)
        assert "/" not in got and ".." not in got, got


def test_check_scheme_allows_only_http():
    """Ссылки на вложения приходят из экспорта VK — их содержимое выбирали
    не мы, а urlopen на file:// отдаёт локальный файл."""
    collect_photos.check_scheme("https://sun9-1.userapi.com/x.jpg")
    collect_photos.check_scheme("http://sun9-1.userapi.com/x.jpg")
    for bad in ("file:///etc/passwd", "ftp://host/x", "/etc/passwd",
                "FILE:///etc/passwd"):
        with pytest.raises(ValueError):
            collect_photos.check_scheme(bad)
