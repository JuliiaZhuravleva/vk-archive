#!/usr/bin/env python3
"""Проставляет версию настроек в мету уже готовых транскриптов.

Новые файлы получают поле `settings` сразу (SETTINGS_VERSION в
transcribe_voice.py). Для сделанных до его появления версия ВОССТАНАВЛИВАЕТСЯ
ПО ВРЕМЕНИ ФАЙЛА — это оценка, а не точная запись, поэтому границы вынесены
сюда явно и задокументированы в docs/transcription-settings.md.

  python3 scripts/label_settings.py            # показать раскладку
  python3 scripts/label_settings.py --write     # записать в мету
  python3 scripts/label_settings.py --reset v1-baseline v2-leak-fixed
                                                # удалить файлы этих версий
                                                # (перепрогон в следующем заходе)
"""
import argparse
import collections
import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANS_DIR = ROOT / "data" / "processed" / "transcription" / "transcripts"

# Границы эпох (локальное время 31.07.2026). Всё, что раньше следующей
# границы, относится к текущей версии.
EPOCHS = [
    (dt.datetime(2026, 7, 31, 1, 1, 34), "v1-baseline",
     "temperature-fallback вкл, утечка MLX, грязные промпты"),
    (dt.datetime(2026, 7, 31, 1, 34, 59), "v2-leak-fixed",
     "+ clear_cache в local-whisper (память), качество то же"),
    (dt.datetime(2026, 7, 31, 1, 36, 40), "v3-temp0",
     "+ temperature=0: нет срывов в повтор, втрое быстрее"),
    (dt.datetime(2026, 7, 31, 1, 48, 47), "v4-clean-prompts",
     "+ чистка соседних реплик и требование орфографии в промптах"),
    (dt.datetime(2100, 1, 1), "v5-boilerplate-filter",
     "+ фильтр заготовок «Субтитры сделал…», «Продолжение следует»"),
]


def version_for(ts: dt.datetime) -> str:
    for boundary, name, _ in EPOCHS:
        if ts < boundary:
            return name
    return EPOCHS[-1][1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="записать в meta.json")
    ap.add_argument("--reset", nargs="*", metavar="VERSION",
                    help="удалить транскрипты указанных версий для перепрогона")
    args = ap.parse_args()

    by_ver = collections.Counter()
    files = collections.defaultdict(list)
    for meta_f in sorted(TRANS_DIR.glob("*.meta.json")):
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
        ver = meta.get("settings")
        if not ver:
            ver = version_for(dt.datetime.fromtimestamp(meta_f.stat().st_mtime))
            if args.write:
                meta["settings"] = ver
                meta["settings_inferred"] = True
                meta_f.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        by_ver[ver] += 1
        files[ver].append(meta_f)

    print("Транскрипты по версиям настроек:")
    descr = {name: d for _, name, d in EPOCHS}
    for _, name, _ in EPOCHS:
        if by_ver.get(name):
            print(f"  {name:24s} {by_ver[name]:5d}  — {descr[name]}")
    if args.write:
        print("\nверсии записаны в meta.json (пометка settings_inferred для оценённых)")

    if args.reset:
        victims = [f for v in args.reset for f in files.get(v, [])]
        if not victims:
            print(f"\nнет транскриптов версий {args.reset}")
            return
        for meta_f in victims:
            txt = meta_f.with_name(meta_f.name.replace(".meta.json", ".txt"))
            txt.unlink(missing_ok=True)
            meta_f.unlink()
        print(f"\nудалено {len(victims)} — прогон сделает их заново")


if __name__ == "__main__":
    main()
