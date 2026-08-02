#!/usr/bin/env python3
"""Скачать фотографии и вытащить из них данные: текст, сцену, лица.

Анализ идёт встроенным в macOS фреймворком Vision — всё на устройстве, наружу
не уходит ничего. Три вещи из одной цепочки: OCR (30 языков, русский есть),
классификатор сцены (1303 категории — отличает скриншот от арта и от фото
людей) и детектор лиц. Замер: ≈95 мс на картинку.

Ценность здесь — извлечённые данные, а не файлы, поэтому `--discard` удаляет
картинку сразу после анализа: место тогда не растёт, а строка в БД остаётся.
Ссылка, размер, sha256 и заголовки ответа дублируются в `photo_analysis`
именно затем, чтобы файл можно было опознать и достать заново, пока ссылка
жива. Строка пишется и при неудаче: мёртвая ссылка — тоже факт.

  python3 scripts/analyze_photos.py --limit 500            # пилот, личные
  python3 scripts/analyze_photos.py --dialog-kind all --discard
  python3 scripts/analyze_photos.py --stats

Пауза — `touch data/processed/photos/PAUSE`, мягкая остановка — `.../STOP`.
"""
import argparse
import hashlib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_media import UA, dest_for          # noqa: E402
from search import DSN                           # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FLAGS = ROOT / "data" / "processed" / "photos"
URL_SIZE = re.compile(r"size=(\d+)x(\d+)")
BATCH = 40

# Из 1303 категорий классификатора нас интересует горстка: по ним отделяется
# полезное (скриншоты, документы) от фона (арты, мемы). Остальное всё равно
# пишется в labels целиком — здесь только то, что выносим в сводку.
SUMMARY_LABELS = ("screenshot", "document", "art", "illustrations", "people",
                  "text", "outdoor", "food", "animal", "plant")


def vision():
    """Импорт отложен: без pyobjc скрипт должен внятно ругаться, а не падать
    трейсбеком на первой строке."""
    try:
        import Vision                                # noqa: PLC0415
        from Foundation import NSURL                 # noqa: PLC0415
    except ImportError as e:
        sys.exit("нужен pyobjc: .venv/bin/pip install pyobjc-framework-Vision "
                 f"pyobjc-framework-Quartz ({e})")
    return Vision, NSURL


def analyze(path: Path, level: str, V, NSURL) -> dict:
    """OCR + классификация сцены + лица за один проход по файлу."""
    url = NSURL.fileURLWithPath_(str(path))
    handler = V.VNImageRequestHandler.alloc().initWithURL_options_(url, None)

    ocr = V.VNRecognizeTextRequest.alloc().init()
    ocr.setRecognitionLevel_(V.VNRequestTextRecognitionLevelAccurate if level == "accurate"
                             else V.VNRequestTextRecognitionLevelFast)
    ocr.setRecognitionLanguages_(["ru-RU", "en-US"])
    ocr.setUsesLanguageCorrection_(True)
    cls = V.VNClassifyImageRequest.alloc().init()
    faces = V.VNDetectFaceRectanglesRequest.alloc().init()

    ok, err = handler.performRequests_error_([ocr, cls, faces], None)
    if not ok:
        raise OSError(f"Vision не смог прочитать файл: {err}")

    lines, conf = [], []
    for obs in (ocr.results() or []):
        cand = obs.topCandidates_(1)
        if cand and len(cand):
            lines.append(cand[0].string())
            conf.append(cand[0].confidence())
    labels = [{"id": o.identifier(), "conf": round(float(o.confidence()), 3)}
              for o in (cls.results() or []) if o.confidence() > 0.25]
    labels.sort(key=lambda x: -x["conf"])
    return {
        "ocr_text": "\n".join(lines) or None,
        "ocr_conf": round(sum(conf) / len(conf), 3) if conf else None,
        "labels": labels[:12],
        "n_faces": len(faces.results() or []),
    }


