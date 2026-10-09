#!/usr/bin/env python3
"""Валидация, автоочистка и сборка данных каталога.

Примеры:
  python scripts/build.py --strict                          # только проверить файлы (для PR)
  python scripts/build.py --archive --check-links           # ежедневное обслуживание
"""
import argparse
import concurrent.futures as cf
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (INCLUDE_EXAMPLES, ROOT, STATE, as_date, cfg, load_dir,  # noqa: E402
                    load_json, save_json, today)

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
UA = "Mozilla/5.0 (compatible; ISIC-Kosice-LinkCheck/1.0)"


# ---------------------------------------------------------------- валидация
def _i18n(d, key, langs, maxlen, required, errs):
    v = d.get(key)
    if v in (None, {}, ""):
        if required:
            errs.append(f"{key}: нужен хотя бы один язык ({', '.join(langs)})")
        return {}
    if not isinstance(v, dict):
        errs.append(f"{key}: ожидается словарь вида sk/en/uk/ru")
        return {}
    out = {}
    for lang, text in v.items():
        if lang not in langs:
            errs.append(f"{key}.{lang}: язык не разрешён (только {', '.join(langs)})")
            continue
        text = " ".join(str(text or "").split())
        if not text:
            continue
        if len(text) > maxlen:
            errs.append(f"{key}.{lang}: {len(text)} символов, максимум {maxlen}")
        out[lang] = text
    if required and not out:
        errs.append(f"{key}: все тексты пустые")
    return out


def _date(d, key, errs, required=False):
    try:
        v = as_date(d.get(key))
    except ValueError:
        errs.append(f"{key}: дата должна быть вида ГГГГ-ММ-ДД")
        return None
    if required and v is None:
        errs.append(f"{key}: обязательное поле (ГГГГ-ММ-ДД)")
    return v


def _url(d, errs):
    u = str(d.get("url") or "").strip()
    p = urlparse(u)
    ok_scheme = p.scheme == "https" or (p.scheme == "http" and os.environ.get("ISIC_ALLOW_HTTP") == "1")
    if not ok_scheme or not p.netloc:
        errs.append("url: нужна ссылка https://...")
    return u


def validate_offer(d, stem, c):
    errs, langs, post = [], c["languages"], c["post"]
    if d.get("_error"):
        return [d["_error"]], None
    oid = str(d.get("id") or "")
    if not SLUG.match(oid):
        errs.append("id: латиница, цифры и дефис (2–61 символ)")
    elif oid != stem:
        errs.append(f"id «{oid}» должен совпадать с именем файла «{stem}.yml»")
    if d.get("category") not in c["categories"]:
        errs.append(f"category: одна из {', '.join(c['categories'])}")
    o = {
        "id": oid,
        "category": d.get("category"),
        "url": _url(d, errs),
        "title": _i18n(d, "title", langs, post["title_max"], True, errs),
        "desc": _i18n(d, "desc", langs, post["desc_max"], True, errs),
        "perk": _i18n(d, "perk", langs, post["perk_max"], False, errs),
        "isic": bool(d.get("isic", True)),
        "free": bool(d.get("free", False)),
        "free_transport": bool(d.get("free_transport", False)),
        "where": " ".join(str(d.get("where") or "").split())[:60],
        "valid_from": _date(d, "valid_from", errs),
        "valid_to": _date(d, "valid_to", errs),
        "verified": _date(d, "verified", errs, required=True),
        "priority": int(d.get("priority") or 0),
        "example": bool(d.get("example", False)),
        "check": d.get("check", {}),
    }
    o["added"] = _date(d, "added", errs) or o["verified"]
    if o["valid_from"] and o["valid_to"] and o["valid_to"] < o["valid_from"]:
        errs.append("valid_to раньше valid_from")
    return errs, o


def validate_ad(d, stem, c):
    errs, langs, post = [], c["languages"], c["post"]
    if d.get("_error"):
        return [d["_error"]], None
    oid = str(d.get("id") or "")
    if not SLUG.match(oid):
        errs.append("id: латиница, цифры и дефис")
    elif oid != stem:
        errs.append(f"id «{oid}» должен совпадать с именем файла «{stem}.yml»")
    placements = d.get("placements") or ["channel", "app"]
    if not set(placements) <= {"channel", "app"}:
        errs.append("placements: channel и/или app")
    a = {
        "id": oid,
        "sponsor": str(d.get("sponsor") or "").strip(),
        "category": d.get("category") if d.get("category") in c["categories"] else "other",
        "url": _url(d, errs),
        "title": _i18n(d, "title", langs, post["title_max"], True, errs),
        "desc": _i18n(d, "desc", langs, post["desc_max"], True, errs),
        "perk": _i18n(d, "perk", langs, post["perk_max"], False, errs),
        "where": " ".join(str(d.get("where") or "").split())[:60],
        "start": _date(d, "start", errs, required=True),
        "end": _date(d, "end", errs, required=True),
        "placements": list(placements),
        "weight": int(d.get("weight") or 1),
        "channel_posts": int(d.get("channel_posts") or 1),
        "example": bool(d.get("example", False)),
        "check": d.get("check", {}),
    }
    if not a["sponsor"]:
        errs.append("sponsor: название рекламодателя обязательно (оно показывается в пометке)")
    return errs, a


