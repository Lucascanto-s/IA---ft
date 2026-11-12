"""Brands and base model names of every car sold in Brazil, from the public FIPE table API.

The list is saved to arquivos/mapa/fipe_modelos.json and used to read product titles
("Bieleta BMW Serie 3 2012" -> BMW / Serie 3). Refresh it with:  python fipe_catalog.py
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Callable

import requests

PROJECT_DIR = Path(__file__).resolve().parent
FIPE_FILE = PROJECT_DIR / "arquivos" / "mapa" / "fipe_modelos.json"
FIPE_API = "https://parallelum.com.br/fipe/api/v1/carros/marcas"
HEADERS = {"User-Agent": "autopecas-image-generator/1.0"}

# First words that only make sense together with the next one ("Grand Siena", "Range Rover", "Tiggo 7").
MODEL_PREFIXES = {
    "serie", "classe", "grand", "gran", "range", "new", "nova", "novo", "space", "santa", "tiggo",
    "discovery", "cr", "hr", "wr", "c4", "rav", "land", "model", "série",
}


def _ascii_words(value: str) -> list[str]:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", ascii_value)


def base_model(fipe_name: str) -> str:
    """'Fusion 2.3 SEL 16V 162cv Aut.' -> 'fusion'; 'Grand Siena 1.4' -> 'grand siena'."""
    words = _ascii_words(fipe_name)
    if not words:
        return ""
    if words[0] in MODEL_PREFIXES and len(words) > 1 and not re.fullmatch(r"\d+\.?\d*", words[1] or ""):
        return f"{words[0]} {words[1]}"
    return words[0]


def _display_brand(name: str) -> str:
    return name if len(name) <= 4 or not name.isupper() else name.title()


def update_fipe_catalog(progress: Callable[[float, str], None] = lambda fraction, message: None) -> dict:
    brands = requests.get(FIPE_API, headers=HEADERS, timeout=20).json()
    catalog: dict[str, list[str]] = {}
    for index, brand in enumerate(brands, start=1):
        name = _display_brand(brand["nome"])
        progress(index / len(brands), f"FIPE: {name} ({index}/{len(brands)})")
        for attempt in range(3):
            try:
                response = requests.get(f"{FIPE_API}/{brand['codigo']}/modelos", headers=HEADERS, timeout=20)
                response.raise_for_status()
                models = response.json().get("modelos", [])
                break
            except (requests.RequestException, ValueError):
                time.sleep(2 + attempt * 3)
        else:
            continue
        catalog[name] = sorted({base_model(model["nome"]) for model in models} - {""})
        time.sleep(0.25)
    FIPE_FILE.parent.mkdir(parents=True, exist_ok=True)
    FIPE_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    load_fipe_catalog.cache_clear()
    return catalog


@lru_cache(maxsize=1)
def load_fipe_catalog() -> dict[str, list[str]]:
    if not FIPE_FILE.exists():
        return {}
    return json.loads(FIPE_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    result = update_fipe_catalog(lambda fraction, message: print(f"[{fraction:5.0%}] {message}", flush=True))
    print(len(result), "marcas,", sum(len(models) for models in result.values()), "modelos")
