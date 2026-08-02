#!/usr/bin/env python3
"""Очередь транскрипции по диалогам: гоняет сторожа по списку peer_id подряд.

  python3 scripts/build_queue.py                 # собрать очередь (какие диалоги)
  nohup python3 scripts/run_queue.py &           # прогнать очередь

Один диалог = один запуск watchdog_transcribe.py (он сам порциями рестартует
транскрипцию и следит за памятью). Очередь добавляет поверх него:
  • порядок и учёт: queue.json — что взято, что сделано, где остановились
  • контроль качества после каждого диалога: доля срывов/заготовок/пустых.
    Больше QUALITY_FAIL_PCT на выборке от QUALITY_MIN_FILES — очередь встаёт,
    потому что дальше гнать плохие расшифровки бессмысленно
  • синхронизацию с БД после каждого диалога
  • те же PAUSE / STOP флаги, что и у остальных частей пайплайна

Resume-safe целиком: состояние — в queue.json, готовые транскрипты на диске
пропускаются. Упало / перезагрузили ноут — просто запусти снова.
"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "processed" / "transcription"
TRANS_DIR = OUT / "transcripts"
QUEUE = OUT / "queue.json"
LOG = OUT / "queue.log"
PAUSE_FLAG = OUT / "PAUSE"
STOP_FLAG = OUT / "STOP"

QUALITY_FAIL_PCT = 5.0    # доля брака, при которой очередь останавливается
QUALITY_MIN_FILES = 20    # ...но только если выборка достаточная

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_bad_transcripts import boilerplate_hits          # noqa: E402
from transcribe_voice import is_suspect                    # noqa: E402


def say(msg: str) -> None:
    line = f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_queue() -> dict:
    if not QUEUE.exists():
        sys.exit("нет queue.json — сначала собери очередь: "
                 "python3 scripts/build_queue.py")
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def save_queue(q: dict) -> None:
    tmp = QUEUE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(QUEUE)


def quality(peer: str) -> dict:
    """Качество расшифровок диалога: сколько срывов, заготовок, пустых."""
    total = suspect = boiler = empty = 0
    for txt in TRANS_DIR.glob(f"{peer}-*.txt"):
        total += 1
        text = txt.read_text(encoding="utf-8").strip()
        if not text:
            empty += 1
            continue
        if is_suspect(text):
            suspect += 1
        if boilerplate_hits(text):
            boiler += 1
    bad = suspect + boiler + empty
    return {"total": total, "suspect": suspect, "boilerplate": boiler,
            "empty": empty, "bad": bad,
            "bad_pct": round(100 * bad / total, 1) if total else 0.0}


def sync_db() -> None:
    venv_py = ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        return
    try:
        r = subprocess.run(
            [str(venv_py), str(ROOT / "scripts" / "db_import.py"), "transcription"],
            capture_output=True, text=True, timeout=900, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        say("импорт в БД: таймаут (данные на диске, не критично)")
        return
    tail = " ".join((r.stdout if r.returncode == 0 else r.stderr or r.stdout)
                    .split())[-160:]
    say(("импорт в БД: " if r.returncode == 0 else "импорт в БД не удался: ") + tail)


def main() -> int:
    q = load_queue()
    items = q["peers"]
    todo = [it for it in items if it.get("status") not in ("done", "skipped")]
    say(f"очередь: всего диалогов {len(items)}, к работе {len(todo)}, "
        f"голосовых осталось {sum(it['pending'] for it in todo)}")

    for it in items:
        if it.get("status") in ("done", "skipped"):
            continue
        if STOP_FLAG.exists():
            say("STOP-флаг — очередь останавливается")
            return 3
        while PAUSE_FLAG.exists():
            time.sleep(60)

        peer, name = it["peer"], it["name"]
        it["status"] = "running"
        it["started"] = datetime.now().isoformat(timespec="seconds")
        save_queue(q)
        say(f"▶ {name} (peer {peer}): {it['pending']} голосовых, "
            f"{it['minutes']:.0f} мин аудио")

        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "watchdog_transcribe.py"),
             "--peer", peer, "--chunk", "100"], cwd=str(ROOT))

        sync_db()
        qual = quality(peer)
        it["quality"] = qual
        it["finished"] = datetime.now().isoformat(timespec="seconds")

        if r.returncode == 3:
            it["status"] = "stopped"
            save_queue(q)
            say("STOP-флаг во время диалога — очередь останавливается")
            return 3
        if r.returncode == 2:
            it["status"] = "alert"
            save_queue(q)
            say(f"⚠ ALERT на диалоге {name} — сторож не смог продвинуться. "
                f"Очередь встаёт, нужен разбор.")
            return 2

        it["status"] = "done"
        save_queue(q)
        say(f"✔ {name}: расшифровок {qual['total']}, брак {qual['bad']} "
            f"({qual['bad_pct']}%) — срывов {qual['suspect']}, "
            f"заготовок {qual['boilerplate']}, пустых {qual['empty']}")

        if (qual["total"] >= QUALITY_MIN_FILES
                and qual["bad_pct"] > QUALITY_FAIL_PCT):
            say(f"⚠ СТОП по качеству: брака {qual['bad_pct']}% "
                f"(порог {QUALITY_FAIL_PCT}%). Дальше не идём — сначала разбор.")
            return 4

    say("ОЧЕРЕДЬ ЗАВЕРШЕНА: " + ", ".join(
        f"{it['name']} {it.get('quality', {}).get('total', 0)}"
        for it in items if it.get("status") == "done"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
