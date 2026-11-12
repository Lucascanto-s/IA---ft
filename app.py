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
VEHICLE_LIBRARY_FILE = FILES_DIR / "mapa" / "vehicles.csv"

st.set_page_config(page_title="Studio de imagens | Autopeças", page_icon="🛠️", layout="wide")
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
    for brand, model in possible:
        brand_tokens = normalized_tokens(brand)
        model_tokens = normalized_tokens(model)
        key = (" ".join(sorted(brand_tokens)), " ".join(sorted(model_tokens)))
        if brand_tokens and model_tokens and brand_tokens.issubset(tokens) and model_tokens.issubset(tokens) and key not in seen:
            matches.append({"brand": brand, "model": model, "years": year_range})
            seen.add(key)
    if matches:
        return matches

    # Titles often omit the brand ("Dobradiça Impreza"): infer it from a distinctive model name.
    model_only = []
    for brand, model in possible:
        model_tokens = normalized_tokens(model)
        key = (" ".join(sorted(normalized_tokens(brand))), " ".join(sorted(model_tokens)))
        if (brand and model_tokens and len(model.replace(" ", "")) >= 4
                and model_tokens.issubset(tokens) and key not in seen):
            model_only.append({"brand": brand, "model": model, "years": year_range})
            seen.add(key)
    if model_only:
        return model_only

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


