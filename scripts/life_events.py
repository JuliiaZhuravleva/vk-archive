#!/usr/bin/env python3
"""Хронология жизни: события, добытые раскопками по архиву, и их основания.

Зачем отдельная таблица, а не заметка в файле. Раскопка стоит дорого — один
факт («когда я проходила такую-то игру») выходит десятком запросов по миллиону
сообщений, — и её результат обязан пережить и следующую сессию, и переимпорт
базы, и вторую выгрузку VK. Поэтому у события есть slug (стабильный ключ),
цитаты-основания копией, шкала уверенности и поле gaps: что осталось
непроверенным. Без gaps повторная раскопка начинается с нуля, потому что
«искали и не нашли» неотличимо от «не искали».

Источник истины — JSON на диске (по умолчанию data/processed/life_events/),
база его зеркалит: так же, как с транскриптами и словарями. Файл можно
править руками, `import` перезаписывает событие по slug целиком вместе с
источниками — «обновить» и «добавить» это одна и та же операция.

  python3 scripts/life_events.py import                 # весь каталог
  python3 scripts/life_events.py import --file x.json   # один файл
  python3 scripts/life_events.py import --dry-run       # только показать
  python3 scripts/life_events.py list                   # хронология
  python3 scripts/life_events.py list --tag игры --from 2018
  python3 scripts/life_events.py show <slug>            # событие с цитатами
  python3 scripts/life_events.py export                 # → markdown

Формат файла: один объект или список объектов.

  {
    "slug": "some-game-playthrough",
    "title": "Прохождение такой-то игры",
    "kind": "media",
    "started_on": "2018-10-01", "precision": "month",
    "ended_on": "2019-10-16", "end_precision": "day",
    "confidence": "verified",
    "summary": "…",
    "tags": ["игры"],
    "gaps": "…",
    "sources": [
      {"peer_id": 123456789, "vk_msg_id": 456, "sent_at": "2018-10-12 10:09",
       "is_own": true, "quote": "цитата из сообщения", "role": "evidence"}
    ]
  }

Из полей обязательны только slug, title, kind и summary. Дата может
отсутствовать целиком (precision='unknown') — событие без даты всё равно
стоит записать, дата уточнится следующей находкой.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DSN, ROOT  # noqa: E402

EVENTS_DIR = ROOT / "data" / "processed" / "life_events"
EXPORT_PATH = EVENTS_DIR / "timeline.md"

KINDS = ("media", "relationship", "work", "move", "health",
         "education", "project", "other")
PRECISIONS = ("day", "month", "year", "unknown")
CONFIDENCES = ("verified", "inferred", "assumed")

# Как показывать дату при каждой точности. Печатать 2018-01-01 для события,
# про которое известен только год, — значит выдать догадку за наблюдение.
FMT = {"day": "%d.%m.%Y", "month": "%m.%Y", "year": "%Y"}


def fmt_date(d, precision):
    if d is None:
        return "дата неизвестна"
    return d.strftime(FMT.get(precision, "%d.%m.%Y"))


def fmt_span(ev):
    """Точность у каждого конца своя: «10.2018 — 16.10.2019» — это ровно то,
    что известно, и видно, какой из концов твёрдый."""
    left = fmt_date(ev["started_on"], ev["precision"])
    if ev["ended_on"] and ev["ended_on"] != ev["started_on"]:
        right = fmt_date(ev["ended_on"],
                         ev.get("end_precision") or ev["precision"])
        return f"{left} — {right}"
    return left


def parse_date(value):
    if not value:
        return None
    return dt.date.fromisoformat(str(value)[:10])


def parse_ts(value):
    """Дата источника: и '2018-10-12', и '2018-10-12 10:09', и полный ISO."""
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"не разобрана дата источника: {value!r}")


def validate(ev):
    """Валидировать ДО записи: половина импортированного файла хуже, чем
    ничего — по базе не видно, где обрыв."""
    errs = []
    for field in ("slug", "title", "kind", "summary"):
        if not ev.get(field):
            errs.append(f"нет обязательного поля {field}")
    if ev.get("kind") and ev["kind"] not in KINDS:
        errs.append(f"kind={ev['kind']!r}, допустимо: {', '.join(KINDS)}")
    prec = ev.get("precision", "day")
    if prec not in PRECISIONS:
        errs.append(f"precision={prec!r}, допустимо: {', '.join(PRECISIONS)}")
    end_prec = ev.get("end_precision")
    if end_prec is not None and end_prec not in PRECISIONS:
        errs.append(f"end_precision={end_prec!r}, "
                    f"допустимо: {', '.join(PRECISIONS)} или отсутствие")
    start, end = ev.get("started_on"), ev.get("ended_on")
    if start and end and parse_date(end) < parse_date(start):
        errs.append(f"ended_on {end} раньше started_on {start}")
    conf = ev.get("confidence", "inferred")
    if conf not in CONFIDENCES:
        errs.append(f"confidence={conf!r}, допустимо: {', '.join(CONFIDENCES)}")
    # Событие без основания не бывает наблюдением: verified означает «видно
    # в архиве», а видно оно в конкретном сообщении.
    if conf == "verified" and not ev.get("sources"):
        errs.append("confidence=verified без sources")
    return errs


def load_files(path):
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    events = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        for ev in (data if isinstance(data, list) else [data]):
            ev["_file"] = f.name
            events.append(ev)
    return events


def check_anchors(cur, events):
    """Сверить (peer_id, vk_msg_id) источников с настоящими сообщениями.

    Ссылка, съехавшая на соседнее сообщение, не выглядит сломанной: цитата
    рядом, дата правдоподобная, и заметить подмену можно только сверкой. При
    этом промах — не повод отказывать в импорте: сообщение могло исчезнуть из
    новой выгрузки VK, ровно поэтому цитата и хранится копией.
    """
    warnings = []
    for ev in events:
        for src in ev.get("sources", []):
            if src.get("peer_id") is None or src.get("vk_msg_id") is None:
                continue
            cur.execute("SELECT sent_at, text FROM messages "
                        "WHERE peer_id = %s AND vk_msg_id = %s",
                        (src["peer_id"], src["vk_msg_id"]))
            row = cur.fetchone()
            if row is None:
                warnings.append(f"{ev['slug']}: сообщения "
                                f"{src['peer_id']}/{src['vk_msg_id']} нет в базе")
                continue
            sent_at, text = row
            want = parse_ts(src.get("sent_at"))
            if want is not None and want.tzinfo is None:
                want = want.replace(tzinfo=dt.timezone.utc)
            if want is not None and want != sent_at:
                warnings.append(
                    f"{ev['slug']}: у {src['peer_id']}/{src['vk_msg_id']} "
                    f"время {sent_at:%d.%m.%Y %H:%M}, а в файле "
                    f"{want:%d.%m.%Y %H:%M}")
            quote = (src.get("quote") or "").strip()
            # Подстрокой, а не по началу: цитируют обычно кусок из середины
            # длинной реплики. Сверка по началу давала бы предупреждение на
            # совершенно правильной выписке.
            if quote and quote[:60] not in (text or ""):
                warnings.append(
                    f"{ev['slug']}: цитата не совпадает с текстом "
                    f"{src['peer_id']}/{src['vk_msg_id']} — «{quote[:40]}…»")
    return warnings


def cmd_import(args):
    path = Path(args.file) if args.file else EVENTS_DIR
    if not path.exists():
        sys.exit(f"нет такого пути: {path}")

    events = load_files(path)
    if not events:
        sys.exit(f"в {path} нет .json с событиями")

    bad = [(ev.get("slug") or ev["_file"], errs)
           for ev in events if (errs := validate(ev))]
    if bad:
        for slug, errs in bad:
            print(f"✗ {slug}: {'; '.join(errs)}", file=sys.stderr)
        sys.exit(f"не импортировано ничего: ошибок в {len(bad)} из {len(events)}")

    seen = {}
    for ev in events:
        if ev["slug"] in seen:
            sys.exit(f"slug {ev['slug']!r} встречается дважды: "
                     f"{seen[ev['slug']]} и {ev['_file']}")
        seen[ev["slug"]] = ev["_file"]

    if args.dry_run:
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            anchor_warnings = check_anchors(cur, events)
        for ev in events:
            print(f"{ev['slug']}: {ev['title']} "
                  f"({len(ev.get('sources', []))} источников)")
        for w in anchor_warnings:
            print(f"! {w}", file=sys.stderr)
        return

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for w in check_anchors(cur, events):
            print(f"! {w}", file=sys.stderr)
        for ev in events:
            cur.execute("""
                INSERT INTO life_events
                  (slug, title, kind, started_on, ended_on, precision,
                   end_precision, confidence, summary, details, tags, gaps,
                   found_on)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                  title = EXCLUDED.title, kind = EXCLUDED.kind,
                  started_on = EXCLUDED.started_on, ended_on = EXCLUDED.ended_on,
                  precision = EXCLUDED.precision,
                  end_precision = EXCLUDED.end_precision,
                  confidence = EXCLUDED.confidence,
                  summary = EXCLUDED.summary, details = EXCLUDED.details,
                  tags = EXCLUDED.tags, gaps = EXCLUDED.gaps,
                  found_on = EXCLUDED.found_on, updated_at = now()
                RETURNING id
            """, (ev["slug"], ev["title"], ev["kind"],
                  parse_date(ev.get("started_on")), parse_date(ev.get("ended_on")),
                  ev.get("precision", "day"), ev.get("end_precision"),
                  ev.get("confidence", "inferred"),
                  ev["summary"], ev.get("details"), ev.get("tags", []),
                  ev.get("gaps"), parse_date(ev.get("found_on"))))
            event_id = cur.fetchone()[0]

            # Источники переписываются целиком: файл — источник истины, и
            # доливать к ним нечего, иначе удалённая из файла цитата осталась
            # бы в базе навсегда.
            cur.execute("DELETE FROM life_event_sources WHERE event_id = %s",
                        (event_id,))
            for src in ev.get("sources", []):
                cur.execute("""
                    INSERT INTO life_event_sources
                      (event_id, peer_id, vk_msg_id, sent_at, is_own, role,
                       quote, note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (event_id, src.get("peer_id"), src.get("vk_msg_id"),
                      parse_ts(src.get("sent_at")), src.get("is_own"),
                      src.get("role", "evidence"), src.get("quote"),
                      src.get("note")))
            print(f"✓ {ev['slug']}: {len(ev.get('sources', []))} источников")
        conn.commit()
    print(f"\nвсего событий в базе: {count_events()}")


