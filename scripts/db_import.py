#!/usr/bin/env python3
"""Импорт архива VK в PostgreSQL (см. db/schema.sql, docs/db-schema.md).

Стадии (можно по отдельности, все идемпотентны):
  .venv/bin/python scripts/db_import.py dialogs        # participation.json
  .venv/bin/python scripts/db_import.py messages       # HTML → messages+attachments
  .venv/bin/python scripts/db_import.py transcription  # batches/vocab/periods/транскрипты
  .venv/bin/python scripts/db_import.py verify         # сверка счётчиков

messages: полная перезаливка диалога (DELETE + COPY) — безопасно перезапускать
после обвала; прогресс печатается каждые 100 диалогов.
"""
import argparse
import datetime as dt
import hashlib
import html as html_mod
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DSN                               # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "messages"
PROC = ROOT / "data" / "processed"
TRANS = PROC / "transcription"

MSK = ZoneInfo("Europe/Moscow")
MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "июн": 6,
          "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}

BOILERPLATE = re.compile(
    r"(субтитры\s*(делал|сделал|создавал|подготовил|от)?|редактор\s+субтитров|"
    r"корректор|dimatorzok|amara\.org|продолжение\s+следует|"
    r"подписывайтесь\s+на\s+канал|спасибо\s+за\s+просмотр|ставьте\s+лайки)",
    re.IGNORECASE)

MSG_SPLIT = re.compile(r'<div class="message" data-id="(\d+)"')
HEADER = re.compile(r'<div class="message__header">(?:<a[^>]*>)?([^<,]+)')
DATE = re.compile(r", (\d{1,2}) (\w{3}) (\d{4}) в (\d{1,2}):(\d{2}):(\d{2})")
# У сообщений БЕЗ вложений блока kludges нет вовсе, поэтому ограничивать текст
# только им нельзя — так терялось 11% сообщений (проверено 31.07.2026).
# Режем по любому из возможных ограничителей, включая пагинацию в конце страницы.
TEXT_STOPS = ('<div class="kludges">', '<div class="pagination',
              '<div class="item">')
ATT = re.compile(
    r'<div class="attachment">\s*'
    r'<div class="attachment__description">([^<]*)</div>\s*'
    r"(?:<a class='attachment__link' href='([^']+)')?", re.S)


def parse_date(body: str):
    m = DATE.search(body)
    if not m or m.group(2) not in MONTHS:
        return None
    d, mon, y, hh, mm, ss = (int(x) if i != 1 else MONTHS[x]
                             for i, x in enumerate(m.groups()))
    try:
        return dt.datetime(y, mon, d, hh, mm, ss, tzinfo=MSK)
    except ValueError:
        return None


def att_kind(att_type: str, url: str) -> str:
    if url and "/amsg/" in url and url.split("?")[0].endswith(".ogg"):
        return "voice"
    if att_type == "Фотография":
        return "photo"
    return "other"


def clean_text(raw: str) -> str:
    txt = html_mod.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"[ \t]+", " ", txt.replace("\x00", "")).strip()


def extract_text(body: str) -> str:
    """Текст сообщения: после заголовка и до вложений/конца блока."""
    head_end = body.find("</div>")          # конец message__header
    rest = body[head_end + 6:] if head_end >= 0 else body
    for stop in TEXT_STOPS:
        i = rest.find(stop)
        if i >= 0:
            rest = rest[:i]
    return clean_text(rest)


