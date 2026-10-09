#!/usr/bin/env python3
"""Фото архива на Google Диске: индекс «имя файла → файл на Диске». Только stdlib.

Ссылки VK на фотографии живут не вечно, а скачанные картинки не обязаны
лежать на этом диске: папку data/media/photos можно выгрузить на Диск и
стереть локально. Связь при этом не теряется, потому что имя файла
вычисляется из ссылки — download_media.dest_for, sha1(url)[:16] + расширение.
Ссылка в манифесте и в базе остаётся строкой и тогда, когда VK по ней уже
ничего не отдаёт, а индекс переводит имя в id файла на Диске:

  вложение → url → dest_for(url, "photo").name → индекс → файл на Диске

Даже без индекса файл находится поиском по имени в папке на Диске; индекс
нужен, чтобы ссылка открывалась сразу и чтобы связь была доказана хешами,
а не одним совпадением имён. Строится из листинга rclone:

  rclone lsjson --files-only --hash gdrive:vk-archive-photos > listing.json
  python3 scripts/photo_drive.py index listing.json [--local-hashes local.tsv]

--local-hashes — хеши, снятые ДО удаления локальной папки: TSV с шапкой
name, size, md5, sha256. С ним индекс пишется, только если наборы имён равны
и каждый файл на Диске совпал с локальным по размеру, md5 и sha256.

  python3 scripts/photo_drive.py link URL [URL ...]   # «-» — ссылки из stdin
  python3 scripts/photo_drive.py coverage             # чем манифест покрыт Диском

link печатает имя файла и ссылку на Диск, саму ссылку VK не повторяет.
Ссылка https://drive.google.com/file/d/<id>/view открывается только тому,
у кого есть доступ к файлу, и доступа сама не даёт. `rclone link` для этого
не годится: он открывает файл всем, у кого есть ссылка.

Коды выхода: 0 — сошлось / всё найдено; 1 — отказ или фото без файла на Диске,
каждое названо по имени; 2 — вход не прочитан. При 1 и 2 индекс не меняется.
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT  # noqa: E402
from download_media import dest_for  # noqa: E402

DEFAULT_INDEX = ROOT / "data" / "processed" / "photo_drive.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "processed" / "media_manifest.jsonl"
VIEW = "https://drive.google.com/file/d/{}/view"
SHOW = 5          # сколько расхождений показывать по имени, остальное — числом


class Refused(ValueError):
    """Вход не доказывает связь: индекс не пишется, причина названа."""


def parse_listing(items: list) -> dict[str, dict]:
    """Вывод rclone lsjson → {имя: строка индекса}.

    Отказ на всём, что делает связь недоказанной: каталог или файл во вложенной
    папке (индекс ведётся по имени в одной плоской папке), два файла с одним
    именем (Диск это допускает, и имя перестаёт указывать на один файл),
    запись без id, размера, md5 или sha256 — сверка с отсутствующим полем
    проходит всегда.
    """
    rows: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict) or not it.get("Name"):
            raise Refused(f"запись без имени: {it!r:.80}")
        name = it["Name"]
        if it.get("IsDir"):
            raise Refused(f"в листинге каталог {name}: нужен rclone lsjson --files-only")
        if it.get("Path", name) != name:
            raise Refused(f"файл {it.get('Path')} не в корне папки: индекс ждёт плоскую папку")
        if name in rows:
            raise Refused(f"на Диске два файла {name}: имя не указывает на один файл")
        hashes = it.get("Hashes") or {}
        size = it.get("Size")
        lacking = [field for field, value in (("id", it.get("ID")), ("md5", hashes.get("md5")),
                                              ("sha256", hashes.get("sha256")))
                   if not value]
        if not isinstance(size, int) or size < 0:
            lacking.append("размера")
        if lacking:
            raise Refused(f"{name}: в листинге нет {', '.join(lacking)} — "
                          "нужен rclone lsjson --hash")
        rows[name] = {"name": name, "drive_id": it["ID"], "bytes": size,
                      "md5": hashes["md5"], "sha256": hashes["sha256"]}
    if not rows:
        raise Refused("листинг пуст: не та папка или на Диске нет файлов")
    return rows


def read_local_hashes(path: Path) -> dict[str, tuple[int, str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        need = ("name", "size", "md5", "sha256")
        if not set(need) <= set(reader.fieldnames or ()):
            raise ValueError(f"в шапке нужны {', '.join(need)}")
        local: dict[str, tuple[int, str, str]] = {}
        for n, r in enumerate(reader, start=2):
            if any(not r.get(k) for k in need):
                raise ValueError(f"строка {n}: не хватает полей")
            if r["name"] in local:
                raise ValueError(f"строка {n}: имя {r['name']} уже было")
            local[r["name"]] = (int(r["size"]), r["md5"], r["sha256"])
    return local


def compare(drive: dict[str, dict], local: dict[str, tuple[int, str, str]]) -> list[str]:
    """Расхождения Диска с локальными хешами, каждое — по имени файла."""
    problems = [f"{n}: был локально, на Диске нет" for n in sorted(local.keys() - drive.keys())]
    problems += [f"{n}: на Диске есть, локально не было"
                 for n in sorted(drive.keys() - local.keys())]
    for name in sorted(local.keys() & drive.keys()):
        row = drive[name]
        for field, want, got in zip(("размер", "md5", "sha256"), local[name],
                                    (row["bytes"], row["md5"], row["sha256"]), strict=True):
            if want != got:
                problems.append(f"{name}: {field} локально {want}, на Диске {got}")
    return problems


def load_index(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if not line.strip():
                continue
            rec = json.loads(line)
            if not (isinstance(rec, dict) and rec.get("name") and rec.get("drive_id")):
                raise ValueError(f"строка {n}: нет name или drive_id")
            rows[rec["name"]] = rec
    return rows


def write_index(rows: dict[str, dict], path: Path) -> None:
    # Временный файл рядом и rename: оборванная запись не должна оставить
    # вместо индекса половину — по ней половина фото «пропала бы» с Диска.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as out:
            for name in sorted(rows):
                out.write(json.dumps(rows[name], ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def drive_link(row: dict) -> str:
    return VIEW.format(row["drive_id"])


def resolve(url: str, index: dict[str, dict]) -> tuple[str, dict | None]:
    """Ссылка VK → (имя файла, строка индекса или None). Имя — ровно то, под
    которым файл скачивался, поэтому берётся из dest_for, а не считается заново."""
    name = dest_for(url, "photo").name
    return name, index.get(name)


def coverage(manifest_lines, index: dict[str, dict]) -> Counter:
    c: Counter = Counter()
    referenced = set()
    for line in manifest_lines:
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("kind") != "photo" or not rec.get("url"):
            continue
        c["photo"] += 1
        name, row = resolve(rec["url"], index)
        if row:
            c["on_drive"] += 1
            referenced.add(name)
        else:
            c["not_on_drive"] += 1
    c["indexed"] = len(index)
    c["unreferenced"] = len(index.keys() - referenced)
    return c


def unreadable(path: Path, why: Exception | str) -> int:
    print(f"не прочитано: {path}: {why}", file=sys.stderr)
    return 2


def refuse(reasons: list[str]) -> int:
    for r in reasons[:SHOW]:
        print(f"ОТКАЗ: {r}", file=sys.stderr)
    if len(reasons) > SHOW:
        print(f"ОТКАЗ: и ещё {len(reasons) - SHOW}", file=sys.stderr)
    print("индекс не записан", file=sys.stderr)
    return 1


def read_index_for_use(path: Path) -> dict[str, dict] | int:
    try:
        index = load_index(path)
    except (OSError, ValueError) as e:
        return unreadable(path, e)
    if not index:
        return unreadable(path, "индекс пуст — по нему любое фото «не на Диске»")
    return index


def cmd_index(args) -> int:
    try:
        items = json.loads(args.listing.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return unreadable(args.listing, e)
    if not isinstance(items, list):
        return unreadable(args.listing, "это не вывод rclone lsjson")
    try:
        rows = parse_listing(items)
    except Refused as e:
        return refuse([str(e)])

    if args.local_hashes:
        try:
            local = read_local_hashes(args.local_hashes)
        except (OSError, ValueError) as e:
            return unreadable(args.local_hashes, e)
        problems = compare(rows, local)
        if problems:
            return refuse(problems)

    # Индекс может расти (досыпали фото), но не терять имена молча: листинг не
    # той папки иначе подменил бы хороший индекс, и link отвечал бы «нет на
    # Диске» про фото, которые там лежат.
    if args.out.exists() and not args.allow_loss:
        try:
            lost = sorted(load_index(args.out).keys() - rows.keys())
        except (OSError, ValueError) as e:
            return unreadable(args.out, f"{e}; перезаписать его — --allow-loss")
        if lost:
            return refuse([f"{n}: был в индексе, в листинге нет" for n in lost]
                          + [f"пропало бы имён: {len(lost)}; если файлы удалены с Диска "
                             "намеренно — --allow-loss"])

    try:
        write_index(rows, args.out)
    except OSError as e:
        print(f"не записано: {args.out}: {e}", file=sys.stderr)
        return 2
    checked = f", сверено с локальными хешами: {len(rows)}" if args.local_hashes else ""
    print(f"индекс: {len(rows)} файлов → {args.out}{checked}")
    return 0


def cmd_link(args) -> int:
    index = read_index_for_use(args.index)
    if isinstance(index, int):
        return index
    urls = sys.stdin if args.urls == ["-"] else args.urls
    missing = 0
    for url in (u.strip() for u in urls):
        if not url:
            continue
        name, row = resolve(url, index)
        if row:
            print(f"{name}\t{drive_link(row)}")
        else:
            missing += 1
            print(f"{name}\tнет на Диске")
    return 1 if missing else 0


def cmd_coverage(args) -> int:
    index = read_index_for_use(args.index)
    if isinstance(index, int):
        return index
    try:
        with args.manifest.open(encoding="utf-8") as f:
            c = coverage(f, index)
    except (OSError, ValueError) as e:
        return unreadable(args.manifest, e)
    print(f"фото-вложений в манифесте: {c['photo']}")
    print(f"  с файлом на Диске: {c['on_drive']}")
    print(f"  без файла на Диске: {c['not_on_drive']}")
    print(f"файлов в индексе: {c['indexed']}, без ссылки из манифеста: {c['unreferenced']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="фото архива на Google Диске")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="листинг Диска → индекс")
    p.add_argument("listing", type=Path, help="вывод rclone lsjson --files-only --hash")
    p.add_argument("--local-hashes", type=Path,
                   help="TSV name/size/md5/sha256, снятый до удаления локальной папки")
    p.add_argument("--out", type=Path, default=DEFAULT_INDEX,
                   help="куда писать индекс (по умолчанию data/processed/photo_drive.jsonl)")
    p.add_argument("--allow-loss", action="store_true",
                   help="разрешить индексу потерять имена: файлы удалены с Диска намеренно")
    p.set_defaults(run=cmd_index)

    p = sub.add_parser("link", help="ссылка VK → файл на Диске")
    p.add_argument("urls", nargs="+", help="ссылки; «-» — читать по одной в строке из stdin")
    p.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    p.set_defaults(run=cmd_link)

    p = sub.add_parser("coverage", help="сколько фото манифеста нашли свой файл на Диске")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    p.set_defaults(run=cmd_coverage)

    args = ap.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
