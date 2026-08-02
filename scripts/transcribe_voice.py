#!/usr/bin/env python3
"""Транскрипция голосовых VK с контекстными промптами.

Пайплайн на каждый батч голосовых (соседние ГС одного диалога):
  1. контекст ±N сообщений из HTML переписки → contexts/*.json
  2. Sonnet (claude -p) пишет initial_prompt для Whisper → batches/*.json
  3. whisper (transcribe.sh) с промптом батча + обрывками соседних реплик
     → transcripts/<peer>-<msgid>.txt + .meta.json
  4. каждый шаг — в run.log.jsonl (append-only)

Resume-safe: готовые транскрипты/промпты пропускаются. Запуск:
  python3 scripts/transcribe_voice.py --peer 123456789 --limit 15 [--order newest]
"""
import argparse
import datetime as dt
import hashlib
import html as html_mod
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OWN_NAME                          # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "messages"
VOICE_DIR = ROOT / "data" / "media" / "voice"
MANIFEST = ROOT / "data" / "processed" / "media_manifest.jsonl"
OUT = ROOT / "data" / "processed" / "transcription"
CTX_DIR = OUT / "contexts"
BATCH_DIR = OUT / "batches"
TRANS_DIR = OUT / "transcripts"
RUN_LOG = OUT / "run.log.jsonl"

VOCAB_DIR = OUT / "vocab"
PAUSE_FLAG = OUT / "PAUSE"   # touch → пауза после текущего файла; rm → дальше
STOP_FLAG = OUT / "STOP"     # touch → мягкая остановка после текущего файла
MAX_ATTEMPTS = 3             # столько раз пробуем сбойный файл, потом пропускаем

# Версия настроек транскрипции. Пишется в мету каждого файла, чтобы потом было
# видно, что чем сделано, и можно было прицельно перепрогнать старое.
# Историю версий и границы по времени см. в docs/transcription-settings.md
SETTINGS_VERSION = "v5-boilerplate-filter"

# --- защита машины от перегрева и исчерпания памяти -------------------------
# whisper-large-v3-mlx держит буферы Metal в общей памяти и чистит их только
# при выгрузке модели (по idle-таймауту). При непрерывном прогоне сервис
# никогда не простаивает → память растёт, пока macOS не убьёт всё подряд
# (проверено 31.07.2026: 36 ГБ съедено за ~220 файлов, force quit).
#
# ВАЖНО: расход MLX не виден в RSS! Замерено 31.07: у сервиса footprint 18 ГБ
# при RSS 0,67 ГБ (ps). Поэтому сторожим системные метрики — свободную память
# и размер компрессора; они растут вместе с утечкой и стоят копейки.
# Ярлык launchd-сервиса whisper. Сервис к этому репозиторию не относится:
# он ставится отдельно (см. README, раздел про транскрипцию) и здесь только
# перезапускается, когда MLX течёт. Другое имя — через переменную окружения.
WHISPER_LABEL = os.environ.get("VK_WHISPER_LABEL", "com.local.whisper-service")
FREE_MEM_FLOOR_PCT = 30       # свободно меньше → перезапуск сервиса
# Порог по компрессору — ОТНОСИТЕЛЬНЫЙ: базовый уровень зависит от того, что
# ещё запущено (ночью ~3 ГБ, днём с Docker/Chrome/VS Code ~7 ГБ). Абсолютный
# порог днём срабатывал вхолостую каждые пару файлов.
COMPRESSED_GROWTH_GB = 5.0    # рост над базовым уровнем → перезапуск
_baseline_compressed = None   # замеряется при старте прогона
LONG_FILE_S = 600             # длинный файл: чистим память до и после него
# Замерено 31.07: утечка ~2 ГБ на файл, а на одном длинном — сразу +9 ГБ.
# Отсюда частые перезапуски. Корневое лечение — clear_cache() после каждого
# задания в самом local-whisper; тогда пороги можно вернуть к мягким.

