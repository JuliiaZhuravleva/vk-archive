#!/usr/bin/env python3
"""Опознание собеседников из удалённых аккаунтов (VK отдаёт их как «DELETED»).

  python3 scripts/identify_deleted.py --min-msgs 500        # показать кандидатов
  python3 scripts/identify_deleted.py --peer 123456789       # опознать один
  python3 scripts/identify_deleted.py --all --write         # опознать все и
                                                            # записать в БД

Как опознаём: имя человека в архиве не сохранилось нигде — VK подменил его на
«DELETED» и в переписке, и в списке друзей, и в лайках. Остаётся только текст.
Скрипт собирает улики (обращения в собственных сообщениях, места, события, общих
знакомых, выборку реплик по годам) и отдаёт их Sonnet, который делает вывод и
честно оценивает уверенность.

Результат — в data/processed/identify/<peer>.json и, с --write, в БД:
dialogs.meta.name_manual + name_guess/confidence/evidence. Имя из meta
переживает переимпорт архива (db_import.py его не затирает).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OWN_NAME, OWN_NICKNAMES           # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "processed" / "identify"
SLIM_CONFIG = os.path.expanduser("~/.claude-slim")

SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "confidence": {"type": "string", "enum": ["высокая", "средняя", "низкая"]},
        "relationship": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "unknown_reason": {"type": "string"},
    },
    "required": ["name", "confidence", "relationship", "evidence"],
})

# Обращения-паразиты: ими зовут кого угодно, имени не выдают.
STOP = set(
    "слушай смотри блин ладно короче милый милая солнце спасибо привет пока "
    "люблю прости ну да нет вот это как что почему когда если конечно кстати "
    "серьезно правда боже господи давай ой ай хорошо окей понял поняла точно "
    "наверное просто типа алло эй сорри извини лол кек ага угу так всё лан "
    "мда ого омг воу хех пфф вай дада ааа ммм погоди понимаешь интересно "
    "круто отлично забей вообще поздно дома".split())
# Обращения к самому себе именем собеседника не считаются. Свои прозвища —
# в VK_OWN_NICKNAMES (см. common.py); без неё фильтруется только VK_OWN_NAME.
_mine = OWN_NICKNAMES or ([OWN_NAME.lower()] if OWN_NAME != "Я" else [])
MINE = (re.compile("^(" + "|".join(map(re.escape, _mine)) + ")", re.I)
        if _mine else re.compile(r"(?!)"))           # (?!) не матчится никогда
VOCATIVES = [re.compile(r"^\s*([А-ЯЁа-яё][а-яё]{2,12})\s*[,!]"),
             re.compile(r",\s*([А-ЯЁа-яё][а-яё]{2,12})\s*[,.!?]*\s*$")]

# Обращение по имени в чате часто идёт без запятой («имя» + просьба одной
# строкой), и шаблонами его не выцепить — служебные слова забивают выдачу.
# Поэтому второй, независимый признак: слова, которые в ЭТОМ диалоге
# встречаются заметно чаще, чем в остальной переписке. Имена и прозвища так
# всплывают сами — на одном из диалогов имя нашлось только этим способом.
WORD = re.compile(r"[а-яёa-z]{3,14}", re.I)


def psql(sql: str) -> str:
    r = subprocess.run(
        ["docker", "exec", "vk-archive-db", "psql", "-U", "vk", "-d",
         "vk_archive", "-t", "-A", "-F", "\t", "-c", sql],
        capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("БД недоступна: " + (r.stderr or r.stdout).strip())
    return r.stdout


def candidates(min_msgs: int) -> list:
    rows = psql(f"""
        SELECT peer_id, total_msgs, own_msgs, voice_total,
               (SELECT min(sent_at)::date FROM messages m WHERE m.peer_id=d.peer_id),
               (SELECT max(sent_at)::date FROM messages m WHERE m.peer_id=d.peer_id)
        FROM dialogs d
        WHERE kind='user' AND useless=false
          AND coalesce(meta->>'name_manual', name) = 'DELETED'
          AND total_msgs >= {min_msgs}
        ORDER BY total_msgs DESC;""")
    out = []
    for line in rows.strip().split("\n"):
        if not line:
            continue
        pid, total, own, voice, first, last = line.split("\t")
        out.append({"peer": pid, "total": int(total), "own": int(own),
                    "voice": int(voice), "first": first, "last": last})
    return out


def vocatives(peer: str) -> list:
    """Как хозяин архива обращается к собеседнику — самый прямой след имени."""
    text = psql(f"SELECT replace(text, chr(10), ' ') FROM messages "
                f"WHERE peer_id={peer} AND is_own=true AND text<>'';")
    c = Counter()
    for line in text.split("\n"):
        for rx in VOCATIVES:
            m = rx.search(line.strip())
            if m:
                w = m.group(1).lower()
                if w not in STOP and not MINE.match(w):
                    c[w] += 1
    return c.most_common(15)


def distinctive(peer: str, top: int = 20) -> list:
    """Слова, характерные именно для этого диалога (частота здесь / везде).

    Считаем по собственным сообщениям: имя собеседника там звучит часто, а в остальных
    переписках оно почти не встречается — такое слово и всплывает наверх.
    """
    here = Counter(WORD.findall(psql(
        f"SELECT lower(text) FROM messages "
        f"WHERE peer_id={peer} AND is_own=true AND text<>'';").lower()))
    other = Counter(WORD.findall(psql(
        f"SELECT lower(text) FROM messages WHERE peer_id<>{peer} AND is_own=true "
        f"AND text<>'' ORDER BY id LIMIT 120000;").lower()))
    n_here, n_other = max(sum(here.values()), 1), max(sum(other.values()), 1)
    scored = []
    for w, c in here.items():
        if c < 4:
            continue
        rate_here = c / n_here
        rate_other = (other.get(w, 0) + 1) / n_other
        ratio = rate_here / rate_other
        if ratio > 3:
            scored.append((w, c, round(ratio, 1)))
    scored.sort(key=lambda x: -x[2])
    return scored[:top]


def sample(peer: str, per_year: int = 6) -> str:
    """Выборка реплик по годам — из неё видно, кто это и о чём говорили."""
    rows = psql(f"""
        WITH ranked AS (
          SELECT to_char(sent_at,'YYYY') y, sent_at, is_own, left(text, 220) t,
                 row_number() OVER (PARTITION BY to_char(sent_at,'YYYY')
                                    ORDER BY length(text) DESC) rn
          FROM messages WHERE peer_id={peer} AND length(text) BETWEEN 40 AND 400)
        SELECT y, to_char(sent_at,'DD.MM.YYYY'), is_own, replace(t, chr(10), ' ')
        FROM ranked WHERE rn <= {per_year} ORDER BY sent_at;""")
    lines = []
    for line in rows.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        _, date, own, t = parts
        who = OWN_NAME if own == "t" else "ОН/ОНА"
        lines.append(f"[{date}] {who}: {t}")
    return "\n".join(lines)


def named_by_others(peer: str, names: list) -> str:
    """Проверка догадки: встречается ли имя в других диалогах рядом с
    теми же событиями. Возвращает короткую сводку упоминаний."""
    if not names:
        return ""
    pat = "|".join(re.escape(n) for n in names[:5])
    rows = psql(f"""
        SELECT d.name, count(*) FROM messages m JOIN dialogs d USING (peer_id)
        WHERE m.peer_id <> {peer} AND m.text ~* '(^|[^а-яё])({pat})([^а-яё]|$)'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8;""")
    return " | ".join(" ".join(r.split("\t")) for r in rows.strip().split("\n") if r)


def ask_sonnet(prompt: str, model: str = "sonnet"):
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [claude, "-p", prompt, "--model", model, "--strict-mcp-config",
           "--json-schema", SCHEMA]
    env = os.environ.copy()
    if os.path.isdir(SLIM_CONFIG):
        env["CLAUDE_CONFIG_DIR"] = SLIM_CONFIG
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return None
    # Схему навязывает --json-schema, но результат уходит в dialogs.name и
    # dialogs.meta, то есть в данные, которые потом читаются как факты.
    # Проверяем форму: тихо записать в базу словарь не той структуры хуже,
    # чем честно вернуть None и переспросить.
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    return data


def identify(info: dict) -> dict:
    peer = info["peer"]
    voc = vocatives(peer)
    smp = sample(peer)
    dist = distinctive(peer)
    guesses = [w for w, n in voc[:5] if n >= 3] + [w for w, _c, _r in dist[:5]]
    cross = named_by_others(peer, guesses)
    ask = (
        f"Ты помогаешь восстановить, с кем именно {OWN_NAME} переписывался(-ась) "
        "ВКонтакте. "
        "Аккаунт собеседника удалён, VK стёр имя везде — остался только текст.\n\n"
        f"Объём: {info['total']} сообщений ({info['own']} от {OWN_NAME}), "
        f"{info['first']} — {info['last']}.\n\n"
        f"Обращения, которые {OWN_NAME} использует в этом диалоге "
        "(слово × сколько раз) "
        "— главный след имени, но среди них бывают и просто словечки:\n"
        + ", ".join(f"{w}×{n}" for w, n in voc) + "\n\n"
        + "Слова, характерные именно для этого диалога (слово × сколько раз × "
          "во сколько раз чаще, чем в остальной переписке) — среди них бывают "
          "имена, прозвища и приметы человека:\n"
        + ", ".join(f"{w}×{c}(×{r})" for w, c, r in dist) + "\n\n"
        + (f"Где эти слова встречаются в ДРУГИХ диалогах {OWN_NAME} "
           f"(диалог — сколько раз): {cross}\n\n" if cross else "")
        + "Выборка реплик по годам:\n" + smp + "\n\n"
        "Верни JSON:\n"
        "- name: имя человека (как его зовут в переписке; фамилия — только если "
        "она явно звучит). Если опознать не удалось — верни пустую строку.\n"
        "- confidence: высокая / средняя / низкая\n"
        f"- relationship: кем этот человек приходится {OWN_NAME} (парень, подруга, "
        "брат, коллега…), "
        "одной строкой с периодом общения\n"
        "- evidence: 2-5 конкретных цитат или фактов, на которых основан вывод\n"
        "- unknown_reason: если имя не определяется — чем именно текст не помог\n\n"
        "ВАЖНО: не выдумывай. Прозвище — это не имя; если в тексте только "
        "прозвище, так и напиши в name (прозвище) и поставь низкую уверенность."
    )
    res = ask_sonnet(ask) or {"name": "", "confidence": "низкая",
                              "relationship": "", "evidence": [],
                              "unknown_reason": "Sonnet не ответил"}
    res.update(peer=peer, vocatives=voc, total=info["total"],
               period=f"{info['first']}—{info['last']}")
    return res


def write_db(res: dict) -> None:
    if not res.get("name"):
        return
    name = res["name"].replace("'", "''")
    meta = json.dumps({"name_manual": res["name"], "name_archive": "DELETED",
                       "name_guess": True, "confidence": res["confidence"],
                       "relationship": res.get("relationship", ""),
                       "evidence": res.get("evidence", [])},
                      ensure_ascii=False).replace("'", "''")
    psql(f"UPDATE dialogs SET name='{name}', meta = meta || '{meta}'::jsonb "
         f"WHERE peer_id={res['peer']};")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", action="append", help="опознать конкретный диалог")
    ap.add_argument("--all", action="store_true", help="все кандидаты")
    ap.add_argument("--min-msgs", type=int, default=500)
    ap.add_argument("--write", action="store_true", help="записать имена в БД")
    args = ap.parse_args()

    cands = candidates(args.min_msgs)
    if args.peer:
        cands = [c for c in cands if c["peer"] in args.peer] or [
            {"peer": p, "total": 0, "own": 0, "voice": 0, "first": "?",
             "last": "?"} for p in args.peer]
    elif not args.all:
        print(f"Удалённых аккаунтов от {args.min_msgs} сообщений: {len(cands)}")
        for c in cands:
            print(f"  {c['peer']:>12}  {c['total']:>6} сообщ.  "
                  f"{c['voice']:>3} ГС  {c['first']}—{c['last']}")
        print("\nопознать: --all [--write] или --peer <id>")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    for c in cands:
        res = identify(c)
        (OUT / f"{c['peer']}.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        mark = {"высокая": "✔", "средняя": "~", "низкая": "?"}.get(
            res["confidence"], "?")
        print(f"{mark} {c['peer']:>12} {c['total']:>6} сообщ. → "
              f"{res['name'] or '(не опознан)'} [{res['confidence']}] "
              f"{res.get('relationship', '')}")
        for e in res.get("evidence", [])[:3]:
            print(f"     • {e}")
        if args.write:
            write_db(res)


if __name__ == "__main__":
    main()
