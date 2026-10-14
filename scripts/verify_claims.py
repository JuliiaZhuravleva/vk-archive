#!/usr/bin/env python3
"""Проверка уровня 2: более сильная модель перечитывает основания черновых утверждений.

Необязательный шаг. Утверждения уровня 1 и без него живут и уточняются
попутно исследованиями (см. docs/claims.md); этот скрипт — способ поднять
целый прогон разом, когда это стоит денег, которые не жалко.

Утверждения диалога пачкой отдаются модели вместе с сообщениями-основаниями
и ±CONTEXT сообщений вокруг каждого. Модель — Claude через `claude -p`: без
инструментов, без MCP, со своим коротким системным промптом и ответом по
JSON-схеме. Вердикты пишутся в журнал claims с уровнем 2 тем же путём, что
и `claims.py revise`: журнал сначала сворачивается целиком, противоречие —
и не пишется ничего. Каждая пачка записывается сразу, так что оборванный
прогон не теряет оплаченного.

  python3 scripts/verify_claims.py --run local-2018-01 --limit 2       # проба
  python3 scripts/verify_claims.py --run local-2018-01 --max-cost 5
  # калибровка: прогнать по уже проверенным исследованием и сравнить, в журнал не пишет
  python3 scripts/verify_claims.py --run local-2011-02 --checked-only --out calib.jsonl
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claims as cl  # noqa: E402
from common import DSN, OWN_NAME  # noqa: E402

CONTEXT = 12
MAX_GROUP = 10
MAX_CHARS = 30000
MAX_MSG = 1200

SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "n": {"type": "integer"},
            "verdict": {"type": "string", "enum": list(cl.VERDICTS)},
            "text": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["n", "verdict", "note"]}}},
    "required": ["verdicts"],
}

SYSTEM = """Ты проверяешь черновые утверждения о жизни автора личного архива переписки \
ВКонтакте. Утверждения написала слабая модель: она хорошо находит, о чём шла речь, \
но путает детали. Твоя задача — сверить каждое утверждение с самими сообщениями. \
Ты не пишешь ничего от себя и не интерпретируешь психологически."""

PROMPT = """Строки «{own}:» — автор архива. В утверждениях автор может называться по имени.
Диалог: {dialog}.

Утверждения:
{claims}

