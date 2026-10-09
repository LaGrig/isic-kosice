"""Общие функции для скриптов ISIC Košice."""
import datetime as dt
import json
import os
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("ISIC_ROOT") or Path(__file__).resolve().parent.parent)
STATE = ROOT / "state"
FALLBACK = ["en", "sk", "uk", "ru"]
INCLUDE_EXAMPLES = os.environ.get("INCLUDE_EXAMPLES", "").lower() in ("1", "true", "yes")


def today() -> dt.date:
    v = os.environ.get("TODAY")
    return dt.date.fromisoformat(v) if v else dt.date.today()


def cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yml").read_text("utf-8"))


def load_dir(name: str):
    """[(path, data)] для всех *.yml в папке (файлы с _ в начале пропускаются)."""
    out = []
    for p in sorted((ROOT / name).glob("*.yml")):
        if p.name.startswith("_"):
            continue
        try:
            data = yaml.safe_load(p.read_text("utf-8")) or {}
            if not isinstance(data, dict):
                data = {"_error": "файл должен быть словарём ключ: значение"}
        except yaml.YAMLError as e:
            data = {"_error": f"ошибка YAML: {str(e)[:160]}"}
        out.append((p, data))
    return out


def as_date(v):
    if v in (None, ""):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


def pick(d, lang, fallback=FALLBACK) -> str:
    """Текст на нужном языке, иначе первый доступный по цепочке запасных."""
    if not isinstance(d, dict):
        return str(d).strip() if d else ""
    for l in [lang, *fallback]:
        v = d.get(l)
        if v:
            return str(v).strip()
    return ""


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8")