def collect(c=None, t=None):
    """Загрузить и проверить offers/ и ads/. Возвращает (offers, ads, errors)."""
    c, t = c or cfg(), t or today()
    offers, ads, errors = [], [], []
    for p, d in load_dir("offers"):
        if d.get("example") and not INCLUDE_EXAMPLES:
            continue
        errs, o = validate_offer(d, p.stem, c)
        if errs:
            errors += [(f"offers/{p.name}", e) for e in errs]
        else:
            o["_path"] = p
            offers.append(o)
    seen = {}
    for o in offers:
        key = o["url"].rstrip("/").lower()
        if key in seen:
            errors.append((f"offers/{o['id']}.yml", f"дубль ссылки с {seen[key]}"))
        seen[key] = o["id"]
    for p, d in load_dir("ads"):
        if d.get("example") and not INCLUDE_EXAMPLES:
            continue
        errs, a = validate_ad(d, p.stem, c)
        if errs:
            errors += [(f"ads/{p.name}", e) for e in errs]
        else:
            a["_path"] = p
            ads.append(a)
    return offers, ads, errors


def offer_status(o, c, t):
    """active | scheduled | expired | stale"""
    if o["valid_to"] and o["valid_to"] < t:
        return "expired"
    if o["valid_from"] and o["valid_from"] > t:
        return "scheduled"
    if o["verified"] + timedelta(days=c["maintenance"]["max_age_days"]) < t:
        return "stale"
    return "active"


def ad_status(a, t):
    if a["end"] < t:
        return "expired"
    if a["start"] > t:
        return "scheduled"
    return "active"


def active_items(c=None, t=None):
    """Показываемые сейчас офферы и реклама (без проверки ссылок — только даты)."""
    c, t = c or cfg(), t or today()
    offers, ads, _ = collect(c, t)
    health = load_json(STATE / "health.json", {})
    dead = c["maintenance"]["dead_after_failures"]
    off = [o for o in offers if offer_status(o, c, t) == "active"
           and health.get(o["id"], {}).get("fails", 0) < dead]
    ad = [a for a in ads if ad_status(a, t) == "active"
          and health.get("ad:" + a["id"], {}).get("fails", 0) < dead]
    return off, ad


# ------------------------------------------------------------ проверка ссылок
def check_url(url, contains=None, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en,sk;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, body = r.status, (r.read(300_000) if contains else b"")
    except urllib.error.HTTPError as e:
        code, body = e.code, b""
    except Exception as e:  # DNS, таймаут, SSL
        return "net", str(e)[:100]
    if code in (401, 403, 429, 999):
        return "unknown", f"HTTP {code} (сайт не пускает ботов — пропущено)"
    if code in (404, 410) or code >= 400:
        return "fail", f"HTTP {code}"
    if contains and contains.lower() not in body.decode("utf-8", "ignore").lower():
        return "fail", f"на странице нет текста «{contains}»"
    return "ok", f"HTTP {code}"


def run_link_checks(items, health, c, t):
    """items: [(health_key, url, check)] -> {key: (status, detail)}; обновляет health."""
    todo = []
    for key, url, chk in items:
        if chk is False or (isinstance(chk, dict) and chk.get("skip")):
            continue
        if health.get(key, {}).get("checked") == str(t):
            continue  # уже проверяли сегодня
        contains = chk.get("contains") if isinstance(chk, dict) else None
        todo.append((key, url, contains))
    results = {}
    timeout = c["maintenance"]["link_timeout_sec"]
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(check_url, u, ct, timeout): k for k, u, ct in todo}
        for f in cf.as_completed(futs):
            results[futs[f]] = f.result()
    # Защита: если «упала» сеть у самого раннера, не наказываем офферы
    nets = sum(1 for s, _ in results.values() if s == "net")
    if len(results) >= 4 and nets / len(results) > 0.5:
        results = {k: ("unknown", "сеть раннера нестабильна — пропущено") if s == "net" else (s, d)
                   for k, (s, d) in results.items()}
    for key, (status, detail) in results.items():
        h = health.setdefault(key, {"fails": 0})
        h["checked"], h["detail"] = str(t), detail
        if status == "ok":
            h["fails"], h["last_ok"] = 0, str(t)
        elif status in ("fail", "net"):
            h["fails"] = h.get("fails", 0) + 1
        else:
            h.setdefault("fails", 0)
    return results


# ------------------------------------------------------------------- архив
def archive(path: Path, reason: str, t):
    dest = ROOT / "archive" / path.name
    if dest.exists():
        dest = dest.with_name(f"{path.stem}-{t}{path.suffix}")
    text = path.read_text("utf-8").rstrip() + f"\narchived_on: {t}\narchived_reason: {reason}\n"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(text, "utf-8")
    path.unlink()