Для каждого утверждения выбери вердикт по фрагментам переписки ниже:
- confirmed — фрагменты прямо подтверждают всё, включая кто что сделал или сказал;
- corrected — суть верна, но деталь неверна; в text дай исправленную редакцию в том же стиле, \
одно-два предложения, только то, что видно в сообщениях;
- refuted — главное утверждение противоречит сообщениям или в них нет ничего похожего;
- unclear — фрагментов недостаточно, чтобы решить.
Частые ошибки черновика: перепутано, кто что сказал или сделал; подозрение или слух подан как \
факт; план или намерение подано как свершившееся; добавлен мотив или чувство, которых в тексте нет; \
неверная дата. Найдёшь такую — это corrected (или refuted, если без неё утверждения не остаётся).
В note — одно предложение: почему, со ссылкой на номера сообщений (#…).

Фрагменты переписки (#номер, дата и время по Москве, кто):
{text}"""


def select(claims, args):
    lo = cl.parse_bound(args.date_from, False) if args.date_from else None
    hi = cl.parse_bound(args.date_to, True) if args.date_to else None
    out = []
    for c in claims.values():
        st = c["state"]
        if args.run and c["run"] not in args.run:
            continue
        if args.peer and c["peer_id"] not in args.peer:
            continue
        if not cl.overlaps(c, lo, hi):
            continue
        checked = st["level"] > 1 or st["status"] == "refuted"
        if (checked and not args.include_checked) or (args.checked_only and not checked):
            continue
        out.append(c)
    out.sort(key=lambda c: (c["peer_id"], c.get("period_from") or "", c["key"]))
    return out


def fetch_context(cur, peer, mids, k):
    """Сообщения-основания и по k вокруг каждого — одним упорядоченным списком.
    Голосовые — отдельным запросом: подзапрос на каждую строку сканировал
    расшифровки целиком (индекса по attachments.message_id нет)."""
    cur.execute("""
        WITH src AS (SELECT sent_at, vk_msg_id FROM messages
                      WHERE peer_id = %(p)s AND vk_msg_id = ANY(%(ids)s))
        SELECT DISTINCT m.vk_msg_id, m.sent_at, m.is_own, m.sender, m.text, m.id
          FROM src, LATERAL (
              (SELECT * FROM messages WHERE peer_id = %(p)s
                 AND (sent_at, vk_msg_id) < (src.sent_at, src.vk_msg_id)
               ORDER BY sent_at DESC, vk_msg_id DESC LIMIT %(k)s)
              UNION ALL
              (SELECT * FROM messages WHERE peer_id = %(p)s
                 AND (sent_at, vk_msg_id) >= (src.sent_at, src.vk_msg_id)
               ORDER BY sent_at, vk_msg_id LIMIT %(k1)s)) m
         ORDER BY m.sent_at, m.vk_msg_id""",
        dict(p=peer, ids=list(mids), k=k, k1=k + 1))
    rows = cur.fetchall()
    cur.execute("""
        SELECT a.message_id, string_agg(v.transcript, ' ')
          FROM voice_transcripts v JOIN attachments a ON a.id = v.attachment_id
         WHERE a.message_id = ANY(%s) AND NOT v.empty_audio
           AND NOT coalesce(v.suspect, false)
         GROUP BY a.message_id""", ([r[5] for r in rows],))
    voice = dict(cur.fetchall())
    return [(*r[:5], voice.get(r[5])) for r in rows]


def render(rows, dialog_name, kind):
    lines, day = [], None
    for mid, sent, own, sender, text, voice in rows:
        body = (text or "").strip()
        if voice:
            body = (body + " [голосовое] " + voice.strip()).strip()
        if not body:
            continue
        local = sent.astimezone(dt.timezone(dt.timedelta(hours=3)))
        if local.date() != day:
            day = local.date()
            lines.append(f"— {day:%d.%m.%Y} —")
        who = OWN_NAME if own else (sender if kind == "chat" and sender else dialog_name)
        lines.append(f"#{mid} {local:%H:%M} {who.split(' ')[0]}: "
                     f"{body.replace(chr(10), ' ')[:MAX_MSG]}")
    return "\n".join(lines)


def groups(selected, cur, k):
    """Пачки утверждений одного диалога, не длиннее MAX_GROUP и MAX_CHARS.
    Окно каждого основания запрашивается один раз; размер пачки — по
    объединению окон, как его потом и увидит модель."""
    sizes = {}  # (peer, основание) → {vk_msg_id: символов}

    def window(peer, mid):
        if (peer, mid) not in sizes:
            sizes[peer, mid] = {r[0]: min(len(r[4] or "") + len(r[5] or ""), MAX_MSG) + 30
                                for r in fetch_context(cur, peer, [mid], k)}
        return sizes[peer, mid]

    by_peer = {}
    for c in selected:
        by_peer.setdefault(c["peer_id"], []).append(c)
    for peer, items in by_peer.items():
        batch, mids, rows = [], set(), {}
        for c in items:
            more = set(c.get("sources") or [])
            if not more:
                continue  # без оснований сверять не с чем
            extra = {}
            for mid in more:
                extra.update(window(peer, mid))
            if batch and (len(batch) >= MAX_GROUP
                          or sum({**rows, **extra}.values()) > MAX_CHARS):
                yield peer, batch, mids
                batch, mids, rows = [], set(), {}
            batch.append(c)
            mids |= more
            rows.update(extra)
        if batch:
            yield peer, batch, mids


def ask(prompt, model, budget):
    with tempfile.TemporaryDirectory() as empty:  # без CLAUDE.md проекта в контексте
        r = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "json",
             "--json-schema", json.dumps(SCHEMA), "--tools", "", "--strict-mcp-config",
             "--system-prompt", SYSTEM, "--no-session-persistence",
             "--disable-slash-commands", "--max-budget-usd", str(budget)],
            input=prompt, capture_output=True, text=True, cwd=empty, timeout=600)
    try:
        env = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude rc={r.returncode}: {(r.stderr or r.stdout)[-400:]}")
    if env.get("is_error") or not env.get("structured_output"):
        raise RuntimeError(f"claude: {env.get('subtype')} "
                           f"status={env.get('api_error_status')} {str(env.get('result'))[:300]}")
    # В modelUsage есть и служебная мелкая модель самого claude -p — берём основную.
    usage = env.get("modelUsage") or {}
    used = max(usage, key=lambda m: usage[m].get("costUSD") or 0) if usage else model
    return env["structured_output"]["verdicts"], env.get("total_cost_usd") or 0.0, used


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="append")
    ap.add_argument("--peer", type=int, action="append")
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--context", type=int, default=CONTEXT,
                    help="сколько сообщений до и после каждого основания")
    ap.add_argument("--limit", type=int, help="только первые N пачек (проба)")
    ap.add_argument("--max-cost", type=float, default=5.0, help="стоп по сумме, $")
    ap.add_argument("--include-checked", action="store_true",
                    help="брать и уже проверенные (для калибровки, только с --out)")
    ap.add_argument("--checked-only", action="store_true",
                    help="только уже проверенные: сверка вердиктов с исследованием")
    ap.add_argument("--out", help="писать вердикты сюда, а не в журнал")
    a = ap.parse_args()
    a.include_checked = a.include_checked or a.checked_only
    if a.include_checked and not a.out:
        sys.exit("--include-checked только вместе с --out: уровень 2 не перебивает 3")
    if not (a.run or a.peer):
        sys.exit("нужен --run или --peer")

    claims = cl.load_all()
    selected = select(claims, a)
    names = cl.dialog_names({c["peer_id"] for c in selected})
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SET jit = off")  # компиляция дороже самих коротких запросов
        kinds = dict(cur.execute("SELECT peer_id, kind FROM dialogs WHERE peer_id = ANY(%s)",
                                 ([*names],)).fetchall())
        todo = list(groups(selected, cur, a.context))
        skipped = len(selected) - sum(len(b) for _, b, _ in todo)
        print(f"утверждений {len(selected)}, пачек {len(todo)}, без оснований {skipped}",
              flush=True)
        log = cl.CLAIMS_DIR / "verify_log.jsonl"
        spent, tally = 0.0, {}
        for i, (peer, batch, mids) in enumerate(todo[: a.limit] if a.limit else todo):
            if spent >= a.max_cost:
                print(f"СТОП: потрачено ${spent:.2f} ≥ --max-cost {a.max_cost}")
                break
            text = render(fetch_context(cur, peer, mids, a.context),
                          names.get(peer, str(peer)), kinds.get(peer))
            # Калибровка сверяет исходный черновик: текущий текст уже исправлен исследованием.
            listing = "\n".join(f"[{n}] {c['text'] if a.checked_only else c['state']['text']}"
                                 for n, c in enumerate(batch, 1))
            prompt = PROMPT.format(own=OWN_NAME, dialog=names.get(peer, peer),
                                   claims=listing, text=text)
            t0 = time.time()
            verdicts, cost, used = ask(prompt, a.model, max(0.5, a.max_cost - spent))
            spent += cost
            revs, bad = [], 0
            seen = set()
            for v in verdicts:
                n = v.get("n")
                if not isinstance(n, int) or not 1 <= n <= len(batch) or n in seen:
                    bad += 1
                    continue
                if v["verdict"] == "corrected" and not (v.get("text") or "").strip():
                    bad += 1
                    continue
                seen.add(n)
                c = batch[n - 1]
                revs.append(dict(claim=c["key"], verdict=v["verdict"], level=2,
                                 by=f"{used} (claude -p)",
                                 method=f"перечитала основания ±{a.context} сообщений",
                                 text=v.get("text") if v["verdict"] == "corrected" else None,
                                 note=v.get("note")))
                tally[v["verdict"]] = tally.get(v["verdict"], 0) + 1
            if a.out:
                with open(a.out, "a", encoding="utf-8") as f:
                    for r in revs:
                        st = claims[r["claim"]]["state"]
                        f.write(json.dumps(dict(r, was_level=st["level"], was_status=st["status"],
                                                was_text=st["text"],
                                                original=claims[r["claim"]]["text"]),
                                           ensure_ascii=False) + "\n")
            else:
                cl.append_revisions(revs, claims)
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(dict(at=dt.datetime.now(dt.timezone.utc).isoformat(
                    timespec="seconds"), model=used, peer=peer, claims=len(batch),
                    verdicts=len(revs), bad=bad, missing=len(batch) - len(revs),
                    cost=round(cost, 4), wall_s=round(time.time() - t0, 1),
                    prompt_chars=len(prompt), dry_run=bool(a.out))) + "\n")
            print(f"[{i + 1}/{len(todo)}] {names.get(peer, peer)[:20]} утверждений {len(batch)} "
                  f"вердиктов {len(revs)} брак {bad} ${cost:.3f} (всего ${spent:.2f}) "
                  f"{time.time() - t0:.0f} с", flush=True)
    print(f"итого: {tally}, ${spent:.2f}")
    if not a.out:
        cl.cmd_sync(argparse.Namespace(quiet=True))


if __name__ == "__main__":
    main()
