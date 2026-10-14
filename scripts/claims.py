#!/usr/bin/env python3
"""Утверждения о жизни из переписки: уровни уверенности и журнал уточнений.

Дешёвая локальная модель, прочитав кусок переписки, находит каркас событий
(о чём шла речь, что происходило), но путает детали: кто что сказал, где факт,
а где чьё-то подозрение, откуда мотив. Перепроверять всё сразу дорого и не
нужно — большая часть утверждений никогда не понадобится. Поэтому они
хранятся с низким уровнем и уточняются попутно: когда исследование поднимает
этот же диалог за этот же период, оно берёт список непроверенных утверждений
окна (`due`) и записывает по ним вердикты (`revise`).

Уровень — это кто последним проверил, а не уверенность модели в себе:
  1 — черновик модели;  2 — более сильная модель перечитала основания;
  3 — исследование с цитатами;  4 — слово хозяина архива.
Более слабая проверка не может перебить более сильную. Опровергнутое не
удаляется (status=refuted): так видно, в чём именно модель ошибается.

Источник истины — файлы в data/processed/claims/:
  runs/<run>.jsonl   утверждения прогона, по одному на строку, не меняются;
  revisions.jsonl    журнал уточнений, только дописывается.
База — зеркало, `sync` пересобирает её целиком.

  python3 scripts/claims.py import-notes notes.jsonl --run local-2018-01 --origin <модель>
  python3 scripts/claims.py sync
  python3 scripts/claims.py due --peer 123 --from 2018-01 --to 2018-02
  python3 scripts/claims.py show <key или его начало>
  python3 scripts/claims.py revise <key> --verdict refuted --level 3 \\
          --by "исследование такое-то" --note "в сообщении это план, а не факт"
  python3 scripts/claims.py revise --file verdicts.jsonl   # пачкой
  python3 scripts/claims.py stats

Формат строки revisions.jsonl:
  {"claim": "<key>", "at": "2026-10-14T12:00:00+00:00", "by": "кто",
   "method": "как", "verdict": "confirmed|corrected|refuted|unclear",
   "level": 3, "text": "<новая редакция, только для corrected>",
   "note": "почему", "ref": "где подробности", "event": "<slug в life_events>"}
"""
import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DSN, ROOT  # noqa: E402
from life_events import parse_bound  # noqa: E402

CLAIMS_DIR = ROOT / "data" / "processed" / "claims"
VERDICTS = ("confirmed", "corrected", "refuted", "unclear")
LEVEL_NAMES = {1: "черновик модели", 2: "перечитано сильной моделью",
               3: "исследование", 4: "слово хозяина"}
STATUS_MARK = {"open": "·", "confirmed": "✓", "corrected": "✎", "refuted": "✗"}


def claim_key(run, peer_id, text):
    """Ключ из содержания, а не из порядкового номера: повторная конвертация
    того же прогона даёт те же ключи, и журнал уточнений не отрывается."""
    digest = hashlib.sha1(f"{peer_id}|{text}".encode()).hexdigest()[:8]
    return f"{run}:{digest}"


def fold(claim, revisions):
    """Текущее состояние утверждения = исходное + журнал по порядку.

    Возвращает (состояние, строки журнала со старыми и новыми значениями).
    ValueError — журнал противоречив, и тогда не пишется ничего: половина
    применённых правок хуже, чем ни одной.
    """
    state = dict(text=claim["text"], level=1, status="open", event=None)
    rows = []
    for seq, rev in enumerate(revisions, 1):
        verdict, level = rev.get("verdict"), rev.get("level")
        where = f"{claim['key']} правка {seq}"
        if verdict not in VERDICTS:
            raise ValueError(f"{where}: verdict={verdict!r}, "
                             f"допустимо: {', '.join(VERDICTS)}")
        if level not in (2, 3, 4):
            raise ValueError(f"{where}: level={level!r}, проверка — это 2, 3 или 4")
        if not rev.get("by"):
            raise ValueError(f"{where}: не указано, кто проверял (by)")
        if verdict == "corrected" and not rev.get("text"):
            raise ValueError(f"{where}: corrected без новой редакции (text)")
        # «Смотрели и не поняли» не должно ничего ломать, поэтому его пишет
        # проверка любого уровня. Вердикт же — только не слабее предыдущего.
        if verdict != "unclear" and level < state["level"]:
            raise ValueError(f"{where}: проверка уровня {level} не может "
                             f"перебить уровень {state['level']}")
        new = dict(state)
        if verdict != "unclear":
            new["level"], new["status"] = level, verdict
            if verdict == "corrected":
                new["text"] = rev["text"]
        if rev.get("event"):
            new["event"] = rev["event"]
        rows.append(dict(seq=seq, at=rev.get("at"), by=rev["by"],
                         method=rev.get("method"), verdict=verdict,
                         old_level=state["level"], new_level=new["level"],
                         old_status=state["status"], new_status=new["status"],
                         old_text=state["text"] if new["text"] != state["text"] else None,
                         new_text=new["text"] if new["text"] != state["text"] else None,
                         note=rev.get("note"), ref=rev.get("ref")))
        state = new
    return state, rows


