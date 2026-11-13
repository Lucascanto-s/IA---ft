import csv
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

import streamlit as st
import pandas as pd
from dotenv import load_dotenv
from PIL import Image, ImageOps

from baselinker import BaseLinkerClient, BaseLinkerError, extract_image_urls
from cover_import import find_related_covers, parse_name
from image_processor import (
    compose_cover,
    compose_vehicle_group,
    content_bbox,
    cover_base_sizes,
    create_set,
    enhance_product,
    extract_vehicle_from_cover,
    fit_on_white,
    load_image,
    prepare_vehicle_image,
    product_image,
    public_urls,
    reposition_image,
    sharpen,
    validate_output,
)
from project_utils import normalized_tokens, sanitize_sku
from ui.drag_canvas import drag_canvas, image_data_url
from ui.theme import apply_theme, flow_steps
from vehicle_search import VehicleSearchError, find_vehicle_images


load_dotenv()
PROJECT_DIR = Path(__file__).resolve().parent
FILES_DIR = PROJECT_DIR / "arquivos"
VEHICLE_MAP_FILE = Path(os.getenv("VEHICLE_MAP_FILE", FILES_DIR / "mapa" / "vehicle_map.csv"))
# Maximum preview widths in px: images shrink with the column but never grow past these,
# so browser zoom or very wide screens do not blow them up.
PREVIEW_PAIR = 480
PREVIEW_WIDE = 720
PREVIEW_THUMB = 280
VEHICLE_LIBRARY_FILE = FILES_DIR / "mapa" / "vehicles.csv"

st.set_page_config(page_title="Studio de imagens | Autopeças", page_icon=":material/photo_camera:", layout="wide")
apply_theme()


def _years(value: str) -> set[int]:
    years = sorted({int(item) for item in re.findall(r"\b(?:19|20)\d{2}\b", value)})
    if len(years) >= 2 and years[-1] - years[0] <= 15:
        return set(range(years[0], years[-1] + 1))
    return set(years)


KNOWN_MODELS = {
    "ford": ["Bronco", "Maverick", "Fusion", "Fiesta", "Focus", "EcoSport", "Ranger", "Mustang", "Ka", "Territory", "Edge", "Escape"],
    "volkswagen": ["Gol", "Golf", "Jetta", "Passat", "Polo", "Virtus", "Taos", "Tiguan", "Amarok", "Saveiro", "Fox"],
    "chevrolet": ["Onix", "Cruze", "Tracker", "S10", "Spin", "Cobalt", "Prisma", "Celta", "Astra", "Vectra"],
    "fiat": ["Uno", "Palio", "Siena", "Strada", "Toro", "Argo", "Mobi", "Pulse", "Fastback"],
    "toyota": ["Corolla", "Hilux", "Yaris", "Etios", "RAV4", "SW4"],
    "honda": ["Civic", "Fit", "City", "HR-V", "CR-V", "Accord"],
    "hyundai": ["HB20", "Tucson", "ix35", "Creta", "Elantra", "Azera"],
    "nissan": ["Kicks", "Versa", "Sentra", "March", "Frontier"],
    "renault": ["Logan", "Sandero", "Duster", "Kwid", "Oroch", "Captur"],
    "jeep": ["Renegade", "Compass", "Commander", "Cherokee", "Wrangler"],
    "subaru": ["Impreza", "Forester", "XV", "Outback", "Legacy", "WRX"],
}


def _display_name(value: str) -> str:
    """'ford' -> 'Ford', '320i' stays '320i', 'BMW' stays 'BMW'."""
    return " ".join(
        word.capitalize() if word.isalpha() and (word.islower() or word.isupper() and len(word) > 3) else word
        for word in value.split()
    )


def _initial_fitments(sku: str, product: dict) -> list[dict[str, str]]:
    return [
        {**row, "brand": _display_name(row["brand"]), "model": _display_name(row["model"])}
        for row in _raw_initial_fitments(sku, product)
    ]


def _year_like(model: str) -> bool:
    """'2008' (Peugeot) reads as a year in titles such as 'Impreza 2007 2008 2009'."""
    return bool(re.fullmatch(r"(?:19|20)\d{2}", model.strip()))


FAMILY_WORDS = {"serie", "series", "série", "classe", "class", "linha"}
# Version codes such as 320i, 328i, 530d (BMW) or C180, E250 (Mercedes).
VARIANT_CODE = re.compile(r"^(?:\d{3}[a-z]{1,3}|[a-z]\d{3}[a-z]{0,2})$", re.IGNORECASE)