# Обёртка над локальным whisper. НЕ входит в репозиторий — это внешняя
# зависимость: скрипт, который принимает .ogg и отдаёт .txt (см. README).
# Путь по умолчанию — тот, по которому она лежит у автора; свой задавайте
# через VK_TRANSCRIBE_SH, иначе транскрипция упадёт на первом же файле.
TRANSCRIBE_SH = os.environ.get(
    "VK_TRANSCRIBE_SH", os.path.expanduser("~/.claude/scripts/transcribe.sh"))
CTX_SPAN = 8          # сообщений контекста в каждую сторону от батча
CLUSTER_GAP = 12      # макс. расстояние (в сообщениях) внутри батча
BATCH_CAP = 8         # макс. ГС в одном батче
VOCAB_EXCERPT_CAP = 8000   # сколько символов словаря отдаём Sonnet

SONNET_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "new_terms": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["prompt", "new_terms"],
})


class StopRun(Exception):
    """Мягкая остановка по STOP-флагу."""


def transcript_done(name: str) -> bool:
    """Готов = есть и .txt, и .meta.json (защита от обвала между записями)."""
    return ((TRANS_DIR / f"{name}.txt").exists()
            and (TRANS_DIR / f"{name}.meta.json").exists())


TYPO_FIX = {"ето": "это", "етому": "этому", "карашо": "хорошо", "довай": "давай",
            "лоол": "лол", "ниче": "ничего", "щас": "сейчас", "че": "чё"}


def clean_snippet(text: str) -> str:
    """Готовит соседнюю реплику для initial_prompt.

    Whisper копирует орфографию промпта, поэтому эмодзи, смайлы-каомодзи и
    намеренные искажения из чата («Ето так здорово ^^'») убираем — иначе они
    протекают в транскрипт.
    """
    text = re.sub(r"[^\w\s.,!?—-]", " ", text, flags=re.UNICODE)
    # Пунктуацию отделяем перед заменой: иначе «карашо!!» мимо словаря
    # опечаток — и искажение всё равно утекает в промпт.
    words = []
    for w in text.split():
        core = w.strip(".,!?—-")
        tail = w[len(core):] if core else w
        words.append(TYPO_FIX.get(core.lower(), core) + tail if core else w)
    return re.sub(r"\s+", " ", " ".join(words)).strip()


# Смех и слова-паразиты в живой речи повторяются подряд совершенно нормально
# («ха-ха-ха-ха», «вот вот вот эти штуки») — по ним срыв не судим.
FILLERS = {"ха", "хах", "хе", "хи", "хо", "ах", "ох", "ой", "эх", "ня", "не",
           "нет", "да", "вот", "ну", "а", "и", "то", "так", "эм", "м", "мм"}


def repetition_stats(text: str) -> tuple:
    """(макс. повтор подряд, какое слово, доля этого повтора в тексте)."""
    words = re.findall(r"\w+", text.lower())
    if not words:
        return 0, "", 0.0
    best = run = 1
    prev = None
    word = words[0]
    for w in words:
        run = run + 1 if w == prev else 1
        if run > best:
            best, word = run, w
        prev = w
    return best, word, best / len(words)


def is_suspect(text: str) -> bool:
    """Настоящий срыв Whisper в повтор, а не эмоциональная речь.

    Проверено на корпусе: живая речь даёт до 6 повторов подряд — так звучат
    ругательства в сердцах и оклик по имени. Настоящие срывы — 9 повторов
    и больше («недель» ×9, «dee» ×37) либо заметная доля всего текста.
    """
    best, word, share = repetition_stats(text)
    if best >= 15:
        return True          # даже «ну» никто не говорит 43 раза подряд
    if word in FILLERS:
        return False
    if best >= 7:
        return True
    # Доля считается только на длинных текстах: в короткой реплике
    # (трижды окликнули по имени) повтор занимает почти весь текст законно.
    return best >= 4 and share > 0.3 and len(re.findall(r"\w+", text)) >= 30


