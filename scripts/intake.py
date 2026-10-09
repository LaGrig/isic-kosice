#!/usr/bin/env python3
"""Превращает одобренную issue-форму «Предложить оффер» в файл offers/<id>.yml.

Читает ISSUE_BODY / ISSUE_NUMBER / ISSUE_USER из окружения (не из shell — безопасно).
Печатает в GITHUB_OUTPUT: ok=true|false, message=<текст для комментария>.
"""
import hashlib
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
from common import ROOT, cfg, today  # noqa: E402

NO = {"", "_no response_", "none", "-"}


def parse(body):
    out, key = {}, None
    for line in body.replace("\r", "").split("\n"):
        if line.startswith("### "):
            key, out[key] = line[4:].strip(), []
        elif key is not None:
            out[key].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def val(s):
    return "" if s.strip().lower() in NO else s.strip()


def result(ok, msg):
    gho = os.environ.get("GITHUB_OUTPUT")
    delim = "EOF_ISIC"
    if gho:
        with open(gho, "a", encoding="utf-8") as f:
            f.write(f"ok={'true' if ok else 'false'}\nmessage<<{delim}\n{msg}\n{delim}\n")
    print(msg)
    return 0 if ok else 1


def main():
    c, t = cfg(), today()
    f = parse(os.environ.get("ISSUE_BODY", ""))
    title, desc = {}, {}
    for lang in c["languages"]:
        tt, dd = val(f.get(f"Title ({lang.upper()})", "")), val(f.get(f"Description ({lang.upper()})", ""))
        if tt:
            title[lang] = tt
        if dd:
            desc[lang] = dd
    cat = val(f.get("Category", "")).split(" ")[0].strip()
    base = re.sub(r"[^a-z0-9]+", "-", (title.get("en") or next(iter(title.values()), "offer")).lower()).strip("-")[:40]
    oid = f"{base or 'offer'}-{hashlib.sha1(val(f.get('Link (https)', '')).encode()).hexdigest()[:4]}"
    d = {"id": oid, "category": cat, "url": val(f.get("Link (https)", "")),
         "isic": val(f.get("ISIC required?", "Yes")).lower().startswith("y"),
         "free": val(f.get("Free?", "No")).lower().startswith("y"),
         "free_transport": val(f.get("Reachable with free public transport/train?", "No")).lower().startswith("y"),
         "title": title, "desc": desc, "verified": str(t), "added": str(t)}
    if val(f.get("Valid until (YYYY-MM-DD)", "")):
        d["valid_to"] = val(f.get("Valid until (YYYY-MM-DD)", ""))
    if val(f.get("Where (place/city)", "")):
        d["where"] = val(f.get("Where (place/city)", ""))
    d["submitted_by"] = os.environ.get("ISSUE_USER", "")

    errs, _ = build.validate_offer(d, oid, c)
    dest = ROOT / "offers" / f"{oid}.yml"
    if dest.exists():
        errs.append("такой оффер (та же ссылка) уже существует")
    if errs:
        return result(False, "Не удалось создать оффер:\n" + "\n".join(f"- {e}" for e in errs))
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), "utf-8")
    return result(True, f"Оффер добавлен: `offers/{oid}.yml`. Появится в каталоге и очереди канала после ближайшего запуска.")


if __name__ == "__main__":
    sys.exit(main())