def count_events():
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM life_events")
        return cur.fetchone()[0]


def fetch_events(tag=None, date_from=None, date_to=None, kind=None):
    sql = ["""SELECT e.id, e.slug, e.title, e.kind, e.started_on, e.ended_on,
                     e.precision, e.end_precision, e.confidence, e.summary,
                     e.details, e.tags, e.gaps, e.found_on,
                     (SELECT count(*) FROM life_event_sources s
                       WHERE s.event_id = e.id) AS n_sources
                FROM life_events e WHERE true"""]
    params = []
    if tag:
        sql.append("AND %s = ANY(e.tags)")
        params.append(tag)
    if kind:
        sql.append("AND e.kind = %s")
        params.append(kind)
    if date_from:
        sql.append("AND (e.started_on IS NULL OR e.started_on >= %s)")
        params.append(date_from)
    if date_to:
        sql.append("AND (e.started_on IS NULL OR e.started_on <= %s)")
        params.append(date_to)
    # Событий без даты немного, и терять их в выдаче нельзя — они как раз
    # ждут уточнения; NULLS LAST кладёт их в конец списка.
    sql.append("ORDER BY e.started_on NULLS LAST, e.slug")
    cols = ("id slug title kind started_on ended_on precision end_precision "
            "confidence summary details tags gaps found_on n_sources").split()
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(" ".join(sql), params)
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def fetch_sources(event_id, cur=None):
    """Основания события. Курсор передаётся, когда источники тянут в цикле:
    без него `export` открывал бы соединение на каждое событие."""
    if cur is None:
        with psycopg.connect(DSN) as conn, conn.cursor() as own:
            return fetch_sources(event_id, own)
    cur.execute("""
        SELECT s.sent_at, s.is_own, s.role, s.quote, s.note,
               s.peer_id, s.vk_msg_id, d.name
          FROM life_event_sources s
          LEFT JOIN dialogs d ON d.peer_id = s.peer_id
         WHERE s.event_id = %s
         ORDER BY s.sent_at NULLS LAST, s.id
    """, (event_id,))
    cols = "sent_at is_own role quote note peer_id vk_msg_id peer_name".split()
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