def _variant_codes(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", text)
    return list(dict.fromkeys(word for word in words if VARIANT_CODE.match(word)))


def _raw_initial_fitments(sku: str, product: dict) -> list[dict[str, str]]:
    """One row per car: expands '320i 328i 335i' in the title into separate vehicles."""
    rows = _title_fitments(sku, product)
    raw_name = str((product.get("text_fields") or {}).get("name", "")) if isinstance(product, dict) else ""
    codes = _variant_codes(raw_name)
    brand = next((row["brand"] for row in rows if row.get("brand")), "")
    if not codes or not brand:
        return rows
    years = next((row["years"] for row in rows if row.get("years")), "")
    code_keys = {code.casefold() for code in codes}
    kept = [
        row for row in rows
        if row["model"].casefold() not in code_keys
        and not (normalized_tokens(row["model"]) & FAMILY_WORDS)
        and not re.fullmatch(r"(?i)s[ée]rie\s*\d+", row["model"].strip())
    ]
    return kept + [{"brand": brand, "model": code, "years": years} for code in codes]


def _title_fitments(sku: str, product: dict) -> list[dict[str, str]]:
    """Suggest separate make/model/year rows from the product title and local catalog."""
    map_row = vehicle_for_sku(sku) or {}
    query = map_row.get("vehicle_key", "").replace("_", " ").strip() or vehicle_query_from_product(product)
    title = vehicle_query_from_product(product)
    search_text = f"{query} {title}".strip()
    tokens = normalized_tokens(search_text)
    years = sorted(_years(search_text))
    year_range = f"{years[0]}-{years[-1]}" if len(years) > 1 else (str(years[0]) if years else "")

    catalog_rows = []
    if VEHICLE_LIBRARY_FILE.exists():
        with VEHICLE_LIBRARY_FILE.open(newline="", encoding="utf-8") as file:
            catalog_rows = list(csv.DictReader(file))
    possible = [(row.get("brand", "").strip(), row.get("model", "").strip()) for row in catalog_rows]
    for brand, models in KNOWN_MODELS.items():
        possible.extend((brand.title(), model) for model in models)

    matches = []
    seen = set()
    seen_models = set()
    for brand, model in possible:
        brand_tokens = normalized_tokens(brand)
        model_tokens = normalized_tokens(model)
        key = (" ".join(sorted(brand_tokens)), " ".join(sorted(model_tokens)))
        if brand_tokens and model_tokens and brand_tokens.issubset(tokens) and model_tokens.issubset(tokens) and key not in seen:
            matches.append({"brand": brand, "model": model, "years": year_range})
            seen.add(key)
            seen_models.add(key[1])

    # Titles often list other cars without the brand ("Audi A3 Golf Jetta", "Dobradiça Impreza"):
    # infer the brand from a distinctive model name. A model that looks like a year (Peugeot 2008)
    # only counts when its brand is written in the title, otherwise "2007 2008 2009" becomes a car.
    brand_names = {" ".join(sorted(normalized_tokens(brand))) for brand, _ in possible}
    for brand, model in possible:
        model_tokens = normalized_tokens(model)
        key = (" ".join(sorted(normalized_tokens(brand))), " ".join(sorted(model_tokens)))
        if (brand and model_tokens and len(model.replace(" ", "")) >= 4
                and not _year_like(model) and key[1] not in brand_names
                and model_tokens.issubset(tokens) and key not in seen and key[1] not in seen_models):
            matches.append({"brand": brand, "model": model, "years": year_range})
            seen.add(key)
            seen_models.add(key[1])
    if matches:
        # "Peugeot 2008 2012 2015": the model number is not one of the years.
        model_years = {int(row["model"]) for row in matches if _year_like(row["model"])}
        if model_years:
            years = sorted(_years(" ".join(
                word for word in re.findall(r"\b(?:19|20)\d{2}\b", search_text) if int(word) not in model_years
            )))
            year_range = f"{years[0]}-{years[-1]}" if len(years) > 1 else (str(years[0]) if years else "")
            matches = [{**row, "years": year_range} for row in matches]
        return matches

    # Generic make/model parser shared with the cover import (knows ~35 brands).
    raw_name = str((product.get("text_fields") or {}).get("name", "")) if isinstance(product, dict) else ""
    model_brands = {
        normalized_model: brand.title() if brand.islower() else brand
        for brand, model in possible
        for normalized_model in [" ".join(sorted(normalized_tokens(model)))]
        if brand and len(normalized_model) >= 4 and " " not in normalized_model
    }
    parsed = parse_name(raw_name or title, model_brands)
    if parsed:
        parsed_years = sorted(parsed.years) or years
        parsed_range = (f"{parsed_years[0]}-{parsed_years[-1]}" if len(parsed_years) > 1
                        else (str(parsed_years[0]) if parsed_years else ""))
        return [{"brand": parsed.brand, "model": parsed.model, "years": parsed_range}]

    # Fall back to one editable suggestion when the model is not in the known list.
    brand = next((name.title() for name in KNOWN_MODELS if normalized_tokens(name).issubset(tokens)), "")
    model = title
    for word in re.findall(r"\b(?:19|20)\d{2}\b", model):
        model = model.replace(word, " ")
    model = re.sub(r"\s+", " ", model).strip()
    if brand:
        model = re.sub(re.escape(brand), "", model, flags=re.IGNORECASE).strip(" -,:;")
    if not model and map_row.get("vehicle_key"):
        model = map_row["vehicle_key"].replace("_", " ").strip()
    return [{"brand": brand, "model": model, "years": year_range}]


def vehicle_for_sku(sku: str) -> dict[str, str] | None:
    if not VEHICLE_MAP_FILE.exists():
        return None
    with VEHICLE_MAP_FILE.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            if row.get("sku", "").strip() == sku:
                return row
    return None


def vehicle_query_from_product(product: dict) -> str:
    text_fields = product.get("text_fields", {})
    if isinstance(text_fields, dict):
        name = text_fields.get("name")
        if isinstance(name, str) and name.strip():
            words = re.findall(r"[A-Za-zÀ-ÿ0-9.,]+", name)
            part_words = {
                "a", "ao", "banco", "cambio", "com", "coxim", "da", "de", "do",
                "direita", "direito", "dianteiro", "esquerda", "esquerdo", "para",
                "peca", "peça", "traseiro", "traseira", "suporte", "trava", "tubo",
                "cano", "alta", "pressao", "pressão", "mangueira", "conjunto",
                "par", "dobradica", "dobradiça", "porta", "fechadura", "eletrica",
                "elétrica", "tensor", "correia", "carcaca", "carcaça", "valvula",
                "válvula", "termostatica", "termostática",
            }
            filtered = [
                word for word in words
                if word.casefold() not in {item.strip() for item in part_words}
                and not re.fullmatch(r"\d+[.,]\d+", word)
            ]
            return " ".join(filtered).strip()
        values = [value.strip() for value in text_fields.values()
                  if isinstance(value, str) and value.strip()]
        if values:
            return " ".join(values)
    return ""


def local_vehicle_for_fitment(brand: str, model: str, years: str = "") -> str:
    matches = local_vehicles_for_fitment(brand, model, years)
    return matches[0] if matches else ""


def local_vehicles_for_fitment(brand: str, model: str, years: str = "") -> list[str]:
    """All catalog images for this make/model whose years overlap, best match first."""
    if not VEHICLE_LIBRARY_FILE.exists():
        return []
    requested_years = _years(years)
    brand_key = normalized_tokens(brand)
    model_key = normalized_tokens(model)
    candidates = []
    with VEHICLE_LIBRARY_FILE.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            if normalized_tokens(row.get("brand", "")) != brand_key:
                continue
            if normalized_tokens(row.get("model", "")) != model_key:
                continue
            row_years = _years(row.get("years", ""))
            if requested_years and row_years and not requested_years.intersection(row_years):
                continue
            image_path = row.get("image_path", "").strip()
            if image_path:
                resolved_path = Path(image_path).expanduser()
                if not resolved_path.is_absolute():
                    resolved_path = PROJECT_DIR / resolved_path
                if resolved_path.exists():
                    exact_years = int(bool(requested_years) and requested_years == row_years)
                    year_overlap = len(requested_years.intersection(row_years))
                    has_year_match = int(bool(year_overlap))
                    candidates.append((exact_years, has_year_match, year_overlap, -len(row_years), str(resolved_path)))
    return list(dict.fromkeys(candidate[4] for candidate in sorted(candidates, reverse=True)))


def client() -> BaseLinkerClient:
    token = os.getenv("BASELINKER_TOKEN", "")
    inventory_id = os.getenv("BASELINKER_INVENTORY_ID", "")
    if not token or not inventory_id:
        raise ValueError("Preencha BASELINKER_TOKEN e BASELINKER_INVENTORY_ID no arquivo .env")
    return BaseLinkerClient(token, inventory_id, os.getenv(
        "BASELINKER_API_URL", "https://api.baselinker.com/connector.php"
    ))


def register_vehicle(brand: str, model: str, years: str, uploaded_file) -> Path:
    with Image.open(io.BytesIO(uploaded_file.getvalue())) as uploaded_image:
        return save_vehicle_image(brand, model, years, uploaded_image.convert("RGBA"))


def save_vehicle_image(brand: str, model: str, years: str, image: Image.Image) -> Path:
    brand = brand.strip()
    model = model.strip()
    years = years.strip()
    if not brand or not model:
        raise ValueError("Informe marca e modelo do veículo")
    safe_key = sanitize_sku(f"{brand}_{model}_{years}").lower()
    vehicle_dir = FILES_DIR / "veiculos"
    vehicle_dir.mkdir(parents=True, exist_ok=True)
    image_path = vehicle_dir / f"{safe_key}.png"
    image.convert("RGBA").save(image_path, format="PNG")

    VEHICLE_LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if VEHICLE_LIBRARY_FILE.exists():
        with VEHICLE_LIBRARY_FILE.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
    relative_path = image_path.relative_to(PROJECT_DIR).as_posix()
    rows = [row for row in rows if not (
        normalized_tokens(row.get("brand", "")) == normalized_tokens(brand)
        and normalized_tokens(row.get("model", "")) == normalized_tokens(model)
        and (
            (_years(row.get("years", "")) == _years(years))
            or bool(_years(row.get("years", "")) and _years(row.get("years", "")) & _years(years))
        )
    )]
    rows.append({"brand": brand, "model": model, "years": years, "image_path": relative_path})
    with VEHICLE_LIBRARY_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["brand", "model", "years", "image_path"])
        writer.writeheader()
        writer.writerows(rows)
    return image_path


DEFAULT_CONTRAST_PCT = 108
DEFAULT_SHARPNESS_PCT = 100


def _reset_part_treatment() -> None:
    st.session_state["part_contrast"] = DEFAULT_CONTRAST_PCT
    st.session_state["part_sharpness"] = DEFAULT_SHARPNESS_PCT


