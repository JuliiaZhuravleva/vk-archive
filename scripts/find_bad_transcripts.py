#!/usr/bin/env python3
"""Ищет транскрипты со срывом Whisper в повтор и готовит их к переделке.

  python3 scripts/find_bad_transcripts.py            # только показать
  python3 scripts/find_bad_transcripts.py --reset    # удалить .txt/.meta,
                                                     # чтобы прогон сделал заново

Признак срыва: одно слово повторяется подряд 4+ раз («недель недель недель…»).
Такие тексты появлялись из-за temperature-fallback в whisper (исправлено
31.07 — temperature=0), поэтому переделывать имеет смысл только после фикса.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANS_DIR = ROOT / "data" / "processed" / "transcription" / "transcripts"

# Логика «срыва» — одна и та же, что при транскрипции: берём её оттуда,
# чтобы детектор и метки в мете не разъезжались.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcribe_voice import is_suspect, repetition_stats  # noqa: E402


BOILERPLATE = re.compile(
    r"(субтитры\s*(делал|сделал|создавал|подготовил|от)?|редактор\s+субтитров|"
    r"корректор|dimatorzok|amara\.org|продолжение\s+следует|"
    r"подписывайтесь\s+на\s+канал|спасибо\s+за\s+просмотр|ставьте\s+лайки)",
    re.IGNORECASE)


def boilerplate_hits(text: str) -> list:
    """Заготовки из субтитров, которые whisper выдумывает на тишине и смехе."""
    return [m.group(0) for m in BOILERPLATE.finditer(text)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true",
                    help="удалить файлы подозрительных транскриптов")
    args = ap.parse_args()

    bad, total = [], 0
    for txt in sorted(TRANS_DIR.glob("*.txt")):
        total += 1
        text = txt.read_text(encoding="utf-8")
        run, word, _ = repetition_stats(text)
        susp = is_suspect(text)
        hits = boilerplate_hits(text)
        if susp or hits:
            reason = []
            if susp:
                reason.append(f"«{word}» ×{run} подряд")
            if hits:
                reason.append("заготовки: " + ", ".join(sorted(set(hits))[:3]))
            bad.append((txt, "; ".join(reason), len(text)))

    print(f"проверено транскриптов: {total}, подозрительных: {len(bad)}")
    for txt, reason, size in bad:
        print(f"  {txt.stem}: {reason} (длина {size})")
    if not bad:
        return
    if not args.reset:
        print("\nдля переделки запусти с --reset (потом прогон сделает их заново)")
        return
    for txt, *_rest in bad:
        txt.unlink()
        meta = txt.with_name(txt.stem + ".meta.json")
        meta.unlink(missing_ok=True)
    print(f"\nудалено {len(bad)} — прогон перетранскрибирует их заново")


if __name__ == "__main__":
    main()
