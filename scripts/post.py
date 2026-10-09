#!/usr/bin/env python3
"""Публикация в Telegram-канал через Bot API (только стандартная библиотека).

  python scripts/post.py post [--max N]   # новые офферы (+ реклама по расписанию)
  python scripts/post.py digest           # подборка недели
  python scripts/post.py menu             # закреплённое меню-навигация
  python scripts/post.py sync             # пометить завершённые офферы в уже опубликованных постах
Флаги: --dry-run (ничего не отправлять), --soft (без токена — тихо выйти).
Секреты: TELEGRAM_BOT_TOKEN, (опц.) TELEGRAM_CHAT_ID.
"""
import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
from common import STATE, cfg, load_json, pick, save_json, today  # noqa: E402

LABELS = {
    "open": {"sk": "🔗 Otvoriť ponuku", "en": "🔗 Open offer", "uk": "🔗 Відкрити пропозицію", "ru": "🔗 Открыть предложение"},
    "catalog": {"sk": "📲 Katalóg", "en": "📲 Catalog", "uk": "📲 Каталог", "ru": "📲 Каталог"},
    "ended": {"sk": "Skončilo", "en": "Ended", "uk": "Закінчилось", "ru": "Закончилось"},
    "digest": {"sk": "Tipy týždňa", "en": "This week's picks", "uk": "Добірка тижня", "ru": "Подборка недели"},
    "menu": {"sk": "Navigácia", "en": "Navigation", "uk": "Навігація", "ru": "Навигация"},
    "menu_hint": {"sk": "Ťukni na kategóriu alebo otvor katalóg.", "en": "Tap a category or open the catalog.",
                  "uk": "Обери категорію або відкрий каталог.", "ru": "Выбери категорию или открой каталог."},
}
H = html.escape


class TgError(Exception):
    pass


def api(token, method, payload, dry):
    if dry:
        print(f"[dry-run] {method}\n{json.dumps(payload, ensure_ascii=False, indent=2)[:2500]}\n")
        return {"message_id": 0}
    base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
    url = f"{base}/bot{token}/{method}"
    data = json.dumps(payload).encode()
    for _ in range(4):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["result"]
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            if e.code == 429:
                time.sleep(body.get("parameters", {}).get("retry_after", 3) + 1)
                continue
            raise TgError(f"{method}: {body.get('description', e.code)}") from None
        except urllib.error.URLError as e:
            raise TgError(f"{method}: сеть недоступна ({e.reason})") from None
    raise TgError(f"{method}: слишком много запросов")


def targets(c):
    """[(chat_id, [языки; первый — основной])]"""
    tg, env = c["telegram"], os.environ.get("TELEGRAM_CHAT_ID")
    if tg.get("channels") and not env:
        return [(cid, [lang]) for lang, cid in tg["channels"].items() if cid]
    prim = c["post"]["primary_lang"]
    return [(env or tg["channel_id"], [prim] + [l for l in c["languages"] if l != prim])]


def deeplink(c, param):
    tg = c["telegram"]
    if tg.get("bot_username") and tg.get("app_short_name"):
        return f"https://t.me/{tg['bot_username']}/{tg['app_short_name']}?startapp={param}"
    return c["project"]["pages_url"].rstrip("/") + "/#" + param


def with_utm(c, url, ad_id, medium):
    sep = "&" if urllib.parse.urlparse(url).query else "?"
    q = urllib.parse.urlencode({"utm_source": c["ads"]["utm_source"], "utm_medium": medium, "utm_campaign": ad_id})
    return url + sep + q


def fmt_date(d):
    return d.strftime("%d.%m.%Y")