MARK = {"verified": "✓", "inferred": "≈", "assumed": "?"}


def cmd_list(args):
    events = fetch_events(args.tag, parse_bound(args.date_from, False),
                          parse_bound(args.date_to, True), args.kind)
    if not events:
        print("ничего не найдено")
        return
    for ev in events:
        tags = f"  [{', '.join(ev['tags'])}]" if ev["tags"] else ""
        print(f"{MARK[ev['confidence']]} {fmt_span(ev):<24} {ev['title']}{tags}")
        print(f"    {ev['summary']}")
        print(f"    slug: {ev['slug']} · источников: {ev['n_sources']}")
        if ev["gaps"]:
            print(f"    не проверено: {ev['gaps']}")
        print()
    print(f"событий: {len(events)}")


def cmd_show(args):
    events = [e for e in fetch_events() if e["slug"] == args.slug]
    if not events:
        sys.exit(f"нет события со slug {args.slug!r}")
    ev = events[0]
    print(f"{ev['title']}\n{'=' * len(ev['title'])}")
    prec = ev["precision"]
    if ev["end_precision"] and ev["end_precision"] != prec:
        prec = f"{prec} / {ev['end_precision']}"
    print(f"когда:      {fmt_span(ev)} (точность: {prec})")
    print(f"уверенность: {ev['confidence']}")
    print(f"тип:        {ev['kind']}")
    if ev["tags"]:
        print(f"теги:       {', '.join(ev['tags'])}")
    print(f"\n{ev['summary']}")
    if ev["details"]:
        print(f"\n{ev['details']}")
    if ev["gaps"]:
        print(f"\nНе проверено: {ev['gaps']}")
    sources = fetch_sources(ev["id"])
    if sources:
        print(f"\nОснования ({len(sources)}):")
        for s in sources:
            who = "Я" if s["is_own"] else (s["peer_name"] or "собеседник")
            when = s["sent_at"].strftime("%d.%m.%Y %H:%M") if s["sent_at"] else "—"
            role = "" if s["role"] == "evidence" else f" [{s['role']}]"
            print(f"  {when}  {s['peer_name'] or '?'} · {who}{role}")
            if s["quote"]:
                print(f"      «{s['quote']}»")
            if s["note"]:
                print(f"      — {s['note']}")


