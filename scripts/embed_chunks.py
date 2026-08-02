#!/usr/bin/env python3
"""Эмбеддинги чанков через локальную Ollama → pgvector.

Resume-safe по построению: работа берётся запросом `embedding IS NULL`,
так что прерванный прогон продолжается с того же места, а повторный запуск
на посчитанном диалоге ничего не делает.

  python3 scripts/embed_chunks.py --peer 123456789 --limit 500   # пилот
  python3 scripts/embed_chunks.py --all                          # весь корпус
  python3 scripts/embed_chunks.py --stats                        # что посчитано

Пауза — `touch data/processed/embeddings/PAUSE`, мягкая остановка — `STOP`.
"""
import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DSN, OLLAMA_EMBED as OLLAMA       # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "processed" / "embeddings"

MODEL = "bge-m3"
DIM = 1024                       # столько ждёт схема: chunks.embedding vector(1024)
BATCH = 16
REQUEST_TIMEOUT = 300
RETRIES = 3

# Ниже этой доли свободной памяти встаём на паузу: рядом крутятся Postgres,
# докер и whisper-сервис, и выдавливать их в своп ради индексации не стоит.
MEM_FREE_MIN_PCT = 15
MEM_CHECK_EVERY = 20             # батчей


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "embed.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


_mem_warned = False


def free_mem_pct() -> float | None:
    """Свободная память в процентах, как её видит macOS.

    При сбое возвращает None, и вызывающий код трактует это как «всё в
    порядке» — то есть защита от нехватки памяти молча выключается на весь
    прогон. Поэтому о первом сбое сообщаем в лог: иначе на прогоне в сто
    тысяч чанков сторож окажется мёртвым, и об этом никто не узнает.
    """
    global _mem_warned
    import subprocess                                # noqa: PLC0415
    try:
        out = subprocess.run(["memory_pressure", "-Q"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as e:
        if not _mem_warned:
            log(f"  memory_pressure недоступен ({e}) — контроль памяти отключён")
            _mem_warned = True
        return None
    for line in out.splitlines():
        if "free percentage" in line:
            return float(line.rsplit(":", 1)[1].strip().rstrip("%"))
    return None


def embed(texts: list[str]) -> list[list[float]]:
    """Батч в Ollama. Сеть локальная, но сервис может быть занят выгрузкой
    модели — поэтому ретраи с паузой, а не падение на первой ошибке."""
    payload = json.dumps({"model": MODEL, "input": texts}).encode()
    last = None
    for attempt in range(1, RETRIES + 1):
        req = urllib.request.Request(
            OLLAMA, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                data = json.load(r)
            vectors = data.get("embeddings") or []
            if len(vectors) != len(texts):
                raise ValueError(f"вернулось {len(vectors)} векторов "
                                 f"на {len(texts)} текстов")
            # Проверяем каждый вектор, а не только первый: битую размерность
            # в середине батча иначе поймает уже Postgres на UPDATE, и весь
            # посчитанный батч придётся считать заново.
            bad = next((i for i, v in enumerate(vectors) if len(v) != DIM), None)
            if bad is not None:
                raise ValueError(f"вектор {bad} размерности {len(vectors[bad])}, "
                                 f"а схема ждёт {DIM}")
            if any(not any(v) for v in vectors):
                raise ValueError("в батче есть нулевой вектор")
            return vectors
        except (urllib.error.URLError, ValueError, OSError, json.JSONDecodeError) as e:
            last = e
            log(f"  попытка {attempt}/{RETRIES} не удалась: {e}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"Ollama не ответила: {last}")


def as_vector(values: list[float]) -> str:
    """pgvector принимает литерал вида '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


def pending_count(conn, peers: list[int] | None) -> int:
    sql = "SELECT count(*) FROM chunks WHERE embedding IS NULL"
    params: tuple = ()
    if peers:
        sql += " AND peer_id = ANY(%s)"
        params = (peers,)
    return conn.execute(sql, params).fetchone()[0]


def show_stats(conn) -> None:
    rows = conn.execute("""
        SELECT d.name, c.emb_model,
               count(*) FILTER (WHERE c.embedding IS NOT NULL) AS done,
               count(*) AS total
          FROM chunks c JOIN dialogs d USING (peer_id)
         GROUP BY d.name, c.emb_model ORDER BY total DESC""").fetchall()
    print(f'{"диалог":<32} {"модель":<10} {"готово":>8} {"всего":>8}')
    for name, model, done, total in rows:
        print(f"{(name or '')[:32]:<32} {(model or '—'):<10} {done:>8} {total:>8}")


def run(conn, peers: list[int] | None, limit: int, batch: int) -> int:
    total_pending = pending_count(conn, peers)
    if limit:
        total_pending = min(total_pending, limit)
    if not total_pending:
        log("считать нечего — все чанки уже с эмбеддингами")
        return 0

    log(f"к обработке {total_pending} чанков, модель {MODEL}, батч {batch}")
    started = time.time()
    done = 0
    n_batches = 0
    sql = ("SELECT id, text FROM chunks WHERE embedding IS NULL"
           + (" AND peer_id = ANY(%s)" if peers else "")
           + " ORDER BY peer_id, ts_from LIMIT %s")

    while done < total_pending:
        if (OUT / "STOP").exists():
            log("найден STOP — останавливаюсь")
            return 3
        while (OUT / "PAUSE").exists():
            log("пауза (файл PAUSE); снять — удалить файл")
            time.sleep(30)

        take = min(batch, total_pending - done)
        params = ((peers, take) if peers else (take,))
        rows = conn.execute(sql, params).fetchall()
        # Закрываем транзакцию до сетевого вызова: иначе соединение висит
        # idle in transaction всё время ответа Ollama (а при ретраях это
        # минуты), autovacuum не может убрать мёртвые версии строк, и на
        # прогоне в сто тысяч UPDATE'ов таблица заметно распухает.
        conn.rollback()
        if not rows:
            break

        t0 = time.time()
        vectors = embed([text for _, text in rows])
        conn.cursor().executemany(
            "UPDATE chunks SET embedding = %s::vector, emb_model = %s "
            "WHERE id = %s",
            [(as_vector(v), MODEL, cid)
             for (cid, _), v in zip(rows, vectors, strict=True)])
        conn.commit()

        done += len(rows)
        n_batches += 1
        if n_batches % 10 == 0 or done >= total_pending:
            rate = done / max(time.time() - started, 0.001)
            eta = (total_pending - done) / max(rate, 0.001)
            log(f"  {done}/{total_pending}  {rate:.1f} чанк/с  "
                f"батч {time.time() - t0:.1f} с  осталось ~{eta / 60:.1f} мин")
        if n_batches % MEM_CHECK_EVERY == 0:
            free = free_mem_pct()
            if free is not None and free < MEM_FREE_MIN_PCT:
                log(f"  памяти свободно {free:.0f}% — жду 60 с")
                time.sleep(60)

    took = time.time() - started
    log(f"готово: {done} чанков за {took / 60:.1f} мин "
        f"({done / max(took, 0.001):.1f} чанк/с)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", action="append", help="peer_id (можно несколько)")
    ap.add_argument("--all", action="store_true", help="все чанки в базе")
    ap.add_argument("--limit", type=int, default=0, help="максимум чанков за прогон")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--stats", action="store_true", help="что уже посчитано")
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        if args.stats:
            show_stats(conn)
            return 0
        if not args.peer and not args.all:
            sys.exit("нужен --peer, --all или --stats")
        peers = [int(p) for p in args.peer] if args.peer else None
        return run(conn, peers, args.limit, args.batch)


if __name__ == "__main__":
    sys.exit(main())
