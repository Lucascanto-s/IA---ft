import csv
import io
import os
import re
from pathlib import Path

import streamlit as st
import pandas as pd
from dotenv import load_dotenv
from PIL import Image

from baselinker import BaseLinkerClient, BaseLinkerError, extract_image_urls
from image_processor import (
    compose_cover,
    compose_vehicle_group,
    create_set,
    load_image,
    product_image,
    public_urls,
    reposition_image,
    sharpen,
    validate_output,
)
from project_utils import normalized_tokens, sanitize_sku
from ui.theme import apply_theme, flow_steps
from vehicle_search import VehicleSearchError, find_vehicle_image


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
}


def _initial_fitments(sku: str, product: dict) -> list[dict[str, str]]:
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
    if not VEHICLE_LIBRARY_FILE.exists():
        return ""
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
    return max(candidates)[4] if candidates else ""


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


def _reset_editor_state() -> None:
    for key in ("cover_vehicle_x", "cover_vehicle_y", "cover_vehicle_scale",
                "cover_product_x", "cover_product_y", "cover_product_scale"):
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
    for fitment in fitments:
        brand = str(fitment.get("brand", "")).strip()
        model = str(fitment.get("model", "")).strip()
        years = str(fitment.get("years", "")).strip()
        if not brand or not model:
            continue
        source = local_vehicle_for_fitment(brand, model, years)
        origin = "Catálogo local" if source else ""
        if not source and sku_source and len(fitments) == 1:
            source = sku_source
            origin = "Imagem cadastrada para este SKU"
        if not source:
            try:
                query = " ".join(part for part in (brand, model, years) if part)
                source = find_vehicle_image(query)
                origin = "Busca online"
            except Exception as error:
                source = ""
                origin = str(error)
        choices.append({
            "brand": brand,
            "model": model,
            "years": years,
            "source": source,
            "origin": origin,
            "status": "pending" if source else "missing",
            "image": None,
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
    cover_layers = {
        "vehicle": vehicle_group,
        "product": product_image(image_urls[1] if len(image_urls) > 1 else image_urls[0]),
    }
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
    }
    _reset_editor_state()
    st.session_state.pop("pending_vehicle_review", None)


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


def _save_final_images(result: dict, previews: list[Image.Image]) -> None:
    for path, image in zip(result["paths"], previews):
        sharpen(image).convert("RGB").save(path, quality=98, subsampling=0)


def _fitment_label(choice: dict) -> str:
    years = f" ({choice['years']})" if choice.get("years") else ""
    return f"{choice['brand']} {choice['model']}{years}"


def _editor_text(value) -> str:
    return "" if pd.isna(value) else str(value).strip()


def _render_vehicle_review() -> None:
    pending = st.session_state.get("pending_vehicle_review")
    if not pending:
        return

    st.subheader(f"Veículos compatíveis · SKU {pending['sku']}")
    st.caption("Uma linha por carro. Cada linha será procurada no catálogo local antes da busca online.")

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
    with review_tab:
        selected = st.selectbox(
            "Veículo para validar",
            options=list(range(len(choices))),
            format_func=lambda index: f"{labels[index]} · {choices[index]['status']}",
            key=f"review_vehicle_{pending['sku']}",
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
                st.image(
                    choice["image"],
                    caption=f"Imagem para {labels[selected]}",
                    width="stretch",
                    alt=f"Prévia do veículo {labels[selected]}",
                )
            except (OSError, ValueError) as error:
                st.error(f"Não consegui abrir essa imagem: {error}")
                choice["status"] = "missing"
        else:
            st.info("Não há imagem para validar. Use a aba de imagem manual ou descarte este veículo.")

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
                st.rerun()
        with discard_col:
            if st.button("Descartar imagem", key=f"discard_vehicle_{pending['sku']}_{selected}"):
                choice["image"] = None
                choice["status"] = "discarded"
                choices[selected] = choice
                pending["choices"] = choices
                st.session_state["pending_vehicle_review"] = pending
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
        uploaded = st.file_uploader(
            "Imagem do computador",
            type=["png", "jpg", "jpeg", "webp"],
            key=f"manual_vehicle_upload_{pending['sku']}_{selected_manual}",
        )
        if uploaded:
            try:
                with Image.open(io.BytesIO(uploaded.getvalue())) as source_image:
                    manual_image = source_image.convert("RGBA")
                st.image(
                    manual_image,
                    caption=f"Prévia manual · {labels[selected_manual]}",
                    width="stretch",
                    alt=f"Imagem enviada para {labels[selected_manual]}",
                )
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
        st.info("Aprove ou descarte cada resultado. Imagens não encontradas podem ser enviadas na aba manual.")
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
    tabs = st.tabs(["Imagem 1 · Capa", "Imagem 2", "Imagem 3", "Imagem 4"])
    with tabs[0]:
        if result.get("cover_layers"):
            preview_column, controls_column = st.columns([1.5, 1])
            with controls_column:
                st.subheader("Veículos compatíveis")
                st.slider("Lado", -400, 400, 0, 10, key="cover_vehicle_x")
                st.slider("Altura", -250, 250, 0, 10, key="cover_vehicle_y")
                st.slider("Zoom", 50, 150, 100, 5, key="cover_vehicle_scale")
                st.subheader("Peça")
                st.slider("Lado", -400, 400, 0, 10, key="cover_product_x")
                st.slider("Altura", -350, 250, 0, 10, key="cover_product_y")
                st.slider("Zoom", 50, 150, 100, 5, key="cover_product_scale")
            with preview_column:
                st.image(_current_previews(result)[0], caption="Prévia limpa da capa", width="stretch")
        else:
            st.image(_current_previews(result)[0], caption="Prévia limpa da capa", width="stretch")

    for index, tab in enumerate(tabs[1:], start=2):
        with tab:
            preview_column, controls_column = st.columns([1.5, 1])
            with controls_column:
                st.slider("Lado", -400, 400, 0, 10, key=f"image_{index}_x")
                st.slider("Altura", -400, 400, 0, 10, key=f"image_{index}_y")
                st.slider("Zoom", 50, 150, 100, 5, key=f"image_{index}_scale")
            with preview_column:
                st.image(_current_previews(result)[index - 1], caption=f"Prévia limpa da imagem {index}",
                         width="stretch")

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


def render_vehicle_registration() -> None:
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