def stage_dialogs(conn) -> None:
    data = json.loads((PROC / "participation.json").read_text(encoding="utf-8"))
    with conn.cursor() as cur:
        for pid, r in data.items():
            cur.execute("""
                INSERT INTO dialogs (peer_id, name, kind, useless, total_msgs,
                    own_msgs, own_share, voice_total, voice_own, years)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (peer_id) DO UPDATE SET
                    -- Имя, проставленное вручную (meta.name_manual), важнее
                    -- имени из архива: VK отдаёт «DELETED» для удалённых
                    -- аккаунтов, а мы знаем, кто это был.
                    name = COALESCE(dialogs.meta->>'name_manual', EXCLUDED.name),
                    useless = EXCLUDED.useless,
                    total_msgs = EXCLUDED.total_msgs,
                    own_msgs = EXCLUDED.own_msgs,
                    own_share = EXCLUDED.own_share,
                    voice_total = EXCLUDED.voice_total,
                    voice_own = EXCLUDED.voice_own, years = EXCLUDED.years
                """, (int(pid), html_mod.unescape(r["name"]), r["kind"],
                      r["useless"], r["total"],
                      r["mine"], r["mine_share"], r["voice"], r["voice_mine"],
                      r["years"]))
    conn.commit()
    print(f"dialogs: {len(data)}")


def stage_messages(conn) -> None:
    dirs = sorted((d for d in RAW.iterdir() if d.is_dir()),
                  key=lambda d: int(d.name))
    voice_dir = ROOT / "data" / "media" / "voice"
    for n, peer_dir in enumerate(dirs, 1):
        pid = int(peer_dir.name)
        msg_rows, att_rows = [], []
        for page in peer_dir.glob("messages*.html"):
            text = page.read_bytes().decode("cp1251", errors="replace")
            parts = MSG_SPLIT.split(text)
            for i in range(1, len(parts) - 1, 2):
                mid, body = int(parts[i]), parts[i + 1]
                hdr = HEADER.search(body)
                # Имя берётся из HTML напрямую, а не через extract_text, так
                # что сущности надо снимать здесь: иначе имя с эмодзи вида
                # «&#127800;» уезжает в чанки и в экспорт как есть.
                sender = html_mod.unescape(hdr.group(1)).strip() if hdr else "?"
                msg_rows.append((
                    pid, mid, parse_date(body), sender, sender == "Вы",
                    extract_text(body), page.name))
                for att_type, url in ATT.findall(body):
                    kind = att_kind(att_type, url)
                    local = None
                    if kind == "voice" and url:
                        p = voice_dir / (hashlib.sha1(url.encode())
                                         .hexdigest()[:16] + ".ogg")
                        local = str(p.relative_to(ROOT)) if p.exists() else None
                    att_rows.append((pid, mid, att_type, kind, url or None,
                                     local, bool(local) if kind == "voice" else None))
        with conn.cursor() as cur:
            # Upsert, а НЕ delete+insert: id сообщений переиспользуются
            # транскриптами и вложениями (внешние ключи), и стирать их нельзя —
            # расшифровки стоили часов работы. Повторный импорт безопасен.
            cur.execute("""
                CREATE TEMP TABLE msg_stage (peer_id bigint, vk_msg_id bigint,
                    sent_at timestamptz, sender text, is_own boolean,
                    text text, page_file text) ON COMMIT DROP""")
            with cur.copy("COPY msg_stage FROM STDIN") as cp:
                seen = set()
                for row in msg_rows:
                    if row[1] in seen:      # дубль data-id на стыке страниц
                        continue
                    seen.add(row[1])
                    cp.write_row(row)
            cur.execute("""
                INSERT INTO messages (peer_id, vk_msg_id, sent_at, sender,
                                      is_own, text, page_file)
                SELECT peer_id, vk_msg_id, sent_at, sender, is_own, text,
                       page_file FROM msg_stage
                ON CONFLICT (peer_id, vk_msg_id) DO UPDATE SET
                    sent_at = EXCLUDED.sent_at, sender = EXCLUDED.sender,
                    is_own = EXCLUDED.is_own, text = EXCLUDED.text,
                    page_file = EXCLUDED.page_file""")
            cur.execute("""
                CREATE TEMP TABLE att_stage (peer_id bigint, vk_msg_id bigint,
                    att_type text, kind text, url text, local_path text,
                    download_ok boolean) ON COMMIT DROP""")
            with cur.copy("COPY att_stage FROM STDIN") as cp:
                # Дедуп нужен здесь так же, как и для сообщений: одно и то же
                # сообщение попадается на стыке страниц messages*.html дважды,
                # и без этого в att_stage уезжают две одинаковые строки. Guard
                # ниже их не спасает — он смотрит на снимок таблицы до начала
                # запроса и обе строки пропускает. Так в базе завелись 1574
                # лишних вложения (аудиозаписи, фото; голосовых среди них нет).
                seen_att = set()
                for row in att_rows:
                    key = (row[1], row[2], row[4])   # vk_msg_id, тип, url
                    if key in seen_att:
                        continue
                    seen_att.add(key)
                    cp.write_row(row)
            # Вложения добавляем только тем сообщениям, у которых их ещё нет —
            # иначе повторный импорт наплодит дубликаты (естественного ключа
            # у вложения нет), а удалять их нельзя: на них ссылаются транскрипты.
            cur.execute("""
                INSERT INTO attachments (message_id, att_type, kind, url,
                                         local_path, download_ok)
                SELECT m.id, s.att_type, s.kind, s.url, s.local_path,
                       s.download_ok
                  FROM att_stage s
                  JOIN messages m ON m.peer_id = s.peer_id
                                 AND m.vk_msg_id = s.vk_msg_id
                 WHERE NOT EXISTS (SELECT 1 FROM attachments a
                                    WHERE a.message_id = m.id)""")
        conn.commit()
        if n % 100 == 0 or n == len(dirs):
            print(f"  {n}/{len(dirs)} диалогов", flush=True)


