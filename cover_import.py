"""Build the local vehicle catalog from the store's own edited covers on BaseLinker.

Every edited cover has the compatible vehicle on top of a white canvas. This module reads
all product names, groups them by make/model/years, downloads a few covers per model
(different years first, to catch each generation), crops the vehicle and saves one catalog
row per distinct vehicle image, with the union of the years it was used for.

Run from the command line:  python cover_import.py
"""
from __future__ import annotations

import csv
import re
import sys
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from baselinker import BaseLinkerClient, extract_image_urls
from fipe_catalog import load_fipe_catalog
from image_processor import extract_vehicle_from_cover, load_image
from project_utils import sanitize_sku

PROJECT_DIR = Path(__file__).resolve().parent
CATALOG_FILE = PROJECT_DIR / "arquivos" / "mapa" / "vehicles.csv"
COVERS_DIR = PROJECT_DIR / "arquivos" / "veiculos" / "capas"
CATALOG_FIELDS = ["brand", "model", "years", "image_path"]

# Name token(s) -> canonical brand. Multi-word brands are matched as consecutive tokens.
BRAND_ALIASES = {
    ("ford",): "Ford", ("volkswagen",): "Volkswagen", ("vw",): "Volkswagen",
    ("chevrolet",): "Chevrolet", ("gm",): "Chevrolet", ("fiat",): "Fiat", ("toyota",): "Toyota",
    ("honda",): "Honda", ("hyundai",): "Hyundai", ("nissan",): "Nissan", ("renault",): "Renault",
    ("jeep",): "Jeep", ("subaru",): "Subaru", ("bmw",): "BMW", ("mercedes", "benz"): "Mercedes-Benz",
    ("mercedes",): "Mercedes-Benz", ("audi",): "Audi", ("kia",): "Kia", ("mitsubishi",): "Mitsubishi",
    ("peugeot",): "Peugeot", ("citroen",): "Citroen", ("volvo",): "Volvo", ("land", "rover"): "Land Rover",
    ("jac",): "JAC", ("caoa", "chery"): "Chery", ("chery",): "Chery", ("dodge",): "Dodge", ("ram",): "RAM",
    ("chrysler",): "Chrysler", ("suzuki",): "Suzuki", ("mini",): "Mini", ("porsche",): "Porsche",
    ("lexus",): "Lexus", ("byd",): "BYD", ("gwm",): "GWM", ("haval",): "GWM", ("troller",): "Troller",
}
# A model name that is two words: "Serie 3", "Range Rover", "Grand Siena", "Classe C".
MODEL_PREFIXES = {"serie", "classe", "grand", "gran", "range", "new", "nova", "novo", "space", "santa", "tiggo"}
NOT_MODELS = {
    "a", "ao", "de", "do", "da", "dos", "das", "e", "para", "com", "sem", "original", "lado", "ld", "le",
    "direito", "esquerdo", "direita", "esquerda", "dianteiro", "traseiro", "dianteira", "traseira",
    "motor", "cambio", "automatico", "manual", "flex", "diesel", "gasolina", "turbo", "sedan", "hatch",
}
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
MAX_COVERS_PER_MODEL = 6


def _years_in(tokens: list[str]) -> set[int]:
    years = sorted({int(token) for token in tokens if YEAR_RE.match(token)})
    if len(years) >= 2 and years[-1] - years[0] <= 15:
        return set(range(years[0], years[-1] + 1))
    return set(years)


def _model_after(tokens: list[str], start: int) -> str:
    words: list[str] = []
    for token in tokens[start:start + 3]:
        if YEAR_RE.match(token) or token in NOT_MODELS or re.fullmatch(r"\d+", token) and not words:
            break
        words.append(token)
        if token not in MODEL_PREFIXES:
            break
    return " ".join(words)


@dataclass
class Fitment:
    brand: str
    model: str
    years: set[int]


