#!/usr/bin/env python3
"""Извлекает URL всех вложений из messages/ в manifest JSONL.

Каждая строка: {kind, url, peer_id, msg_id, att_type, file}
kind: voice (psv4 .ogg) | photo (userapi jpg) | other
Дедупликация по URL (пересланные сообщения дублируют вложения).
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "messages"
OUT = ROOT / "data" / "processed" / "media_manifest.jsonl"

MSG_SPLIT = re.compile(r'<div class="message" data-id="(\d+)"')
ATT = re.compile(
    r'<div class="attachment">\s*'
    r'<div class="attachment__description">([^<]*)</div>\s*'
    r"(?:<a class='attachment__link' href='([^']+)')?",
    re.S,
)


def classify(att_type: str, url: str) -> str:
    if "/amsg/" in url and url.split("?")[0].endswith(".ogg"):
        return "voice"
    if att_type == "Фотография":
        return "photo"
    return "other"


def main() -> None:
    seen: set[str] = set()
    counts: dict[str, int] = {}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as out:
        for peer_dir in sorted(RAW.iterdir()):
            if not peer_dir.is_dir():
                continue
            for page in peer_dir.glob("messages*.html"):
                text = page.read_bytes().decode("cp1251", errors="replace")
                # режем на сообщения, чтобы знать msg_id каждого вложения
                parts = MSG_SPLIT.split(text)
                # parts = [prefix, id1, body1, id2, body2, ...]
                for i in range(1, len(parts) - 1, 2):
                    msg_id, body = parts[i], parts[i + 1]
                    for att_type, url in ATT.findall(body):
                        if not url or url in seen:
                            continue
                        seen.add(url)
                        kind = classify(att_type, url)
                        counts[kind] = counts.get(kind, 0) + 1
                        out.write(
                            json.dumps(
                                {
                                    "kind": kind,
                                    "url": url,
                                    "peer_id": peer_dir.name,
                                    "msg_id": msg_id,
                                    "att_type": att_type,
                                    "file": str(page.relative_to(RAW)),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
    print(f"manifest: {OUT}", file=sys.stderr)
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}", file=sys.stderr)


if __name__ == "__main__":
    main()