def write_batch(conn, rows: list, retries: int = 6):
    """Записать батч, пережив перезапуск базы.

    Docker Desktop за одну сессию падал дважды, и каждый раз это убивало
    часовой прогон: скачанное осталось на диске, но строки не записались, а
    процесс умер на `OperationalError`. Резюме потом всё догоняет, но час
    работы жалко. Возвращает соединение — возможно, уже новое."""
    cols = list(rows[0])
    # Перезаписываем только строки с net_error — наши же транзиентные сбои,
    # которые pending() специально отдаёт на повтор. Всё остальное (успех,
    # честный 404, заглушка) неприкосновенно: DO NOTHING по смыслу, но
    # выражено через DO UPDATE ... WHERE, потому что DO NOTHING не пропустил бы
    # повторную попытку вообще и net_error навсегда остался бы в базе.
    upd = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "attachment_id")
    sql = (f"insert into photo_analysis ({','.join(cols)}) values "
           + ",".join(["(" + ",".join(["%s"] * len(cols)) + ")"] * len(rows))
           + f" on conflict (attachment_id) do update set {upd}"
           + " where photo_analysis.status = 'net_error'")
    params = [r[c] for r in rows for c in cols]
    last = None
    for attempt in range(1, retries + 1):
        try:
            if conn is None:
                conn = psycopg.connect(DSN)
            conn.execute(sql, params)
            conn.commit()
            return conn
        except psycopg.OperationalError as e:
            # Переподключение делается в НАЧАЛЕ следующей попытки, а не здесь.
            # Первая версия вызывала psycopg.connect() прямо в обработчике —
            # и когда база ещё не поднялась (Postgres в докере стартует
            # десятки секунд), connect() бросал OperationalError изнутри
            # except, мимо цикла. Ретраев было не шесть, а один: ровно так
            # прогон и умер в третий раз, трейсбек указывал на connect().
            last = e
            try:
                if conn is not None:
                    conn.close()
            except Exception:                       # noqa: BLE001
                pass
            conn = None
            if attempt == retries:
                raise
            print(f"  база не отвечает ({str(e).splitlines()[0]}) — "
                  f"жду и переподключаюсь, попытка {attempt} из {retries}")
            time.sleep(min(5 * attempt, 30))
    if last:
        raise last
    return conn


def safe_analyze(res: dict, level: str, V, NSURL) -> dict:
    """Обёртка для пула: одна битая картинка не должна ронять весь батч.
    Размер берём здесь же, пока файл ещё точно на диске."""
    path = res["path"]
    try:
        out = analyze(path, level, V, NSURL)
        out["width"], out["height"] = dims(path)
        return out
    except Exception as e:                          # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def fetch(url: str, dest: Path, retries: int = 3) -> dict:
    """Скачивание с теми же граблями, что у голосовых: VK-CDN умеет отдать
    HTML-заглушку с кодом 200, и она молча ляжет на диск под именем .jpg."""
    last = ""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                head = {k: r.headers.get(k) for k in
                        ("Content-Type", "Last-Modified", "ETag")
                        if r.headers.get(k)}
            if not data:
                raise OSError("пустой ответ")
            if data[:15].lstrip()[:1] == b"<":
                return {"status": "html_stub",
                        "error": f"вместо файла разметка ({len(data)} б)"}
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Имя файла — хеш URL, а один и тот же URL висит на разных
            # сообщениях: в корпусе 3020 таких повторов. Соседние по времени
            # дубли попадают в один батч и качаются параллельно, поэтому
            # временный файл должен быть свой у каждого потока — иначе один
            # переименует его из-под другого, и тот упадёт на FileNotFoundError.
            tmp = dest.with_suffix(f"{dest.suffix}.{threading.get_ident()}.part")
            try:
                tmp.write_bytes(data)
                tmp.rename(dest)
            finally:
                tmp.unlink(missing_ok=True)   # если rename не дошёл — не мусорим
            return {"status": "ok", "bytes": len(data), "headers": head,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "content_type": head.get("Content-Type"), "path": dest}
        except urllib.error.HTTPError as e:
            # 4xx — приговор: файла нет и не будет. 5xx — сбой на их стороне,
            # и он часто разовый: на прогоне личных переписок семь ссылок из
            # первых 3800 отдали 500, и без повтора они бы навсегда записались
            # мёртвыми. Различать обязательно, иначе теряем живые фото.
            if e.code < 500 or attempt == retries:
                return {"status": "http_error", "error": f"HTTP {e.code}"}
            last = f"HTTP {e.code}"
            time.sleep(2 * attempt)
        except Exception as e:                      # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(2 * attempt)
    # Отдельный статус, а не http_error: сюда попадают обрыв сети, таймаут,
    # нехватка места — то есть НАШИ беды, а не ответ сервера. Разница важна,
    # потому что pending() пропускает всё, на что уже есть строка: без этого
    # деления пятиминутный обрыв Wi-Fi навсегда пометил бы живые фото
    # мёртвыми, и повторный запуск их бы не переспросил.
    return {"status": "net_error", "error": last}