# ------------------------------------------------------------------ рендер
def render_item(c, it, langs, is_ad=False):
    cats, labels = c["categories"], c["ads"]["labels"]
    cat = cats.get(it["category"], cats["other"])
    lang = langs[0]
    badges = []
    if is_ad:
        badges.append("📣 " + " · ".join(dict.fromkeys(labels[l] for l in langs)))
    else:
        if it["isic"]:
            badges.append("🪪 ISIC")
        if it["free"]:
            badges.append("🆓")
        if it["free_transport"]:
            badges.append("🚆")
    head = f"{cat['emoji']} <b>{H(pick(cat['name'], lang).upper())}</b>"
    if badges:
        head += "  ·  " + "  ".join(badges)
    perk = pick(it["perk"], lang)
    lines = [head, f"<b>{H(pick(it['title'], lang))}</b>"]
    if perk:
        lines.append(f"✨ {H(perk)}")
    lines += ["", H(pick(it["desc"], lang))]
    facts = []
    if it.get("where"):
        facts.append(f"📍 {H(it['where'])}")
    end = it.get("end") if is_ad else it.get("valid_to")
    if end:
        facts.append(f"⏳ {fmt_date(end)}")
    if is_ad:
        facts.append(f"{H(labels[lang])}: {H(it['sponsor'])}")
    if facts:
        lines += ["", "  ·  ".join(facts)]
    extras = [l for l in langs[1:] if it["title"].get(l)]
    if extras:
        parts = []
        for l in extras:
            p = f"<b>{l.upper()}</b> · <b>{H(it['title'][l])}</b>"
            if it["desc"].get(l):
                p += f"\n{H(it['desc'][l])}"
            if it["perk"].get(l):
                p += f"\n✨ {H(it['perk'][l])}"
            parts.append(p)
        lines += ["", "<blockquote expandable>" + "\n\n".join(parts) + "</blockquote>"]
    tags = [f"#{it['category']}", "#Kosice"]
    if is_ad:
        tags.append("#ad")
    else:
        tags += ["#ISIC"] if it["isic"] else []
        tags += ["#free"] if it["free"] else []
    lines += ["", " ".join(tags)]
    return "\n".join(lines)


def keyboard(c, it, lang, is_ad=False):
    url = with_utm(c, it["url"], it["id"], "channel") if is_ad else it["url"]
    return {"inline_keyboard": [[
        {"text": LABELS["open"][lang], "url": url},
        {"text": LABELS["catalog"][lang], "url": deeplink(c, "o_" + it["id"] if not is_ad else "all")},
    ]]}