def stage_transcription(conn) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from transcribe_voice import load_periods  # noqa: PLC0415 — нужен не всем стадиям
    with conn.cursor() as cur:
        # батчи промптов
        n_b = 0
        for bf in sorted((TRANS / "batches").glob("batch-*.json")):
            b = json.loads(bf.read_text(encoding="utf-8"))
            peer = int(b["key"].split("-")[0])
            cur.execute("""
                INSERT INTO prompt_batches (key, peer_id, batch_date, context,
                    prompt, prompt_source, new_terms)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET prompt = EXCLUDED.prompt,
                    prompt_source = EXCLUDED.prompt_source,
                    new_terms = EXCLUDED.new_terms
                """, (b["key"], peer, b.get("date"), b["context"], b["prompt"],
                      b["prompt_source"], json.dumps(b.get("new_terms", []),
                                                     ensure_ascii=False)))
            n_b += 1
        # транскрипты: name = <peer>-<vk_msg_id>
        n_t, missing = 0, []
        for meta_f in sorted((TRANS / "transcripts").glob("*.meta.json")):
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
            peer, mid = meta_f.stem.replace(".meta", "").rsplit("-", 1)
            txt_f = meta_f.with_name(meta_f.name.replace(".meta.json", ".txt"))
            transcript = (txt_f.read_text(encoding="utf-8").strip()
                          if txt_f.exists() else None)
            cur.execute("""
                SELECT a.id, a.message_id FROM attachments a
                  JOIN messages m ON m.id = a.message_id
                 WHERE m.peer_id = %s AND m.vk_msg_id = %s AND a.kind = 'voice'
                """, (int(peer), int(mid)))
            row = cur.fetchone()
            if not row:
                missing.append(meta_f.stem)
                continue
            cur.execute("""
                INSERT INTO voice_transcripts (attachment_id, message_id,
                    duration_s, transcript, empty_audio, whisper_prompt,
                    batch_key, wall_s, settings, max_repeat, suspect,
                    boilerplate)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (attachment_id) DO UPDATE SET
                    transcript = EXCLUDED.transcript,
                    empty_audio = EXCLUDED.empty_audio,
                    whisper_prompt = EXCLUDED.whisper_prompt,
                    batch_key = EXCLUDED.batch_key, wall_s = EXCLUDED.wall_s,
                    settings = EXCLUDED.settings,
                    max_repeat = EXCLUDED.max_repeat,
                    suspect = EXCLUDED.suspect,
                    boilerplate = EXCLUDED.boilerplate
                """, (row[0], row[1], meta.get("duration_s"), transcript,
                      not transcript, meta.get("whisper_prompt"),
                      meta.get("batch"), meta.get("wall_s"),
                      meta.get("settings"), meta.get("max_repeat"),
                      bool(meta.get("suspect")),
                      bool(transcript and BOILERPLATE.search(transcript))))
            n_t += 1
        # словарь: термины из ```text-блока (manual) + Автособранное (sonnet)
        n_v = 0
        for vf in (TRANS / "vocab").glob("*.md"):
            if vf.stem.endswith("-periods"):
                continue
            peer = int(vf.stem)
            text = vf.read_text(encoding="utf-8")
            block = re.search(r"```text\n(.*?)```", text, re.S)
            if block:
                for term in re.split(r",\s*", " ".join(block.group(1).split())):
                    term = term.strip(" .")
                    if 1 < len(term) <= 60:
                        cur.execute("""
                            INSERT INTO vocab_terms (peer_id, term, source)
                            VALUES (%s, %s, 'manual')
                            ON CONFLICT (peer_id, term) DO NOTHING
                            """, (peer, term))
                        n_v += 1
            auto = re.search(r"## Автособранное \(Sonnet\)\n(.*)", text, re.S)
            if auto:
                for line in auto.group(1).splitlines():
                    m = re.match(r"- (.+?)\s*[—–]\s*(.+?)\s*\*\(встретилось: "
                                 r"([^,]+), (.+?)\)\*", line)
                    if not m:
                        continue
                    cur.execute("""
                        INSERT INTO vocab_terms (peer_id, term, gloss, source,
                            first_batch, first_date)
                        VALUES (%s, %s, %s, 'sonnet', %s, %s)
                        ON CONFLICT (peer_id, term) DO UPDATE SET
                            gloss = EXCLUDED.gloss
                        """, (peer, m.group(1).strip(), m.group(2).strip(),
                              m.group(3).strip(), m.group(4).strip()))
                    n_v += 1
        # периоды
        n_p = 0
        for vf in (TRANS / "vocab").glob("*-periods.md"):
            peer = int(vf.stem.replace("-periods", ""))
            for year, p in load_periods(str(peer)).items():
                cur.execute("""
                    INSERT INTO dialog_periods (peer_id, year_from,
                        description, whisper_prompt)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (peer_id, year_from) DO UPDATE SET
                        description = EXCLUDED.description,
                        whisper_prompt = EXCLUDED.whisper_prompt
                    """, (peer, year, p["desc"], p["prompt"]))
                n_p += 1
    conn.commit()
    print(f"batches: {n_b}, transcripts: {n_t} (не найдено: {len(missing)}), "
          f"vocab: {n_v}, periods: {n_p}")
    if missing:
        print("  без пары в БД:", ", ".join(missing[:10]))