def prior_attempts(name: str) -> int:
    f = TRANS_DIR / f"{name}.meta.json"
    if not f.exists():
        return 0
    try:
        return int(json.loads(f.read_text(encoding="utf-8")).get("attempts", 1))
    except (json.JSONDecodeError, ValueError, OSError):
        return 1


def attempts_exhausted(name: str) -> bool:
    """Файл падал MAX_ATTEMPTS раз — не мучаем его при каждом перезапуске."""
    return not transcript_done(name) and prior_attempts(name) >= MAX_ATTEMPTS


def check_flags() -> None:
    """Вызывается перед каждым файлом: STOP → выходим, PAUSE → ждём."""
    if STOP_FLAG.exists():
        raise StopRun
    if PAUSE_FLAG.exists():
        print("⏸ пауза (rm data/processed/transcription/PAUSE — продолжить)",
              flush=True)
        log("pause", "run", True, "paused")
        while PAUSE_FLAG.exists():
            if STOP_FLAG.exists():
                raise StopRun
            time.sleep(10)
        print("▶ продолжаем", flush=True)


def log(stage: str, key: str, ok: bool, info: str = "") -> None:
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "stage": stage, "key": key, "ok": ok, "info": info,
        }, ensure_ascii=False) + "\n")


def ogg_duration(path: Path) -> float:
    size = path.stat().st_size
    with path.open("rb") as f:
        f.seek(max(0, size - 65536))
        tail = f.read()
    i = tail.rfind(b"OggS")
    if i < 0 or i + 14 > len(tail):
        return 0.0
    return struct.unpack("<q", tail[i + 6:i + 14])[0] / 48000.0


def peer_names() -> dict:
    text = (RAW / "index-messages.html").read_bytes().decode("cp1251")
    return dict(re.findall(r'href="(-?\d+)/messages0\.html"[^>]*>([^<]+)', text))


def parse_page(path: Path) -> list:
    """Сообщения страницы в хронологическом порядке."""
    text = path.read_bytes().decode("cp1251", errors="replace")
    parts = re.split(r'<div class="message" data-id="(\d+)"', text)
    msgs = []
    for i in range(1, len(parts) - 1, 2):
        mid, body = parts[i], parts[i + 1]
        hdr = re.search(r'<div class="message__header">(?:<a[^>]*>)?([^<,]+)', body)
        date = re.search(r", (\d{1,2} \w{3} \d{4} в \d{1,2}:\d{2}:\d{2})", body)
        # у сообщений без вложений блока kludges нет — ограничиваем текст любым
        # из возможных стопов, иначе теряется 11% сообщений (проверено 31.07)
        head_end = body.find("</div>")
        rest = body[head_end + 6:] if head_end >= 0 else body
        for stop in ('<div class="kludges">', '<div class="pagination',
                     '<div class="item">'):
            i = rest.find(stop)
            if i >= 0:
                rest = rest[:i]
        txt = html_mod.unescape(re.sub(r"<[^>]+>", " ", rest))
        txt = re.sub(r"\s+", " ", txt).strip()
        msgs.append({
            "id": mid,
            "sender": hdr.group(1).strip() if hdr else "?",
            "date": date.group(1) if date else "",
            "text": txt,
            "voice": "/amsg/" in body,
        })
    msgs.reverse()
    return msgs


def render_context(msgs: list, lo: int, hi: int) -> str:
    lines = []
    for m in msgs[max(0, lo):hi]:
        body = "🎤 [голосовое сообщение]" if m["voice"] and not m["text"] else m["text"][:120]
        if not body:
            continue
        lines.append(f'{m["sender"]}: {body}')
    return "\n".join(lines[-60:])


def vocab_path(peer: str) -> Path:
    return VOCAB_DIR / f"{peer}.md"