def overlaps(claim, date_from=None, date_to=None):
    """Пересекается ли период утверждения с окном. Утверждение без дат
    показывается всегда: оно как раз ждёт уточнения."""
    if claim.get("period_from") is None:
        return True
    start = dt.date.fromisoformat(str(claim["period_from"]))
    end = dt.date.fromisoformat(str(claim.get("period_to") or claim["period_from"]))
    return ((date_to is None or start <= date_to)
            and (date_from is None or end >= date_from))


def read_jsonl(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_all(base=CLAIMS_DIR):
    """Все утверждения с журналом; ValueError — если что-то не сходится."""
    claims = {}
    for f in sorted((base / "runs").glob("*.jsonl")):
        for c in read_jsonl(f):
            if c["key"] in claims:
                raise ValueError(f"ключ {c['key']} дважды (второй раз в {f.name})")
            claims[c["key"]] = dict(c, revisions=[])
    for rev in read_jsonl(base / "revisions.jsonl"):
        if rev.get("claim") not in claims:
            raise ValueError(f"правка к несуществующему утверждению {rev.get('claim')!r}")
        claims[rev["claim"]]["revisions"].append(rev)
    for c in claims.values():
        c["state"], c["journal"] = fold(c, c["revisions"])
    return claims


def resolve(claims, prefix):
    """Полный ключ, его начало или начало хэша после двоеточия: хэш короче
    и именно его видно в выдаче due."""
    hits = [k for k in claims if k == prefix] or [
        k for k in claims
        if k.startswith(prefix) or k.split(":", 1)[-1].startswith(prefix)]
    if len(hits) != 1:
        sys.exit(f"{prefix!r}: {'нет такого' if not hits else f'подходит {len(hits)}'}")
    return hits[0]


def cmd_import_notes(args):
    """Заметки прогона дайджеста ({peer, first_id, notes: [{theme, note, ids}]})
    → утверждения. Даты берутся из сообщений-оснований в базе: в заметках
    только «ДД.ММ» без года."""
    out = CLAIMS_DIR / "runs" / f"{args.run}.jsonl"
    if out.exists() and not args.replace:
        sys.exit(f"{out} уже есть; --replace перезапишет (ключи из содержания, "
                 f"журнал уточнений не оторвётся, если заметки те же)")
    records = read_jsonl(Path(args.notes))
    claims, seen = [], set()
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        def dates(peer, ids):
            cur.execute("SELECT min(sent_at at time zone 'Europe/Moscow')::date, "
                        "max(sent_at at time zone 'Europe/Moscow')::date, count(*) "
                        "FROM messages WHERE peer_id = %s AND vk_msg_id = ANY(%s)",
                        (peer, list(ids)))
            return cur.fetchone()

        for r in records:
            for n in r.get("notes") or []:
                text = (n.get("note") or "").strip()
                if not text:
                    continue
                # Модель бывает повторяет номер в одной заметке.
                ids = list(dict.fromkeys(i for i in n.get("ids", []) if isinstance(i, int)))
                lo, hi, found = dates(r["peer"], ids or [r["first_id"]])
                if ids and found != len(ids):
                    sys.exit(f"{r['peer']}: из {ids} в базе только {found}")
                key = claim_key(args.run, r["peer"], text)
                if key in seen:  # модель повторила заметку слово в слово
                    continue
                seen.add(key)
                claims.append(dict(key=key, run=args.run, origin=args.origin,
                                   theme=n.get("theme"), peer_id=r["peer"],
                                   period_from=str(lo) if lo else None,
                                   period_to=str(hi) if hi else None,
                                   text=text, sources=ids, basis=n.get("basis")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in claims),
                   encoding="utf-8")
    print(f"{len(claims)} утверждений → {out}")


def cmd_sync(args):
    try:
        claims = load_all()
    except ValueError as e:
        sys.exit(f"не синхронизировано ничего: {e}")
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM claims")  # каскадом и источники, и журнал
        for c in claims.values():
            st = c["state"]
            cur.execute("""
                INSERT INTO claims (key, run, origin, theme, peer_id, period_from,
                  period_to, original_text, text, level, status, event_slug,
                  n_revisions)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (c["key"], c["run"], c["origin"], c.get("theme"), c.get("peer_id"),
                 c.get("period_from"), c.get("period_to"), c["text"], st["text"],
                 st["level"], st["status"], st["event"], len(c["journal"])))
            for mid in c.get("sources", []):
                cur.execute("INSERT INTO claim_sources VALUES (%s, %s, %s) "
                            "ON CONFLICT DO NOTHING", (c["key"], c["peer_id"], mid))
            for j in c["journal"]:
                cur.execute("""
                    INSERT INTO claim_revisions (claim_key, seq, revised_at,
                      revised_by, method, verdict, old_level, new_level,
                      old_status, new_status, old_text, new_text, note, ref)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (c["key"], j["seq"], j["at"], j["by"], j["method"], j["verdict"],
                     j["old_level"], j["new_level"], j["old_status"], j["new_status"],
                     j["old_text"], j["new_text"], j["note"], j["ref"]))
        conn.commit()
    if not args.quiet:
        cmd_stats(args)


def cmd_revise(args):
    claims = load_all()
    if args.file:
        new = read_jsonl(Path(args.file))
    else:
        if not (args.key and args.verdict and args.level and args.by):
            sys.exit("нужны key, --verdict, --level, --by (или --file)")
        new = [dict(claim=args.key, verdict=args.verdict, level=args.level,
                    by=args.by, method=args.method, text=args.text,
                    note=args.note, ref=args.ref, event=args.event)]
    for rev in new:
        rev["claim"] = resolve(claims, rev["claim"])
    try:
        append_revisions(new, claims)
    except ValueError as e:
        sys.exit(f"не записано ничего: {e}")
    print(f"записано правок: {len(new)}")
    args.quiet = True
    cmd_sync(args)


def append_revisions(new, claims=None, base=CLAIMS_DIR):
    """Дописать правки в журнал. Сначала сворачивается всё вместе с новыми,
    и только если сошлось — запись; иначе ValueError и файл не тронут."""
    claims = load_all(base) if claims is None else claims
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    new = [{k: v for k, v in dict(rev, at=rev.get("at") or now).items() if v is not None}
           for rev in new]
    touched = {}
    for rev in new:
        if rev.get("claim") not in claims:
            raise ValueError(f"правка к несуществующему утверждению {rev.get('claim')!r}")
        c = touched.setdefault(rev["claim"], dict(claims[rev["claim"]]))
        c["revisions"] = c["revisions"] + [rev]
    for c in touched.values():
        c["state"], c["journal"] = fold(c, c["revisions"])
    with (base / "revisions.jsonl").open("a", encoding="utf-8") as f:
        for rev in new:
            f.write(json.dumps(rev, ensure_ascii=False) + "\n")
    claims.update(touched)


def cmd_due(args):
    """Что стоит проверить в окне, которое сейчас всё равно читается."""
    claims = load_all()
    lo, hi = parse_bound(args.date_from, False), parse_bound(args.date_to, True)
    hits = [c for c in claims.values()
            if (not args.peer or c["peer_id"] in args.peer)
            and c["state"]["status"] != "refuted"
            and c["state"]["level"] <= args.max_level
            and overlaps(c, lo, hi)]
    hits.sort(key=lambda c: (c.get("period_from") or "", c["key"]))
    names = dialog_names({c["peer_id"] for c in hits})
    for c in hits:
        st = c["state"]
        print(f"{STATUS_MARK[st['status']]} L{st['level']} {c['key']}  "
              f"{c.get('period_from')}…{c.get('period_to')}  "
              f"{names.get(c['peer_id'], c['peer_id'])} · {c.get('theme')}")
        print(f"    {st['text']}")
        print(f"    основания: {', '.join(map(str, c.get('sources', []))) or '—'}")
    print(f"\nк проверке: {len(hits)}")


def dialog_names(peers):
    if not peers:
        return {}
    with psycopg.connect(DSN) as conn:
        rows = conn.execute("SELECT peer_id, name FROM dialogs WHERE peer_id = ANY(%s)",
                            (list(peers),)).fetchall()
    return dict(rows)


def cmd_show(args):
    claims = load_all()
    c = claims[resolve(claims, args.key)]
    st = c["state"]
    print(f"{c['key']}  {STATUS_MARK[st['status']]} {st['status']}, "
          f"уровень {st['level']} ({LEVEL_NAMES[st['level']]})")
    print(f"прогон: {c['run']} · {c['origin']} · тема: {c.get('theme')} · "
          f"{c.get('period_from')}…{c.get('period_to')}")
    if st["event"]:
        print(f"событие: {st['event']}")
    print(f"\n{st['text']}")
    if st["text"] != c["text"]:
        print(f"(исходно: {c['text']})")
    with psycopg.connect(DSN) as conn:
        for mid in c.get("sources", []):
            row = conn.execute(
                "SELECT to_char(sent_at at time zone 'Europe/Moscow', 'DD.MM.YYYY HH24:MI'), "
                "is_own, text FROM messages WHERE peer_id = %s AND vk_msg_id = %s",
                (c["peer_id"], mid)).fetchone()
            if row:
                who = "сам(а)" if row[1] else "собеседник"
                print(f"  {mid} {row[0]} {who}: {(row[2] or '').replace(chr(10), ' ')[:200]}")
            else:
                print(f"  {mid}: нет в базе")
    for j in c["journal"]:
        change = f"L{j['old_level']}→L{j['new_level']}, {j['old_status']}→{j['new_status']}"
        print(f"\n  {j['seq']}. {j['at']} · {j['by']} · {j['verdict']} ({change})")
        if j["method"]:
            print(f"     способ: {j['method']}")
        if j["new_text"]:
            print(f"     было: {j['old_text']}\n     стало: {j['new_text']}")
        if j["note"]:
            print(f"     {j['note']}")
        if j["ref"]:
            print(f"     см.: {j['ref']}")


def cmd_stats(args):
    claims = load_all()
    table = {}
    for c in claims.values():
        key = (c["run"], c["state"]["level"], c["state"]["status"])
        table[key] = table.get(key, 0) + 1
    for (run, level, status), n in sorted(table.items()):
        print(f"{run:<28} L{level} {status:<10} {n}")
    print(f"всего утверждений: {len(claims)}, правок: "
          f"{sum(len(c['journal']) for c in claims.values())}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import-notes", help="заметки прогона → утверждения")
    p.add_argument("notes")
    p.add_argument("--run", required=True, help="имя прогона, часть ключа")
    p.add_argument("--origin", required=True, help="модель или метод")
    p.add_argument("--replace", action="store_true")
    p.set_defaults(func=cmd_import_notes)

    p = sub.add_parser("sync", help="файлы → база (пересборка целиком)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("revise", help="записать вердикт проверки")
    p.add_argument("key", nargs="?")
    p.add_argument("--file", help="jsonl с правками пачкой")
    p.add_argument("--verdict", choices=VERDICTS)
    p.add_argument("--level", type=int, choices=(2, 3, 4))
    p.add_argument("--by")
    p.add_argument("--method")
    p.add_argument("--text", help="новая редакция (для corrected)")
    p.add_argument("--note")
    p.add_argument("--ref")
    p.add_argument("--event", help="slug события в life_events")
    p.set_defaults(func=cmd_revise)

    p = sub.add_parser("due", help="непроверенное в окне")
    p.add_argument("--peer", type=int, action="append")
    p.add_argument("--from", dest="date_from", metavar="DATE")
    p.add_argument("--to", dest="date_to", metavar="DATE")
    p.add_argument("--max-level", type=int, default=2)
    p.set_defaults(func=cmd_due)

    p = sub.add_parser("show", help="утверждение, основания и история")
    p.add_argument("key")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("stats", help="сколько чего по уровням")
    p.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
