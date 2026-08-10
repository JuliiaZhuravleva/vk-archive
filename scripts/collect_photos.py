#!/usr/bin/env python3
"""Собрать найденные раскопкой фотографии в отдельную папку.

Раскопка обычно кончается списком attachment_id, а хочется папку с картинками
и подписями: кто, кому, когда, что было в сообщении рядом. Скрипт качает
вложения по их URL из photo_analysis и кладёт рядом manifest.md.

  python3 scripts/collect_photos.py --out data/processed/collections/тема \
          --id 101 202
  python3 scripts/collect_photos.py --out … --where "pa.ocr_text ~* 'слово'"
  python3 scripts/collect_photos.py --out … --id 123 --dry-run

Почему не analyze_photos.py --keep: тот ходит по всему корпусу по своим
правилам отбора и с --discard стирает файл сразу после разбора. Здесь наоборот:
точечный список из десятка картинок, которые нужно именно сохранить.

Условие --where подставляется в SQL как есть — это ваш собственный SQL, ровно
как в psql. Не подставляйте в него ничего, что пришло извне.

Имена файлов: ДАТА_ВРЕМЯ_диалог_свои-или-чужие_attid.jpg. Дата первой частью,
чтобы `ls` сразу давал хронологию. Повторный запуск по тому же каталогу
безопасен: файл перезаписывается только если содержимое отличается по размеру.
"""
import argparse
import datetime as dt
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DSN, ROOT  # noqa: E402

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

# Ссылки на вложения приходят из HTML-экспорта VK, то есть их содержимое
# выбирали не мы. urlopen понимает не только http: на file:// он молча отдаёт
# локальный файл, и битый или подделанный экспорт превратил бы качалку в
# копирование чего угодно с диска в папку с находками.
ALLOWED_SCHEMES = ("http", "https")


def safe(name, limit=40):
    """Имя диалога в имя файла: без слэшей, точек, эмодзи и пробелов.

    Это ещё и граница безопасности, а не только косметика: название беседы
    задаёт кто угодно из её участников, а мы подставляем его в путь. Точки и
    слэши срезаются здесь, поэтому «../../» до файловой системы не доезжает.
    """
    name = re.sub(r"[^\w\s-]", "", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", "-", name)[:limit] or "без-имени"


def fetch(where_sql, ids):
    sql = """
        SELECT a.id, pa.url, m.sent_at, m.is_own, m.text, m.peer_id, m.vk_msg_id,
               d.name, pa.ocr_text, pa.width, pa.height
          FROM photo_analysis pa
          JOIN attachments a ON a.id = pa.attachment_id
          JOIN messages m ON m.id = pa.message_id
          JOIN dialogs d ON d.peer_id = m.peer_id
         WHERE pa.url IS NOT NULL AND (%s)
         ORDER BY m.sent_at
    """
    if ids:
        cond, params = "a.id = ANY(%s)", [list(ids)]
    else:
        # Именно None, а не пустой список: со списком psycopg всё равно идёт
        # искать плейсхолдеры и падает на первом же проценте, то есть на любом
        # LIKE '%слово%' — самом естественном способе задать --where.
        cond, params = where_sql, None
    cols = ("id url sent_at is_own text peer_id vk_msg_id peer_name "
            "ocr_text width height").split()
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(sql % cond, params)
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def check_scheme(url):
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"схема {scheme or 'без схемы'!r} не разрешена")


def download(url, dest):
    """True — записали файл, False — на диске уже лежал такой же по размеру.

    Скачивается в обоих случаях: у вложений VK нет стабильного ETag, и дешевле
    забрать картинку, чем гадать. Экономится только запись.
    """
    check_scheme(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if dest.exists() and dest.stat().st_size == len(data):
        return False
    dest.write_bytes(data)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="каталог назначения")
    ap.add_argument("--id", nargs="+", type=int, metavar="ATT_ID",
                    help="attachment_id вложений")
    ap.add_argument("--where", help="SQL-условие вместо списка id")
    ap.add_argument("--title", help="заголовок в manifest.md")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.id and not args.where:
        sys.exit("нужен --id или --where")

    rows = fetch(args.where, args.id)
    if not rows:
        sys.exit("под условие ничего не попало")

    # Промах в списке id важнее молчаливого «скачали, сколько нашлось»:
    # у части вложений url пустой (VK перестал отдавать), и тогда картинки
    # в папке просто не будет, а понять это по итоговому счётчику нельзя.
    if args.id:
        missing = set(args.id) - {r["id"] for r in rows}
        if missing:
            print(f"! без url или вне photo_analysis: "
                  f"{', '.join(map(str, sorted(missing)))}", file=sys.stderr)

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out

    if args.dry_run:
        for r in rows:
            print(f"{r['sent_at']:%Y-%m-%d %H:%M}  {r['peer_name']}  "
                  f"{'своё' if r['is_own'] else 'чужое'}  att={r['id']}")
        print(f"\n{len(rows)} шт. → {out}")
        return

    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# {args.title or out.name}", "",
             f"Собрано {dt.date.today().isoformat()} скриптом "
             f"`scripts/collect_photos.py`. {len(rows)} вложений.", ""]
    got = skipped = failed = 0
    for r in rows:
        who = "своё" if r["is_own"] else "чужое"
        fname = (f"{r['sent_at']:%Y-%m-%d_%H%M}_{safe(r['peer_name'])}"
                 f"_{who}_{r['id']}.jpg")
        dest = out / fname
        try:
            if download(r["url"], dest):
                got += 1
            else:
                skipped += 1
            status = ""
        except (urllib.error.URLError, urllib.error.HTTPError,
                ValueError, OSError) as e:
            failed += 1
            status = f" — **НЕ СКАЧАЛОСЬ: {e}**"
        lines += [f"## {fname}{status}", "",
                  f"- {r['sent_at']:%d.%m.%Y %H:%M}, диалог «{r['peer_name']}» "
                  f"(peer_id {r['peer_id']}, vk_msg_id {r['vk_msg_id']})",
                  f"- отправитель: {'я' if r['is_own'] else 'собеседник'}",
                  f"- размер: {r['width']}×{r['height']}"]
        if (r["text"] or "").strip():
            lines.append(f"- текст сообщения: «{r['text'].strip()}»")
        if (r["ocr_text"] or "").strip():
            ocr = r["ocr_text"].strip().replace("\n", " / ")
            lines.append(f"- распознано на картинке: {ocr}")
        lines.append("")
        print(f"{'✓' if not status else '✗'} {fname}")

    (out / "manifest.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nскачано {got}, уже было {skipped}, не удалось {failed} → {out}")


if __name__ == "__main__":
    main()