def load_vocab(peer: str) -> str:
    p = vocab_path(peer)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def load_periods(peer: str) -> dict:
    """vocab/<peer>-periods.md → {год: {desc, prompt}}.

    Формат файла: секции `### N. Период YYYY года ...` с описанием тем и
    ```text-блоком готового промпта Whisper (см. <peer_id>-periods.md).
    """
    p = VOCAB_DIR / f"{peer}-periods.md"
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    periods = {}
    for m in re.finditer(r"###[^\n]*?(\d{4}) год[^\n]*\n(.*?)(?=\n###|\Z)",
                         text, re.S):
        year, body = int(m.group(1)), m.group(2)
        block = re.search(r"```text\n(?:Промпт[^\n]*\n)?(.*?)```", body, re.S)
        periods[year] = {
            "desc": re.sub(r"```text.*?```", "", body, flags=re.S).strip()[:2500],
            "prompt": " ".join(block.group(1).split()) if block else "",
        }
    return periods


def period_for(periods: dict, date: str) -> dict:
    """Ближайший период ≤ года сообщения (или самый ранний)."""
    if not periods:
        return {}
    m = re.search(r"(\d{4})", date)
    if not m:
        return {}
    year = int(m.group(1))
    ys = sorted(periods)
    best = max([y for y in ys if y <= year], default=ys[0])
    return periods[best]


def append_vocab(peer: str, terms: list, batch_key: str, date: str) -> list:
    """Дописывает новые термины в словарь диалога. Возвращает реально добавленные."""
    p = vocab_path(peer)
    existing = load_vocab(peer)
    if not existing:
        existing = f"# Словарь переписки (peer {peer})\n"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(existing, encoding="utf-8")
    section = "## Автособранное (Sonnet)"
    low = existing.lower()
    added = []
    for t in terms:
        t = " ".join(t.split()).strip("-–— ").strip()
        term_key = re.split(r"\s*[—–-]\s*", t, maxsplit=1)[0].strip().lower()
        if not term_key or len(term_key) < 2 or term_key in low:
            continue
        added.append(f"- {t} *(встретилось: {batch_key}, {date})*")
        low += "\n" + term_key
    if added:
        with p.open("a", encoding="utf-8") as f:
            if section not in existing:
                f.write(f"\n{section}\n\n")
            f.write("\n".join(added) + "\n")
    return added


SLIM_CONFIG = os.path.expanduser("~/.claude-slim")
_slim_broken = False


def run_claude(ask: str, model: str):
    """claude -p через слим-конфиг (если авторизован), иначе дефолтный.

    Возвращает (stdout, config_name) или (None, None).
    """
    global _slim_broken
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [claude, "-p", ask, "--model", model, "--strict-mcp-config",
           "--json-schema", SONNET_SCHEMA]
    variants = []
    if os.path.isdir(SLIM_CONFIG) and not _slim_broken:
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = SLIM_CONFIG
        variants.append(("slim", env))
    variants.append(("default", None))
    for name, env in variants:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=240, env=env)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0:
            return r.stdout, name
        if name == "slim":
            _slim_broken = True
            log("config", "slim-fallback", False, (r.stderr or r.stdout)[:200])
    return None, None


def fallback_prompt(peer_name: str, period: dict) -> str:
    if period.get("prompt"):
        return period["prompt"][:300]
    return (f"Голосовое из переписки ВКонтакте {OWN_NAME} с «{peer_name}». "
            "Разговорная русская речь, сленг, бытовые темы.")


