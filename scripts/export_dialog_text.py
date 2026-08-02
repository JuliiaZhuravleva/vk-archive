#!/usr/bin/env python3
"""Выгрузка текста диалога за период — например, для NotebookLM.

  python3 scripts/export_dialog_text.py --peer 123456789 --from 2021-01 --to 2021-12

Пишет data/processed/exports/<peer>-<from>-<to>.txt: хронологический текст
«[дата] Имя: сообщение», голосовые помечены [голосовое]. Медиа и стикеры
опускаются. Файл предназначен для ручной загрузки во внешние инструменты —
помни, что это личные данные.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcribe_voice import parse_page, peer_names  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OWN_NAME                          # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "messages"
EXPORTS = ROOT / "data" / "processed" / "exports"

MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "июн": 6,
          "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}


def ym(date: str):
    """'27 дек 2018 в 0:58:19' → (2018, 12) | None."""
    m = re.match(r"(\d{1,2}) (\w{3}) (\d{4})", date)
    if not m or m.group(2) not in MONTHS:
        return None
    return int(m.group(3)), MONTHS[m.group(2)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", required=True)
    ap.add_argument("--from", dest="date_from", required=True, help="YYYY-MM")
    ap.add_argument("--to", dest="date_to", required=True, help="YYYY-MM")
    args = ap.parse_args()

    lo = tuple(int(x) for x in args.date_from.split("-"))
    hi = tuple(int(x) for x in args.date_to.split("-"))
    peer_dir = RAW / args.peer
    if not peer_dir.is_dir():
        sys.exit(f"нет диалога {args.peer}")
    name = peer_names().get(args.peer, args.peer)

    rows = []
    for page in peer_dir.glob("messages*.html"):
        for m in parse_page(page):
            d = ym(m["date"])
            if not d or not (lo <= d <= hi):
                continue
            body = m["text"] or ("[голосовое]" if m["voice"] else "")
            if not body:
                continue
            rows.append((d, int(m["id"]), f'[{m["date"]}] {m["sender"]}: {body}'))
    rows.sort(key=lambda r: r[1])

    EXPORTS.mkdir(parents=True, exist_ok=True)
    out = EXPORTS / f"{args.peer}-{args.date_from}-{args.date_to}.txt"
    header = (f"Переписка ВКонтакте: {OWN_NAME} — {name} (peer {args.peer}), "
              f"период {args.date_from}..{args.date_to}, "
              f"{len(rows)} сообщений\n\n")
    out.write_text(header + "\n".join(r[2] for r in rows) + "\n", encoding="utf-8")
    print(f"{out} ({len(rows)} сообщений, {out.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    main()
