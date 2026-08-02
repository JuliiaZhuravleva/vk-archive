#!/usr/bin/env python3
"""Экспорт переписки VK в md/txt с разбивкой по размеру файла.

Читает из PostgreSQL, поэтому голосовые попадают в текст расшифровками —
именно ради этого всё и делалось. Конвенция имён и разбивки повторяет
telegram_exporter: <название>_<timestamp>_partNN.<ext>.

  python3 scripts/export_chat.py --peer 123456789 --max-size 2
  python3 scripts/export_chat.py --peer 123456789 --format txt --from 2021-01
  python3 scripts/export_chat.py --list          # какие диалоги есть

Вложения обозначаются пометкой ([Фотография], [Видеозапись] …). Голосовые —
🎤 с расшифровкой; если расшифровки нет, помечается явно.
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DSN                               # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXPORT_DIR = ROOT / "data" / "processed" / "exports"

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]


def sanitize(text: str, max_len: int = 50) -> str:
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in text)
    return re.sub(r"_+", "_", safe).strip("_ ")[:max_len] or "chat"


def out_path(title: str, peer: str, ext: str, stamp: str, part=None) -> Path:
    base = f"{sanitize(title)}_{peer}"
    name = f"{base}_{stamp}_part{part:02d}.{ext}" if part else f"{base}_{stamp}.{ext}"
    return EXPORT_DIR / name


def fmt_duration(seconds) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


def render(msg: dict, prev_date, fmt: str) -> tuple:
    """(текст блока, новая дата-разделитель)."""
    ts = msg["sent_at"]
    day = ts.date() if ts else None
    out = []
    if day != prev_date:
        head = (f"{day.day} {MONTHS_RU[day.month - 1]} {day.year}"
                if day else "без даты")
        out.append(f"\n## {head}\n\n" if fmt == "md" else f"\n===== {head} =====\n\n")
    who = msg["sender"] or "?"
    when = ts.strftime("%H:%M") if ts else "--:--"
    out.append(f"**{who}** · {when}\n" if fmt == "md" else f"[{when}] {who}:\n")

    body = []
    if msg["text"]:
        body.append(msg["text"])
    if msg["transcript"] is not None or msg["has_voice"]:
        dur = fmt_duration(msg["duration_s"])
        if msg["transcript"]:
            body.append(f"🎤 *Голосовое ({dur}):* {msg['transcript']}"
                        if fmt == "md" else
                        f"[голосовое {dur}] {msg['transcript']}")
        else:
            body.append(f"🎤 *Голосовое ({dur}) — без расшифровки*"
                        if fmt == "md" else f"[голосовое {dur}, без расшифровки]")
    if msg["atts"]:
        kinds = ", ".join(f"[{a}]" for a in msg["atts"] if a)
        if kinds:
            body.append(kinds)
    out.append(("\n".join(body) if body else "*(пусто)*") + "\n\n")
    return "".join(out), day


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", help="peer_id диалога")
    ap.add_argument("--format", choices=["md", "txt"], default="md")
    ap.add_argument("--max-size", type=float, default=0,
                    help="максимум МБ на файл (0 = один файл)")
    ap.add_argument("--from", dest="date_from", help="YYYY-MM или YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="YYYY-MM или YYYY-MM-DD")
    ap.add_argument("--list", action="store_true", help="показать диалоги")
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        if args.list or not args.peer:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT peer_id, name, total_msgs, voice_total
                      FROM dialogs WHERE NOT useless AND total_msgs > 200
                     ORDER BY total_msgs DESC LIMIT 30""")
                print(f'{"peer_id":>12}  {"сообщений":>9}  {"ГС":>5}  название')
                for pid, name, total, voice in cur.fetchall():
                    print(f"{pid:>12}  {total:>9}  {voice or 0:>5}  {name}")
            return

        with conn.cursor() as cur:
            cur.execute("SELECT name, total_msgs FROM dialogs WHERE peer_id=%s",
                        (int(args.peer),))
            row = cur.fetchone()
            if not row:
                sys.exit(f"нет диалога {args.peer}")
            title, total_msgs = row

        where, params = ["m.peer_id = %s"], [int(args.peer)]
        for bound, op in ((args.date_from, ">="), (args.date_to, "<")):
            if not bound:
                continue
            parts = [int(x) for x in bound.split("-")]
            d = dt.date(parts[0], parts[1] if len(parts) > 1 else 1,
                        parts[2] if len(parts) > 2 else 1)
            # Верхняя граница включающая: сдвигаем на начало следующего
            # периода той же гранулярности. Раньше обрабатывался только
            # случай «год-месяц», и остальные тихо врали: `--to 2016`
            # означало `< 2016-01-01`, то есть весь 2016 год выпадал, а
            # `--from 2020 --to 2020` давало заведомо пустой экспорт.
            if op == "<":
                if len(parts) == 1:                # --to 2021 → включая год
                    d = dt.date(d.year + 1, 1, 1)
                elif len(parts) == 2:              # --to 2021-12 → включая декабрь
                    d = dt.date(d.year + d.month // 12, d.month % 12 + 1, 1)
                else:                              # --to 2021-12-15 → включая 15-е
                    d = d + dt.timedelta(days=1)
            where.append(f"m.sent_at {op} %s")
            params.append(d)

        sql = f"""
            SELECT m.sent_at, m.sender, m.text,
                   v.transcript, v.duration_s,
                   bool_or(a.kind = 'voice') AS has_voice,
                   array_remove(array_agg(DISTINCT a.att_type), NULL) AS atts
              FROM messages m
              LEFT JOIN attachments a ON a.message_id = m.id
              LEFT JOIN voice_transcripts v ON v.message_id = m.id
             WHERE {' AND '.join(where)}
             GROUP BY m.id, m.sent_at, m.sender, m.text, v.transcript, v.duration_s
             ORDER BY m.sent_at, m.vk_msg_id"""

        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        cap = int(args.max_size * 1024 * 1024) if args.max_size else None
        part = 1 if cap else None

        header = (f"# {title}\n\n"
                  f"Экспорт переписки ВКонтакте (peer {args.peer}). "
                  f"Всего сообщений в диалоге: {total_msgs}.\n"
                  f"Голосовые включены расшифровками.\n\n---\n")
        if args.format == "txt":
            header = re.sub(r"[#*]", "", header)

        path = out_path(title, args.peer, args.format, stamp, part)
        f = path.open("w", encoding="utf-8")
        f.write(header)
        size = len(header.encode())
        files, n_msg, n_voice, prev_date = [], 0, 0, None

        with conn.cursor(name="export_cur") as cur:      # серверный курсор
            cur.itersize = 2000
            cur.execute(sql, params)
            for rec in cur:
                msg = dict(zip(("sent_at", "sender", "text", "transcript",
                                "duration_s", "has_voice", "atts"), rec,
                               strict=True))
                block, prev_date = render(msg, prev_date, args.format)
                blob = block.encode()
                if cap and size + len(blob) > cap:
                    f.close()
                    files.append((path, size))
                    print(f"  часть {part}: {path.name} ({size/1048576:.2f} МБ)")
                    part += 1
                    path = out_path(title, args.peer, args.format, stamp, part)
                    f = path.open("w", encoding="utf-8")
                    cont = (f"# {title} (часть {part})\n\n"
                            f"*Продолжение экспорта, начало — часть 1.*\n\n---\n")
                    if args.format == "txt":
                        cont = re.sub(r"[#*]", "", cont)
                    f.write(cont)
                    size = len(cont.encode())
                    prev_date = None          # дату повторяем в новой части
                    block, prev_date = render(msg, prev_date, args.format)
                    blob = block.encode()
                f.write(block)
                size += len(blob)
                n_msg += 1
                n_voice += bool(msg["transcript"])
        f.close()
        files.append((path, size))
        if cap:
            print(f"  часть {part}: {path.name} ({size/1048576:.2f} МБ)")

    total_mb = sum(s for _, s in files) / 1048576
    print(f"\nготово: {n_msg} сообщений (из них {n_voice} с расшифровкой ГС) "
          f"→ {len(files)} файл(ов), {total_mb:.1f} МБ")
    print(f"каталог: {EXPORT_DIR}")


if __name__ == "__main__":
    main()