def call_sonnet(context: str, peer_name: str, batch_date: str, vocab: str,
                period: dict, model: str) -> tuple:
    """Возвращает (prompt, new_terms, source): source = <model>/<config> | fallback."""
    # Статичные части (инструкция + словарь + период) идут первыми — стабильный
    # префикс даёт prompt-cache-хиты между вызовами в пределах 1 ч TTL.
    # Период меняется редко (батчи идут хронологически), словарь снапшотится.
    vocab_part = (
        "Известный словарь специфичных терминов этой переписки:\n"
        + vocab[:VOCAB_EXCERPT_CAP]
        if vocab else "Словарь переписки пока пуст."
    )
    period_part = (
        "Описание периода общения, в который попадает фрагмент:\n"
        + period["desc"] + "\n\n" if period.get("desc") else ""
    )
    ask = (
        "Ты готовишь initial_prompt для Whisper-транскрипции голосовых из "
        f"переписки ВКонтакте {OWN_NAME} с «{peer_name}».\n"
        "Верни JSON:\n"
        "- prompt: ОДНА строка до 200 символов — словарная подсказка для Whisper: "
        "имена и термины, которые могут прозвучать именно в голосовых из "
        "фрагмента ниже (бери из контекста + подходящие по теме и периоду "
        "из словаря и описания периода). ВАЖНО: только правильная орфография! "
        "В переписке много намеренных искажений («ето», «карашо», «довай», "
        "«лоол») — их писать НЕЛЬЗЯ, Whisper копирует орфографию промпта и "
        "начинает коверкать текст. Пиши слова так, как они должны выглядеть "
        "в транскрипте.\n"
        "- new_terms: новые специфичные термины/имена/сленг из фрагмента, которых "
        "ещё НЕТ в словаре, формат «термин — краткое пояснение». Пустой список, "
        "если таких нет.\n\n"
        f"{vocab_part}\n\n"
        f"{period_part}"
        f"Фрагмент переписки от {batch_date} (🎤 — голосовые, которые будем "
        f"расшифровывать):\n{context}"
    )
    for _attempt in (1, 2):
        out, cfg = run_claude(ask, model)
        if out is None:
            continue
        try:
            data = json.loads(out.strip())
            prompt = " ".join(data["prompt"].split()).strip('"«»')
            if len(prompt) >= 20:
                # Схему навязывает --json-schema, но полагаться на неё одну
                # нельзя: если модель вернёт new_terms строкой, list() молча
                # разберёт её на буквы, и в словарь уедет мусор посимвольно.
                raw_terms = data.get("new_terms") or []
                terms = ([t for t in raw_terms if isinstance(t, str)]
                         if isinstance(raw_terms, list) else [])
                return prompt[:250], terms, f"{model}/{cfg}"
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return fallback_prompt(peer_name, period), [], "fallback"


def free_mem_pct() -> int:
    r = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True)
    m = re.search(r"free percentage:\s*(\d+)", r.stdout)
    return int(m.group(1)) if m else 100


def compressed_gb() -> float:
    """Размер компрессора памяти — растёт вместе с утечкой MLX."""
    r = subprocess.run(["vm_stat"], capture_output=True, text=True)
    m = re.search(r"occupied by compressor:\s*(\d+)", r.stdout)
    return int(m.group(1)) * 16384 / 1073741824 if m else 0.0


def swap_used_gb() -> float:
    r = subprocess.run(["sysctl", "-n", "vm.swapusage"],
                       capture_output=True, text=True)
    m = re.search(r"used\s*=\s*([\d.]+)M", r.stdout)
    return float(m.group(1)) / 1024 if m else 0.0