def dims(path: Path) -> tuple[int | None, int | None]:
    """Размер картинки без разбора формата вручную — через тот же Quartz."""
    try:
        from Quartz import (CGImageSourceCreateWithURL,       # noqa: PLC0415
                            CGImageSourceCopyPropertiesAtIndex)
        from Foundation import NSURL                          # noqa: PLC0415
        src = CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
        props = CGImageSourceCopyPropertiesAtIndex(src, 0, None) if src else None
        if props:
            return props.get("PixelWidth"), props.get("PixelHeight")
    except Exception:                               # noqa: BLE001
        pass
    return None, None


def pending(conn, args) -> list:
    where = ["a.kind = 'photo'"]
    params: list = []
    if args.dialog_kind != "all":
        where.append("d.kind = %s")
        params.append(args.dialog_kind)
    if args.peer:
        where.append("d.peer_id = ANY(%s)")
        params.append([int(p) for p in args.peer])
    if args.min_own_share:
        # Беседы по умолчанию бесполезны, исключения — те, где хозяин архива
        # реально участвовал. Порог разделяет резко: в самой большой мем-беседе
        # 58 тыс. фото при 13 своих сообщениях из 195 тысяч, а в дружеской
        # беседе — 5 тыс. фото при доле 28%. Отсечка по 8% оставляет
        # 15 тыс. фото из 94 тыс.
        where.append("coalesce(d.own_share, 0) >= %s")
        params.append(args.min_own_share)
    # ORDER BY — сначала старые. Замер на личных переписках: в 2020–2021 мертвы
    # 0,2–0,5 процента ссылок, в 2015–2019 уже 2–3, в 2010–2011 около девяти, и
    # доминирует честный 404 — фото удалены навсегда. Старое и хрупкое надо
    # забирать первым: чем дольше ждём, тем меньше останется.
    #
    # Комментарий держим здесь, а не внутри SQL: знак процента в тексте запроса
    # psycopg разбирает как плейсхолдер и падает на «got '%,'».
    sql = f"""
        select a.id, a.message_id, a.url from attachments a
          join messages m on m.id = a.message_id
          join dialogs d on d.peer_id = m.peer_id
         where {' AND '.join(where)}
           and a.url is not null
           and not exists (select 1 from photo_analysis p
                            where p.attachment_id = a.id
                              and p.status <> 'net_error')
         order by m.sent_at"""
    if args.limit:
        sql += f" limit {int(args.limit)}"
    return conn.execute(sql, params).fetchall()