# FIPE brand names -> the names used in titles and in the catalog.
FIPE_BRAND_NAMES = {
    "GM - Chevrolet": "Chevrolet", "VW - VolksWagen": "Volkswagen", "Kia Motors": "Kia",
    "Caoa Chery": "Chery", "Caoa Chery/Chery": "Chery", "Caoa Changan": "Changan", "Citroën": "Citroen",
    "MINI": "Mini", "smart": "Smart", "Great Wall": "GWM",
}
# Portuguese words that are also FIPE model names; never treat them as a car without a brand.
COMMON_WORDS = {
    "tipo", "premio", "idea", "bravo", "classic", "sport", "space", "cross", "panorama", "grand", "line",
    "life", "prima", "plus", "max", "top", "super", "turbo", "coupe", "city", "trend", "free", "work",
    "door", "light", "motor", "porta", "cabine", "pick", "furgao", "truck", "carga", "duna", "club",
}


@lru_cache(maxsize=1)
def _fipe_index() -> tuple[dict[tuple[str, ...], str], dict[str, set[str]], dict[str, str]]:
    """(brand token tuple -> brand, brand -> base models, unique model -> brand)."""
    brand_tokens: dict[tuple[str, ...], str] = dict(BRAND_ALIASES)
    models_by_brand: dict[str, set[str]] = defaultdict(set)
    for fipe_brand, models in load_fipe_catalog().items():
        brand = FIPE_BRAND_NAMES.get(fipe_brand, fipe_brand)
        key = tuple(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", brand).encode("ascii", "ignore")
                                .decode().lower()))
        if key and key not in brand_tokens:
            brand_tokens[key] = brand
        models_by_brand[brand].update(model for model in models if len(model) >= 2)
    owners: dict[str, set[str]] = defaultdict(set)
    for brand, models in models_by_brand.items():
        for model in models:
            owners[model].add(brand)
    unique = {
        model: next(iter(brands)) for model, brands in owners.items()
        if len(brands) == 1 and len(model.replace(" ", "")) >= 4 and model not in COMMON_WORDS
        and not model.isdigit()
    }
    return brand_tokens, dict(models_by_brand), unique


def _fipe_model_at(tokens: list[str], start: int, models: set[str]) -> str:
    for length in (2, 1):
        candidate = " ".join(tokens[start:start + length])
        if len(tokens[start:start + length]) == length and candidate in models:
            return candidate
    return ""