def service_footprint_gb() -> float:
    """Реальный расход памяти сервиса (top MEM). Дорого (~1-2 с) — для отчётов."""
    r = subprocess.run(["pgrep", "-f", "whisper_service.main"],
                       capture_output=True, text=True)
    total = 0.0
    for pid in r.stdout.split():
        try:
            t = subprocess.run(["top", "-l", "1", "-pid", pid, "-stats", "mem"],
                               capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            continue
        m = re.findall(r"^\s*([\d.]+)([KMG])\s*$", t.stdout, re.M)
        if m:
            val, unit = m[-1]
            total += float(val) * {"K": 1 / 1048576, "M": 1 / 1024, "G": 1.0}[unit]
    return total


def restart_service(reason: str) -> None:
    """Перезапуск whisper-сервиса — единственный способ вернуть память MLX.

    Сервис поднимется сам: transcribe.sh делает launchctl start, если он лёг.
    """
    print(f"  ↻ перезапуск whisper-сервиса: {reason}", flush=True)
    log("service", "restart", True, reason)
    subprocess.run(["launchctl", "stop", WHISPER_LABEL], capture_output=True)
    time.sleep(8)


def memory_guard(force_reason: str = "") -> None:
    """Следит, чтобы сервис не съел машину. Ждёт, если памяти мало."""
    global _baseline_compressed
    free, comp = free_mem_pct(), compressed_gb()
    if _baseline_compressed is None:
        _baseline_compressed = comp
        print(f"  базовый уровень компрессора: {comp:.1f} ГБ "
              f"(тревога от {comp + COMPRESSED_GROWTH_GB:.1f} ГБ)", flush=True)
    reason = force_reason
    if not reason and free < FREE_MEM_FLOOR_PCT:
        reason = f"свободно всего {free}% памяти"
    if not reason and comp > _baseline_compressed + COMPRESSED_GROWTH_GB:
        reason = (f"компрессор вырос до {comp:.1f} ГБ "
                  f"(база {_baseline_compressed:.1f})")
    if not reason:
        return
    restart_service(f"{reason} (свободно {free}%, компрессор {comp:.1f} ГБ, "
                    f"swap {swap_used_gb():.1f} ГБ)")
    for _ in range(30):                       # ждём, пока память вернётся
        if free_mem_pct() >= FREE_MEM_FLOOR_PCT:
            break
        print(f"  ⏳ ждём освобождения памяти (свободно {free_mem_pct()}%)",
              flush=True)
        time.sleep(20)
    # после чистки уровень мог осесть — пересчитываем базу, чтобы не зациклиться
    _baseline_compressed = min(_baseline_compressed, compressed_gb())


def whisper_timeout(duration: float) -> int:
    return max(180, int(duration * 3) + 90)


def whisper(ogg: Path, prompt: str, name: str, duration: float) -> tuple:
    """(ok, info, wall_s). Время меряется только вокруг самого вызова."""
    t0 = time.monotonic()
    r = subprocess.run(
        [TRANSCRIBE_SH, str(ogg), "--prompt", prompt, "--language", "ru",
         "--formats", "txt", "--output-dir", str(TRANS_DIR), "--name", name],
        capture_output=True, text=True, timeout=whisper_timeout(duration),
    )
    wall = time.monotonic() - t0
    txt_file = TRANS_DIR / f"{name}.txt"
    if r.returncode != 0 or not txt_file.exists():
        return False, (r.stderr or r.stdout)[-300:], wall
    return True, txt_file.read_text(encoding="utf-8").strip(), wall


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = все")
    ap.add_argument("--order", choices=["newest", "oldest"], default="newest")
    ap.add_argument("--model", default="sonnet",
                    help="модель для промптов батчей (sonnet | haiku)")
    ap.add_argument("--prompt-mode", choices=["sonnet", "period"],
                    default="sonnet",
                    help="period = без LLM: готовый промпт периода + реплики рядом")
    ap.add_argument("--sleep", type=float, default=3.0,
                    help="пауза между файлами, с (снижает нагрев; 0 = без паузы)")
    ap.add_argument("--audio-budget", type=float, default=1800.0,
                    help="секунд аудио между профилактическими чистками памяти "
                         "сервиса. После фикса clear_cache() в local-whisper "
                         "утечки нет, поэтому профилактика редкая — а от сюрпризов "
                         "страхуют пороги по свободной памяти и компрессору")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for d in (CTX_DIR, BATCH_DIR, TRANS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    peer_name = peer_names().get(args.peer, args.peer)

    # голосовые этого диалога из манифеста
    voice = []
    with MANIFEST.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec["kind"] == "voice" and rec["peer_id"] == args.peer:
                p = VOICE_DIR / (hashlib.sha1(rec["url"].encode()).hexdigest()[:16] + ".ogg")
                if p.exists():
                    rec["local"] = p
                    voice.append(rec)
    voice.sort(key=lambda r: int(r["msg_id"]), reverse=(args.order == "newest"))
    todo = [r for r in voice
            if not transcript_done(f'{args.peer}-{r["msg_id"]}')
            and not attempts_exhausted(f'{args.peer}-{r["msg_id"]}')]
    if args.limit:
        todo = todo[:args.limit]
    print(f"диалог: {peer_name} | всего ГС скачано: {len(voice)} | в работу: {len(todo)}",
          flush=True)
    if not todo:
        return

    # страницы и позиции
    pages = {}
    pos = {}
    for rec in todo:
        page = RAW / rec["file"]
        if rec["file"] not in pages:
            pages[rec["file"]] = parse_page(page)
        msgs = pages[rec["file"]]
        idx = next((i for i, m in enumerate(msgs) if m["id"] == rec["msg_id"]), None)
        if idx is None:
            log("locate", rec["msg_id"], False, "msg not found in page")
            continue
        pos[rec["msg_id"]] = (rec["file"], idx)

    # кластеризация в батчи: одна страница, близкие позиции
    by_page = {}
    for rec in todo:
        if rec["msg_id"] in pos:
            by_page.setdefault(pos[rec["msg_id"]][0], []).append(rec)
    batches = []
    for page_file, recs in by_page.items():
        recs.sort(key=lambda r: pos[r["msg_id"]][1])
        cur = [recs[0]]
        for r in recs[1:]:
            near = pos[r["msg_id"]][1] - pos[cur[-1]["msg_id"]][1] <= CLUSTER_GAP
            if near and len(cur) < BATCH_CAP:
                cur.append(r)
            else:
                batches.append((page_file, cur))
                cur = [r]
        batches.append((page_file, cur))

    # словарь снапшотится на весь прогон: стабильный префикс промпта → кэш-хиты;
    # новые термины пишутся в файл и подхватятся следующим запуском
    vocab_snapshot = load_vocab(args.peer)
    periods = load_periods(args.peer)
    if periods:
        print(f"периоды из словаря: {sorted(periods)}", flush=True)

    def batch_meta(bi):
        page_file, recs = batches[bi]
        msgs = pages[page_file]
        lo = pos[recs[0]["msg_id"]][1] - CTX_SPAN
        hi = pos[recs[-1]["msg_id"]][1] + CTX_SPAN + 1
        context = render_context(msgs, lo, hi)
        bkey = f'{args.peer}-{recs[0]["msg_id"]}-{recs[-1]["msg_id"]}'
        batch_date = next(
            (msgs[pos[r["msg_id"]][1]]["date"] for r in recs
             if msgs[pos[r["msg_id"]][1]]["date"]), "?")
        return page_file, recs, context, bkey, batch_date

    if args.dry_run:
        for bi in range(len(batches)):
            _, recs, context, bkey, _ = batch_meta(bi)
            print(f"[dry] батч {bkey}: {len(recs)} ГС\n{context}\n", flush=True)
        return

    def prepare(bi):
        """Промпт батча: с диска, от Sonnet или из периода. На шаг впереди whisper."""
        page_file, recs, context, bkey, batch_date = batch_meta(bi)
        bfile = BATCH_DIR / f"batch-{bkey}.json"
        if bfile.exists():
            return json.loads(bfile.read_text(encoding="utf-8"))
        period = period_for(periods, batch_date)
        if args.prompt_mode == "period":
            prompt, new_terms, source = fallback_prompt(peer_name, period), [], "period"
        else:
            prompt, new_terms, source = call_sonnet(
                context, peer_name, batch_date, vocab_snapshot, period, args.model)
        added = append_vocab(args.peer, new_terms, bkey, batch_date)
        batch = {"key": bkey, "page": page_file, "peer_name": peer_name,
                 "msg_ids": [r["msg_id"] for r in recs], "date": batch_date,
                 "context": context, "prompt": prompt, "prompt_source": source,
                 "new_terms": new_terms, "vocab_added": len(added)}
        bfile.write_text(json.dumps(batch, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        log("sonnet", bkey, True,
            f"{source}: {prompt[:60]} | +{len(added)} терминов")
        print(f"батч {bkey} ({len(recs)} ГС, промпт {source}, "
              f"+{len(added)} терминов в словарь): {batch['prompt']}", flush=True)
        return batch

    if STOP_FLAG.exists():          # затерявшийся флаг с прошлого запуска
        STOP_FLAG.unlink()
    done = fail = 0
    since_restart = 0
    stopped = False
    memory_guard("старт прогона — начинаем с чистой памяти")
    prefetch = ThreadPoolExecutor(max_workers=1)
    fut = prefetch.submit(prepare, 0)
    try:
      for bi, (page_file, recs) in enumerate(batches):
        batch = fut.result()
        if bi + 1 < len(batches):
            fut = prefetch.submit(prepare, bi + 1)
        msgs = pages[page_file]

        for rec in recs:
            name = f'{args.peer}-{rec["msg_id"]}'
            if transcript_done(name) or attempts_exhausted(name):
                continue
            check_flags()
            _, idx = pos[rec["msg_id"]]
            snips = [s for s in (clean_snippet(m["text"][:60])
                                 for m in msgs[max(0, idx - 2):idx + 3]
                                 if m["text"]) if len(s) > 3][:3]
            wprompt = (batch["prompt"] + " Реплики рядом: " + " | ".join(snips))[:320]
            ctx_rec = {"msg_id": rec["msg_id"], "page": page_file, "idx": idx,
                       "date": msgs[idx]["date"], "sender": msgs[idx]["sender"],
                       "whisper_prompt": wprompt}
            (CTX_DIR / f"ctx-{name}.json").write_text(
                json.dumps(ctx_rec, ensure_ascii=False, indent=1), encoding="utf-8")

            duration = ogg_duration(rec["local"])
            # профилактика: раз в N файлов, перед длинным файлом и по факту
            # нехватки памяти — сервис перезапускается, MLX-буферы освобождаются
            # утечка пропорциональна длительности аудио, поэтому бюджет
            # считаем в секундах звука, а не в файлах
            if since_restart + duration > args.audio_budget:
                memory_guard(f"обработано {since_restart:.0f} с звука с прошлой "
                             f"чистки, дальше файл на {duration:.0f} с")
                since_restart = 0
            else:
                memory_guard()
            since_restart += duration
            try:
                ok, info, wall = whisper(rec["local"], wprompt, name, duration)
            except subprocess.TimeoutExpired:
                ok, info, wall = False, "whisper timeout", whisper_timeout(duration)
            rep, _, _ = repetition_stats(info) if ok else (0, "", 0.0)
            meta = {**ctx_rec, "ogg": rec["local"].name, "duration_s": round(duration, 1),
                    "wall_s": round(wall, 1), "ok": ok,
                    "attempts": prior_attempts(name) + 1,
                    "max_repeat": rep, "suspect": ok and is_suspect(info),
                    "settings": SETTINGS_VERSION,
                    "batch": batch["key"], "error": None if ok else info}
            (TRANS_DIR / f"{name}.meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            log("whisper", name, ok, f"{duration:.0f}s/{wall:.0f}s")
            done += ok
            fail += not ok
            mark = "✓" if ok else "✗"
            preview = info[:90] if ok else f"ОШИБКА: {info[:90]}"
            print(f"  {mark} {name} [{msgs[idx]['date']}] {duration:.0f}с/{wall:.0f}с: "
                  f"{preview}", flush=True)
            if duration >= LONG_FILE_S:       # после длинного — сразу прибраться
                memory_guard(f"после длинного файла {duration/60:.1f} мин")
                since_restart = 0
            if args.sleep:                    # передышка для процессора
                time.sleep(args.sleep)

    except StopRun:
        stopped = True
        STOP_FLAG.unlink(missing_ok=True)
        print("⏹ остановлено по STOP-флагу; продолжить — просто перезапустить "
              "ту же команду", flush=True)
        log("stop", "run", True, "graceful stop")
    prefetch.shutdown(wait=False)
    print(f"итого: ok={done} fail={fail}"
          + (" (остановлено досрочно)" if stopped else ""), flush=True)
    sys.exit(1 if (fail and not stopped) else 0)


if __name__ == "__main__":
    main()
