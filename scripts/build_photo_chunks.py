#!/usr/bin/env python3
"""Чанки из текста, распознанного на фотографиях.

Анализ фото (scripts/analyze_photos.py) сложил в `photo_analysis` пять с
лишним миллионов символов — скриншоты переписок, объявления, документы,
расписания. В поиске их не было: `search.py` смотрит только в `chunks`.
Здесь осмысленные фотографии превращаются в такие же чанки, как окна
переписки, — с тем же заголовком, той же датой и тем же tsvector. После
этого они попадают и в полнотекстовый поиск, и в векторный (нужен прогон
embed_chunks.py), и во второй ярус, который строится поверх чанков.

  python3 scripts/build_photo_chunks.py --all --sample 5    # посмотреть
  python3 scripts/build_photo_chunks.py --all --write
  python3 scripts/build_photo_chunks.py --peer 123456789 --write

Осмысленность — три порога, откалиброванные по выборке из корпуса
(см. docs/db-schema.md): уверенность OCR, длина и число настоящих слов.
Повторный прогон resume-safe: вставка ON CONFLICT DO NOTHING по тому же
ключу, посчитанные эмбеддинги не теряются.
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_chunks import ensure_schema, header_for            # noqa: E402
from common import DSN, OWN_NAME                              # noqa: E402

# Ниже этой уверенности OCR выдаёт кашу, а не текст: сверка выборки глазами
# показала, что на 0.3–0.55 живут книжная страница под углом, вывеска в
# перспективе и логотипы — распознаётся набор обрывков слов вперемешку с
# латиницей. С 0.6 начинается читаемое. Отбрасывается при этом 4%
# фотографий с текстом. (Примеров здесь нет намеренно: это содержимое
# личного архива, ему место только в data/.)
MIN_CONF = 0.6
# Короче сорока символов — подпись на меме, ник, дата на скриншоте: искать
# там нечего, а в индекс такое добавляет шум.
MIN_CHARS = 40
# Порогов по уверенности и длине мало: набор артикулов, номеров и обрывков
# латиницы бывает и длинным, и уверенным — слов в нём нет, а символов
# полторы сотни. Требуем хотя бы три настоящих слова.
MIN_WORDS = 3
WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)

# Метки сцены от классификатора macOS Vision приходят по-английски, а
# спрашивать будут по-русски («скриншот переписки», «документ»). Перевод
# нужен ровно ради полнотекстового поиска: вектор bge-m3 многоязычный и
# понял бы и оригинал. Незнакомую метку оставляем как есть — лучше
# английское слово в чанке, чем потерянный признак.
LABEL_RU = {
    "document": "документ", "screenshot": "скриншот", "people": "люди",
    "adult": "человек", "art": "рисунок", "illustrations": "иллюстрация",
    "structure": "здание", "animal": "животное", "outdoor": "улица",
    "mammal": "животное", "printed_page": "печатная страница", "map": "карта",
    "feline": "кошка", "handwriting": "рукописный текст", "cat": "кошка",
    "clothing": "одежда", "adult_cat": "кошка", "land": "пейзаж",
    "sky": "небо", "grass": "трава", "suit": "костюм", "machine": "техника",
    "cloudy": "облака", "blue_sky": "небо", "food": "еда", "dog": "собака",
    "car": "машина", "vehicle": "транспорт", "plant": "растение",
    "tree": "дерево", "water": "вода", "indoor": "помещение",
    "furniture": "мебель", "book": "книга", "text": "текст",
    "sign": "вывеска", "poster": "плакат", "toy": "игрушка",
    "electronics": "техника", "flower": "цветы", "snow": "снег",
    "night": "ночь", "face": "лицо", "child": "ребёнок", "drawing": "рисунок",
    "wood_processed": "дерево", "container": "ёмкость", "chart": "график",
    "jeans": "джинсы", "bottle": "бутылка", "tableware": "посуда",
    "utensil": "посуда", "eyeglasses": "очки", "optical_equipment": "очки",
    "liquid": "жидкость", "consumer_electronics": "техника",
    "diagram": "схема", "conveyance": "транспорт", "interior_room": "комната",
    "textile": "ткань", "computer": "компьютер", "jacket": "куртка",
    "window": "окно", "tool": "инструмент", "decoration": "украшение",
    "foliage": "листва", "plate": "тарелка", "canine": "собака",
    "crowd": "толпа", "fence": "забор", "fruit": "фрукты", "bird": "птица",
    "automobile": "машина", "celebration": "праздник",
}
# Ниже этой уверенности метка сцены — догадка классификатора, в текст чанка
# такое не пускаем. Больше четырёх меток тоже не берём: дальше идут синонимы
# («mammal», «feline», «cat», «adult_cat» на одной кошке).
MIN_LABEL_CONF = 0.7
MAX_LABELS = 4

# Порядковый номер фотографии внутри сообщения. Считается по ВСЕМ
# проанализированным фото сообщения, а не по прошедшим отбор: иначе смена
# порога сдвинула бы номера, и те же фотографии легли бы в базу второй раз
# под новыми ключами. attachment_id раздаётся при импорте и больше не
# меняется, так что нумерация стабильна между прогонами. Отбор диалога стоит
# ВНУТРИ окна: на нумерацию это не влияет (сообщение принадлежит ровно одному
# диалогу), а снаружи планировщик не может протолкнуть условие в окно и на
# каждый диалог перечитывал всю таблицу целиком.
SQL_PHOTOS = """
    WITH ranked AS (
      SELECT p.attachment_id, p.message_id, p.status, p.ocr_text, p.ocr_conf,
             p.labels, p.n_faces, p.url,
             row_number() OVER (PARTITION BY p.message_id
                                    ORDER BY p.attachment_id) AS ord
        FROM photo_analysis p
        JOIN messages m2 ON m2.id = p.message_id
       WHERE m2.peer_id = %s
    )
    SELECT p.attachment_id, p.ord, p.ocr_text, p.ocr_conf, p.labels,
           p.n_faces, p.url, m.vk_msg_id, m.sent_at, m.is_own, m.sender
      FROM ranked p
      JOIN messages m ON m.id = p.message_id
     WHERE p.status = 'ok'
       AND p.ocr_conf >= %s
       AND length(p.ocr_text) >= %s
     ORDER BY m.sent_at, m.vk_msg_id, p.ord"""

SQL_INSERT = """
    INSERT INTO chunks (peer_id, msg_from, msg_to, part, ts_from, ts_to,
                        n_messages, text, meta, source)
    VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, 'photo')
    ON CONFLICT (peer_id, msg_from, msg_to, part, source) DO NOTHING"""


def label_names(labels, bad: list | None = None) -> list[str]:
    """Метки сцены по-русски, самые уверенные первыми.

    Испорченную метку пропускаем, но считаем: массовая порча поля `labels`
    (сбой в analyze_photos.py, смена формата) иначе просто выродила бы все
    чанки в «фотография» без единого тега, и заметить это было бы нечем —
    пропуск ничем не отличается от честного отсутствия меток.
    """
    picked = []
    for item in labels or []:
        if not isinstance(item, dict):
            if bad is not None:
                bad.append(1)
            continue
        try:
            conf = float(item.get("conf") or 0)
        except (TypeError, ValueError):
            if bad is not None:
                bad.append(1)
            continue
        name = (item.get("id") or "").strip()
        if name and conf >= MIN_LABEL_CONF:
            picked.append((conf, LABEL_RU.get(name, name.replace("_", " "))))
    picked.sort(key=lambda x: -x[0])
    out: list[str] = []
    for _, name in picked:
        if name not in out:                 # «feline» и «cat» дают одно слово
            out.append(name)
        if len(out) >= MAX_LABELS:
            break
    return out


def meaningful(text: str) -> bool:
    """Есть ли в распознанном хоть что-то, что можно искать."""
    return len(WORD.findall(text or "")) >= MIN_WORDS


def photo_chunk(rec, name: str, kind: str, bad_labels: list | None = None) -> dict | None:
    (att_id, ord_, ocr, conf, labels, n_faces, url,
     vk_msg_id, sent_at, is_own, sender) = rec
    ocr = (ocr or "").strip()
    if not meaningful(ocr):
        return None

    # Кто прислал. В личной переписке собеседник — это сам диалог: в экспорте
    # он подписан «Вы» или «DELETED», и подставлять надо опознанное имя.
    if is_own:
        who = OWN_NAME
    else:
        who = (sender or "").strip()
        if not who or who in {"DELETED", "Deleted", "deleted", "Вы"}:
            who = name if kind == "user" else "?"
    when = sent_at.strftime("%H:%M") if sent_at else "--:--"
    tags = label_names(labels, bad_labels)
    what = "фотография" + (", " + ", ".join(tags) if tags else "")
    # Длинный распознанный текст не режем и не обрезаем. Самый длинный в
    # корпусе — 6,8 тыс. символов, в лимит bge-m3 (8192 токена) это входит,
    # а деление на части пришлось бы кодировать в part, где уже лежит номер
    # фотографии в сообщении.
    body = (f"{header_for(name, kind, sent_at)}\n"
            f"{who} ({when}): [{what}]\n"
            f"Текст на фотографии: {ocr}")
    return {
        "msg_from": vk_msg_id, "msg_to": vk_msg_id, "part": int(ord_),
        "ts_from": sent_at, "ts_to": sent_at, "text": body,
        "meta": {
            "attachment_id": int(att_id),
            "senders": [who],
            "has_own": bool(is_own),
            "ocr_conf": round(float(conf), 3),
            "ocr_chars": len(ocr),
            "labels": tags,
            "n_faces": int(n_faces or 0),
            # Ссылка на момент анализа: файлы удаляются, а опознать фотографию
            # потом надо. Полная мета (sha256, размеры, заголовки) остаётся
            # в photo_analysis, здесь только то, что нужно глазами.
            "url": url,
            "month": sent_at.strftime("%Y-%m") if sent_at else None,
        },
    }


def process(conn, peer_id: int, name: str, kind: str,
            write: bool, rebuild: bool, args) -> dict:
    if write and rebuild:
        conn.execute("DELETE FROM chunks WHERE peer_id = %s AND source = 'photo'",
                     (peer_id,))

    stats = {"photos": 0, "chunks": 0, "chars": 0, "inserted": 0, "stale": 0,
             "skipped": 0, "bad_labels": 0}
    keys: list[tuple] = []
    batch: list[tuple] = []
    bad_labels: list = []
    rows = conn.execute(SQL_PHOTOS, (peer_id, args.min_conf, args.min_chars))
    wcur = conn.cursor()
    for rec in rows:
        stats["photos"] += 1
        chunk = photo_chunk(rec, name, kind, bad_labels)
        if not chunk:
            stats["skipped"] += 1
            continue
        stats["chunks"] += 1
        stats["chars"] += len(chunk["text"])
        if args.sample and stats["chunks"] <= args.sample:
            print(f"\n--- фото {chunk['meta']['attachment_id']}, "
                  f"conf {chunk['meta']['ocr_conf']}, "
                  f"{chunk['meta']['ocr_chars']} симв. ---")
            print(chunk["text"][:1200])
        if not write:
            continue
        keys.append((chunk["msg_from"], chunk["part"]))
        batch.append((peer_id, chunk["msg_from"], chunk["msg_to"],
                      chunk["part"], chunk["ts_from"], chunk["ts_to"],
                      chunk["text"], psycopg.types.json.Jsonb(chunk["meta"])))
        if len(batch) >= 500:
            wcur.executemany(SQL_INSERT, batch)
            stats["inserted"] += max(wcur.rowcount, 0)
            batch = []
    stats["bad_labels"] = len(bad_labels)
    if write and batch:
        wcur.executemany(SQL_INSERT, batch)
        stats["inserted"] += max(wcur.rowcount, 0)

    if write:
        # Уборка: фотография могла перестать проходить порог (порог подняли)
        # или исчезнуть из photo_analysis. Чужие чанки не трогаем — только
        # свои, source = 'photo'. Пустой список ключей — это не «нечего
        # делать», а «в диалоге не осталось ни одной подходящей фотографии»:
        # ровно тот случай, когда старые чанки и надо снести.
        #
        # Но пустой список бывает и по другой причине — от дефекта в самом
        # SQL_PHOTOS. Тогда ноль строк вернётся для ВСЕХ диалогов подряд, и
        # прогон по `--all` молча снесёт весь слой фотографий вместе с
        # эмбеддингами. Поэтому перед сносом задаём тот же вопрос независимой
        # формулировкой — без CTE и оконной функции, теми же порогами. Пороги
        # повторить обязательно: их поднимают руками, и тогда ноль строк —
        # честный ответ, а не поломка. Расходятся ответы только если сломан
        # сам SQL_PHOTOS.
        if not keys and stats["photos"] == 0:
            control = conn.execute(
                """SELECT count(*) FROM photo_analysis p
                     JOIN messages m ON m.id = p.message_id
                    WHERE m.peer_id = %s AND p.status = 'ok'
                      AND p.ocr_conf >= %s AND length(p.ocr_text) >= %s""",
                (peer_id, args.min_conf, args.min_chars)).fetchone()[0]
            if control:
                conn.rollback()
                raise RuntimeError(
                    f"диалог {peer_id}: выборка вернула ноль фотографий, а "
                    f"контрольный запрос — {control}. Похоже на дефект "
                    f"SQL_PHOTOS, а не на пустой диалог. Уборка отменена")
        stats["stale"] = conn.execute(
            """DELETE FROM chunks c
                WHERE c.peer_id = %s AND c.source = 'photo'
                  AND NOT EXISTS (
                    SELECT 1 FROM unnest(%s::bigint[], %s::int[]) AS k(msg, part)
                     WHERE k.msg = c.msg_from AND k.part = c.part)""",
            (peer_id, [k[0] for k in keys], [k[1] for k in keys])).rowcount
        conn.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", action="append", help="peer_id (можно несколько)")
    ap.add_argument("--all", action="store_true",
                    help="все диалоги, где есть проанализированные фото")
    ap.add_argument("--write", action="store_true", help="писать в БД")
    ap.add_argument("--rebuild", action="store_true",
                    help="снести фото-чанки диалога (теряются эмбеддинги)")
    ap.add_argument("--sample", type=int, default=0,
                    help="показать N первых чанков каждого диалога")
    ap.add_argument("--min-conf", type=float, default=MIN_CONF, dest="min_conf")
    ap.add_argument("--min-chars", type=int, default=MIN_CHARS, dest="min_chars")
    args = ap.parse_args()

    if not args.peer and not args.all:
        sys.exit("нужен --peer или --all")

    with psycopg.connect(DSN) as conn:
        ensure_schema(conn)
        conn.commit()
        if args.all:
            # Диалоги, помеченные бесполезными, здесь НЕ отсеиваются — в
            # отличие от build_chunks.py. «Бесполезен» ставилось по переписке:
            # одна реплика, ничего не сказано. Но фотография с распознанным
            # текстом — это содержимое независимо от того, что вокруг неё
            # написали, а чанк самодостаточен: в нём есть и имя, и дата.
            # Таких чанков единицы на десятки тысяч — цена вопроса невелика.
            # Второе условие — не про построение, а про уборку: если при
            # перепрогоне анализа все фотографии диалога стали мёртвыми
            # ссылками, по первому условию диалог выпадет из выборки, и его
            # старые чанки останутся в индексе навсегда.
            rows = conn.execute("""
                SELECT d.peer_id, d.name, d.kind
                  FROM dialogs d
                 WHERE EXISTS (SELECT 1 FROM photo_analysis p
                                 JOIN messages m ON m.id = p.message_id
                                WHERE m.peer_id = d.peer_id AND p.status = 'ok')
                    OR EXISTS (SELECT 1 FROM chunks c
                                WHERE c.peer_id = d.peer_id
                                  AND c.source = 'photo')
                 ORDER BY d.total_msgs DESC""").fetchall()
        else:
            wanted = {int(p) for p in args.peer}
            rows = conn.execute(
                "SELECT peer_id, name, kind FROM dialogs WHERE peer_id = ANY(%s)",
                (list(wanted),)).fetchall()
            if len(rows) != len(wanted):
                sys.exit("не все peer_id найдены")

        started = dt.datetime.now()
        total = {k: 0 for k in ("photos", "chunks", "chars", "inserted",
                                "stale", "skipped", "bad_labels")}
        for peer_id, name, kind in rows:
            st = process(conn, peer_id, name, kind, args.write, args.rebuild, args)
            for k in total:
                total[k] += st[k]
            # Печатаем и когда чанков ноль, но что-то удалено: без этого
            # самый опасный случай — диалог, у которого снесли всё, — был
            # единственным, о котором прогон не сообщал ни строчки.
            if st["chunks"] or st["stale"]:
                avg = st["chars"] // st["chunks"] if st["chunks"] else 0
                stale = f"  снесено {st['stale']}" if st["stale"] else ""
                print(f"{peer_id:>12}  {st['chunks']:>6} фото-чанков  "
                      f"ср. {avg:>5} симв.  "
                      f"отсеяно {st['skipped']:>4}{stale}  {name[:30]}")

        took = (dt.datetime.now() - started).total_seconds()
        print(f"\nитого: {total['chunks']} чанков из {total['photos']} фото "
              f"(отсеяно бессловесных {total['skipped']}), "
              f"{total['chars'] / 1048576:.1f} МБ текста, за {took:.1f} с")
        if total["bad_labels"]:
            print(f"ВНИМАНИЕ: испорченных меток сцены пропущено "
                  f"{total['bad_labels']} — проверьте photo_analysis.labels")
        if args.write:
            print(f"вставлено новых: {total['inserted']}, "
                  f"удалено устаревших: {total['stale']}")
        else:
            print("режим прикидки — в БД ничего не записано (нужен --write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