def _current_previews(result: dict) -> list[Image.Image]:
    if result.get("cover_layers"):
        cover = compose_cover(
            result["cover_layers"]["vehicle"], result["cover_layers"]["product"],
            st.session_state.get("cover_vehicle_x", 0), st.session_state.get("cover_vehicle_y", 0),
            st.session_state.get("cover_vehicle_scale", 100), st.session_state.get("cover_product_x", 0),
            st.session_state.get("cover_product_y", 0), st.session_state.get("cover_product_scale", 100),
            resampling=Image.Resampling.BILINEAR,
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
            resampling=Image.Resampling.BILINEAR,
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


def _save_final_images(result: dict, previews: list[Image.Image]) -> None:
    for path, image in zip(result["paths"], previews):
        sharpen(image).convert("RGB").save(path, quality=98, subsampling=0)


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
    with st.expander("Usar os carros da capa de outro SKU", expanded=bool(st.session_state.get(state_key))):
        st.caption("Pega a imagem 1 de outro anúncio, remove a peça e usa só os carros na capa deste SKU.")
        sku_col, button_col = st.columns([3, 1])
        with sku_col:
            other_sku = st.text_input("SKU com a capa pronta", placeholder="00002185",
                                      key=f"copy_cars_sku_{pending['sku']}").strip()
        with button_col:
            st.write("")
            st.write("")
            fetch = st.button("Buscar capa", key=f"copy_cars_fetch_{pending['sku']}", width="stretch")
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
            st.image(_framed(copied["image"]), caption=f"Carros da capa do SKU {copied['sku']}", width="stretch",
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


def _render_vehicle_review() -> None:
    pending = st.session_state.get("pending_vehicle_review")
    if not pending:
        return

    st.subheader(f"Veículos compatíveis · SKU {pending['sku']}")
    st.caption("Uma linha por carro. Cada linha será procurada no catálogo local antes da busca online.")
    _render_copy_cars_from_sku(pending)

    if pending["choices"] is None:
        st.write("Confirme a marca, o modelo e os anos antes de pesquisar as imagens.")
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
        if st.button("Buscar imagens dos veículos", type="primary", key=f"search_fitments_{pending['sku']}"):
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

    if st.button("Editar veículos compatíveis", key=f"edit_fitments_{pending['sku']}"):
        pending["choices"] = None
        pending["fitment_version"] += 1
        st.session_state["pending_vehicle_review"] = pending
        st.rerun()

    labels = [_fitment_label(choice) for choice in choices]
    review_tab, manual_tab = st.tabs(["Validar imagem buscada", "Adicionar ou substituir manualmente"])
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
            format_func=lambda index: f"{labels[index]} · {choices[index]['status']}",
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
                    st.image(_framed(choice["image"]), caption="Foto encontrada", width="stretch",
                             alt=f"Foto original do veículo {labels[selected]}")
                with cutout_col:
                    st.image(_framed(choice["cutout"]), caption="Como vai ficar na capa", width="stretch",
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
        with approve_col:
            if st.button(
                "Aprovar imagem",
                type="primary",
                disabled=not choice.get("image"),
                key=f"approve_vehicle_{pending['sku']}_{selected}",
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
                _go_to_next_pending(selected)
                st.rerun()
        with discard_col:
            if st.button("Descartar imagem", key=f"discard_vehicle_{pending['sku']}_{selected}"):
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
                           google_images_url(manual_choice, transparent=False), width="stretch")
        with trans_col:
            st.link_button("Google Imagens · fundo transparente",
                           google_images_url(manual_choice, transparent=True), width="stretch")
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
                    st.image(_framed(manual_image), caption="Imagem escolhida", width="stretch",
                             alt=f"Imagem escolhida para {labels[selected_manual]}")
                with cutout_col:
                    st.image(_framed(manual_cutout), caption="Como vai ficar na capa", width="stretch",
                             alt=f"Veículo {labels[selected_manual]} recortado")
                save_manual = st.checkbox(
                    "Salvar ou substituir no catálogo local",
                    value=True,
                    key=f"save_manual_{pending['sku']}_{selected_manual}",
                )
                if st.button(
                    "Aprovar imagem manual",
                    type="primary",
                    key=f"approve_manual_{pending['sku']}_{selected_manual}",
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
                    st.rerun()
            except (OSError, ValueError) as error:
                st.error(f"Arquivo de imagem inválido: {error}")

    approved = [choice for choice in choices if choice["status"] == "approved"]
    unresolved = [choice for choice in choices if choice["status"] not in ("approved", "discarded")]
    st.divider()
    st.write("Status dos veículos")
    for choice in choices:
        st.caption(f"{_fitment_label(choice)} — {choice['status']}")
    if unresolved:
        pending_names = ", ".join(_fitment_label(choice) for choice in unresolved)
        st.warning(
            f"Para liberar **Gerar capa e imagens**, aprove ou descarte também: **{pending_names}**. "
            "Selecione o veículo na lista acima. Imagens não encontradas podem ser enviadas na aba manual."
        )
    if not approved:
        st.warning("Aprove pelo menos uma imagem de veículo para gerar a capa.")
    if st.button(
        "Gerar capa e imagens",
        type="primary",
        disabled=not approved or bool(unresolved),
        key=f"generate_approved_{pending['sku']}",
    ):
        try:
            _generate_sku(pending, approved)
            st.rerun()
        except (BaseLinkerError, ValueError, VehicleSearchError, OSError) as error:
            st.error(str(error))


def render_processor() -> None:
    st.header("Studio de imagens de autopeças")
    st.caption("Gere, revise e publique conjuntos de imagens com segurança.")
    stage = 1 if not st.session_state.get("pending_vehicle_review") and not st.session_state.get("result") else 2
    if st.session_state.get("result"):
        stage = 3
    flow_steps(stage)
    with st.sidebar:
        st.subheader("Lote de SKUs")
        queue_text = st.text_area(
            "Cole de 5 a 10 SKUs, um por linha",
            placeholder="PEC-001\nPEC-002\nPEC-003",
            key="sku_batch_input",
        )
        st.caption("O lote consulta os produtos em conjunto; você aprova cada um antes de seguir.")
        if st.button("Iniciar lote", width="stretch", type="primary"):
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
        st.divider()
        batch = st.session_state.get("sku_batch")
        if batch and not batch.get("completed"):
            st.progress(
                (batch["current_index"] + 1) / len(batch["skus"]),
                text=f"SKU {batch['current_index'] + 1} de {len(batch['skus'])}",
            )
        status = (
            "Falha na consulta do lote" if st.session_state.get("batch_error")
            else
            "Em revisão de veículos" if st.session_state.get("pending_vehicle_review")
            else "Em revisão de imagens" if st.session_state.get("result")
            else "Aguardando SKU"
        )
        st.metric("Status", status)

    batch_error = st.session_state.get("batch_error")
    if batch_error:
        st.error(f"Não consegui carregar o SKU {batch_error['sku']}: {batch_error['error']}")
        retry_col, skip_col = st.columns(2)
        with retry_col:
            if st.button("Tentar novamente", type="primary", key="retry_batch_sku"):
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
        with skip_col:
            if st.button("Pular SKU com falha", key="skip_failed_batch_sku"):
                _load_next_batch_sku()
                st.rerun()
        return

    if st.session_state.get("pending_vehicle_review"):
        _render_vehicle_review()
        return

    result = st.session_state.get("result")
    if not result:
        batch = st.session_state.get("sku_batch")
        if batch and batch.get("completed"):
            st.success("Lote concluído.")
        else:
            st.info("Adicione SKUs à fila para começar.")
        return

    st.success(f"SKU {result['sku']} carregado. Ajuste e revise antes de publicar.")
    if result.get("batch_total", 1) > 1:
        st.caption(f"Lote: SKU {result['batch_index'] + 1} de {result['batch_total']}")
    if result.get("approved_vehicles"):
        st.caption("Veículos na capa: " + ", ".join(
            _fitment_label(vehicle) for vehicle in result["approved_vehicles"]
        ))
    for warning in result.get("warnings", []):
        st.warning(warning)
    for prefix in ["cover_vehicle", "cover_product"] + [f"image_{index}" for index in range(2, 5)]:
        st.session_state.setdefault(f"{prefix}_x", 0)
        st.session_state.setdefault(f"{prefix}_y", 0)
        st.session_state.setdefault(f"{prefix}_scale", 100)
    st.session_state.setdefault("part_contrast", DEFAULT_CONTRAST_PCT)
    st.session_state.setdefault("part_sharpness", DEFAULT_SHARPNESS_PCT)

    if result.get("product_raw"):
        with st.container(border=True):
            st.markdown("**Tratamento da peça** · vale para a capa e as imagens 2, 3 e 4")
            contrast_col, sharpness_col, reset_col = st.columns([2, 2, 1], vertical_alignment="bottom")
            with contrast_col:
                st.slider("Contraste", 80, 140, step=1, key="part_contrast", format="%d%%",
                          help="100% = foto original. Padrão: 108%.")
            with sharpness_col:
                st.slider("Nitidez", 0, 200, step=5, key="part_sharpness", format="%d%%",
                          help="0% = sem nitidez extra. Padrão: 100%.")
            with reset_col:
                st.button("Restaurar padrão", on_click=_reset_part_treatment, width="stretch",
                          icon=":material/restart_alt:")
        with st.spinner("Aplicando tratamento na peça..."):
            _apply_part_treatment(result, st.session_state["part_contrast"],
                                  st.session_state["part_sharpness"])
    assets = _drag_assets(result)

    tabs = st.tabs(["Imagem 1 · Capa", "Imagem 2", "Imagem 3", "Imagem 4"])
    with tabs[0]:
        if result.get("cover_layers"):
            preview_column, controls_column = st.columns([1.5, 1])
            with controls_column:
                st.caption("Ajuste fino (também dá para arrastar na imagem)")
                st.subheader("Veículos compatíveis")
                st.slider("Lado", -400, 400, step=1, key="cover_vehicle_x")
                st.slider("Altura", -250, 250, step=1, key="cover_vehicle_y")
                st.slider("Zoom", 50, 150, step=5, key="cover_vehicle_scale")
                st.subheader("Peça")
                st.slider("Lado", -400, 400, step=1, key="cover_product_x")
                st.slider("Altura", -350, 250, step=1, key="cover_product_y")
                st.slider("Zoom", 50, 150, step=5, key="cover_product_scale")
            with preview_column:
                vehicle_base, product_base = cover_base_sizes(
                    assets["vehicle_size"], assets["product_size"],
                    st.session_state["cover_vehicle_scale"],
                )
                drag_canvas([
                    {"id": "vehicle", "label": "Veículos", "src": assets["vehicle"],
                     "base_w": vehicle_base[0], "base_h": vehicle_base[1], "top": 25, "z": 1,
                     "shadow": True, "x_range": [-400, 400], "y_range": [-250, 250],
                     "scale_range": [50, 150]},
                    {"id": "product", "label": "Peça", "src": assets["product"],
                     "base_w": product_base[0], "base_h": product_base[1], "top": 500, "z": 2,
                     "shadow": False, "x_range": [-400, 400], "y_range": [-350, 250],
                     "scale_range": [50, 150]},
                ], {"vehicle": "cover_vehicle", "product": "cover_product"}, key="drag_cover")
        else:
            st.image(_framed(_current_previews(result)[0]), caption="Prévia limpa da capa", width="stretch",
                     alt="Prévia da capa")

    for index, tab in enumerate(tabs[1:], start=2):
        with tab:
            preview_column, controls_column = st.columns([1.5, 1])
            with controls_column:
                st.caption("Ajuste fino (também dá para arrastar na imagem)")
                st.slider("Lado", -400, 400, step=1, key=f"image_{index}_x")
                st.slider("Altura", -400, 400, step=1, key=f"image_{index}_y")
                st.slider("Zoom", 50, 150, step=5, key=f"image_{index}_scale")
            with preview_column:
                content_size = assets[f"image_{index}_size"]
                drag_canvas([
                    {"id": "product", "label": f"Peça · imagem {index}", "src": assets[f"image_{index}"],
                     "base_w": content_size[0], "base_h": content_size[1], "top": None, "z": 1,
                     "shadow": False, "x_range": [-400, 400], "y_range": [-400, 400],
                     "scale_range": [50, 150]},
                ], {"product": f"image_{index}"}, key=f"drag_image_{index}")

    previews = _current_previews(result)
    validation = [validate_output(image) for image in previews]
    invalid = [str(index) for index, report in enumerate(validation, start=1) if not report["valid"]]
    if invalid:
        st.warning(f"Revise o enquadramento das imagens: {', '.join(invalid)}.")
    else:
        st.success("As quatro imagens estão centralizadas e sem cortes.")

    st.divider()
    st.subheader("Revisão e publicação")
    st.warning("A publicação substitui as posições 1 a 4 e preserva automaticamente as fotos da posição 5 em diante.")
    base_url = os.getenv("PUBLIC_IMAGE_BASE_URL", "").strip()
    if base_url:
        st.caption("As imagens serão enviadas por URLs públicas.")
    else:
        st.info("Sem URL pública configurada: as imagens serão enviadas diretamente pelo computador, em base64.")
    confirm = st.checkbox("Confirmo que revisei as quatro imagens e quero publicar este SKU.")
    publish, undo = st.columns(2)
    with publish:
        if st.button("Publicar no BaseLinker", type="primary", width="stretch",
                     disabled=not confirm):
            try:
                api = client()
                backup = api.save_images_backup(result["sku"], result["product_id"], result["product_images"])
                _save_final_images(result, previews)
                if base_url:
                    images = public_urls(result["paths"], base_url, result["sku"])
                else:
                    images = [api.image_file_to_data(path) for path in result["paths"]]
                api.update_images(result["product_id"], images, result["product_images"][4:])
                result["backup_path"] = backup
                result["published"] = True
                batch = st.session_state.get("sku_batch")
                if batch and batch["current_index"] == len(batch["skus"]) - 1:
                    batch["completed"] = True
                    st.session_state["sku_batch"] = batch
                st.session_state["result"] = result
                st.success(f"Publicado com backup em {backup}.")
            except (BaseLinkerError, ValueError, OSError) as error:
                st.error(str(error))
    with undo:
        if st.button("Desfazer última publicação", width="stretch",
                     disabled=not result.get("backup_path")):
            try:
                client().restore_images(result["backup_path"])
                st.success("Imagens originais restauradas.")
            except (BaseLinkerError, OSError, ValueError) as error:
                st.error(str(error))

    batch = st.session_state.get("sku_batch")
    if (
        batch
        and len(batch["skus"]) > 1
        and not batch.get("completed")
        and batch["current_index"] < len(batch["skus"]) - 1
    ):
        next_col, skip_col = st.columns(2)
        with next_col:
            if st.button(
                "Próximo SKU do lote",
                type="primary",
                width="stretch",
                disabled=not result.get("published"),
                key=f"next_batch_{result['sku']}",
            ):
                try:
                    with st.spinner("Abrindo o próximo SKU do lote..."):
                        _load_next_batch_sku()
                    st.rerun()
                except (BaseLinkerError, ValueError, VehicleSearchError, OSError) as error:
                    st.error(str(error))
        with skip_col:
            if st.button(
                "Pular SKU e continuar",
                width="stretch",
                key=f"skip_batch_{result['sku']}",
            ):
                try:
                    with st.spinner("Abrindo o próximo SKU do lote..."):
                        _load_next_batch_sku()
                    st.rerun()
                except (BaseLinkerError, ValueError, VehicleSearchError, OSError) as error:
                    st.error(str(error))
    elif batch and batch.get("completed") and result.get("published"):
        st.success("Lote concluído.")


def render_cover_import() -> None:
    st.subheader("Importar carros das capas do BaseLinker")
    st.caption(
        "Lê todos os produtos, pega a imagem 1 (capa) de alguns anúncios de cada modelo, recorta o carro "
        "e salva no catálogo local. Depois disso, a busca de veículos encontra esses carros na hora. "
        "Só lê o BaseLinker; não altera nenhum anúncio. Pode levar alguns minutos."
    )
    if st.button("Importar carros das capas", key="import_covers"):
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
    render_cover_import()
    st.divider()
    st.header("Veículos compatíveis")
    st.caption("Cadastre uma imagem do veículo para reutilizá-la nas próximas buscas.")
    with st.form("vehicle_form"):
        uploaded_file = st.file_uploader("Imagem do veículo", type=["png", "jpg", "jpeg", "webp"])
        brand = st.text_input("Marca", placeholder="Chevrolet")
        model = st.text_input("Modelo", placeholder="Cruze")
        years = st.text_input("Anos", placeholder="2014 a 2016")
        submitted = st.form_submit_button("Salvar veículo", type="primary")
    if uploaded_file:
        st.image(uploaded_file, caption="Pré-visualização", width=320)
    if submitted:
        try:
            if not uploaded_file:
                raise ValueError("Envie uma imagem.")
            st.success(f"Veículo salvo em {register_vehicle(brand, model, years, uploaded_file)}")
        except (OSError, ValueError) as error:
            st.error(str(error))


process_tab, vehicle_tab = st.tabs(["Processar SKU", "Veículos"])
with process_tab:
    render_processor()
with vehicle_tab:
    render_vehicle_registration()
