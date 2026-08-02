#!/usr/bin/env python3
"""Поиск по архиву: полнотекстовый, векторный и гибридный.

Гибрид склеивает два списка по RRF (reciprocal rank fusion): каждый чанк
получает 1/(k+позиция) от каждого способа, баллы складываются. Это работает
без калибровки шкал — у ts_rank и косинуса они несопоставимы, а ранги
сравнивать можно.

  python3 scripts/search.py "переезд в другой город"
  python3 scripts/search.py "поиск работы" --from 2014 --to 2016
  python3 scripts/search.py "билеты" --mode fts     # сравнить режимы
  python3 scripts/search.py "билеты" --peer 123456789 --top 5 --full
  python3 scripts/search.py "расписание" --source photo   # только по картинкам

Ищутся чанки двух пород сразу: окна переписки и текст, распознанный на
фотографиях (см. build_photo_chunks.py). В выдаче фото помечены «(фото)».
"""
import argparse
import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DSN, OLLAMA_EMBED as OLLAMA       # noqa: E402

MODEL = "bge-m3"
DIM = 1024                        # столько ждёт схема: chunks.embedding vector(1024)
# Смягчает вклад верхних позиций: без k первый результат перевешивал бы
# всё остальное. 60 — общепринятое значение из работы про RRF.
RRF_K = 60
# Сколько кандидатов уходит кросс-энкодеру. Смысл пересортировки в том, чтобы
# достать правильный ответ с глубины: на замере промахи стояли на рангах
# 25, 31, 47 и 61, так что мельче сотни брать нечего.
RERANK_POOL = 100