# ------------------------------------------------------------------- команды
def send_item(c, st, token, dry, it, is_ad):
    sent = []
    for chat, langs in targets(c):
        text = render_item(c, it, langs, is_ad)
        res = api(token, "sendMessage", {
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": keyboard(c, it, langs[0], is_ad)}, dry)
        sent.append({"chat": chat, "id": res["message_id"]})
        time.sleep(1.2)
    if dry:
        return
    now = str(today())
    if is_ad:
        rec = st["ads"].setdefault(it["id"], {"count": 0})
        rec.update(count=rec["count"] + 1, last_posted=now)
        rec.setdefault("messages", []).extend(sent)
        st["_meta"]["posts_since_ad"] = 0
    else:
        st["offers"][it["id"]] = {"messages": sent, "posted_at": now, "ended": False,
                                  "title": it["title"], "perk": it["perk"]}
        st["_meta"]["posts_since_ad"] = st["_meta"].get("posts_since_ad", 0) + 1
    save_json(STATE / "posted.json", st)


def cmd_post(c, st, token, dry, limit):
    t = today()
    offers, ads = build.active_items(c, t)
    ads = [a for a in ads if "channel" in a["placements"]
           and st["ads"].get(a["id"], {}).get("count", 0) < a["channel_posts"]]
    queue = [o for o in offers if o["id"] not in st["offers"]]
    queue.sort(key=lambda o: (-o["priority"], o["valid_to"] is None, str(o["valid_to"] or ""), str(o["added"])))
    every = c["ads"]["every_n_posts"]
    done = 0
    while done < limit:
        if ads and st["_meta"].get("posts_since_ad", 0) >= every:
            ads.sort(key=lambda a: (st["ads"].get(a["id"], {}).get("last_posted", ""), -a["weight"]))
            ad = ads.pop(0)
            send_item(c, st, token, dry, ad, True)
        elif queue:
            send_item(c, st, token, dry, queue.pop(0), False)
            if dry:  # в dry-run счётчик не сохраняется — имитируем
                st["_meta"]["posts_since_ad"] = st["_meta"].get("posts_since_ad", 0) + 1
        else:
            break
        done += 1
    print(f"Опубликовано постов: {done}; осталось в очереди: {len(queue)}")


def cmd_sync(c, st, token, dry):
    t = today()
    offers, _, _ = build.collect(c, t)
    live = {o["id"] for o in offers if build.offer_status(o, c, t) != "expired"}
    n = 0
    for oid, rec in st["offers"].items():
        if rec.get("ended") or oid in live:
            continue
        for m in rec.get("messages", []):
            chat_langs = {ch: l for ch, l in targets(c)}
            langs = chat_langs.get(m["chat"], c["languages"])
            title = pick(rec["title"], langs[0])
            ended = " · ".join(dict.fromkeys(LABELS["ended"][l] for l in langs))
            text = f"⛔ <b>{H(ended)}</b>\n<s>{H(title)}</s>"
            try:
                api(token, "editMessageText", {"chat_id": m["chat"], "message_id": m["id"],
                                               "text": text, "parse_mode": "HTML"}, dry)
            except TgError as e:
                print(f"::warning::sync {oid}: {e}")
        rec["ended"] = True
        n += 1
    if not dry:
        save_json(STATE / "posted.json", st)
    print(f"Помечено завершёнными: {n}")


def cmd_digest(c, st, token, dry):
    offers, _ = build.active_items(c, today())
    offers.sort(key=lambda o: (o["valid_to"] is None, str(o["valid_to"] or ""), str(o["added"])))
    top = offers[: c["post"]["digest_items"]]
    if not top:
        print("Нет активных офферов для подборки")
        return
    for chat, langs in targets(c):
        lang = langs[0]
        rows = []
        for i, o in enumerate(top, 1):
            cat = c["categories"][o["category"]]
            perk = pick(o["perk"], lang)
            tail = f" — {H(perk)}" if perk else ""
            until = f" · ⏳ {o['valid_to'].strftime('%d.%m.')}" if o["valid_to"] else ""
            rows.append(f"{i}. {cat['emoji']} <a href=\"{H(deeplink(c, 'o_' + o['id']))}\">"
                        f"{H(pick(o['title'], lang))}</a>{tail}{until}")
        text = f"🗓 <b>{H(LABELS['digest'][lang])}</b>\n\n" + "\n".join(rows) + "\n\n#digest #ISIC #Kosice"
        api(token, "sendMessage", {
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": [[{"text": LABELS["catalog"][lang], "url": deeplink(c, "all")}]]}}, dry)


def cmd_menu(c, st, token, dry):
    for chat, langs in targets(c):
        lang = langs[0]
        cats = c["categories"]
        lines = [f"🧭 <b>{H(LABELS['menu'][lang])}</b>", H(LABELS["menu_hint"][lang]), ""]
        lines += [f"{v['emoji']} #{k} — {H(pick(v['name'], lang))}" for k, v in cats.items()]
        if len(langs) > 1:
            extra = []
            for l in langs[1:]:
                extra.append(f"<b>{l.upper()}</b> · " + H(LABELS["menu_hint"][l]) + "\n" +
                             "  ".join(f"{v['emoji']} {H(pick(v['name'], l))}" for v in cats.values()))
            lines += ["", "<blockquote expandable>" + "\n\n".join(extra) + "</blockquote>"]
        btns = [{"text": f"{v['emoji']} {pick(v['name'], lang)}", "url": deeplink(c, "c_" + k)} for k, v in cats.items()]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([{"text": LABELS["catalog"][lang], "url": deeplink(c, "all")}])
        payload = {"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}, "reply_markup": {"inline_keyboard": rows}}
        mid = st["_meta"].get("menu", {}).get(chat)
        if mid and not dry:
            try:
                api(token, "editMessageText", {**payload, "message_id": mid}, dry)
                print(f"Меню обновлено в {chat}")
                continue
            except TgError as e:
                print(f"::notice::старое меню не отредактировать ({e}), публикую заново")
        res = api(token, "sendMessage", payload, dry)
        api(token, "pinChatMessage", {"chat_id": chat, "message_id": res["message_id"],
                                      "disable_notification": True}, dry)
        if not dry:
            st["_meta"].setdefault("menu", {})[chat] = res["message_id"]
            save_json(STATE / "posted.json", st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["post", "digest", "menu", "sync"])
    ap.add_argument("--max", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--soft", action="store_true")
    a = ap.parse_args()
    dry = a.dry_run or os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token and not dry:
        msg = "Нет секрета TELEGRAM_BOT_TOKEN (Settings → Secrets and variables → Actions)"
        print(("::warning::" if a.soft else "::error::") + msg)
        return 0 if a.soft else 2
    c = cfg()
    st = load_json(STATE / "posted.json", {})
    st.setdefault("_meta", {})
    st.setdefault("offers", {})
    st.setdefault("ads", {})
    try:
        if a.command == "post":
            cmd_post(c, st, token, dry, a.max or c["post"]["max_posts_per_run"])
        elif a.command == "sync":
            cmd_sync(c, st, token, dry)
        elif a.command == "digest":
            cmd_digest(c, st, token, dry)
        else:
            cmd_menu(c, st, token, dry)
    except TgError as e:
        print(f"::error::{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
