#!/usr/bin/env python3
"""Чанки поискового указателя: запросы, по которым стоит показать разговор.

Локальная модель читает переписку кусками и не пересказывает её, а делит на
разговоры (от сообщения до сообщения) и к каждому пишет 3–6 запросов, при
которых этот разговор окажется полезной находкой. Это приём doc2query: ключ
поиска обогащается, а текст переписки не трогается и остаётся единственным
источником. Выход модели — файлы queries_YYYY-MM.jsonl, по строке на кусок:

  {"peer": …, "talks": [{"from": <vk_msg_id>, "to": <vk_msg_id>,
                         "queries": ["…", …], "n": <реплик>}], …}

Здесь каждый разговор становится чанком породы `queries`: шапка (с кем,
когда) и список запросов. Запросы не приклеиваются к тексту переписки, а
лежат отдельными строками — так в гибридном поиске указатель и переписка
ранжируются порознь и сливаются RRF, а шум выдуманного запроса не портит
вектор самой переписки. Нашёлся чанк-указатель — читать надо сообщения
msg_from..msg_to.

  python3 scripts/build_query_chunks.py data/processed/digests/queries_2018-11.jsonl --sample 5
  python3 scripts/build_query_chunks.py data/processed/digests/queries_2018-*.jsonl --write

Повторный прогон безопасен: вставка по ключу с обновлением, эмбеддинг
сбрасывается только у чанка, чей текст поменялся. Уборка — в пределах месяца
файла: указатель, которого в файле больше нет, удаляется.
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_chunks import ensure_schema, header_for            # noqa: E402
from common import DSN                                        # noqa: E402

# Месяц файла — по московскому времени: так его нарезает модель, и граница
# уборки должна совпадать с границей нарезки, иначе разговор в ночь на первое
# число окажется «чужим» для обоих месяцев.
TZ = ZoneInfo("Europe/Moscow")
FILE_MONTH = re.compile(r"queries_(\d{4})-(\d\d)\.jsonl$")
# Запрос из одного слова — это имя или тема без предмета («игра», «Вадим»):
# такой ключ находит всё подряд. Модели это запрещено, но изредка проскакивает.
MIN_WORDS = 2
MAX_QUERIES = 6

SQL_INSERT = """
    INSERT INTO chunks (peer_id, msg_from, msg_to, part, ts_from, ts_to,
                        n_messages, text, meta, source)
    VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s, 'queries')
    ON CONFLICT (peer_id, msg_from, msg_to, part, source) DO UPDATE
       SET ts_from = EXCLUDED.ts_from, ts_to = EXCLUDED.ts_to,
           n_messages = EXCLUDED.n_messages, meta = EXCLUDED.meta,
           embedding = CASE WHEN chunks.text = EXCLUDED.text
                            THEN chunks.embedding END,
           emb_model = CASE WHEN chunks.text = EXCLUDED.text
                            THEN chunks.emb_model END,
           text = EXCLUDED.text"""


def clean_queries(queries) -> list[str]:
    """Без повторов (без учёта регистра) и без однословных ключей."""
    out, seen = [], set()
    for q in queries or []:
        if not isinstance(q, str):
            continue
        q = " ".join(q.split()).strip(" .,;«»\"")
        key = q.lower()
        if len(q.split()) < MIN_WORDS or key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out[:MAX_QUERIES]


def month_bounds(path: Path) -> tuple[dt.datetime, dt.datetime]:
    m = FILE_MONTH.search(path.name)
    if not m:
        sys.exit(f"{path.name}: имя не queries_YYYY-MM.jsonl — месяц для уборки не известен")
    y, mo = int(m.group(1)), int(m.group(2))
    start = dt.datetime(y, mo, 1, tzinfo=TZ)
    end = dt.datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=TZ)
    return start, end


def talks_of(path: Path) -> tuple[dict, dict]:
    """Разговоры файла по ключу (peer, from, to) и сводка по браку.

    Один и тот же разговор может прийти дважды (кусок пересчитан и дописан),
    тогда берётся последний: файл только дописывается."""
    talks, st = {}, {"records": 0, "talks": 0, "dropped_queries": 0, "empty": 0}
    for line in path.open(encoding="utf-8"):
        rec = json.loads(line)
        st["records"] += 1
        for t in rec.get("talks") or []:
            st["talks"] += 1
            qs = clean_queries(t.get("queries"))
            st["dropped_queries"] += len(t.get("queries") or []) - len(qs)
            if not qs:
                st["empty"] += 1
                continue
            talks[(rec["peer"], t["from"], t["to"])] = dict(queries=qs, n=t.get("n"))
    return talks, st


def process(conn, path: Path, write: bool, sample: int) -> dict:
    start, end = month_bounds(path)
    talks, st = talks_of(path)
    st.update(chunks=0, no_message=0, stale=0)
    peers = sorted({p for p, _, _ in talks})
    names = dict((p, (n, k)) for p, n, k in conn.execute(
        "SELECT peer_id, name, kind FROM dialogs WHERE peer_id = ANY(%s)",
        (peers,)).fetchall())
    ts = {(p, mid): t for p, mid, t in conn.execute(
        """SELECT peer_id, vk_msg_id, sent_at FROM messages
            WHERE peer_id = ANY(%s) AND sent_at >= %s AND sent_at < %s""",
        (peers, start, end)).fetchall()}

    batch, keys = [], []
    for (peer, a, b), t in sorted(talks.items()):
        ta, tb = ts.get((peer, a)), ts.get((peer, b))
        if peer not in names or ta is None or tb is None:
            # Номер сообщения модель берёт из текста куска, а clean() в
            # пилоте пропускает только номера куска, так что промах здесь —
            # это сообщение, пропавшее из базы после прогона модели.
            st["no_message"] += 1
            continue
        name, kind = names[peer]
        body = (f"{header_for(name, kind, ta.astimezone(TZ))}\n"
                "Поисковый указатель разговора:\n"
                + "\n".join(f"— {q}" for q in t["queries"]))
        st["chunks"] += 1
        if sample and st["chunks"] <= sample:
            print(f"\n--- {peer} #{a}–#{b}, {t['n']} реплик ---\n{body}")
        meta = {"queries": t["queries"], "index_file": path.name,
                "month": start.strftime("%Y-%m")}
        keys.append((peer, a, b))
        batch.append((peer, a, b, ta, tb, t["n"], body, psycopg.types.json.Jsonb(meta)))

    if not write:
        return st
    if talks and not batch:
        # В файле есть разговоры, а в базе не нашлось ни одного сообщения:
        # это не «всё устарело», а не та база или не тот месяц. Уборка снесла
        # бы весь месяц указателя вместе с векторами.
        conn.rollback()
        raise RuntimeError(f"{path.name}: {len(talks)} разговоров, ни одного в базе — уборка отменена")
    conn.cursor().executemany(SQL_INSERT, batch)
    st["stale"] = conn.execute(
        """DELETE FROM chunks c
            WHERE c.source = 'queries' AND c.ts_from >= %s AND c.ts_from < %s
              AND NOT EXISTS (
                SELECT 1 FROM unnest(%s::bigint[], %s::bigint[], %s::bigint[])
                              AS k(peer, a, b)
                 WHERE k.peer = c.peer_id AND k.a = c.msg_from AND k.b = c.msg_to)""",
        (start, end, [k[0] for k in keys], [k[1] for k in keys],
         [k[2] for k in keys])).rowcount
    conn.commit()
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path, help="queries_YYYY-MM.jsonl")
    ap.add_argument("--write", action="store_true", help="писать в БД")
    ap.add_argument("--sample", type=int, default=0, help="показать N первых чанков файла")
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        ensure_schema(conn)
        conn.commit()
        for path in args.files:
            st = process(conn, path, args.write, args.sample)
            print(f"{path.name}: кусков {st['records']}, разговоров {st['talks']}, "
                  f"чанков {st['chunks']}, без запросов {st['empty']}, "
                  f"снято запросов {st['dropped_queries']}, "
                  f"нет сообщения в базе {st['no_message']}"
                  + (f", снесено устаревших {st['stale']}" if args.write else ""))
    if not args.write:
        print("режим прикидки — в БД ничего не записано (нужен --write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