def embed_query(text: str) -> str:
    """Вектор запроса. Ответ проверяем, а не принимаем на веру: битый или
    нулевой эмбеддинг не сломает запрос — он останется синтаксически верным
    литералом нужной длины, поиск отработает и вернёт правдоподобную на вид
    выдачу, в которой ранжирование случайно. Заметить это по результату
    невозможно, поэтому падаем сразу."""
    payload = json.dumps({"model": MODEL, "input": [text]}).encode()
    req = urllib.request.Request(
        OLLAMA, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        vectors = json.load(r).get("embeddings") or []
    if len(vectors) != 1:
        raise RuntimeError(f"Ollama вернула {len(vectors)} векторов вместо одного")
    vec = vectors[0]
    if len(vec) != DIM:
        raise RuntimeError(f"размерность {len(vec)}, а схема ждёт {DIM}")
    if not any(vec):
        raise RuntimeError("Ollama вернула нулевой вектор — модель не в порядке")
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def build_filters(args) -> tuple[str, list]:
    where, params = ["TRUE"], []
    if args.peer:
        where.append("c.peer_id = ANY(%s)")
        params.append([int(p) for p in args.peer])
    # Чанки бывают двух пород: окна переписки и текст с фотографий. По
    # умолчанию ищем везде — фотография с распечаткой расписания отвечает на
    # вопрос ровно так же, как реплика. Флаг нужен для разбора: посмотреть,
    # что вообще нашлось на картинках, или наоборот убрать их из выдачи.
    if getattr(args, "source", None):
        where.append("c.source = ANY(%s)")
        params.append(list(args.source))
    for bound, op in ((args.date_from, ">="), (args.date_to, "<")):
        if not bound:
            continue
        parts = [int(x) for x in bound.split("-")]
        d = dt.date(parts[0], parts[1] if len(parts) > 1 else 1,
                    parts[2] if len(parts) > 2 else 1)
        # Верхняя граница всегда включающая: сдвигаем на начало следующего
        # периода той же гранулярности. Для полной даты этого сдвига не было,
        # и `--to 2016-05-10` молча теряло весь день 10 мая — а `--from` при
        # той же записи день включает. Запрос за один день давал пустоту.
        if op == "<":
            if len(parts) == 1:                    # --to 2016 → включая 2016 год
                d = dt.date(d.year + 1, 1, 1)
            elif len(parts) == 2:                  # --to 2016-05 → включая май
                d = dt.date(d.year + d.month // 12, d.month % 12 + 1, 1)
            else:                                  # --to 2016-05-10 → включая 10-е
                d = d + dt.timedelta(days=1)
        where.append(f"c.ts_from {op} %s")
        params.append(d)
    return " AND ".join(where), params


def search(conn, args) -> list:
    flt, flt_params = build_filters(args)
    pool = max(args.top * 5, 50)      # глубина каждого списка до слияния

    # ВАЖНО: на таком масштабе основной путь — точный перебор, а не HNSW.
    # Замер на корпусе порядка сотни тысяч чанков: перебор 378 мс, индекс при
    # ef_search=1000 — 440 мс при том же recall. Индекс с настройками
    # послабее быстрее, но начинает терять соседей: при ef_search=200
    # трудный запрос давал 6 попаданий из 10, при 500 — те же 6. Соседи в
    # плотных областях стоят вплотную (0.4973 против 0.5026), и обход графа
    # их проскакивает. Поэтому ORDER BY идёт по алиасу dist — так планировщик
    # выбирает перебор. Индекс оставлен на вырост: если корпус вырастет в
    # разы, перебор станет дороже и вернуться к нему можно одной правкой
    # (повторить выражение c.embedding <=> %s в ORDER BY вместо алиаса).
    conn.execute("SET LOCAL hnsw.ef_search = 1000")   # потолок параметра

    # Ранг считается ПОВЕРХ уже отобранного подзапросом, а не поверх индексного
    # скана. Если поставить row_number() прямо над сканом, план становится
    # Limit → WindowAgg → Index Scan: оконная функция тянет из индекса поток
    # целиком, а HNSW отдаёт по расстоянию только первые ef_search кандидатов
    # и дальше порядок теряет. Ранги проставлялись по испорченному потоку, и
    # чанк, честно стоящий вторым по косинусу, не попадал даже в топ-20.
    vec_cte = f"""
        vec AS (
          SELECT id, row_number() OVER (ORDER BY dist) AS rank
            FROM (SELECT c.id, c.embedding <=> %s::vector AS dist
                    FROM chunks c
                   WHERE c.embedding IS NOT NULL AND {flt}
                   ORDER BY dist
                   LIMIT {pool}) t
        )"""
    fts_cte = f"""
        fts AS (
          SELECT c.id,
                 row_number() OVER (
                   ORDER BY ts_rank_cd(c.tsv, q.query) DESC) AS rank
            FROM chunks c, websearch_to_tsquery('russian', %s) q(query)
           WHERE c.tsv @@ q.query AND {flt}
           ORDER BY ts_rank_cd(c.tsv, q.query) DESC
           LIMIT {pool}
        )"""

    params: list = []
    if args.mode in ("vector", "hybrid"):
        emb = embed_query(args.query)
        # Порядок здесь — это порядок плейсхолдеров в тексте запроса:
        # сначала вектор, потом фильтры. Перепутать легко, а падает оно
        # невнятным «malformed array literal» — вектор уезжает в peer_id.
        vec_params = [emb, *flt_params]
    if args.mode == "vector":
        ctes, join = vec_cte, "vec v"
        score = f"1.0 / ({RRF_K} + v.rank)"
        cols = "v.rank AS vrank, NULL::bigint AS frank"
        params += vec_params
    elif args.mode == "fts":
        ctes, join = fts_cte, "fts f"
        score = f"1.0 / ({RRF_K} + f.rank)"
        cols = "NULL::bigint AS vrank, f.rank AS frank"
        params += [args.query, *flt_params]
    else:
        ctes = vec_cte + "," + fts_cte
        join = "vec v FULL OUTER JOIN fts f USING (id)"
        score = (f"COALESCE(1.0 / ({RRF_K} + v.rank), 0) + "
                 f"COALESCE(1.0 / ({RRF_K} + f.rank), 0)")
        cols = "v.rank AS vrank, f.rank AS frank"
        params += [*vec_params, args.query, *flt_params]

    ident = "COALESCE(v.id, f.id)" if args.mode == "hybrid" else (
        "v.id" if args.mode == "vector" else "f.id")
    sql = f"""
        WITH {ctes}
        SELECT c.peer_id, d.name, c.ts_from, c.ts_to, c.n_messages,
               c.text, {score} AS score, {cols}, c.source
          FROM {join}
          JOIN chunks c ON c.id = {ident}
          JOIN dialogs d USING (peer_id)
         ORDER BY score DESC
         LIMIT %s"""
    params.append(args.top)
    return conn.execute(sql, params).fetchall()


def run_search(conn, args, rerank: bool | None = None) -> list:
    """Поиск, при `--rerank` — с пересортировкой кросс-энкодером.

    Кросс-энкодер не ищет, а только переупорядочивает: сначала обычный поиск
    отдаёт сотню кандидатов, потом модель читает каждую пару «запрос + чанк» и
    расставляет их заново. Балл в выдаче при этом меняет природу — вместо суммы
    RRF там сырой логит кросс-энкодера, сравнимый только внутри запроса.

    Политика: **человеку — по флагу, автоматике — всегда**. Пересортировка даёт
    MRR 0.592 против 0.451, но стоит 4 секунды вместо половины; живой человек
    эту разницу чувствует, а конвейеру (второй ярус, дайджесты) её ждать не
    жалко. Поэтому вызывающему коду доступен явный `rerank=True` — не нужно
    подделывать поле в args, чтобы включить пересортировку.

    Пересортировку применять целиком, без страховки: слияние ранга
    кросс-энкодера с исходным рангом поиска по RRF проверено при k = 60, 10 и 3
    и оказалось хуже (0.513–0.529 против 0.592), порог по уверенности модели
    дал +0.002. Подробности — docs/db-schema.md."""
    if rerank is None:
        rerank = getattr(args, "rerank", False)
    if not rerank:
        return search(conn, args)

    pool = max(getattr(args, "rerank_pool", None) or RERANK_POOL, args.top)
    rows = search(conn, SimpleNamespace(**{**vars(args), "top": pool}))
    if not rows:
        return rows

    from rerank import scores                        # noqa: PLC0415
    sc = scores(args.query, [r[5] for r in rows])
    order = sorted(range(len(rows)), key=lambda i: sc[i], reverse=True)
    return [(*rows[i][:6], sc[i], *rows[i][7:]) for i in order][:args.top]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--mode", choices=["hybrid", "vector", "fts"],
                    default="hybrid")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--peer", action="append")
    ap.add_argument("--from", dest="date_from", help="YYYY / YYYY-MM / YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--full", action="store_true", help="целиком, а не отрывком")
    ap.add_argument("--source", action="append", choices=["messages", "photo"],
                    help="искать только в переписке или только по фото")
    ap.add_argument("--rerank", action="store_true",
                    help="пересортировать выдачу кросс-энкодером (медленнее)")
    ap.add_argument("--rerank-pool", type=int, default=RERANK_POOL,
                    dest="rerank_pool", help="сколько кандидатов пересортировать")
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        rows = run_search(conn, args)

    if not rows:
        print("ничего не найдено")
        return 1
    mode = args.mode + (f" + реранк топ-{args.rerank_pool}" if args.rerank else "")
    print(f'«{args.query}» · режим {mode} · {len(rows)} результатов\n')
    for i, (_peer, name, ts_from, _ts_to, n_msgs, text, score,
            vrank, frank, source) in enumerate(rows, 1):
        when = ts_from.strftime("%d.%m.%Y %H:%M") if ts_from else "—"
        ranks = (f"vec #{vrank}" if vrank else "vec —") + \
                ("  " + (f"fts #{frank}" if frank else "fts —"))
        what = "фото" if source == "photo" else f"{n_msgs} реплик"
        print(f"[{i}] {score:.4f}  {ranks}  {when}  {name}  ({what})")
        body = text if args.full else text[:400] + ("…" if len(text) > 400 else "")
        print("    " + body.replace("\n", "\n    ") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
