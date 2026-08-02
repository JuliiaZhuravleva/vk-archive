#!/usr/bin/env python3
"""Разметка диалогов по реальному участию хозяина архива.

Для каждого диалога считает: всего сообщений, своих («Вы»), голосовых
(всего / своих), годы активности. Пишет data/processed/participation.json
c полем useless (хозяин архива фактически не участвовал) и печатает отчёт.

Правило useless: своих сообщений < 30 И доля < 2%. Порог можно менять —
пересчёт безопасен (файл перезаписывается целиком).
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "messages"
OUT = ROOT / "data" / "processed" / "participation.json"

MSG = re.compile(r'<div class="message" data-id="\d+"(.*?)'
                 r'(?=<div class="message" data-id="|\Z)', re.S)
MINE = re.compile(r'<div class="message__header">Вы,')
YEAR = re.compile(r", \d{1,2} \w{3} (\d{4}) в")

MIN_OWN = 30      # пороги «реального участия»
MIN_SHARE = 0.02


def main() -> None:
    names = dict(re.findall(
        r'href="(-?\d+)/messages0\.html"[^>]*>([^<]+)',
        (RAW / "index-messages.html").read_bytes().decode("cp1251")))
    result = {}
    for peer_dir in sorted(RAW.iterdir()):
        if not peer_dir.is_dir():
            continue
        pid = peer_dir.name
        total = mine = voice = voice_mine = 0
        years = set()
        for page in peer_dir.glob("messages*.html"):
            text = page.read_bytes().decode("cp1251", errors="replace")
            for m in MSG.finditer(text):
                body = m.group(1)
                total += 1
                is_mine = bool(MINE.search(body))
                mine += is_mine
                if "/amsg/" in body:
                    voice += 1
                    voice_mine += is_mine
                y = YEAR.search(body)
                if y:
                    years.add(int(y.group(1)))
        share = mine / total if total else 0.0
        kind = ("chat" if pid.startswith("2000000")
                else "community" if pid.startswith("-") else "user")
        result[pid] = {
            "name": names.get(pid, pid),
            "kind": kind,
            "total": total,
            "mine": mine,
            "mine_share": round(share, 4),
            "voice": voice,
            "voice_mine": voice_mine,
            "years": sorted(years),
            "useless": mine < MIN_OWN and share < MIN_SHARE,
        }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    rows = sorted(result.items(), key=lambda kv: -kv[1]["total"])
    useless = [r for r in rows if r[1]["useless"]]
    print(f"диалогов: {len(rows)}, из них useless: {len(useless)}", file=sys.stderr)
    print(f"отчёт: {OUT}", file=sys.stderr)
    print("\nГрупповые чаты (по объёму):")
    for _pid, r in rows:
        if r["kind"] != "chat":
            continue
        mark = "✗ useless" if r["useless"] else "✓"
        print(f'  {mark:9s} {r["name"][:32]:32s} всего {r["total"]:6d} | '
              f'моих {r["mine"]:6d} ({r["mine_share"]:5.1%}) | '
              f'ГС {r["voice"]:4d} (моих {r["voice_mine"]})')


if __name__ == "__main__":
    main()
