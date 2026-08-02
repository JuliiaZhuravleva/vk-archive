#!/usr/bin/env python3
"""Сторож ночного прогона транскрипции: следит и перезапускает при падении.

  nohup python3 scripts/watchdog_transcribe.py --peer 123456789 &

Что делает раз в 60 с:
  • живой ли процесс транскрипции этого диалога → если да, ничего не трогает
  • PAUSE/STOP флаг → уважает волю пользователя, не перезапускает
  • процесс умер, а работа осталась → перезапускает (прогон resume-safe)
  • всё сделано → пишет итог и выходит

Защита от шторма: если N перезапусков подряд не дали ни одного нового
транскрипта — останавливается и пишет ALERT в лог (значит, ломается системно).
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "processed" / "transcription"
TRANS_DIR = OUT / "transcripts"
VOICE_DIR = ROOT / "data" / "media" / "voice"
MANIFEST = ROOT / "data" / "processed" / "media_manifest.jsonl"
WATCH_LOG = OUT / "watchdog.log"

# Коды выхода — их читает очередь (run_queue.py), чтобы отличить «диалог
# закончен» от «сломалось» и от «пользователь попросил остановиться».
EXIT_DONE, EXIT_ALERT, EXIT_STOP = 0, 2, 3

MAX_ATTEMPTS = 3
CHECK_EVERY = 60
STALL_LIMIT = 5          # перезапусков без прогресса подряд
MIN_FREE_PCT = 25        # ниже — не запускаем новую порцию, ждём


def say(msg: str) -> None:
    line = f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    with WATCH_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def voice_files(peer: str) -> list:
    out = []
    with MANIFEST.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec["kind"] == "voice" and rec["peer_id"] == peer:
                p = VOICE_DIR / (hashlib.sha1(rec["url"].encode())
                                 .hexdigest()[:16] + ".ogg")
                if p.exists():
                    out.append(rec["msg_id"])
    return out


def status(peer: str, ids: list) -> tuple:
    """(готово, исчерпано попыток, осталось)."""
    done = exhausted = 0
    for mid in ids:
        name = f"{peer}-{mid}"
        txt, meta = TRANS_DIR / f"{name}.txt", TRANS_DIR / f"{name}.meta.json"
        if txt.exists() and meta.exists():
            done += 1
        elif meta.exists():
            try:
                if int(json.loads(meta.read_text(encoding="utf-8"))
                       .get("attempts", 1)) >= MAX_ATTEMPTS:
                    exhausted += 1
            except (json.JSONDecodeError, ValueError, OSError):
                pass
    return done, exhausted, len(ids) - done - exhausted


def running(peer: str) -> bool:
    r = subprocess.run(["pgrep", "-f", f"transcribe_voice.py --peer {peer}"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def sync_db() -> None:
    """Догрузить свежие транскрипты в PostgreSQL. Необязательный шаг:

    база лежит / Docker не запущен — просто пишем в лог и работаем дальше,
    источник истины остаётся на диске.
    """
    venv_py = ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        return
    try:
        r = subprocess.run(
            [str(venv_py), str(ROOT / "scripts" / "db_import.py"), "transcription"],
            capture_output=True, text=True, timeout=600, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        say("импорт в БД: таймаут (не критично, данные на диске)")
        return
    if r.returncode == 0:
        say("импорт в БД: " + " ".join(r.stdout.split())[-160:])
    else:
        say("импорт в БД не удался (не критично): "
            + " ".join((r.stderr or r.stdout).split())[-160:])


def free_mem_pct() -> int:
    r = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True)
    m = re.search(r"free percentage:\s*(\d+)", r.stdout)
    return int(m.group(1)) if m else 100


def start(peer: str, chunk: int) -> None:
    """Порция файлов на один процесс: отработал — вышел, память вернулась.

    Без caffeinate: машине разрешено засыпать, прогон продолжится после
    пробуждения (он resume-safe).
    """
    with (OUT / f"run-{peer}.log").open("a", encoding="utf-8") as log:
        subprocess.Popen(
            ["nice", "-n", "5", sys.executable,
             str(ROOT / "scripts" / "transcribe_voice.py"),
             "--peer", peer, "--order", "newest", "--limit", str(chunk)],
            stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT),
            start_new_session=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", required=True)
    ap.add_argument("--chunk", type=int, default=100,
                    help="файлов на один процесс (порциями — память не копится)")
    args = ap.parse_args()

    ids = voice_files(args.peer)
    done0, exh0, left0 = status(args.peer, ids)
    say(f"сторож на посту: всего {len(ids)}, готово {done0}, осталось {left0}")

    restarts = 0
    stalled = 0
    last_done = done0
    while True:
        done, exh, left = status(args.peer, ids)
        if left == 0:
            sync_db()
            say(f"ЗАВЕРШЕНО: готово {done}, пропущено по лимиту попыток {exh}, "
                f"порций за ночь {restarts}")
            return EXIT_DONE
        if (OUT / "STOP").exists():
            say(f"STOP-флаг — сторож уходит (осталось {left})")
            return EXIT_STOP
        if (OUT / "PAUSE").exists():
            time.sleep(CHECK_EVERY)
            continue
        if not running(args.peer):
            free = free_mem_pct()
            if free < MIN_FREE_PCT:
                say(f"мало свободной памяти ({free}%) — жду, не запускаю")
                time.sleep(CHECK_EVERY)
                continue
            if done > last_done:
                stalled = 0
            else:
                stalled += 1
            if stalled >= STALL_LIMIT:
                say(f"ALERT: {stalled} перезапусков без прогресса, "
                    f"осталось {left}. Останавливаюсь — нужен разбор.")
                return EXIT_ALERT
            if restarts:              # порция закончилась — синхронизируем БД
                sync_db()
            restarts += 1
            say(f"порция #{restarts}: готово {done}, осталось {left}, "
                f"свободно памяти {free}%")
            start(args.peer, args.chunk)
            time.sleep(20)
        last_done = max(last_done, done)
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    sys.exit(main())
