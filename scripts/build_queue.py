#!/usr/bin/env python3
"""Собирает очередь транскрипции: какие диалоги берём в работу и в каком порядке.

  python3 scripts/build_queue.py                  # показать, что попадёт
  python3 scripts/build_queue.py --write          # записать queue.json

Кого берём по умолчанию:
  • личные переписки (kind=user), не помеченные бесполезными
  • беседы из --chat (по умолчанию ни одной: см. --chat)

Кого НЕ берём:
  • диалоги с флагом useless (своих сообщений нет — размечено
    analyze_participation.py)
  • беседы с сомнительным участием: сотни-тысячи чужих сообщений и десятки
    своих. Формально не useless, но расшифровывать там чужие голосовые
    смысла нет. Такие перечисляются в выводе — при желании добавляй --chat
  • сообщества

Порядок: сначала диалоги из --first (для них результат нужен раньше),
дальше по убыванию количества голосовых — крупные диалоги дают материал
раньше, мелкие подчищаются в конце.

Пересборка не теряет прогресс: статусы уже пройденных диалогов переносятся.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "processed" / "transcription"
QUEUE = OUT / "queue.json"
MANIFEST = ROOT / "data" / "processed" / "media_manifest.jsonl"
VOICE_DIR = ROOT / "data" / "media" / "voice"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcribe_voice import ogg_duration                  # noqa: E402

SQL = """
SELECT d.peer_id, d.kind, d.useless, d.name, d.own_share,
       count(a.id) FILTER (WHERE vt.attachment_id IS NULL) AS pending
FROM dialogs d
JOIN messages m ON m.peer_id = d.peer_id
JOIN attachments a ON a.message_id = m.id AND a.kind = 'voice'
LEFT JOIN voice_transcripts vt ON vt.attachment_id = a.id
GROUP BY 1, 2, 3, 4, 5
HAVING count(a.id) FILTER (WHERE vt.attachment_id IS NULL) > 0
ORDER BY pending DESC;
"""


def db_rows() -> list:
    r = subprocess.run(
        ["docker", "exec", "vk-archive-db", "psql", "-U", "vk", "-d",
         "vk_archive", "-t", "-A", "-F", "\t", "-c", SQL],
        capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("БД недоступна: " + (r.stderr or r.stdout).strip())
    rows = []
    for line in r.stdout.strip().split("\n"):
        if not line:
            continue
        peer, kind, useless, name, share, pending = line.split("\t")
        rows.append({"peer": peer, "kind": kind, "useless": useless == "t",
                     "name": name, "share": float(share or 0),
                     "pending": int(pending)})
    return rows


def audio_minutes(peers: set) -> dict:
    """Длительность скачанного аудио по диалогам — для оценки времени прогона."""
    secs = {p: 0.0 for p in peers}
    with MANIFEST.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec["kind"] != "voice" or rec["peer_id"] not in peers:
                continue
            p = VOICE_DIR / (hashlib.sha1(rec["url"].encode())
                             .hexdigest()[:16] + ".ogg")
            if p.exists():
                secs[rec["peer_id"]] += ogg_duration(p)
    return {p: s / 60 for p, s in secs.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    # Беседы по умолчанию не берём: в групповом чате своих реплик обычно
    # единицы процентов, и расшифровывать там нечего. Исключения задаются
    # руками — те беседы, где доля собственных сообщений заметная (на этом корпусе
    # были четыре чата с долей 27–43%). Найти такие помогает
    # analyze_participation.py; список держим вне репозитория, он про
    # конкретных людей.
    ap.add_argument("--chat", action="append", default=[],
                    help="peer_id беседы, которую тоже берём (можно повторять)")
    ap.add_argument("--first", action="append", default=[],
                    help="peer_id, которые идут в начало очереди")
    ap.add_argument("--write", action="store_true", help="записать queue.json")
    args = ap.parse_args()

    rows = db_rows()
    chats = set(args.chat)
    take, skip = [], []
    for r in rows:
        if r["peer"] in chats:
            take.append(r)
        elif r["useless"]:
            skip.append((r, "не участвовала (useless)"))
        elif r["kind"] == "community":
            skip.append((r, "сообщество"))
        elif r["kind"] == "chat":
            skip.append((r, f"беседа, участие {r['share'] * 100:.1f}%"))
        else:
            take.append(r)

    mins = audio_minutes({r["peer"] for r in take})
    first = args.first
    take.sort(key=lambda r: (first.index(r["peer"]) if r["peer"] in first
                             else len(first), -r["pending"]))

    prev = {}
    if QUEUE.exists():
        prev = {it["peer"]: it
                for it in json.loads(QUEUE.read_text(encoding="utf-8"))["peers"]}

    peers = []
    for r in take:
        it = {"peer": r["peer"], "name": r["name"], "pending": r["pending"],
              "minutes": round(mins.get(r["peer"], 0.0), 1), "status": "todo"}
        old = prev.get(r["peer"])
        if old and old.get("status") in ("done", "skipped"):
            it.update(status=old["status"], quality=old.get("quality"),
                      finished=old.get("finished"))
        peers.append(it)

    total_min = sum(it["minutes"] for it in peers if it["status"] == "todo")
    total_n = sum(it["pending"] for it in peers if it["status"] == "todo")
    print(f"В работу: {len(peers)} диалогов, {total_n} голосовых, "
          f"{total_min / 60:.1f} ч аудио")
    for it in peers:
        mark = {"done": "✔", "skipped": "–"}.get(it["status"], " ")
        print(f" {mark} {it['peer']:>12}  {it['pending']:>4} ГС  "
              f"{it['minutes']:6.1f} мин  {it['name']}")

    print(f"\nПропущено: {len(skip)} диалогов, "
          f"{sum(r['pending'] for r, _ in skip)} голосовых")
    for r, why in sorted(skip, key=lambda x: -x[0]["pending"]):
        print(f"   {r['peer']:>12}  {r['pending']:>4} ГС  {r['name']} — {why}")

    if not args.write:
        print("\nдля записи очереди: --write")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    QUEUE.write_text(json.dumps(
        {"created": datetime.now().isoformat(timespec="seconds"),
         "peers": peers}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nзаписано: {QUEUE}")


if __name__ == "__main__":
    main()