# --------------------------------------------------------------------- main
def _ser(o, drop=("_path", "check", "example")):
    return {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in o.items() if k not in drop}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", action="store_true", help="переносить истёкшие/мёртвые в archive/")
    ap.add_argument("--check-links", action="store_true")
    ap.add_argument("--strict", action="store_true", help="код выхода 1 при ошибках в файлах")
    args = ap.parse_args()

    c, t = cfg(), today()
    offers, ads, errors = collect(c, t)
    health = load_json(STATE / "health.json", {})
    report, archived = [], []
    dead_n = c["maintenance"]["dead_after_failures"]

    # 1) истёкшие -> архив
    keep_o = []
    for o in offers:
        st = offer_status(o, c, t)
        if st == "expired" and not o["example"]:
            if args.archive:
                archive(o["_path"], "expired", t)
                archived.append((o["id"], "expired"))
                continue
        keep_o.append(o)
    offers = keep_o
    keep_a = []
    for a in ads:
        if ad_status(a, t) == "expired" and not a["example"] and args.archive:
            archive(a["_path"], "expired", t)
            archived.append(("ad:" + a["id"], "expired"))
            continue
        keep_a.append(a)
    ads = keep_a

    # 2) проверка ссылок -> архив после N дней подряд
    if args.check_links:
        items = [(o["id"], o["url"], o["check"]) for o in offers
                 if offer_status(o, c, t) == "active" and not o["example"]]
        items += [("ad:" + a["id"], a["url"], a["check"]) for a in ads
                  if ad_status(a, t) == "active" and not a["example"]]
        res = run_link_checks(items, health, c, t)
        for k, (s, d) in sorted(res.items()):
            if s != "ok":
                report.append(f"- {'⚠️' if s == 'unknown' else '❌'} `{k}` — {d} (подряд сбоев: {health[k]['fails']})")
        if args.archive:
            keep_o = []
            for o in offers:
                if not o["example"] and health.get(o["id"], {}).get("fails", 0) >= dead_n:
                    archive(o["_path"], "dead-link", t)
                    archived.append((o["id"], "dead-link"))
                else:
                    keep_o.append(o)
            offers = keep_o
            keep_a = []
            for a in ads:
                if not a["example"] and health.get("ad:" + a["id"], {}).get("fails", 0) >= dead_n:
                    archive(a["_path"], "dead-link", t)
                    archived.append(("ad:" + a["id"], "dead-link"))
                else:
                    keep_a.append(a)
            ads = keep_a

    # чистим health от удалённых
    live = {o["id"] for o in offers} | {"ad:" + a["id"] for a in ads}
    health = {k: v for k, v in health.items() if k in live}
    save_json(STATE / "health.json", health)

    # 3) публичный JSON для Mini App
    shown = [o for o in offers if offer_status(o, c, t) == "active"
             and health.get(o["id"], {}).get("fails", 0) < dead_n]
    shown_ads = [a for a in ads if ad_status(a, t) == "active" and "app" in a["placements"]
                 and health.get("ad:" + a["id"], {}).get("fails", 0) < dead_n]
    shown.sort(key=lambda o: (-o["priority"], str(o["added"])), reverse=False)
    hidden = [(o["id"], offer_status(o, c, t)) for o in offers if offer_status(o, c, t) != "active"]
    tg = c["telegram"]
    save_json(ROOT / "docs" / "data" / "offers.json", {
        "generated": str(t),
        "site": {"name": c["project"]["name"], "repo": c["project"]["repo"],
                 "bot": tg.get("bot_username"), "app": tg.get("app_short_name"),
                 "pages_url": c["project"]["pages_url"],
                 "ads": {"every": c["ads"]["catalog_every"], "labels": c["ads"]["labels"],
                         "utm_source": c["ads"]["utm_source"]}},
        "categories": c["categories"],
        "offers": [_ser(o) for o in shown],
        "ads": [_ser(a) for a in shown_ads],
    })

    # 4) отчёт
    lines = [f"## ISIC Košice — отчёт {t}", "",
             f"Показывается: **{len(shown)}** офферов, **{len(shown_ads)}** рекламных карточек."]
    if archived:
        lines += ["", "### В архив", *[f"- `{i}` — {r}" for i, r in archived]]
    if hidden:
        lines += ["", "### Скрыто (не в архиве)", *[
            f"- `{i}` — {'устарел: обнови дату `verified`' if s == 'stale' else 'ещё не началось'}"
            for i, s in hidden]]
    if report:
        lines += ["", "### Ссылки", *report]
    if errors:
        lines += ["", "### Ошибки в файлах", *[f"- `{f}`: {m}" for f, m in errors]]
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(text, "utf-8")
    for f, m in errors:
        print(f"::error file={f}::{m}")
    return 1 if (errors and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
