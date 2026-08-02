#!/usr/bin/env python3
"""Замер качества поиска на контрольных запросах.

Для каждого запроса заранее известно, какой чанк правильный (диалог + дата).
Скрипт гоняет все три режима и смотрит, на какой позиции этот чанк оказался.
Итог — MRR (среднее от 1/позиция) и доля попаданий в первую пятёрку.

  python3 scripts/eval_search.py
  python3 scripts/eval_search.py --top 20 --verbose
  python3 scripts/eval_search.py --rerank      # + пересортировка кросс-энкодером

Случаи — в data/processed/embeddings/eval_cases.json (личные данные).
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from search import RERANK_POOL, DSN, run_search           # noqa: E402

CASES_DIR = (Path(__file__).resolve().parent.parent / "data" / "processed"
             / "embeddings")
CASES = CASES_DIR / "eval_cases.json"
# Отдельный набор: ответ лежит только на фотографии. Держать его отдельно
# обязательно — иначе основной замер перестанет быть сравнимым с прежними,
# а он и нужен как раз для сравнения. Каждая цель выбрана так, чтобы за
# сутки вокруг неё в диалоге не было других фотографий: допуск в сутки
# засчитал бы соседнюю, и случай стал бы бессмысленно лёгким.
PHOTO_CASES = CASES_DIR / "eval_cases_photos.json"
# (как показывать, режим поиска, пересортировывать ли)
MODES = [("fts", "fts", False), ("vector", "vector", False),
         ("hybrid", "hybrid", False)]
# Проверяем пересортировку и поверх гибрида, и поверх чистого вектора: если
# кросс-энкодер сам разбирается в релевантности, полнотекстовая половина
# слияния может оказаться лишней.
RERANK_MODES = [("vector+rr", "vector", True), ("hybrid+rr", "hybrid", True)]
# Чанк режется по разговорам, так что «тот самый» момент может оказаться
# в соседнем окне того же дня или следующего. Допуск в сутки.
DAY_TOLERANCE = 1


def rank_of(rows, peer: int, day: dt.date) -> tuple[int | None, str | None]:
    """Позиция ожидаемого чанка и его порода.

    Породу возвращаем не для красоты: допуск в сутки означает, что засчитаться
    может любой чанк того же диалога за тот же день — на разговорном наборе
    фотография, случайно присланная в тот же вечер, на фото-наборе наоборот
    реплика. Вызывающий код сверяет породу с ожидаемой и предупреждает, иначе
    подмена молча прибавилась бы к MRR.
    """
    for i, row in enumerate(rows, 1):
        got_peer, _, ts_from = row[0], row[1], row[2]
        if got_peer != peer or ts_from is None:
            continue
        if abs((ts_from.date() - day).days) <= DAY_TOLERANCE:
            return i, (row[9] if len(row) > 9 else "messages")
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--verbose", action="store_true",
                    help="показывать первый результат каждого режима")
    ap.add_argument("--rerank", action="store_true",
                    help="добавить режимы с пересортировкой кросс-энкодером")
    ap.add_argument("--rerank-pool", type=int, default=RERANK_POOL,
                    dest="rerank_pool")
    # Нужен, чтобы отделить вытеснение от поломки: `--source messages`
    # воспроизводит корпус до появления фото-чанков и должен давать
    # ровно прежние числа.
    ap.add_argument("--source", action="append", choices=["messages", "photo"])
    ap.add_argument("--photo-cases", action="store_true", dest="photo_cases",
                    help="мерить на наборе, где ответ только на фотографии")
    args = ap.parse_args()

    path = PHOTO_CASES if args.photo_cases else CASES
    if not path.exists():
        sys.exit(f"нет файла со случаями: {path}")
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not cases:
        sys.exit("набор контрольных запросов пуст — мерить нечего")
    variants = MODES + (RERANK_MODES if args.rerank else [])
    ranks: dict[str, list] = {label: [] for label, _, _ in variants}
    spent: dict[str, float] = {label: 0.0 for label, _, _ in variants}
    # Сбой пайплайна и честный «не нашли» — разные вещи. Если их сложить в одну
    # колонку промахов, systematic-падение режима прочитается как «модель тут
    # просто хуже», а не как «поиск сломан».
    broke: dict[str, int] = {label: 0 for label, _, _ in variants}
    off_source: dict[str, int] = {label: 0 for label, _, _ in variants}

    with psycopg.connect(DSN) as conn:
        for case in cases:
            day = dt.date.fromisoformat(case["date"])
            print(f'\n«{case["query"]}»')
            print(f'  ожидаем: {case["peer"]} / {case["date"]} — {case["note"]}')
            for label, mode, rr in variants:
                opts = SimpleNamespace(
                    query=case["query"], mode=mode, top=args.top,
                    peer=None, date_from=None, date_to=None,
                    source=args.source,
                    rerank=rr, rerank_pool=args.rerank_pool)
                t0 = time.time()
                try:
                    rows = run_search(conn, opts)
                except Exception as e:               # noqa: BLE001
                    # Прогон долгий; терять из-за одного сбоя всю сводку MRR
                    # обиднее, чем недосчитать один случай. Промах помечаем
                    # явно, чтобы он не сошёл за честный «не найден».
                    print(f"    {label:<10} СБОЙ: {e}")
                    ranks[label].append(None)
                    broke[label] += 1
                    try:
                        conn.rollback()
                    except Exception:            # noqa: BLE001
                        # Если сбой был обрывом соединения, rollback на мёртвом
                        # соединении бросит снова — уже вне try, и весь замер
                        # рухнет ровно там, где мы старались его сберечь.
                        pass
                    continue
                spent[label] += time.time() - t0
                r, src = rank_of(rows, case["peer"], day)
                ranks[label].append(r)
                # Считаем не «сколько фото», а «сколько НЕ той породы, какую
                # мерим». На разговорном наборе чужая порода — фотография, на
                # фото-наборе наоборот. Без этого различия предупреждение
                # срабатывало на фото-наборе всегда и объявляло верный замер
                # несравнимым, а обратный перекос не ловился вовсе.
                want = "photo" if args.photo_cases else "messages"
                if r and src != want:
                    off_source[label] += 1
                mark = (f"#{r}" + (f" ({src}!)" if src != want else "")) \
                    if r else f"не найден в топ-{args.top}"
                print(f"    {label:<10} {mark}")
                if args.verbose and rows:
                    head = rows[0][5].split("\n", 1)[-1][:110]
                    print(f"            1-й: {rows[0][1]} · {head}…")

    print("\n" + "=" * 66)
    print(f'{"режим":<10} {"MRR":>6} {"в топ-1":>8} {"в топ-5":>8} '
          f'{"промахов":>9} {"сбоев":>6} {"с/запрос":>9}')
    for label, _, _ in variants:
        rs = ranks[label]
        mrr = sum(1 / r for r in rs if r) / len(rs)
        misses = sum(1 for r in rs if not r) - broke[label]
        print(f"{label:<10} {mrr:>6.3f} "
              f"{sum(1 for r in rs if r == 1):>8} "
              f"{sum(1 for r in rs if r and r <= 5):>8} "
              f"{misses:>9} {broke[label]:>6} "
              f"{spent[label] / len(cases):>9.2f}")
    if any(broke.values()):
        print("ВНИМАНИЕ: были сбои — MRR посчитан по неполным данным")
    if any(off_source.values()):
        other = "перепиской" if args.photo_cases else "фотографией"
        print(f"ВНИМАНИЕ: часть случаев засчитана {other} того же дня — "
              f"{dict((k, v) for k, v in off_source.items() if v)}. "
              "Допуск в сутки засчитывает любой чанк диалога за тот же день, "
              "так что этот MRR измеряет не то, что задумано")
    print(f"случаев: {len(cases)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