def _apply_part_treatment(result: dict, contrast_pct: int, sharpness_pct: int) -> None:
    """Re-treat the part (cover layer and images 2-4) when contrast/sharpness change."""
    raw = result.get("product_raw")
    settings = (contrast_pct, sharpness_pct)
    if not raw or result.get("treatment") == settings:
        return
    params = {"contrast": contrast_pct / 100, "sharpness": sharpness_pct / 100}
    # Same photo in several positions is treated once; distinct photos in parallel.
    unique = {id(image): image for image in raw.values()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        treated = dict(zip(unique, pool.map(lambda image: enhance_product(image, **params),
                                            unique.values())))
    if result.get("cover_layers"):
        result["cover_layers"]["product"] = treated[id(raw[1])]
    for index in range(2, 5):
        result["editor_sources"][index] = fit_on_white(treated[id(raw[index])], (10, 10, 1190, 1190))
    result["treatment"] = settings
    result.pop("drag_assets", None)


def _reset_editor_state() -> None:
    for key in ("cover_vehicle_x", "cover_vehicle_y", "cover_vehicle_scale",
                "cover_product_x", "cover_product_y", "cover_product_scale",
                "part_contrast", "part_sharpness"):
        st.session_state.pop(key, None)
    for index in range(2, 5):
        for suffix in ("x", "y", "scale"):
            st.session_state.pop(f"image_{index}_{suffix}", None)


def _fetch_sku_data(sku: str) -> dict:
    product_id, product = client().find_product(sku.strip())
    image_urls = extract_image_urls(product.get("images", product))
    if not image_urls:
        raise ValueError("Nenhuma imagem foi encontrada no produto")
    return {
        "sku": sku.strip(),
        "product_id": product_id,
        "product": product,
        "product_images": image_urls,
        "fitments": _initial_fitments(sku, product),
    }


def _load_sku(sku: str, data: dict | None = None) -> None:
    data = data or _fetch_sku_data(sku)
    safe_sku = sanitize_sku(sku)
    batch = st.session_state.get("sku_batch") or {"skus": [sku.strip()], "current_index": 0}
    st.session_state["pending_vehicle_review"] = {
        "sku": safe_sku,
        "source_sku": data["sku"],
        "batch_index": batch["current_index"],
        "batch_total": len(batch["skus"]),
        "product_id": data["product_id"],
        "product": data["product"],
        "product_images": data["product_images"],
        "fitments": data["fitments"],
        "choices": None,
        "fitment_version": 0,
    }
    st.session_state.pop("batch_error", None)
    st.session_state.pop("result", None)


def _start_sku_batch(skus: list[str]) -> None:
    batch = {
        "skus": skus,
        "items": [],
        "current_index": 0,
        "completed": False,
    }
    st.session_state["sku_batch"] = batch
    st.session_state.pop("result", None)
    st.session_state.pop("pending_vehicle_review", None)
    st.session_state.pop("batch_error", None)
    for sku in skus:
        try:
            batch["items"].append(_fetch_sku_data(sku))
        except (BaseLinkerError, ValueError, OSError) as error:
            batch["items"].append({"sku": sku, "error": str(error)})
    st.session_state["sku_batch"] = batch
    _load_batch_index(0)


def _load_batch_index(index: int) -> bool:
    batch = st.session_state.get("sku_batch")
    if not batch or index >= len(batch["skus"]):
        return False
    batch["current_index"] = index
    st.session_state["sku_batch"] = batch
    item = batch["items"][index]
    if item.get("error"):
        st.session_state["batch_error"] = {"sku": batch["skus"][index], "error": item["error"]}
        st.session_state.pop("pending_vehicle_review", None)
        st.session_state.pop("result", None)
        return False
    _load_sku(batch["skus"][index], item)
    return True


def _load_next_batch_sku() -> bool:
    batch = st.session_state.get("sku_batch")
    if not batch:
        return False
    next_index = batch["current_index"] + 1
    if next_index >= len(batch["skus"]):
        batch["completed"] = True
        st.session_state["sku_batch"] = batch
        st.session_state.pop("batch_error", None)
        st.session_state.pop("result", None)
        st.session_state.pop("pending_vehicle_review", None)
        return False
    return _load_batch_index(next_index)


def _search_fitments(pending: dict, fitments: list[dict[str, str]]) -> list[dict]:
    map_row = vehicle_for_sku(pending.get("source_sku", pending["sku"])) or {}
    choices = []
    sku_source = map_row.get("vehicle_image_url", "").strip()
    # Cars already used on covers of other products for the same vehicle come first.
    related: list[tuple[str, Path]] = []
    title = str((pending.get("product", {}).get("text_fields") or {}).get("name", ""))
    if title:
        try:
            related = find_related_covers(client(), title, pending.get("product_id", ""))
        except Exception:
            related = []
    for position, fitment in enumerate(fitments):
        brand = str(fitment.get("brand", "")).strip()
        model = str(fitment.get("model", "")).strip()
        years = str(fitment.get("years", "")).strip()
        if not brand or not model:
            continue
        alternatives: list[str] = []
        origins: list[str] = []
        if position == 0:
            alternatives += [str(path) for _, path in related]
            origins += [f"Capa do SKU {sku} (BaseLinker)" for sku, _ in related]
        local = local_vehicles_for_fitment(brand, model, years)
        alternatives += local
        origins += ["Catálogo local"] * len(local)
        if not alternatives and sku_source and len(fitments) == 1:
            alternatives, origins = [sku_source], ["Imagem cadastrada para este SKU"]
        error_message = ""
        if not alternatives:
            try:
                query = " ".join(part for part in (brand, model, years) if part)
                online = find_vehicle_images(query)
                alternatives += online
                origins += ["Busca online"] * len(online)
            except Exception as error:
                error_message = str(error)
        source = alternatives[0] if alternatives else ""
        origin = origins[0] if origins else error_message
        choices.append({
            "brand": brand,
            "model": model,
            "years": years,
            "source": source,
            "origin": origin,
            "status": "pending" if source else "missing",
            "image": None,
            "alternatives": alternatives,
            "alternative_origins": origins,
            "alternative_index": 0,
        })
    return choices


def _generate_sku(pending: dict, approved_choices: list[dict]) -> None:
    image_urls = pending["product_images"]
    vehicle_images = [choice["image"] for choice in approved_choices]
    vehicle_group = compose_vehicle_group(vehicle_images)

    output_dir = Path(os.getenv("OUTPUT_DIR", FILES_DIR / "saida")) / pending["sku"]
    output_dir.mkdir(parents=True, exist_ok=True)
    generated = create_set(image_urls, vehicle_group, str(output_dir))
    sources = [load_image(url) for url in image_urls[:4]]
    while len(sources) < 4:
        sources.append(sources[-1].copy())
    # Part cut out but not yet treated, so contrast/sharpness can be changed in the editor.
    padded_urls = (list(image_urls[:4]) + [image_urls[:4][-1]] * 4)[:4]
    product_raw = {
        1: product_image(image_urls[1] if len(image_urls) > 1 else image_urls[0], enhance=False),
        **{index: product_image(padded_urls[index - 1], enhance=False) for index in range(2, 5)},
    }
    # The treated part is filled in by _apply_part_treatment below.
    cover_layers = {"vehicle": vehicle_group, "product": product_raw[1]}
    previews = [load_image(str(output_dir / f"{index}.jpg")) if (output_dir / f"{index}.jpg").exists()
               else Image.new("RGB", (1200, 1200), "white") for index in range(1, 5)]
    st.session_state["result"] = {
        "sku": pending["sku"],
        "product_id": pending["product_id"],
        "product_name": _product_name(pending.get("product", {})),
        "batch_index": pending["batch_index"],
        "batch_total": pending["batch_total"],
        "published": False,
        "product_images": image_urls,
        "paths": generated.paths,
        "warnings": generated.warnings,
        "cover_layers": cover_layers,
        "approved_vehicles": [
            {key: choice[key] for key in ("brand", "model", "years", "origin")}
            for choice in approved_choices
        ],
        "editor_sources": {
            1: load_image(str(generated.paths[0])) if cover_layers is None else sources[0],
            2: sources[1],
            3: sources[2],
            4: sources[3],
        },
        "previews": previews,
        "backup_path": None,
        "product_raw": product_raw,
    }
    _apply_part_treatment(st.session_state["result"], DEFAULT_CONTRAST_PCT, DEFAULT_SHARPNESS_PCT)
    _reset_editor_state()
    st.session_state.pop("pending_vehicle_review", None)


def _drag_assets(result: dict) -> dict:
    """Browser copies of each layer, encoded once per generated SKU."""
    assets = result.get("drag_assets")
    if assets:
        return assets
    assets = {}
    to_encode: dict[str, Image.Image] = {}
    layers = result.get("cover_layers")
    if layers:
        previous = result.get("drag_vehicle_asset")
        if previous and previous[0] is layers["vehicle"]:
            assets["vehicle"] = previous[1]  # the cars do not change with the part treatment
        else:
            to_encode["vehicle"] = ImageOps.contain(layers["vehicle"], (1060, 390))
        to_encode["product"] = layers["product"]
        assets["vehicle_size"] = layers["vehicle"].size
        assets["product_size"] = layers["product"].size
    for index in range(2, 5):
        source = result["editor_sources"][index]
        bbox = content_bbox(source)
        content = source.crop(bbox) if bbox else source
        to_encode[f"image_{index}"] = content
        assets[f"image_{index}_size"] = content.size
    with ThreadPoolExecutor(max_workers=4) as pool:
        assets.update(zip(to_encode, pool.map(image_data_url, to_encode.values())))
    if layers:
        result["drag_vehicle_asset"] = (layers["vehicle"], assets["vehicle"])
    result["drag_assets"] = assets
    return assets


def _current_previews(result: dict, final: bool = False) -> list[Image.Image]:
    """Fast bilinear render for the screen; final=True uses Lanczos for the published files."""
    resampling = Image.Resampling.LANCZOS if final else Image.Resampling.BILINEAR
    if result.get("cover_layers"):
        cover = compose_cover(
            result["cover_layers"]["vehicle"], result["cover_layers"]["product"],
            st.session_state.get("cover_vehicle_x", 0), st.session_state.get("cover_vehicle_y", 0),
            st.session_state.get("cover_vehicle_scale", 100), st.session_state.get("cover_product_x", 0),
            st.session_state.get("cover_product_y", 0), st.session_state.get("cover_product_scale", 100),
            resampling=resampling,
        )
    else:
        cover = result["editor_sources"][1]
    previews = [cover]
    for index in range(2, 5):
        previews.append(reposition_image(
            result["editor_sources"][index],
            st.session_state.get(f"image_{index}_x", 0),
            st.session_state.get(f"image_{index}_y", 0),
            st.session_state.get(f"image_{index}_scale", 100),
            resampling=resampling,
        ))
    return previews


def google_images_url(choice: dict, transparent: bool = False) -> str:
    """Google Images search opened in the user's own browser, filtered by background."""
    years = sorted(_years(str(choice.get("years", ""))))
    year_text = f"{years[0]} {years[-1]}" if len(years) > 1 else (str(years[0]) if years else "")
    query = " ".join(part for part in (choice.get("brand", ""), choice.get("model", ""), year_text) if part)
    query += " png" if transparent else " fundo branco"
    tbs = "ic:trans" if transparent else "ic:specific,isc:white"
    return "https://www.google.com/search?" + urlencode({"udm": "2", "hl": "pt-BR", "tbs": tbs, "q": query})


def _framed(image: Image.Image) -> Image.Image:
    """Screen-only black border so the white canvas does not blend into the page."""
    flat = Image.new("RGBA", image.size, (255, 255, 255, 255))
    flat.alpha_composite(image.convert("RGBA"))
    return ImageOps.expand(flat.convert("RGB"), border=4, fill="black")


def _square_thumb(image: Image.Image, side: int = 420) -> Image.Image:
    """Cover miniature inside a fixed white square with a thin grey frame (same size on every card)."""
    flat = Image.new("RGBA", image.size, (255, 255, 255, 255))
    flat.alpha_composite(image.convert("RGBA"))
    square = ImageOps.pad(flat.convert("RGB"), (side, side), color="white")
    return ImageOps.expand(square, border=3, fill="#cbd5e1")


def _save_final_images(result: dict) -> list[Image.Image]:
    # The part already got its own contrast/sharpness (editor sliders). A global sharpen here
    # would also hit the cars on the cover, so it is only used when there is no part treatment.
    treated = bool(result.get("product_raw"))
    finals = _current_previews(result, final=True)
    for path, image in zip(result["paths"], finals):
        (image if treated else sharpen(image)).convert("RGB").save(path, quality=98, subsampling=0)
    return finals


def _fitment_label(choice: dict) -> str:
    years = f" ({choice['years']})" if choice.get("years") else ""
    return f"{choice['brand']} {choice['model']}{years}"


def _editor_text(value) -> str:
    return "" if pd.isna(value) else str(value).strip()


def _cars_from_sku_cover(sku: str) -> Image.Image:
    """Crop the vehicle strip (one or more cars) from image 1 of another SKU."""
    _, product = client().find_product(sku)
    urls = extract_image_urls(product.get("images", product))
    if not urls:
        raise ValueError(f"O SKU {sku} não tem imagens")
    cars = extract_vehicle_from_cover(load_image(urls[0]), allow_multiple=True)
    if cars is None:
        raise ValueError(
            f"A imagem 1 do SKU {sku} não parece uma capa com carro em cima e peça embaixo, em fundo branco"
        )
    return cars


def _render_copy_cars_from_sku(pending: dict) -> None:
    state_key = f"copied_cars_{pending['sku']}"
    with st.expander("Usar os carros da capa de outro SKU", expanded=bool(st.session_state.get(state_key)),
                     icon=":material/content_copy:"):
        st.caption("Pega a imagem 1 de outro anúncio, remove a peça e usa só os carros na capa deste SKU.")
        sku_col, button_col = st.columns([3, 1], vertical_alignment="bottom")
        with sku_col:
            other_sku = st.text_input("SKU com a capa pronta", placeholder="00002185",
                                      key=f"copy_cars_sku_{pending['sku']}").strip()
        with button_col:
            fetch = st.button("Buscar capa", key=f"copy_cars_fetch_{pending['sku']}", width="stretch",
                              icon=":material/search:")
        if fetch:
            if not other_sku:
                st.error("Informe o SKU.")
            else:
                try:
                    with st.spinner(f"Lendo a capa do SKU {other_sku}..."):
                        st.session_state[state_key] = {"sku": other_sku, "image": _cars_from_sku_cover(other_sku)}
                except (BaseLinkerError, ValueError, OSError) as error:
                    st.session_state.pop(state_key, None)
                    st.error(str(error))
        copied = st.session_state.get(state_key)
        if copied:
            st.image(_framed(copied["image"]), caption=f"Carros da capa do SKU {copied['sku']}", width=PREVIEW_WIDE,
                     alt=f"Carros recortados da capa do SKU {copied['sku']}")
            if st.button("Usar estes carros na capa", type="primary", key=f"copy_cars_use_{pending['sku']}"):
                pending["choices"] = [{
                    "brand": "Carros da capa do SKU",
                    "model": copied["sku"],
                    "years": "",
                    "source": f"capa do SKU {copied['sku']}",
                    "origin": f"Capa do SKU {copied['sku']}",
                    "status": "approved",
                    "image": copied["image"],
                    "cutout": None,
                    "alternatives": [],
                    "alternative_index": 0,
                }]
                st.session_state["pending_vehicle_review"] = pending
                st.session_state.pop(state_key, None)
                st.rerun()


STATUS_BADGES = {
    "approved": ("aprovado", ":material/check_circle:", "green"),
    "pending": ("pendente", ":material/schedule:", "orange"),
    "missing": ("sem imagem", ":material/image_not_supported:", "red"),
    "discarded": ("descartado", ":material/block:", "gray"),
}

# Outcome of each SKU of the batch, shown in the checklist at the end.
BATCH_STATUS = {
    "published": ("Publicado", ":material/check_circle:", "green"),
    "edited": ("Editado, não publicado", ":material/edit_note:", "orange"),
    "skipped": ("Pulado", ":material/skip_next:", "gray"),
    "failed": ("Falha ao carregar", ":material/error:", "red"),
    "todo": ("Não aberto", ":material/radio_button_unchecked:", "gray"),
}


def _product_name(product: dict) -> str:
    return str(((product or {}).get("text_fields") or {}).get("name", "")).strip()


def _render_sku_title(sku: str, name: str, batch_index: int = 0, batch_total: int = 1,
                      vehicles: list[dict] | None = None, published: bool = False) -> None:
    """Big product name (what the cars are read from) with the SKU and batch position above it."""
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.badge(f"SKU {sku}", icon=":material/sell:", color="blue")
        if batch_total > 1:
            st.badge(f"Lote: {batch_index + 1} de {batch_total}", color="violet")
        for vehicle in vehicles or []:
            st.badge(_fitment_label(vehicle), icon=":material/directions_car:", color="gray")
        if published:
            st.badge("Publicado", icon=":material/check_circle:", color="green")
    title = name or f"SKU {sku}"
    st.header(re.sub(r"([\\`*_#\[\]<>|~$])", r"\\\1", title), anchor=False)


def _record_batch_item(status: str, cover: Image.Image | None = None, name: str = "",
                       publish_data: dict | None = None) -> None:
    """Remember how the current SKU ended (and a small copy of its cover) for the final checklist.

    publish_data (edited SKUs only) keeps what is needed to publish it later from the summary.
    """
    batch = st.session_state.get("sku_batch")
    if not batch:
        return
    index = batch["current_index"]
    summary = batch.setdefault("summary", {})
    previous = summary.get(index, {})
    thumb = None
    if cover is not None:
        thumb = cover.convert("RGB")
        thumb.thumbnail((420, 420), Image.Resampling.LANCZOS)
    summary[index] = {
        "sku": batch["skus"][index],
        "name": name or previous.get("name", ""),
        "status": status,
        "cover": thumb if thumb is not None else previous.get("cover"),
        "publish_data": publish_data if status == "edited" else None,
    }
    st.session_state["sku_batch"] = batch


def _advance_batch(status: str, cover: Image.Image | None = None, name: str = "",
                   publish_data: dict | None = None) -> None:
    """Record the current SKU and open the next one (or the checklist after the last one)."""
    _record_batch_item(status, cover, name, publish_data)
    with st.spinner("Abrindo o próximo SKU do lote..."):
        _load_next_batch_sku()
    st.rerun()


def _render_skip_sku_button(pending: dict) -> None:
    batch = st.session_state.get("sku_batch")
    if not batch:
        return
    is_last = batch["current_index"] >= len(batch["skus"]) - 1
    if st.button("Pular e ver resumo do lote" if is_last else "Pular para o próximo SKU",
                 key=f"skip_review_{pending['sku']}", icon=":material/skip_next:",
                 help="Não gera imagens para este SKU. Ele aparece como pulado no resumo do lote."):
        _advance_batch("skipped", name=_product_name(pending.get("product", {})))


def _render_batch_summary(batch: dict) -> None:
    """Checklist at the end of the batch: SKU, cover and outcome of every SKU."""
    summary = batch.get("summary", {})
    entries = [summary.get(index) or {"sku": sku, "name": "", "status": "todo", "cover": None}
               for index, sku in enumerate(batch["skus"])]
    published = sum(entry["status"] == "published" for entry in entries)
    st.subheader("Resumo do lote", icon=":material/checklist:")
    with st.container(horizontal=True, gap="small"):
        st.badge(f"{published} de {len(entries)} publicados", icon=":material/check_circle:",
                 color="green" if published == len(entries) else "orange")
        for status in ("edited", "skipped", "failed", "todo"):
            count = sum(entry["status"] == status for entry in entries)
            if count:
                label, icon, color = BATCH_STATUS[status]
                st.badge(f"{count} {label.lower()}", icon=icon, color=color)
    st.caption("Confira as capas. Para refazer um SKU, clique em **Abrir de novo**.")
    _render_publish_all(batch, entries)
    per_row = 4
    for start in range(0, len(entries), per_row):
        columns = st.columns(per_row)
        for offset, entry in enumerate(entries[start:start + per_row]):
            index = start + offset
            label, icon, color = BATCH_STATUS[entry["status"]]
            with columns[offset], st.container(border=True):
                with st.container(horizontal_alignment="center"):
                    if entry.get("cover") is not None:
                        st.image(_square_thumb(entry["cover"]), width=PREVIEW_THUMB,
                                 alt=f"Capa do SKU {entry['sku']}")
                    else:
                        with st.container(border=True, height=PREVIEW_THUMB, width=PREVIEW_THUMB,
                                          horizontal_alignment="center", vertical_alignment="center"):
                            st.markdown(":gray[:material/hide_image: Sem capa]")
                st.markdown(f"**SKU {entry['sku']}**")
                if entry.get("name"):
                    st.caption(entry["name"])
                st.badge(label, icon=icon, color=color)
                if st.button("Abrir de novo", key=f"reopen_batch_{index}", icon=":material/replay:",
                             width="stretch"):
                    batch["completed"] = False
                    st.session_state["sku_batch"] = batch
                    _load_batch_index(index)
                    st.rerun()


def _render_publish_all(batch: dict, entries: list[dict]) -> None:
    """Publish every edited SKU of the batch at once, from the images saved when it was left."""
    for message in st.session_state.pop("publish_all_errors", []):
        st.error(message, icon=":material/error:")
    ready =[(index, entry) for index, entry in enumerate(entries)
             if entry["status"] == "edited" and entry.get("publish_data")]
    if not ready:
        return
    with st.container(border=True):
        st.markdown(f"**:material/publish: {len(ready)} SKU(s) editado(s) prontos para publicar**")
        st.caption(
            ":material/info: Cada publicação substitui as posições 1 a 4 e preserva as fotos da posição 5 "
            "em diante. Um backup das imagens atuais de cada SKU é salvo antes."
        )
        confirm = st.checkbox("Conferi as capas abaixo e quero publicar todos os editados.",
                              key="confirm_publish_all")
        if not st.button(f"Publicar todos os editados ({len(ready)})", type="primary", disabled=not confirm,
                         icon=":material/publish:", key="publish_all"):
            return
        base_url = os.getenv("PUBLIC_IMAGE_BASE_URL", "").strip()
        summary = batch.setdefault("summary", {})
        errors = []
        progress = st.progress(0.0, text="Publicando...")
        try:
            api = client()
        except (BaseLinkerError, ValueError) as error:
            st.error(str(error), icon=":material/error:")
            return
        for done, (index, entry) in enumerate(ready, start=1):
            data = entry["publish_data"]
            progress.progress((done - 1) / len(ready), text=f"Publicando SKU {data['sku']} ({done} de {len(ready)})...")
            try:
                api.save_images_backup(data["sku"], data["product_id"], data["product_images"])
                if base_url:
                    images = public_urls(data["paths"], base_url, data["sku"])
                else:
                    images = [api.image_file_to_data(path) for path in data["paths"]]
                api.update_images(data["product_id"], images, data["product_images"][4:])
            except (BaseLinkerError, ValueError, OSError) as error:
                errors.append(f"SKU {data['sku']}: {error}")
                continue
            summary[index] = {**entry, "status": "published", "publish_data": None}
        progress.progress(1.0, text="Concluído.")
        st.session_state["sku_batch"] = batch
        st.session_state.pop("confirm_publish_all", None)
        if errors:
            st.session_state["publish_all_errors"] = errors
        else:
            st.toast(f"{len(ready)} SKU(s) publicados no BaseLinker.", icon=":material/check_circle:")
        st.rerun()


def _render_vehicle_review() -> None:
    pending = st.session_state.get("pending_vehicle_review")
    if not pending:
        return

    _render_sku_title(pending["sku"], _product_name(pending.get("product", {})),
                      pending.get("batch_index", 0), pending.get("batch_total", 1))
    _render_copy_cars_from_sku(pending)

    if pending["choices"] is None:
        st.markdown("**Confira a marca, o modelo e os anos de cada carro.** Uma linha por carro; "
                    "cada linha é procurada primeiro no catálogo local e depois na internet.")
        editor_key = f"fitments_{pending['sku']}_{pending['fitment_version']}"
        fitments_editor = st.data_editor(
            pd.DataFrame(pending["fitments"], columns=["brand", "model", "years"]),
            num_rows="dynamic",
            hide_index=True,
            column_config={
                "brand": st.column_config.TextColumn("Marca"),
                "model": st.column_config.TextColumn("Nome do carro"),
                "years": st.column_config.TextColumn("Anos compatíveis"),
            },
            key=editor_key,
        )
        with st.container(horizontal=True, gap="small"):
            search = st.button("Buscar imagens dos veículos", type="primary",
                               key=f"search_fitments_{pending['sku']}", icon=":material/image_search:")
            _render_skip_sku_button(pending)
        if search:
            fitments = [
                {key: _editor_text(row.get(key, "")) for key in ("brand", "model", "years")}
                for row in fitments_editor.to_dict("records")
                if _editor_text(row.get("brand", "")) and _editor_text(row.get("model", ""))
            ]
            if not fitments:
                st.error("Informe pelo menos uma marca e um modelo.")
            else:
                pending["fitments"] = fitments
                with st.spinner("Buscando primeiro no catálogo local e depois na internet..."):
                    pending["choices"] = _search_fitments(pending, fitments)
                st.session_state["pending_vehicle_review"] = pending
                st.rerun()
        return

    choices = pending["choices"]
    if not choices:
        st.warning("Nenhuma linha de veículo válida. Edite a lista e tente novamente.")
        if st.button("Editar veículos", key=f"edit_empty_fitments_{pending['sku']}"):
            pending["choices"] = None
            pending["fitment_version"] += 1
            st.session_state["pending_vehicle_review"] = pending
            st.rerun()
        return

    with st.container(horizontal=True, gap="small"):
        if st.button("Editar veículos compatíveis", key=f"edit_fitments_{pending['sku']}",
                     icon=":material/edit:"):
            pending["choices"] = None
            pending["fitment_version"] += 1
            st.session_state["pending_vehicle_review"] = pending
            st.rerun()
        _render_skip_sku_button(pending)

    labels = [_fitment_label(choice) for choice in choices]
    review_tab, manual_tab = st.tabs([":material/fact_check: Validar imagem buscada",
                                      ":material/upload: Adicionar ou substituir manualmente"])
    review_key = f"review_vehicle_{pending['sku']}"
    next_key = f"review_next_{pending['sku']}"
    if next_key in st.session_state:
        st.session_state[review_key] = st.session_state.pop(next_key)

    def _go_to_next_pending(current: int) -> None:
        order = list(range(current + 1, len(choices))) + list(range(0, current))
        for index in order:
            if choices[index]["status"] not in ("approved", "discarded"):
                st.session_state[next_key] = index
                return

    with review_tab:
        selected = st.selectbox(
            "Veículo para validar",
            options=list(range(len(choices))),
            format_func=lambda index: (
                f"{labels[index]} · {STATUS_BADGES.get(choices[index]['status'], (choices[index]['status'],))[0]}"
            ),
            key=review_key,
        )
        choice = choices[selected]
        if not isinstance(choice.get("source"), str):
            recovered_source = local_vehicle_for_fitment(
                choice.get("brand", ""), choice.get("model", ""), choice.get("years", "")
            )
            choice["source"] = recovered_source
            if recovered_source:
                choice["origin"] = "Catálogo local"
                choice["status"] = "pending"
            choices[selected] = choice
            pending["choices"] = choices
            st.session_state["pending_vehicle_review"] = pending
        st.caption(choice["origin"] or "Não encontrado no catálogo nem na internet.")
        if choice["status"] == "discarded":
            st.info("Imagem descartada. Para incluir este carro, envie uma imagem na aba manual.")
        elif choice["source"]:
            try:
                if choice["image"] is None:
                    choice["image"] = load_image(choice["source"])
                    choice["cutout"] = None
                if choice.get("cutout") is None:
                    with st.spinner("Recortando o veículo..."):
                        cutout = prepare_vehicle_image(choice["image"])
                        choice["cutout"] = ImageOps.pad(
                            cutout.convert("RGBA"), (900, 450), color=(255, 255, 255, 255)
                        )
                original_col, cutout_col = st.columns(2)
                with original_col:
                    st.image(_framed(choice["image"]), caption="Foto encontrada", width=PREVIEW_PAIR,
                             alt=f"Foto original do veículo {labels[selected]}")
                with cutout_col:
                    st.image(_framed(choice["cutout"]), caption="Como vai ficar na capa", width=PREVIEW_PAIR,
                             alt=f"Veículo {labels[selected]} recortado")
            except (OSError, ValueError) as error:
                st.error(f"Não consegui abrir essa imagem: {error}")
                choice["status"] = "missing"
        else:
            st.info("Não há imagem para validar. Use a aba de imagem manual ou descarte este veículo.")

        alternatives = choice.get("alternatives") or []
        if len(alternatives) > 1 and choice["status"] in ("pending", "missing"):
            position = choice.get("alternative_index", 0)
            if st.button(
                f"Ver outra opção ({position + 1} de {len(alternatives)})",
                key=f"next_alternative_{pending['sku']}_{selected}",
                icon=":material/swap_horiz:",
            ):
                position = (position + 1) % len(alternatives)
                alternative_origins = choice.get("alternative_origins") or []
                choice.update({
                    "alternative_index": position,
                    "origin": alternative_origins[position] if position < len(alternative_origins)
                    else choice["origin"],
                    "source": alternatives[position],
                    "image": None,
                    "status": "pending",
                })
                choices[selected] = choice
                pending["choices"] = choices
                st.session_state["pending_vehicle_review"] = pending
                st.rerun()

        save_online = False
        if choice["status"] != "discarded" and choice["source"] and choice["origin"] == "Busca online":
            save_online = st.checkbox(
                "Salvar no catálogo local depois de aprovar",
                value=False,
                key=f"save_online_{pending['sku']}_{selected}",
            )
        approve_col, discard_col = st.columns(2)
        last_review = _is_last_review(choices, selected)
        with approve_col:
            if st.button(
                "Aprovar e gerar capa" if last_review else "Aprovar imagem",
                type="primary",
                disabled=not choice.get("image"),
                key=f"approve_vehicle_{pending['sku']}_{selected}",
                icon=":material/auto_awesome:" if last_review else ":material/check:",
                help="É o último veículo: aprova e já gera a capa." if last_review else None,
            ):
                if save_online:
                    path = save_vehicle_image(
                        choice["brand"], choice["model"], choice["years"], choice["image"]
                    )
                    choice["source"] = str(path)
                    choice["origin"] = "Catálogo local"
                choice["status"] = "approved"
                choices[selected] = choice
                pending["choices"] = choices
                st.session_state["pending_vehicle_review"] = pending
                if last_review:
                    _generate_from_review(pending, choices)
                else:
                    _go_to_next_pending(selected)
                    st.rerun()
        with discard_col:
            if st.button("Descartar imagem", key=f"discard_vehicle_{pending['sku']}_{selected}",
                         icon=":material/close:"):
                choice["image"] = None
                choice["status"] = "discarded"
                choices[selected] = choice
                pending["choices"] = choices
                st.session_state["pending_vehicle_review"] = pending
                _go_to_next_pending(selected)
                st.rerun()

    with manual_tab:
        selected_manual = st.selectbox(
            "Carro que receberá a imagem",
            options=list(range(len(choices))),
            format_func=lambda index: labels[index],
            key=f"manual_vehicle_{pending['sku']}",
        )
        manual_choice = choices[selected_manual]
        if manual_choice["source"]:
            st.caption("Enviar uma imagem aqui substitui a imagem cadastrada para esta marca, modelo e anos.")

        st.markdown("**1. Procure a foto no Google** (abre no seu navegador, já filtrado):")
        white_col, trans_col = st.columns(2)
        with white_col:
            st.link_button("Google Imagens · fundo branco",
                           google_images_url(manual_choice, transparent=False), width="stretch",
                           icon=":material/open_in_new:")
        with trans_col:
            st.link_button("Google Imagens · fundo transparente",
                           google_images_url(manual_choice, transparent=True), width="stretch",
                           icon=":material/open_in_new:")
        st.caption("No Google, clique na foto, depois clique com o botão direito nela e escolha "
                   "**Copiar endereço da imagem**. Ou salve a foto e envie o arquivo abaixo.")

        st.markdown("**2. Cole o endereço ou envie o arquivo:**")
        pasted_url = st.text_input(
            "Endereço da imagem (https://...)",
            key=f"manual_vehicle_url_{pending['sku']}_{selected_manual}",
            placeholder="https://exemplo.com/carro.png",
        ).strip()
        uploaded = st.file_uploader(
            "Imagem do computador",
            type=["png", "jpg", "jpeg", "webp"],
            key=f"manual_vehicle_upload_{pending['sku']}_{selected_manual}",
        )
        if uploaded or pasted_url:
            try:
                if uploaded:
                    with Image.open(io.BytesIO(uploaded.getvalue())) as source_image:
                        manual_image = source_image.convert("RGBA")
                else:
                    if not pasted_url.startswith(("http://", "https://")):
                        raise ValueError("o endereço precisa começar com http:// ou https://")
                    with st.spinner("Baixando a imagem..."):
                        manual_image = load_image(pasted_url)
                with st.spinner("Recortando o veículo..."):
                    manual_cutout = ImageOps.pad(
                        prepare_vehicle_image(manual_image).convert("RGBA"), (900, 450),
                        color=(255, 255, 255, 255),
                    )
                original_col, cutout_col = st.columns(2)
                with original_col:
                    st.image(_framed(manual_image), caption="Imagem escolhida", width=PREVIEW_PAIR,
                             alt=f"Imagem escolhida para {labels[selected_manual]}")
                with cutout_col:
                    st.image(_framed(manual_cutout), caption="Como vai ficar na capa", width=PREVIEW_PAIR,
                             alt=f"Veículo {labels[selected_manual]} recortado")
                save_manual = st.checkbox(
                    "Salvar ou substituir no catálogo local",
                    value=True,
                    key=f"save_manual_{pending['sku']}_{selected_manual}",
                )
                last_manual = _is_last_review(choices, selected_manual)
                if st.button(
                    "Aprovar imagem manual e gerar capa" if last_manual else "Aprovar imagem manual",
                    type="primary",
                    key=f"approve_manual_{pending['sku']}_{selected_manual}",
                    icon=":material/auto_awesome:" if last_manual else ":material/check:",
                ):
                    if save_manual:
                        path = save_vehicle_image(
                            manual_choice["brand"], manual_choice["model"],
                            manual_choice["years"], manual_image,
                        )
                        manual_choice["source"] = str(path)
                        manual_choice["origin"] = "Imagem manual salva no catálogo local"
                    else:
                        manual_choice["origin"] = "Imagem manual desta sessão"
                    manual_choice["image"] = manual_image
                    manual_choice["status"] = "approved"
                    choices[selected_manual] = manual_choice
                    pending["choices"] = choices
                    st.session_state["pending_vehicle_review"] = pending
                    if last_manual:
                        _generate_from_review(pending, choices)
                    else:
                        st.rerun()
            except (OSError, ValueError) as error:
                st.error(f"Arquivo de imagem inválido: {error}")

    approved = [choice for choice in choices if choice["status"] == "approved"]
    unresolved = [choice for choice in choices if choice["status"] not in ("approved", "discarded")]
    with st.container(border=True):
        st.markdown("**:material/checklist: Status dos veículos**")
        with st.container(horizontal=True, gap="small"):
            for choice in choices:
                label, icon, color = STATUS_BADGES.get(choice["status"], (choice["status"], None, "gray"))
                st.badge(f"{_fitment_label(choice)} · {label}", icon=icon, color=color)
        if unresolved:
            pending_names = ", ".join(_fitment_label(choice) for choice in unresolved)
            st.warning(
                f"Para liberar **Gerar capa e imagens**, aprove ou descarte também: **{pending_names}**. "
                "Selecione o veículo na lista acima. Imagens não encontradas podem ser enviadas na aba manual.",
                icon=":material/pending_actions:",
            )
        elif not approved:
            st.warning("Aprove pelo menos uma imagem de veículo para gerar a capa.", icon=":material/warning:")
        if st.button(
            "Gerar capa e imagens",
            type="primary",
            disabled=not approved or bool(unresolved),
            key=f"generate_approved_{pending['sku']}",
            icon=":material/auto_awesome:",
        ):
            _generate_from_review(pending, choices)


def _is_last_review(choices: list[dict], selected: int) -> bool:
    """True when every other car is already approved or discarded (this approval finishes the list)."""
    return all(choice["status"] in ("approved", "discarded")
               for index, choice in enumerate(choices) if index != selected)


def _generate_from_review(pending: dict, choices: list[dict]) -> None:
    approved = [choice for choice in choices if choice["status"] == "approved"]
    if not approved:
        st.warning("Aprove pelo menos uma imagem de veículo para gerar a capa.", icon=":material/warning:")
        return
    try:
        with st.spinner("Gerando a capa e as imagens 2 a 4..."):
            _generate_sku(pending, approved)
    except (BaseLinkerError, ValueError, VehicleSearchError, OSError) as error:
        st.error(str(error), icon=":material/error:")
        return
    st.rerun()


POSITION_SLIDERS = {
    "cover_vehicle": {"x": (-400, 400), "y": (-250, 250)},
    "cover_product": {"x": (-400, 400), "y": (-350, 250)},
    "image_2": {"x": (-400, 400), "y": (-400, 400)},
    "image_3": {"x": (-400, 400), "y": (-400, 400)},
    "image_4": {"x": (-400, 400), "y": (-400, 400)},
}


def _position_sliders(prefix: str) -> None:
    """Side / height / zoom bars. persist_state keeps the values while the bars are hidden."""
    ranges = POSITION_SLIDERS[prefix]
    st.slider("Lado", *ranges["x"], step=1, key=f"{prefix}_x", persist_state="page")
    st.slider("Altura", *ranges["y"], step=1, key=f"{prefix}_y", persist_state="page")
    st.slider("Zoom", 50, 150, step=5, key=f"{prefix}_scale", persist_state="page", format="%d%%")


def _status_label() -> tuple[str, str, str]:
    if st.session_state.get("batch_error"):
        return "Falha na consulta", ":material/error:", "red"
    if st.session_state.get("pending_vehicle_review"):
        return "Revisando veículos", ":material/directions_car:", "blue"
    result = st.session_state.get("result")
    if result and result.get("published"):
        return "Publicado", ":material/check_circle:", "green"
    if result:
        return "Ajustando imagens", ":material/tune:", "blue"
    if (st.session_state.get("sku_batch") or {}).get("completed"):
        return "Lote concluído", ":material/task_alt:", "green"
    return "Aguardando SKU", ":material/hourglass_empty:", "gray"


def _render_sidebar() -> None:
    with st.sidebar:
        st.subheader("Lote de SKUs", icon=":material/inventory_2:")
        queue_text = st.text_area(
            "SKUs, um por linha",
            placeholder="00010280\n00004278\n00002185",
            key="sku_batch_input",
            height=150,
            help="De 1 a 10 SKUs. Também aceita separar por vírgula ou ponto e vírgula.",
        )
        if st.button("Iniciar lote", width="stretch", type="primary", icon=":material/play_arrow:"):
            skus = list(dict.fromkeys(
                item.strip() for item in re.split(r"[\n,;]+", queue_text) if item.strip()
            ))
            if not skus:
                st.error("Informe pelo menos um SKU.")
            elif len(skus) > 10:
                st.error("O lote aceita no máximo 10 SKUs.")
            else:
                st.session_state["sku_batch"] = {
                    "skus": skus,
                    "items": [],
                    "current_index": 0,
                    "completed": False,
                }
                try:
                    with st.spinner(f"Consultando {len(skus)} SKU(s) no BaseLinker..."):
                        _start_sku_batch(skus)
                    st.rerun()
                except (BaseLinkerError, ValueError, VehicleSearchError, OSError) as error:
                    st.error(str(error))
        st.caption("O lote consulta os produtos de uma vez; você aprova cada um antes de seguir.")

        st.space("small")
        label, icon, color = _status_label()
        st.badge(label, icon=icon, color=color)
        batch = st.session_state.get("sku_batch")
        if batch and not batch.get("completed"):
            current = batch["current_index"] + 1
            st.progress(current / len(batch["skus"]), text=f"SKU {current} de {len(batch['skus'])}")
            st.caption(" · ".join(
                f"**{sku}**" if index == batch["current_index"] else sku
                for index, sku in enumerate(batch["skus"])
            ))
        elif batch and batch.get("completed"):
            st.caption(f"Lote de {len(batch['skus'])} SKU(s) concluído.")


def _render_batch_error(batch_error: dict) -> None:
    st.error(f"Não consegui carregar o SKU {batch_error['sku']}: {batch_error['error']}",
             icon=":material/error:")
    with st.container(horizontal=True):
        if st.button("Tentar novamente", type="primary", key="retry_batch_sku", icon=":material/refresh:"):
            batch = st.session_state["sku_batch"]
            index = batch["current_index"]
            try:
                with st.spinner(f"Consultando {batch['skus'][index]}..."):
                    item = _fetch_sku_data(batch["skus"][index])
                batch["items"][index] = item
                st.session_state["sku_batch"] = batch
                _load_sku(batch["skus"][index], item)
                st.rerun()
            except (BaseLinkerError, ValueError, OSError) as error:
                st.session_state["batch_error"] = {
                    "sku": batch["skus"][index],
                    "error": str(error),
                }
                st.rerun()
        if st.button("Pular SKU com falha", key="skip_failed_batch_sku", icon=":material/skip_next:"):
            _advance_batch("failed")


def _render_empty_state() -> None:
    batch = st.session_state.get("sku_batch")
    if batch and batch.get("completed"):
        st.success("Lote concluído. Cole novos SKUs na barra lateral para começar outro.",
                   icon=":material/task_alt:")
        _render_batch_summary(batch)
        return
    with st.container(border=True):
        st.subheader("Comece pela barra lateral", icon=":material/arrow_back:")
        st.markdown(
            "1. Cole os SKUs (um por linha) e clique em **Iniciar lote**.\n"
            "2. Confira os carros compatíveis e aprove as imagens.\n"
            "3. Ajuste a capa e as imagens 2 a 4: **arraste a peça com o mouse**, "
            "segure **Ctrl + roda do mouse** para o zoom.\n"
            "4. Revise e publique no BaseLinker."
        )


def _render_editor_header(result: dict) -> None:
    _render_sku_title(result["sku"], result.get("product_name", ""), result.get("batch_index", 0),
                      result.get("batch_total", 1), result.get("approved_vehicles", []),
                      bool(result.get("published")))
    for warning in result.get("warnings", []):
        st.warning(warning, icon=":material/warning:")


def _render_toolbar(result: dict) -> None:
    with st.container(border=True):
        contrast_col, sharpness_col, reset_col, fine_col = st.columns(
            [3, 3, 1.6, 2.2], vertical_alignment="bottom", gap="medium"
        )
        has_raw = bool(result.get("product_raw"))
        with contrast_col:
            st.slider("Contraste da peça", 80, 140, step=1, key="part_contrast", format="%d%%",
                      disabled=not has_raw, help="100% = foto original. Padrão: 108%.")
        with sharpness_col:
            st.slider("Nitidez da peça", 0, 200, step=5, key="part_sharpness", format="%d%%",
                      disabled=not has_raw, help="0% = sem nitidez extra. Padrão: 100%.")
        with reset_col:
            st.button("Padrão", on_click=_reset_part_treatment, width="stretch", disabled=not has_raw,
                      icon=":material/restart_alt:", help="Volta contraste e nitidez para o padrão.")
        with fine_col:
            st.toggle("Ajuste fino", key="fine_tune",
                      help="Mostra as barras de lado, altura e zoom ao lado de cada imagem.")


def _framing_badge(report: dict, cover: bool = False) -> None:
    if report["valid"]:
        st.badge("Sem cortes" if cover else "Centralizada e sem cortes", icon=":material/check_circle:",
                 color="green")
    elif report["clipped"]:
        st.badge("A imagem está cortada na borda", icon=":material/crop:", color="orange")
    else:
        st.badge(
            f"Fora do centro ({report['center_offset_x']:+d}, {report['center_offset_y']:+d} px)",
            icon=":material/filter_center_focus:", color="orange",
        )


def _image_tab(fine_tune: bool):
    """Columns for one tab: canvas centred when the bars are hidden, canvas + bars otherwise."""
    if fine_tune:
        canvas_col, controls_col = st.columns([3, 2], gap="large")
        return canvas_col, controls_col
    _, canvas_col, _ = st.columns([1, 4, 1])
    return canvas_col, None


def _render_image_tabs(result: dict, assets: dict, reports: list[dict]) -> None:
    fine_tune = st.session_state.get("fine_tune", False)
    tabs = st.tabs([
        ":material/photo_library: Imagem 1 · Capa",
        ":material/image: Imagem 2",
        ":material/image: Imagem 3",
        ":material/image: Imagem 4",
    ])
    with tabs[0]:
        if result.get("cover_layers"):
            canvas_col, controls_col = _image_tab(fine_tune)
            with canvas_col:
                vehicle_base, product_base = cover_base_sizes(
                    assets["vehicle_size"], assets["product_size"],
                    st.session_state["cover_vehicle_scale"],
                )
                drag_canvas([
                    {"id": "vehicle", "label": "Veículos", "src": assets["vehicle"],
                     "base_w": vehicle_base[0], "base_h": vehicle_base[1], "top": 25, "z": 1,
                     "shadow": True, "x_range": list(POSITION_SLIDERS["cover_vehicle"]["x"]),
                     "y_range": list(POSITION_SLIDERS["cover_vehicle"]["y"]), "scale_range": [50, 150]},
                    {"id": "product", "label": "Peça", "src": assets["product"],
                     "base_w": product_base[0], "base_h": product_base[1], "top": 500, "z": 2,
                     "shadow": False, "x_range": list(POSITION_SLIDERS["cover_product"]["x"]),
                     "y_range": list(POSITION_SLIDERS["cover_product"]["y"]), "scale_range": [50, 150]},
                ], {"vehicle": "cover_vehicle", "product": "cover_product"}, key="drag_cover")
                _framing_badge(reports[0], cover=True)
            if controls_col:
                with controls_col:
                    with st.container(border=True):
                        st.markdown("**:material/directions_car: Veículos**")
                        _position_sliders("cover_vehicle")
                    with st.container(border=True):
                        st.markdown("**:material/build: Peça**")
                        _position_sliders("cover_product")
        else:
            _, canvas_col, _ = st.columns([1, 4, 1])
            with canvas_col:
                st.image(_framed(_current_previews(result)[0]), caption="Prévia da capa", width=PREVIEW_WIDE,
                         alt="Prévia da capa")
                _framing_badge(reports[0])

    for index, tab in enumerate(tabs[1:], start=2):
        with tab:
            canvas_col, controls_col = _image_tab(fine_tune)
            with canvas_col:
                content_size = assets[f"image_{index}_size"]
                drag_canvas([
                    {"id": "product", "label": f"Peça · imagem {index}", "src": assets[f"image_{index}"],
                     "base_w": content_size[0], "base_h": content_size[1], "top": None, "z": 1,
                     "shadow": False, "x_range": list(POSITION_SLIDERS[f"image_{index}"]["x"]),
                     "y_range": list(POSITION_SLIDERS[f"image_{index}"]["y"]), "scale_range": [50, 150]},
                ], {"product": f"image_{index}"}, key=f"drag_image_{index}")
                _framing_badge(reports[index - 1])
            if controls_col:
                with controls_col:
                    with st.container(border=True):
                        st.markdown("**:material/build: Peça**")
                        _position_sliders(f"image_{index}")


def _render_publish(result: dict, previews: list[Image.Image], reports: list[dict]) -> None:
    with st.container(border=True):
        st.subheader("Revisar e publicar", icon=":material/cloud_upload:")
        invalid = [str(index) for index, report in enumerate(reports, start=1) if not report["valid"]]
        if invalid:
            st.warning(f"Revise o enquadramento das imagens {', '.join(invalid)} antes de publicar.",
                       icon=":material/warning:")
        st.caption(
            ":material/info: A publicação substitui as posições 1 a 4 e preserva as fotos da posição 5 "
            "em diante. Um backup das imagens atuais é salvo antes."
        )
        base_url = os.getenv("PUBLIC_IMAGE_BASE_URL", "").strip()
        st.caption(
            ":material/link: Envio por URLs públicas." if base_url
            else ":material/computer: Sem URL pública configurada: as imagens vão direto do computador (base64)."
        )
        confirm = st.checkbox("Revisei as quatro imagens e quero publicar este SKU.",
                              key=f"confirm_publish_{result['sku']}")
        batch = st.session_state.get("sku_batch")
        is_last = not batch or batch["current_index"] >= len(batch["skus"]) - 1
        with st.container(horizontal=True):
            publish = st.button("Publicar no BaseLinker", type="primary", disabled=not confirm,
                                icon=":material/publish:", key=f"publish_{result['sku']}")
            publish_next = bool(batch) and st.button(
                "Publicar e ver resumo do lote" if is_last else "Publicar e ir para o próximo",
                type="primary", disabled=not confirm, icon=":material/fast_forward:",
                key=f"publish_next_{result['sku']}",
            )
            if st.button("Desfazer última publicação", disabled=not result.get("backup_path"),
                         icon=":material/undo:", key=f"undo_{result['sku']}"):
                try:
                    client().restore_images(result["backup_path"])
                    st.success("Imagens originais restauradas.", icon=":material/check_circle:")
                except (BaseLinkerError, OSError, ValueError) as error:
                    st.error(str(error), icon=":material/error:")
        if publish or publish_next:
            finals = _publish(result, base_url)
            if finals and publish_next:
                _advance_batch("published", finals[0], result.get("product_name", ""))
        _render_batch_navigation(result, previews)


def _publish(result: dict, base_url: str) -> list[Image.Image] | None:
    """Back up the current images, save the final files and send them. Returns the images sent."""
    try:
        api = client()
        backup = api.save_images_backup(result["sku"], result["product_id"], result["product_images"])
        finals = _save_final_images(result)
        if base_url:
            images = public_urls(result["paths"], base_url, result["sku"])
        else:
            images = [api.image_file_to_data(path) for path in result["paths"]]
        api.update_images(result["product_id"], images, result["product_images"][4:])
    except (BaseLinkerError, ValueError, OSError) as error:
        st.error(str(error), icon=":material/error:")
        return None
    result["backup_path"] = backup
    result["published"] = True
    result["published_cover"] = finals[0]
    st.session_state["result"] = result
    _record_batch_item("published", finals[0], result.get("product_name", ""))
    st.toast("Publicado no BaseLinker.", icon=":material/check_circle:")
    st.success(f"Publicado. Backup salvo em {backup}.", icon=":material/check_circle:")
    return finals


def _render_batch_navigation(result: dict, previews: list[Image.Image]) -> None:
    """Next SKU is always available; without publishing, the SKU is marked as not published."""
    batch = st.session_state.get("sku_batch")
    if not batch:
        return
    is_last = batch["current_index"] >= len(batch["skus"]) - 1
    published = bool(result.get("published"))
    if is_last:
        label, icon = "Concluir lote e ver resumo", ":material/checklist:"
    else:
        label, icon = ("Próximo SKU" if published else "Próximo SKU sem publicar"), ":material/arrow_forward:"
    if st.button(label, type="secondary", key=f"next_batch_{result['sku']}", icon=icon,
                 help=None if published else "Este SKU aparece como “Editado, não publicado” no resumo."):
        if published:
            _advance_batch("published", result.get("published_cover"), result.get("product_name", ""))
            return
        # Save the edited images now so the SKU can be published later from the batch summary.
        publish_data = None
        cover = previews[0]
        try:
            cover = _save_final_images(result)[0]
            publish_data = {
                "sku": result["sku"],
                "product_id": result["product_id"],
                "product_images": result["product_images"],
                "paths": list(result["paths"]),
            }
        except (OSError, ValueError) as error:
            st.warning(f"Não foi possível salvar as imagens para publicar depois: {error}",
                       icon=":material/warning:")
        _advance_batch("edited", cover, result.get("product_name", ""), publish_data)


def _flow_stage() -> int:
    if st.session_state.get("pending_vehicle_review"):
        return 2
    result = st.session_state.get("result")
    if result:
        return 4 if result.get("published") else 3
    if (st.session_state.get("sku_batch") or {}).get("completed"):
        return 5  # every step done
    return 1


def render_processor() -> None:
    flow_steps(_flow_stage())
    _render_sidebar()

    batch_error = st.session_state.get("batch_error")
    if batch_error:
        _render_batch_error(batch_error)
        return

    if st.session_state.get("pending_vehicle_review"):
        _render_vehicle_review()
        return

    result = st.session_state.get("result")
    if not result:
        _render_empty_state()
        return

    for prefix in POSITION_SLIDERS:
        st.session_state.setdefault(f"{prefix}_x", 0)
        st.session_state.setdefault(f"{prefix}_y", 0)
        st.session_state.setdefault(f"{prefix}_scale", 100)
    st.session_state.setdefault("part_contrast", DEFAULT_CONTRAST_PCT)
    st.session_state.setdefault("part_sharpness", DEFAULT_SHARPNESS_PCT)
    st.session_state.setdefault("fine_tune", False)

    _render_editor_header(result)
    _render_toolbar(result)
    if result.get("product_raw"):
        with st.spinner("Aplicando contraste e nitidez na peça..."):
            _apply_part_treatment(result, st.session_state["part_contrast"],
                                  st.session_state["part_sharpness"])
    assets = _drag_assets(result)
    previews = _current_previews(result)
    reports = [validate_output(image) for image in previews]
    if result.get("cover_layers"):
        # The cover is cars on top + part below by design: only cuts at the border matter there.
        reports[0] = {**reports[0], "valid": not reports[0]["clipped"]}
    _render_image_tabs(result, assets, reports)
    _render_publish(result, previews, reports)


def render_cover_import() -> None:
    st.subheader("Importar carros das capas do BaseLinker", icon=":material/cloud_download:")
    st.caption(
        "Lê todos os produtos, pega a imagem 1 (capa) de alguns anúncios de cada modelo, recorta o carro "
        "e salva no catálogo local. Depois disso, a busca de veículos encontra esses carros na hora. "
        "Só lê o BaseLinker; não altera nenhum anúncio. Pode levar alguns minutos."
    )
    if st.button("Importar carros das capas", key="import_covers", icon=":material/download:"):
        from cover_import import import_covers

        bar = st.progress(0.0, text="Iniciando...")
        try:
            summary = import_covers(client(), lambda fraction, message: bar.progress(min(fraction, 1.0), text=message))
            st.success(
                f"{summary['vehicles']} imagens de carros salvas, de {summary['models']} modelos "
                f"({summary['recognized']} de {summary['products']} produtos reconhecidos)."
            )
        except (BaseLinkerError, ValueError, OSError) as error:
            st.error(str(error))


def render_vehicle_registration() -> None:
    with st.container(border=True):
        render_cover_import()
    st.subheader("Cadastrar veículo", icon=":material/add_photo_alternate:")
    st.caption("Cadastre uma imagem do veículo para reutilizá-la nas próximas buscas.")
    with st.form("vehicle_form"):
        uploaded_file = st.file_uploader("Imagem do veículo", type=["png", "jpg", "jpeg", "webp"])
        brand_col, model_col, years_col = st.columns(3)
        with brand_col:
            brand = st.text_input("Marca", placeholder="Chevrolet")
        with model_col:
            model = st.text_input("Modelo", placeholder="Cruze")
        with years_col:
            years = st.text_input("Anos", placeholder="2014 a 2016")
        submitted = st.form_submit_button("Salvar veículo", type="primary", icon=":material/save:")
    if uploaded_file:
        st.image(uploaded_file, caption="Pré-visualização", width=320)
    if submitted:
        try:
            if not uploaded_file:
                raise ValueError("Envie uma imagem.")
            st.success(f"Veículo salvo em {register_vehicle(brand, model, years, uploaded_file)}")
        except (OSError, ValueError) as error:
            st.error(str(error))


st.title("Studio de imagens de autopeças", icon=":material/photo_camera:")
st.caption("Gere, ajuste e publique as imagens dos anúncios com segurança.")
process_tab, vehicle_tab = st.tabs([":material/auto_awesome: Processar SKU", ":material/directions_car: Catálogo de veículos"])
with process_tab:
    render_processor()
with vehicle_tab:
    render_vehicle_registration()
