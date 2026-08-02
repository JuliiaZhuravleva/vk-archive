#!/usr/bin/env python3
"""Качает медиа из media_manifest.jsonl. Только stdlib.

  python3 scripts/download_media.py --kind voice [--limit N] [--workers 4]

Resume: уже скачанные файлы пропускаются. Имя файла: sha1(url)[:16] + расширение
(маппинг url→файл пишется в data/processed/downloads.log.jsonl).
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "processed" / "media_manifest.jsonl"
LOG = ROOT / "data" / "processed" / "downloads.log.jsonl"
MEDIA = ROOT / "data" / "media"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def dest_for(url: str, kind: str) -> Path:
    base = url.split("?")[0]
    ext = ("." + base.rsplit(".", 1)[-1]) if "." in base.rsplit("/", 1)[-1] else ""
    name = hashlib.sha1(url.encode()).hexdigest()[:16] + ext
    sub = {"voice": "voice", "photo": "photos"}.get(kind, "other")
    return MEDIA / sub / name


def fetch(url: str, dest: Path, retries: int = 3) -> tuple[bool, str]:
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if not data:
                raise OSError("empty response")
            # VK-CDN умеет отдавать HTML-заглушку с кодом 200. Такой файл
            # ляжет на диск под именем .ogg, скачивание отметится успешным,
            # а whisper потом получит на вход мусор. Проверка непустоты от
            # этого не спасает — нужен признак содержимого.
            if data[:15].lstrip()[:1] == b"<":
                raise OSError(f"вместо файла пришла разметка ({len(data)} б)")
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(data)
            tmp.rename(dest)
            return True, f"{len(data)}b"
        except Exception as exc:  # noqa: BLE001 — логируем и ретраим любой сбой
            if attempt == retries:
                return False, str(exc)
            time.sleep(2 * attempt)
    return False, "unreachable"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=["voice", "photo", "other"])
    ap.add_argument("--limit", type=int, default=0, help="0 = без лимита")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    items = []
    with MANIFEST.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec["kind"] != args.kind:
                continue
            dest = dest_for(rec["url"], rec["kind"])
            if dest.exists():
                continue
            items.append((rec, dest))
            if args.limit and len(items) >= args.limit:
                break

    print(f"к скачиванию: {len(items)}", file=sys.stderr)
    MEDIA.joinpath({"voice": "voice", "photo": "photos"}.get(args.kind, "other")).mkdir(
        parents=True, exist_ok=True
    )

    ok = fail = 0
    with LOG.open("a", encoding="utf-8") as log, ThreadPoolExecutor(args.workers) as ex:
        futs = {
            ex.submit(fetch, rec["url"], dest): (rec, dest) for rec, dest in items
        }
        for fut in as_completed(futs):
            rec, dest = futs[fut]
            success, info = fut.result()
            ok += success
            fail += not success
            log.write(
                json.dumps(
                    {
                        "url": rec["url"],
                        "path": str(dest.relative_to(ROOT)),
                        "peer_id": rec["peer_id"],
                        "msg_id": rec["msg_id"],
                        "ok": success,
                        "info": info,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            done = ok + fail
            if done % 200 == 0 or done == len(items):
                print(f"  {done}/{len(items)} (ошибок: {fail})", file=sys.stderr)

    print(f"готово: ok={ok} fail={fail}", file=sys.stderr)
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