def cmd_export(args):
    events = fetch_events()
    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    out = Path(args.out) if args.out else EXPORT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Хронология",
             "",
             f"Сгенерировано {dt.date.today().isoformat()} из таблицы "
             f"`life_events`. Правится не здесь, а в JSON "
             f"(`{EVENTS_DIR.relative_to(ROOT)}/`) + `life_events.py import`.",
             "",
             "Метки уверенности: ✓ verified (видно в архиве), "
             "≈ inferred (вывод из увиденного), ? assumed (догадка).",
             ""]
    year = None
    for ev in events:
        y = ev["started_on"].year if ev["started_on"] else None
        if y != year:
            year = y
            lines += [f"## {year or 'Дата неизвестна'}", ""]
        tags = f" · {', '.join(ev['tags'])}" if ev["tags"] else ""
        lines += [f"### {MARK[ev['confidence']]} {ev['title']}",
                  "",
                  f"**{fmt_span(ev)}**{tags}",
                  "",
                  ev["summary"], ""]
        if ev["details"]:
            lines += [ev["details"], ""]
        if ev["gaps"]:
            lines += [f"*Не проверено:* {ev['gaps']}", ""]
        sources = fetch_sources(ev["id"], cur)
        if sources:
            lines.append("Основания:")
            lines.append("")
            for s in sources:
                who = "Я" if s["is_own"] else (s["peer_name"] or "собеседник")
                when = (s["sent_at"].strftime("%d.%m.%Y %H:%M")
                        if s["sent_at"] else "—")
                quote = f" «{s['quote']}»" if s["quote"] else ""
                note = f" — {s['note']}" if s["note"] else ""
                lines.append(f"- {when}, {s['peer_name'] or '?'}, {who}:{quote}{note}")
            lines.append("")
    cur.close()
    conn.close()
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(events)} событий → {out}")


def parse_bound(value, upper):
    """--from 2018 это 01.01.2018, --to 2018 — 31.12.2018 включительно.
    Ровно та же семантика границ, что в search.py и export_chat.py."""
    if not value:
        return None
    parts = [int(p) for p in str(value).split("-")]
    if len(parts) == 1:
        return dt.date(parts[0], 12, 31) if upper else dt.date(parts[0], 1, 1)
    if len(parts) == 2:
        y, m = parts
        if not upper:
            return dt.date(y, m, 1)
        return (dt.date(y + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1))
    return dt.date(*parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="JSON → база (перезапись по slug)")
    p.add_argument("--file", help="файл или каталог; по умолчанию весь каталог")
    p.add_argument("--dry-run", action="store_true", help="только проверить")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("list", help="хронология")
    p.add_argument("--tag")
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--from", dest="date_from", metavar="DATE")
    p.add_argument("--to", dest="date_to", metavar="DATE")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="событие с цитатами-основаниями")
    p.add_argument("slug")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("export", help="markdown-таймлайн")
    p.add_argument("--out")
    p.set_defaults(func=cmd_export)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