def stage_verify(conn) -> None:
    with conn.cursor() as cur:
        for q, label in [
            ("SELECT count(*) FROM dialogs", "dialogs"),
            ("SELECT count(*) FROM messages", "messages"),
            ("SELECT count(*) FROM attachments", "attachments"),
            ("SELECT count(*) FROM attachments WHERE kind='voice'", "  voice"),
            ("SELECT count(*) FROM attachments WHERE kind='voice' "
             "AND local_path IS NOT NULL", "  voice скачано"),
            ("SELECT count(*) FROM voice_transcripts", "voice_transcripts"),
            ("SELECT count(*) FROM prompt_batches", "prompt_batches"),
            ("SELECT count(*) FROM vocab_terms", "vocab_terms"),
            ("SELECT count(*) FROM dialog_periods", "dialog_periods"),
            ("SELECT min(sent_at), max(sent_at) FROM messages", "период"),
        ]:
            cur.execute(q)
            print(f"{label}: {cur.fetchone()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["dialogs", "messages", "transcription",
                                      "verify", "all"])
    args = ap.parse_args()
    with psycopg.connect(DSN) as conn:
        stages = (["dialogs", "messages", "transcription", "verify"]
                  if args.stage == "all" else [args.stage])
        for s in stages:
            print(f"=== {s} ===", flush=True)
            globals()[f"stage_{s}"](conn)


if __name__ == "__main__":
    main()