def parse_name(name: str, model_brands: dict[str, str] | None = None) -> Fitment | None:
    """Find make, model and years in a product title such as 'Trava Banco Ford Fusion 2007 2008'.

    Brands and models come from the FIPE table (fipe_catalog.py) plus local aliases.
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    tokens = re.findall(r"[a-z0-9]+", ascii_name)
    years = _years_in(tokens)
    brand_tokens, models_by_brand, unique_models = _fipe_index()
    ordered_brands = sorted(brand_tokens.items(), key=lambda item: -len(item[0]))
    for index in range(len(tokens)):
        for alias, brand in ordered_brands:
            if tuple(tokens[index:index + len(alias)]) != alias:
                continue
            start = index + len(alias)
            model = _fipe_model_at(tokens, start, models_by_brand.get(brand, set()))
            if not model or model in MODEL_PREFIXES:
                model = _model_after(tokens, start) or model
            if model:
                return Fitment(brand, model, years)
    # No brand in the title: recognise the model ("Dobradiça Impreza" -> Subaru Impreza).
    known = {**unique_models, **(model_brands or {})}
    for index in range(len(tokens)):
        for length in (2, 1):
            candidate = " ".join(tokens[index:index + length])
            if len(tokens[index:index + length]) == length and len(candidate) >= 4 and candidate in known:
                return Fitment(known[candidate], candidate, years)
    return None


GENERIC_TITLE_WORDS = NOT_MODELS | {
    "kit", "par", "jogo", "peca", "pecas", "novo", "nova", "usado", "usada", "genuino", "original",
    "v", "cv", "gdi", "tb", "16v", "8v", "12v", "24v", "mpi", "tsi", "tfsi", "flex", "cabine", "dupla",
}
RELATED_DIR = PROJECT_DIR / "arquivos" / "veiculos" / "capas" / "relacionados"


def vehicle_tokens(title: str) -> tuple[str, set[str], set[int]] | None:
    """Brand plus the words that identify the car (model, versions like 320i) and years."""
    fitment = parse_name(title)
    if not fitment:
        return None
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    tokens = re.findall(r"[a-z0-9]+", ascii_title)
    brand_words = set(re.findall(r"[a-z0-9]+", fitment.brand.lower())) | {"vw", "gm", "caoa", "benz"}
    model_words = set(fitment.model.split())
    start = next((index for index, token in enumerate(tokens) if token in model_words or token in brand_words), 0)
    words = {
        token for token in tokens[start:]
        if token not in brand_words and token not in GENERIC_TITLE_WORDS and not YEAR_RE.match(token)
        and not re.fullmatch(r"\d{1,2}|\d{5,}", token)
    } | model_words
    return fitment.brand, words, fitment.years


def find_related_covers(client: BaseLinkerClient, title: str, exclude_product_id: str = "",
                        limit: int = 6) -> list[tuple[str, Path]]:
    """Covers of other products for the same car, cropped to the car(s). Returns (sku, image path)."""
    parsed = vehicle_tokens(title)
    if not parsed:
        return []
    brand, words, years = parsed
    search = brand.split()[0] if brand not in ("Mercedes-Benz", "Land Rover") else brand.split("-")[0].split()[0]
    products: dict[str, dict] = {}
    for query in dict.fromkeys([search, *sorted(words, key=len, reverse=True)[:2]]):
        try:
            result = client.call("getInventoryProductsList", {"inventory_id": client.inventory_id, "filter_name": query})
            products.update(result.get("products", {}))
        except Exception:
            continue
    scored = []
    for product_id, item in products.items():
        if str(product_id) == str(exclude_product_id):
            continue
        other = vehicle_tokens(item.get("name", ""))
        if not other or other[0] != brand:
            continue
        shared = len(words & other[1])
        if not shared:
            continue
        year_overlap = len(years & other[2]) if years and other[2] else 0
        if years and other[2] and not year_overlap:
            continue
        scored.append((shared, year_overlap, str(product_id), str(item.get("sku", "")).strip()))
    scored.sort(reverse=True)
    chosen = scored[: limit + 3]
    if not chosen:
        return []
    data = client.call(
        "getInventoryProductsData", {"inventory_id": client.inventory_id, "products": [c[2] for c in chosen]}
    ).get("products", {})

    def crop(entry):
        _, _, product_id, sku = entry
        urls = extract_image_urls((data.get(product_id) or {}).get("images", {}))
        if not urls:
            return None
        try:
            cars = extract_vehicle_from_cover(load_image(urls[0]), allow_multiple=True)
        except Exception:
            return None
        return (sku, product_id, cars) if cars is not None else None

    RELATED_DIR.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, Path]] = []
    signatures: list[np.ndarray] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for found in pool.map(crop, chosen):
            if not found or len(results) >= limit:
                continue
            sku, product_id, cars = found
            signature = _signature(cars)
            if any((signature != other).mean() < 0.12 for other in signatures):
                continue  # same car picture as an earlier result
            signatures.append(signature)
            path = RELATED_DIR / f"{sanitize_sku(product_id)}.png"
            cars.save(path, format="PNG")
            results.append((sku, path))
    return results


@dataclass
class _Cluster:
    signature: np.ndarray
    image: Image.Image
    years: set[int] = field(default_factory=set)
    count: int = 0


def _signature(image: Image.Image) -> np.ndarray:
    small = np.asarray(image.convert("L").resize((32, 12)), dtype=np.float32)
    return small > small.mean()


def _pick_products(products: list[tuple[str, set[int]]], limit: int) -> list[str]:
    """Prefer products whose year sets differ, so each vehicle generation is sampled."""
    chosen, seen_years = [], []
    for product_id, years in sorted(products, key=lambda item: min(item[1]) if item[1] else 0):
        if any(years == other for other in seen_years):
            continue
        chosen.append(product_id)
        seen_years.append(years)
    for product_id, _ in products:
        if len(chosen) >= limit:
            break
        if product_id not in chosen:
            chosen.append(product_id)
    if len(chosen) > limit:
        step = len(chosen) / limit
        chosen = [chosen[int(index * step)] for index in range(limit)]
    return chosen


def _format_years(years: set[int]) -> str:
    if not years:
        return ""
    return str(min(years)) if len(years) == 1 else f"{min(years)} a {max(years)}"


def import_covers(
    client: BaseLinkerClient,
    progress: Callable[[float, str], None] = lambda fraction, message: None,
    max_covers_per_model: int = MAX_COVERS_PER_MODEL,
) -> dict[str, int]:
    progress(0.0, "Lendo os nomes dos produtos no BaseLinker...")
    products: dict[str, dict] = {}
    page = 1
    while True:
        batch = client.call("getInventoryProductsList", {"inventory_id": client.inventory_id, "page": page})
        products.update(batch.get("products", {}))
        if len(batch.get("products", {})) < 1000:
            break
        page += 1

    parsed: dict[str, Fitment] = {}
    for product_id, item in products.items():
        fitment = parse_name(item.get("name", ""))
        if fitment:
            parsed[product_id] = fitment
    # Learn model -> brand from titles that name both, then retry titles without a brand.
    votes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for fitment in parsed.values():
        if " " not in fitment.model:
            votes[fitment.model][fitment.brand] += 1
    model_brands = {
        model: max(brands, key=brands.get) for model, brands in votes.items()
        if sum(brands.values()) >= 3 and max(brands.values()) / sum(brands.values()) >= 0.8
    }
    for product_id, item in products.items():
        if product_id not in parsed:
            fitment = parse_name(item.get("name", ""), model_brands)
            if fitment:
                parsed[product_id] = fitment

    groups: dict[tuple[str, str], list[tuple[str, set[int]]]] = defaultdict(list)
    for product_id, fitment in parsed.items():
        groups[(fitment.brand, fitment.model)].append((product_id, fitment.years))

    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    new_rows: list[dict[str, str]] = []
    total = len(groups)
    for done, ((brand, model), members) in enumerate(sorted(groups.items()), start=1):
        progress(done / (total + 1), f"{brand} {model.title()} ({done}/{total})")
        chosen = _pick_products(members, max_covers_per_model)
        years_by_id = dict(members)
        try:
            data = client.call(
                "getInventoryProductsData", {"inventory_id": client.inventory_id, "products": chosen}
            ).get("products", {})
        except Exception:
            continue

        def crop(product_id: str):
            urls = extract_image_urls((data.get(product_id) or {}).get("images", {}))
            if not urls:
                return product_id, None
            try:
                return product_id, extract_vehicle_from_cover(load_image(urls[0]))
            except Exception:
                return product_id, None

        clusters: list[_Cluster] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for product_id, vehicle in pool.map(crop, chosen):
                if vehicle is None:
                    continue
                signature = _signature(vehicle)
                cluster = next((c for c in clusters if (c.signature != signature).mean() < 0.12), None)
                if cluster is None:
                    cluster = _Cluster(signature, vehicle)
                    clusters.append(cluster)
                cluster.years |= years_by_id.get(product_id, set())
                cluster.count += 1

        for index, cluster in enumerate(clusters, start=1):
            years = _format_years(cluster.years)
            key = sanitize_sku(f"{brand}_{model}_{years}_{index}").lower()
            path = COVERS_DIR / f"{key}.png"
            cluster.image.save(path, format="PNG")
            new_rows.append({
                "brand": brand, "model": model.title(), "years": years,
                "image_path": path.relative_to(PROJECT_DIR).as_posix(),
            })

    # Keep manual rows; replace only rows created by a previous import.
    covers_prefix = COVERS_DIR.relative_to(PROJECT_DIR).as_posix() + "/"
    rows = []
    if CATALOG_FILE.exists():
        with CATALOG_FILE.open(newline="", encoding="utf-8") as file:
            rows = [row for row in csv.DictReader(file) if not row.get("image_path", "").startswith(covers_prefix)]
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CATALOG_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CATALOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows + new_rows)
    progress(1.0, "Concluído")
    return {"products": len(products), "recognized": len(parsed), "models": total, "vehicles": len(new_rows)}


def _client_from_env() -> BaseLinkerClient:
    import os

    from dotenv import load_dotenv

    load_dotenv(PROJECT_DIR / ".env")
    return BaseLinkerClient(
        os.environ["BASELINKER_TOKEN"], os.environ["BASELINKER_INVENTORY_ID"],
        os.getenv("BASELINKER_API_URL", "https://api.baselinker.com/connector.php"),
    )


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else MAX_COVERS_PER_MODEL
    summary = import_covers(_client_from_env(), lambda f, m: print(f"[{f:5.0%}] {m}", flush=True), limit)
    print(summary)
