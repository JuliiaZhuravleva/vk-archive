#!/usr/bin/env python3
"""Дополняет манифест вложениями из новой выгрузки, не трогая старые строки.

  python3 scripts/build_media_manifest.py --out data/processed/media_manifest.new.jsonl
  python3 scripts/merge_media_manifest.py data/processed/media_manifest.new.jsonl

Почему не пересобрать манифест целиком. download_media.py кладёт файл под
именем sha1(url), а VK на каждую выгрузку отдаёт то же самое фото с другого
CDN-шарда: sun9-78 → sun9-9 при том же пути, размере и подписи. Полная
пересборка меняет sha1 у сотни тысяч записей, dest.exists() перестаёт находить
уже скачанное, и всё качается заново, а прежний кэш остаётся сиротой. У
голосовых URL, наоборот, устойчив (проверено 05.08.2026: 9191 из 9191 совпали
побайтово), но полагаться на это для всех типов нельзя.

Поэтому строки существующего манифеста сохраняются как есть, а из нового
берутся только вложения, которых в нём нет. Проверок две, и обе нужны:

* по (peer_id, msg_id) — вложения одного сообщения не должны оказаться
  размазаны между двумя выгрузками с разными хостами;
* по URL без хоста и домена — build_media_manifest.py дедуплицирует по URL,
  а порядок обхода каталогов между запусками не совпадает, поэтому одно и то же
  пересланное вложение в двух выгрузках приписано разным сообщениям. Без этой
  проверки такие строки выглядят новыми: на прогоне 05.08.2026 их было 638 из
  1344 кандидатов, и каждая скачалась бы вторым файлом-близнецом.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT                               # noqa: E402

DEFAULT_BASE = ROOT / "data" / "processed" / "media_manifest.jsonl"

# vk.com → vk.ru и sun9-78 → sun9-9: разные выгрузки отдают тот же файл с
# другого шарда CDN, путь, размер и подпись при этом совпадают.
SHARD = re.compile(r"^https?://(?:www\.)?(?:vk\.(?:com|ru)|sun\d+-\d+\.userapi\.com)/")


def key(rec: dict) -> tuple[str, str]:
    return str(rec["peer_id"]), str(rec["msg_id"])


def url_key(rec: dict) -> str:
    return SHARD.sub("//", rec["url"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("new", type=Path, help="манифест новой выгрузки")
    ap.add_argument("--base", type=Path, default=DEFAULT_BASE,
                    help=f"текущий манифест (по умолчанию {DEFAULT_BASE})")
    ap.add_argument("--write", action="store_true",
                    help="без него только показывает, что будет добавлено")
    args = ap.parse_args()

    base_lines = args.base.read_text(encoding="utf-8").splitlines()
    base_recs = [json.loads(line) for line in base_lines if line.strip()]
    have_msg = {key(r) for r in base_recs}
    have_url = {url_key(r) for r in base_recs}

    added, kinds, skipped_url = [], Counter(), 0
    for line in args.new.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if key(rec) in have_msg:
            continue
        u = url_key(rec)
        if u in have_url:
            skipped_url += 1
            continue
        have_url.add(u)
        added.append(line)
        kinds[rec["kind"]] += 1

    print(f"в текущем манифесте: {len(base_lines)} строк, {len(have_msg)} сообщений")
    print(f"пропущено как уже известные файлы (другой шард/другое сообщение): {skipped_url}")
    print(f"будет добавлено:     {len(added)} строк")
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    if not args.write:
        print("\nбез --write ничего не записано")
        return
    if not added:
        print("\nдобавлять нечего")
        return

    # Пишем во временный файл рядом и подменяем rename'ом: манифест — единственная
    # связь между уже скачанными файлами и их URL, оборванная запись потеряет её.
    tmp = args.base.with_suffix(args.base.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        for line in base_lines:
            out.write(line + "\n")
        for line in added:
            out.write(line + "\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, args.base)
    print(f"\n{args.base}: {len(base_lines)} → {len(base_lines) + len(added)} строк")


if __name__ == "__main__":
    main()