def stats(conn) -> None:
    rows = conn.execute("""select status, count(*) from photo_analysis
                            group by 1 order by 2 desc""").fetchall()
    print("по статусам:", ", ".join(f"{s}={n}" for s, n in rows) or "пусто")
    tot = sum(n for _, n in rows)
    if not tot:
        return
    with_text = conn.execute("""select count(*) from photo_analysis
                                 where length(coalesce(ocr_text,'')) >= 8""").fetchone()[0]
    faces = conn.execute("select count(*) from photo_analysis where n_faces > 0").fetchone()[0]
    print(f"с текстом: {with_text} ({with_text/tot:.0%}) · с лицами: {faces} ({faces/tot:.0%})")
    print("\nчастые метки:")
    for lab, n in conn.execute("""
            select l->>'id', count(*) from photo_analysis, jsonb_array_elements(labels) l
             where (l->>'conf')::float >= 0.5 group by 1 order by 2 desc limit 12"""):
        print(f"   {lab:<18} {n}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = все")
    ap.add_argument("--dialog-kind", default="user",
                    choices=["user", "chat", "community", "all"], dest="dialog_kind")
    ap.add_argument("--peer", action="append")
    ap.add_argument("--min-own-share", type=float, default=0.0,
                    dest="min_own_share",
                    help="брать только диалоги, где доля своих сообщений не ниже "
                         "(для бесед разумно 0.08)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--level", default="accurate", choices=["accurate", "fast"])
    ap.add_argument("--discard", action="store_true",
                    help="удалять картинку после анализа (данные остаются в БД)")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    V, NSURL = (None, None) if args.stats else vision()
    FLAGS.mkdir(parents=True, exist_ok=True)

    # Соединением управляем вручную, а не через `with`: при перезапуске базы
    # write_batch подставляет НОВОЕ соединение, а контекстный менеджер закрыл
    # бы старый объект и оставил новый висеть.
    conn = psycopg.connect(DSN)
    try:
        if args.stats:
            stats(conn)
            return 0

        todo = pending(conn, args)
        print(f"к обработке: {len(todo)} фото · уровень {args.level} · "
              f"файлы {'удаляются' if args.discard else 'остаются'}")
        if not todo:
            return 0

        done = failed = t_dl = t_an = 0
        started = time.time()
        for i in range(0, len(todo), BATCH):
            # STOP проверяется и внутри ожидания на паузе: иначе поставленный
            # во время PAUSE стоп-флаг не сработал бы никогда, и остановить
            # прогон можно было бы только сняв паузу — что неочевидно.
            while (FLAGS / "PAUSE").exists() and not (FLAGS / "STOP").exists():
                print("PAUSE…")
                time.sleep(20)
            if (FLAGS / "STOP").exists():
                print("STOP — останавливаюсь")
                break

            batch = todo[i:i + BATCH]
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                got = list(pool.map(
                    lambda r: (r, fetch(r[2], dest_for(r[2], "photo"))), batch))
            t_dl += time.time() - t0

            # Анализ тоже параллелится, хотя и скромно: Vision частично
            # распараллеливает сам, замер дал 100 мс в один поток и 70 в
            # четыре, а дальше упор. Восемь потоков не быстрее четырёх.
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=4) as pool:
                done_an = dict(pool.map(
                    lambda it: (it[0][0], safe_analyze(it[1], args.level, V, NSURL)),
                    [g for g in got if g[1]["status"] == "ok"]))

            rows, to_unlink = [], []
            for (att_id, msg_id, url), res in got:
                m = URL_SIZE.search(url or "")
                base = {"attachment_id": att_id, "message_id": msg_id,
                        "url": url, "url_size": f"{m.group(1)}x{m.group(2)}" if m else None,
                        "status": res["status"], "error": res.get("error"),
                        "bytes": res.get("bytes"), "sha256": res.get("sha256"),
                        "content_type": res.get("content_type"),
                        "headers": json.dumps(res.get("headers") or {}, ensure_ascii=False),
                        "width": None, "height": None, "ocr_text": None,
                        "ocr_conf": None, "ocr_level": args.level,
                        "labels": "[]", "n_faces": None, "kept_file": False}
                if res["status"] == "ok":
                    path = res["path"]
                    a = done_an.get(att_id) or {}
                    if a.get("error") or not a:
                        base.update(status="unreadable",
                                    error=a.get("error", "анализ не выполнен"))
                    else:
                        base.update(width=a.get("width"), height=a.get("height"),
                                    ocr_text=a.get("ocr_text"),
                                    ocr_conf=a.get("ocr_conf"),
                                    n_faces=a.get("n_faces"),
                                    labels=json.dumps(a.get("labels", []),
                                                      ensure_ascii=False))
                    if args.discard and base["status"] == "ok":
                        # Удаляем ПОСЛЕ успешной записи (см. ниже), иначе при
                        # падении базы между удалением и коммитом мы теряем и
                        # файл, и результат анализа: resume полезет качать
                        # заново, а ссылка к тому времени может быть мертва.
                        # Файл с unreadable не трогаем вовсе — по нему ещё
                        # может понадобиться разбор.
                        to_unlink.append(path)
                    else:
                        base["kept_file"] = True
                    if base["status"] == "ok":
                        done += 1
                    else:
                        failed += 1
                else:
                    failed += 1
                rows.append(base)
            t_an += time.time() - t0

            conn = write_batch(conn, rows)
            for p in to_unlink:
                p.unlink(missing_ok=True)
            to_unlink.clear()

            n = i + len(batch)
            el = time.time() - started
            eta = (len(todo) - n) * el / n
            print(f"  {n}/{len(todo)} · ок {done}, сбоев {failed} · "
                  f"{el/n*1000:.0f} мс/шт (скачивание {t_dl/n*1000:.0f}, "
                  f"анализ {t_an/n*1000:.0f}) · осталось ~{eta/60:.0f} мин")

        # Мёртвая ссылка — не ошибка прогона, а факт про архив; печатаем отдельно.
        print(f"\nготово: успешно {done}, не удалось {failed} "
              f"({failed/(done+failed or 1):.1%} ссылок мертвы или нечитаемы)")
        stats(conn)
    finally:
        try:
            conn.close()
        except Exception:                           # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
