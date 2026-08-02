#!/usr/bin/env python3
"""Нарезка переписки на чанки для векторного поиска.

Окно — не фиксированный кусок текста, а кусок РАЗГОВОРА: границы ставятся
по паузам (люди разошлись и вернулись через несколько часов — это разные
темы), а внутри сессии набирается до целевого размера. Голосовые входят
расшифровкой из voice_transcripts, иначе в чанке будет дыра на месте
самого содержательного.

  python3 scripts/build_chunks.py --peer 123456789          # прикинуть
  python3 scripts/build_chunks.py --peer 123456789 --write
  python3 scripts/build_chunks.py --all --write             # весь корпус

Повторный прогон не трогает уже посчитанные эмбеддинги: вставка идёт
ON CONFLICT DO NOTHING по (peer_id, msg_from, msg_to, part, source). Полная
пересборка диалога — флагом --rebuild; фото-чанки (source = 'photo') она не
трогает, ими занимается build_photo_chunks.py.

Своё имя задаётся переменной окружения `VK_OWN_NAME` (по умолчанию «Я»):
им подписаны собственные реплики внутри чанка, то есть оно уходит в текст
и в эмбеддинг. Задавать его надо ДО первой сборки — смена имени потом
означает пересборку чанков и пересчёт векторов.
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DSN, OWN_NAME                     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# Пауза, после которой разговор считается новым. Три часа: переписка в VK
# рваная, но внутри одного вечера тема держится.
GAP_MINUTES = 180
# Целевой размер чанка. Русский текст ~3 символа на токен, то есть ~400
# токенов — на такой длине эмбеддинг ещё «про одно», а не про всё сразу.
TARGET_CHARS = 1200
HARD_MAX_CHARS = 2600
MAX_MESSAGES = 80
# Нахлёст: последние реплики уходят в начало следующего чанка, чтобы ответ
# не оторвался от вопроса на границе окна. Ограничение по символам
# обязательно: расшифровка голосового — это 500–900 символов в одной
# реплике, и нахлёст «две последние» утаскивал в новый чанк уже больше
# целевого размера. Чанки вырождались в три реплики, а одно голосовое
# попадало в три-четыре чанка подряд.
OVERLAP_MESSAGES = 2
OVERLAP_MAX_CHARS = 400

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]

# В экспорте свои сообщения подписаны «Вы», а собеседник из удалённого
# аккаунта — «DELETED». Для поиска и то и другое бесполезно: спрашивать будут
# «что <имя> говорил про…», а не «что DELETED говорил». Своё имя — из
# common.OWN_NAME (переменная окружения VK_OWN_NAME).
ANON_SENDERS = {"DELETED", "Deleted", "deleted", ""}

# Повтор юнита в 1–4 символа пять и более раз подряд — признак залипшей
# клавиши. Юнит из одних цифр и разделителей исключаем: см. collapse_repeats.
REPEATED_UNIT = re.compile(r"(.{1,4}?)\1{4,}")
DIGITS_ONLY = re.compile(r"[\d.,\s]+")

SQL_MESSAGES = """
    SELECT m.vk_msg_id, m.sent_at, m.sender, m.is_own, m.text,
           v.transcript, v.duration_s,
           array_remove(array_agg(DISTINCT a.att_type), NULL) AS atts
      FROM messages m
      LEFT JOIN attachments a ON a.message_id = m.id
      LEFT JOIN voice_transcripts v ON v.message_id = m.id
     WHERE m.peer_id = %s
     GROUP BY m.id, m.vk_msg_id, m.sent_at, m.sender, m.is_own, m.text,
              v.transcript, v.duration_s
     ORDER BY m.sent_at, m.vk_msg_id"""

SQL_INSERT = """
    INSERT INTO chunks (peer_id, msg_from, msg_to, part, ts_from, ts_to,
                        n_messages, text, meta, source)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'messages')
    ON CONFLICT (peer_id, msg_from, msg_to, part, source) DO NOTHING"""


def ensure_schema(conn) -> None:
    """Миграция для баз, поднятых до появления колонок part и source.

    В db/schema.sql колонки и уникальный индекс уже есть, так что на свежей
    установке это пустая операция. Оставлено ради существующих томов —
    удалять только вместе с ними, иначе старая база молча разойдётся
    со схемой.

    Уникальный индекс включает source: у фото-чанков part значит другое
    (номер фото в сообщении, а не часть длинной реплики), и длинная реплика
    с частями 1..N в том же сообщении, что и фотография, столкнулась бы
    с фото-чанком ключами. Индекс без source пересоздаём, а не дополняем:
    ON CONFLICT ищет индекс ровно по перечисленным колонкам.

    Каждый шаг сперва спрашивает каталог и только потом трогает таблицу.
    `ALTER TABLE ... IF NOT EXISTS` выглядит безобидно, но берёт ACCESS
    EXCLUSIVE ДО проверки: холостая миграция на каждом запуске всё равно
    блокирует chunks, а вставшая в очередь блокировка задерживает и
    читателей. Плюс lock_timeout, чтобы прогон не висел молча за чужой
    транзакцией, а падал с внятной ошибкой.
    """
    conn.execute("SET LOCAL lock_timeout = '5s'")
    have = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        " WHERE table_name = 'chunks'").fetchall()}
    if "part" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN "
                     "part smallint NOT NULL DEFAULT 0")
    if "source" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN "
                     "source text NOT NULL DEFAULT 'messages'")
    # CHECK в db/schema.sql есть, а ALTER его не ставил — на томе, где
    # колонка появилась миграцией, ограничения не было. Без него опечатка
    # («photos», «Photo») заводит третью породу строк: её не отбирает ни один
    # фильтр по source и не убирает ни одна из двух уборок, зато в поиск по
    # умолчанию она попадает.
    known = {r[0] for r in conn.execute(
        "SELECT conname FROM pg_constraint "
        " WHERE conrelid = 'chunks'::regclass").fetchall()}
    if "chunks_source_check" not in known:
        conn.execute("ALTER TABLE chunks ADD CONSTRAINT chunks_source_check "
                     "CHECK (source IN ('messages', 'photo'))")
    conn.execute("DROP INDEX IF EXISTS chunks_span_idx")
    old = conn.execute(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        " WHERE c.relname = 'chunks_span_part_idx' AND i.indnatts = 4").fetchone()
    if old:
        conn.execute("DROP INDEX chunks_span_part_idx")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS chunks_span_part_idx "
                 "ON chunks (peer_id, msg_from, msg_to, part, source)")


def fmt_duration(seconds) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


def speaker(msg: dict, fallback: str | None) -> str:
    """Кто говорит. В личной переписке собеседник — это сам диалог, так что
    «DELETED» меняем на опознанное имя. В беседе отправителей много, там
    имя из экспорта — единственный источник правды, и fallback пустой."""
    if msg["is_own"]:
        return OWN_NAME
    sender = (msg["sender"] or "").strip()
    if (sender in ANON_SENDERS or sender == "Вы") and fallback:
        return fallback
    return sender or "?"


def collapse_repeats(text: str) -> str:
    """Схлопнуть залипшие повторы: «ХАХАХА…» на четыре тысячи символов,
    «!!!!!!!!!», «ааааааа». В беседах такое встречается и раздувает чанк,
    не добавляя ни капли смысла. Оставляем три повтора — эмоция сохраняется,
    объём нет. Исходное сообщение цело, чанк ссылается на него по vk_msg_id.

    Цифры не трогаем. Без этой оговорки «мне 1000000 рублей» превращалось
    в «мне 1000 рублей», «10000000 раз» — в «1000 раз», а «0,0000001» — в
    «0,0001»: правило про повторы юнита в 1–4 символа с одинаковым успехом
    ловит и залипшую клавишу, и обычное круглое число.
    """
    def keep(m: re.Match) -> str:
        unit = m.group(1)
        return m.group(0) if DIGITS_ONLY.fullmatch(unit) else unit * 3

    return REPEATED_UNIT.sub(keep, text)


def render_message(msg: dict, fallback: str | None) -> str:
    """Одна реплика в том виде, в каком её увидит модель эмбеддингов."""
    body = []
    if msg["text"]:
        body.append(collapse_repeats(msg["text"].strip()))
    if msg["transcript"]:
        body.append(f"[голосовое {fmt_duration(msg['duration_s'])}] "
                    f"{collapse_repeats(msg['transcript'].strip())}")
    if not body and msg["atts"]:
        # Пустая реплика с вложением — сама по себе смысла не несёт, но
        # выкидывать её нельзя: «прислал фото» бывает ответом на вопрос.
        body.append(", ".join(f"[{a}]" for a in msg["atts"] if a))
    if not body:
        return ""
    when = msg["sent_at"].strftime("%H:%M") if msg["sent_at"] else "--:--"
    return f"{speaker(msg, fallback)} ({when}): " + " ".join(body)


def header_for(name: str, kind: str, ts) -> str:
    """Шапка чанка: с кем и когда. Без неё вектор не знает ни собеседника,
    ни года — а половина запросов к архиву именно про это."""
    what = {"user": f"Переписка с {name}",
            "chat": f"Беседа «{name}»"}.get(kind, f"Сообщество «{name}»")
    if not ts:
        return what + "."
    return (f"{what}, {ts.day} {MONTHS_RU[ts.month - 1]} {ts.year} года.")


def flush(buf: list, name: str, kind: str, part: int = 0) -> dict | None:
    """Собрать накопленные реплики в чанк."""
    # Считаем только те реплики, что реально попали в текст. Пустые (без
    # текста, без расшифровки, без вложений) отфильтровывались из body, но
    # границы окна и n_messages брались по всему буферу — и msg_to мог
    # указывать на сообщение, содержимого которого в чанке нет вовсе.
    # А ключ (peer_id, msg_from, msg_to, part) объявлен естественным ключом
    # чанка, то есть ключ переставал соответствовать содержимому.
    kept = [(m, ln) for m, ln in buf if ln]
    if not kept:
        return None
    body = "\n".join(ln for _, ln in kept)
    # Чанк без единого слова (только вложения и смайлы) искать нечем.
    # Пометки вида «[Фотография]» и подписи говорящих снимаем перед проверкой:
    # иначе «Я (20:49): [Фотография]» считается содержательным, хотя искать
    # в нём нечего. Внутри большого разговора такая реплика остаётся — она
    # отсеивается только когда весь чанк из них и состоит.
    meaningful = re.sub(r"\[[^]]*\]", " ", body)
    meaningful = re.sub(r"^[^:\n]{0,60}\(\d\d:\d\d\):", " ", meaningful,
                        flags=re.MULTILINE)
    if not re.search(r"[а-яёa-z]{3}", meaningful, re.I):
        return None
    first, last = kept[0][0], kept[-1][0]
    fallback = name if kind == "user" else None
    senders = sorted({speaker(m, fallback) for m, _ in kept})
    n_voice = sum(1 for m, _ in kept if m["transcript"])
    ts_from, ts_to = first["sent_at"], last["sent_at"]
    return {
        "msg_from": first["vk_msg_id"], "msg_to": last["vk_msg_id"],
        "part": part,
        "ts_from": ts_from, "ts_to": ts_to, "n_messages": len(kept),
        "text": header_for(name, kind, ts_from) + "\n" + body,
        "meta": {
            "senders": senders,
            "n_voice": n_voice,
            "has_own": any(m["is_own"] for m, _ in kept),
            # Месяц пригодится второму ярусу — сборке хронологии по времени.
            "month": ts_from.strftime("%Y-%m") if ts_from else None,
        },
    }


def split_long(line: str) -> list[str]:
    """Одна реплика длиннее жёсткого лимита — режем её на части.

    Двадцатиминутное голосовое даёт 12 тысяч символов в одном сообщении, и
    в одном векторе оно превращается в кашу: конкретное упоминание тонет
    среди всего остального. Точек в расшифровках часто нет вовсе, поэтому
    после предложений идёт запасной вариант — по словам.
    """
    prefix, _, body = line.partition(": ")
    prefix += ": "
    atoms: list[str] = []
    for sentence in re.split(r"(?<=[.!?…])\s+", body):
        if len(sentence) <= TARGET_CHARS:
            atoms.append(sentence)
            continue
        cur = ""
        for w in sentence.split():
            # Слово длиннее целевого размера — режем прямо по символам.
            # Без этого «ХХАХАХАХА…» без пробелов на 4 тыс. символов уезжает
            # в чанк одним куском: делить по словам там нечего.
            while len(w) > TARGET_CHARS:
                if cur:
                    atoms.append(cur)
                    cur = ""
                atoms.append(w[:TARGET_CHARS])
                w = w[TARGET_CHARS:]
            if cur and len(cur) + len(w) + 1 > TARGET_CHARS:
                atoms.append(cur)
                cur = ""
            cur = f"{cur} {w}".strip()
        if cur:
            atoms.append(cur)

    parts, cur = [], ""
    for atom in atoms:
        if cur and len(cur) + len(atom) + 1 > TARGET_CHARS:
            parts.append(prefix + cur)
            cur = ""
        cur = f"{cur} {atom}".strip()
    if cur:
        parts.append(prefix + cur)
    return parts or [line]


def tail_overlap(buf: list) -> list:
    """Хвост, переходящий в следующий чанк: не больше OVERLAP_MESSAGES реплик
    и не больше OVERLAP_MAX_CHARS символов. Длинную реплику не переносим
    вовсе — иначе она размножится по всему индексу."""
    tail, size = [], 0
    for item in reversed(buf[-OVERLAP_MESSAGES:]):
        length = len(item[1])
        if size + length > OVERLAP_MAX_CHARS:
            break
        tail.insert(0, item)
        size += length
    return tail


def chunk_dialog(rows, name: str, kind: str):
    """Поток сообщений → поток чанков."""
    fallback = name if kind == "user" else None
    buf, size, prev_ts = [], 0, None
    for rec in rows:
        msg = dict(zip(("vk_msg_id", "sent_at", "sender", "is_own", "text",
                        "transcript", "duration_s", "atts"), rec,
                       strict=True))
        line = render_message(msg, fallback)
        gap = (prev_ts and msg["sent_at"]
               and (msg["sent_at"] - prev_ts).total_seconds() > GAP_MINUTES * 60)

        if len(line) > HARD_MAX_CHARS:
            # Реплика сама по себе больше лимита: закрываем накопленное и
            # отдаём её частями. Соседей к ней не подмешиваем — иначе
            # границы частей поедут при следующей пересборке.
            if buf:
                chunk = flush(buf, name, kind)
                if chunk:
                    yield chunk
                buf, size = [], 0
            for n, sub in enumerate(split_long(line), 1):
                chunk = flush([(msg, sub)], name, kind, part=n)
                if chunk:
                    yield chunk
            prev_ts = msg["sent_at"] or prev_ts
            continue

        too_big = size + len(line) > HARD_MAX_CHARS
        if buf and (gap or too_big or len(buf) >= MAX_MESSAGES):
            chunk = flush(buf, name, kind)
            if chunk:
                yield chunk
            # После паузы разговор новый — нахлёст только внутри сессии.
            buf = [] if gap else tail_overlap(buf)
            size = sum(len(ln) for _, ln in buf)
        buf.append((msg, line))
        size += len(line)
        prev_ts = msg["sent_at"] or prev_ts
        if size >= TARGET_CHARS:
            chunk = flush(buf, name, kind)
            if chunk:
                yield chunk
            buf = tail_overlap(buf)
            size = sum(len(ln) for _, ln in buf)
    if buf:
        chunk = flush(buf, name, kind)
        if chunk:
            yield chunk


def process(conn, peer_id: int, name: str, kind: str,
            write: bool, rebuild: bool) -> dict:
    if write and rebuild:
        conn.execute("DELETE FROM chunks WHERE peer_id = %s "
                     "AND source = 'messages'", (peer_id,))

    stats = {"chunks": 0, "chars": 0, "voice": 0, "inserted": 0,
             "msgs": 0, "src_msgs": 0, "stale": 0}
    keys: list[tuple] = []
    stats["src_msgs"] = conn.execute(
        "SELECT count(*) FROM messages WHERE peer_id = %s", (peer_id,)).fetchone()[0]
    with conn.cursor(name=f"chunk_{peer_id}") as cur:
        cur.itersize = 5000
        cur.execute(SQL_MESSAGES, (peer_id,))
        wcur = conn.cursor()
        batch = []
        for chunk in chunk_dialog(cur, name, kind):
            stats["chunks"] += 1
            stats["chars"] += len(chunk["text"])
            # Части одной длинной реплики (part 1..N) считаем один раз:
            # иначе двадцатиминутное голосовое добавляет десять сообщений
            # и десять ГС вместо одного, и метрика дублирования врёт вверх
            # ровно там, где голосовых больше всего.
            if chunk["part"] <= 1:
                stats["voice"] += chunk["meta"]["n_voice"]
                stats["msgs"] += chunk["n_messages"]
            if not write:
                continue
            keys.append((chunk["msg_from"], chunk["msg_to"], chunk["part"]))
            batch.append((peer_id, chunk["msg_from"], chunk["msg_to"],
                          chunk["part"], chunk["ts_from"], chunk["ts_to"],
                          chunk["n_messages"], chunk["text"],
                          psycopg.types.json.Jsonb(chunk["meta"])))
            if len(batch) >= 500:
                wcur.executemany(SQL_INSERT, batch)
                stats["inserted"] += wcur.rowcount if wcur.rowcount > 0 else 0
                batch = []
        if write and batch:
            wcur.executemany(SQL_INSERT, batch)
            stats["inserted"] += wcur.rowcount if wcur.rowcount > 0 else 0
    if write:
        # Убираем чанки, которых в новой нарезке уже нет. Границы окон —
        # функция всей последовательности сообщений: доимпортировали в
        # середину диалога хоть одну реплику, и все последующие окна
        # сдвинулись. Без уборки в индексе оставались два перекрывающихся
        # набора окон на один кусок переписки, и поиск выдавал их обоих,
        # занимая две позиции в топе вместо одной.
        stats["stale"] = conn.execute("""
            DELETE FROM chunks c
             WHERE c.peer_id = %s
               AND c.source = 'messages'
               AND NOT EXISTS (
                 SELECT 1 FROM unnest(%s::bigint[], %s::bigint[], %s::int[])
                              AS k(msg_from, msg_to, part)
                  WHERE k.msg_from = c.msg_from AND k.msg_to = c.msg_to
                    AND k.part = c.part)""",
            (peer_id, [k[0] for k in keys], [k[1] for k in keys],
             [k[2] for k in keys])).rowcount if keys else 0
        conn.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", action="append", help="peer_id (можно несколько)")
    ap.add_argument("--all", action="store_true",
                    help="все диалоги с реальным участием (не useless)")
    ap.add_argument("--kind", choices=["user", "chat", "community"],
                    help="только диалоги этого типа")
    ap.add_argument("--write", action="store_true", help="писать в БД")
    ap.add_argument("--rebuild", action="store_true",
                    help="снести старые чанки диалога (теряются эмбеддинги)")
    ap.add_argument("--sample", type=int, default=0,
                    help="показать N примеров чанков")
    args = ap.parse_args()

    if not args.peer and not args.all and not args.kind:
        sys.exit("нужен --peer, --kind или --all")

    with psycopg.connect(DSN) as conn:
        ensure_schema(conn)
        conn.commit()
        if args.all or args.kind:
            rows = conn.execute(
                "SELECT peer_id, name, kind FROM dialogs WHERE NOT useless "
                "AND total_msgs > 0 AND (%s::text IS NULL OR kind = %s) "
                "ORDER BY total_msgs DESC", (args.kind, args.kind)).fetchall()
        else:
            rows = conn.execute(
                "SELECT peer_id, name, kind FROM dialogs WHERE peer_id = ANY(%s)",
                ([int(p) for p in args.peer],)).fetchall()
            if len(rows) != len({int(p) for p in args.peer}):
                sys.exit("не все peer_id найдены")

        if args.sample:
            for peer_id, name, kind in rows[:1]:
                with conn.cursor(name="sample") as cur:
                    cur.itersize = 2000
                    cur.execute(SQL_MESSAGES, (peer_id,))
                    for i, ch in enumerate(chunk_dialog(cur, name, kind)):
                        if i >= args.sample:
                            break
                        print(f"\n--- чанк {i + 1}: {ch['n_messages']} реплик, "
                              f"{len(ch['text'])} симв., ГС {ch['meta']['n_voice']} ---")
                        print(ch["text"][:1500])
            return 0

        started = dt.datetime.now()
        total = {"chunks": 0, "chars": 0, "voice": 0, "inserted": 0,
                 "msgs": 0, "src_msgs": 0, "stale": 0}
        for peer_id, name, kind in rows:
            st = process(conn, peer_id, name, kind,
                         args.write, args.rebuild)
            for k in total:
                total[k] += st[k]
            if st["chunks"]:
                dup = st["msgs"] / max(st["src_msgs"], 1)
                print(f"{peer_id:>12}  {st['chunks']:>7} чанков  "
                      f"ср. {st['chars'] // max(st['chunks'], 1):>5} симв.  "
                      f"ГС {st['voice']:>5}  дубл. ×{dup:.2f}  {name[:30]}")

        took = (dt.datetime.now() - started).total_seconds()
        print(f"\nитого: {total['chunks']} чанков, "
              f"{total['chars'] / 1048576:.1f} МБ текста, "
              f"{total['voice']} голосовых внутри, "
              f"дублирование ×{total['msgs'] / max(total['src_msgs'], 1):.2f}, "
              f"за {took:.1f} с")
        if args.write:
            print(f"вставлено новых: {total['inserted']}, "
                  f"удалено устаревших: {total['stale']}")
        else:
            print("режим прикидки — в БД ничего не записано (нужен --write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
