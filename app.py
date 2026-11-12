import csv
import os
import re
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from PIL import Image

from baselinker import BaseLinkerClient, BaseLinkerError, extract_image_urls
from image_processor import (
    compose_cover,
    content_bands,
    create_set,
    load_image,
    prepare_vehicle,
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
    return {int(item) for item in re.findall(r"\b(?:19|20)\d{2}\b", value)}


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
                "peca", "peça", "traseiro", "traseira", "suporte", "trava",
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


def local_vehicle_for_query(query: str, year: int | None = None) -> str:
    if not VEHICLE_LIBRARY_FILE.exists():
        return ""
    query_words = normalized_tokens(query)
    requested_years = {year} if year else _years(query)
    candidates: list[tuple[int, int, int, str]] = []
    with VEHICLE_LIBRARY_FILE.open(newline="", encoding="utf-8") as file:
        for index, row in enumerate(csv.DictReader(file)):
            required = normalized_tokens(f"{row.get('brand', '')} {row.get('model', '')}")
            if not required or not required.issubset(query_words):
                continue
            row_years = _years(row.get("years", ""))
            if requested_years and row_years and not requested_years.intersection(row_years):
                continue
            matched_year = len(requested_years.intersection(row_years))
            newest_year = max(row_years, default=0)
            image_path = row.get("image_path", "").strip()
            if image_path:
                resolved_path = Path(image_path).expanduser()
                if not resolved_path.is_absolute():
                    resolved_path = PROJECT_DIR / resolved_path
                if resolved_path.exists():
                    candidates.append((matched_year, len(required), newest_year, image_path))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3]


def vehicle_source_for_product(sku: str, product: dict) -> str:
    """Resolve vehicle art in database order, using the web only as a last resort."""
    vehicle = vehicle_for_sku(sku) or {}
    source = vehicle.get("vehicle_image_url", "").strip()
    query = vehicle.get("vehicle_key", "").strip() or vehicle_query_from_product(product)
    if source:
        return source
    if not query:
        raise ValueError("Cadastre o veículo na aba Veículos ou no nome do produto")
    return local_vehicle_for_query(query) or find_vehicle_image(query)


def client() -> BaseLinkerClient:
    token = os.getenv("BASELINKER_TOKEN", "")
    inventory_id = os.getenv("BASELINKER_INVENTORY_ID", "")
    if not token or not inventory_id:
        raise ValueError("Preencha BASELINKER_TOKEN e BASELINKER_INVENTORY_ID no arquivo .env")
    return BaseLinkerClient(token, inventory_id, os.getenv(
        "BASELINKER_API_URL", "https://api.baselinker.com/connector.php"
    ))


def register_vehicle(brand: str, model: str, years: str, uploaded_file) -> Path:
    brand = brand.strip()
    model = model.strip()
    years = years.strip()
    if not brand or not model:
        raise ValueError("Informe marca e modelo do veículo")
    safe_key = sanitize_sku(f"{brand}_{model}_{years}").lower()
    vehicle_dir = FILES_DIR / "veiculos"
    vehicle_dir.mkdir(parents=True, exist_ok=True)
    extension = Path(uploaded_file.name).suffix.lower() or ".png"
    image_path = vehicle_dir / f"{safe_key}{extension}"
    image_path.write_bytes(uploaded_file.getvalue())

    VEHICLE_LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if VEHICLE_LIBRARY_FILE.exists():
        with VEHICLE_LIBRARY_FILE.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
    relative_path = image_path.relative_to(PROJECT_DIR).as_posix()
    rows = [row for row in rows if not (
        row.get("brand", "").strip().lower() == brand.lower()
        and row.get("model", "").strip().lower() == model.lower()
        and row.get("years", "").strip().lower() == years.lower()
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


def _generate_sku(sku: str) -> None:
    product_id, product = client().find_product(sku)
    vehicle_source = vehicle_source_for_product(sku, product)
    image_urls = extract_image_urls(product.get("images", product))
    if not image_urls:
        raise ValueError("Nenhuma imagem foi encontrada no produto")

    safe_sku = sanitize_sku(sku)
    output_dir = Path(os.getenv("OUTPUT_DIR", FILES_DIR / "saida")) / safe_sku
    output_dir.mkdir(parents=True, exist_ok=True)
    generated = create_set(image_urls, vehicle_source, str(output_dir))
    sources = [load_image(url) for url in image_urls[:4]]
    while len(sources) < 4:
        sources.append(sources[-1].copy())
    cover_layers = None
    if len(content_bands(sources[0])) < 2:
        cover_layers = {
            "vehicle": prepare_vehicle(vehicle_source),
            "product": product_image(image_urls[1] if len(image_urls) > 1 else image_urls[0]),
        }
    previews = [load_image(str(output_dir / f"{index}.jpg")) if (output_dir / f"{index}.jpg").exists()
               else Image.new("RGB", (1200, 1200), "white") for index in range(1, 5)]
    st.session_state["result"] = {
        "sku": safe_sku,
        "product_id": product_id,
        "product_images": image_urls,
        "paths": generated.paths,
        "warnings": generated.warnings,
        "cover_layers": cover_layers,
        "editor_sources": {
            1: load_image(str(paths[0])) if cover_layers is None else sources[0],
            2: sources[1],
            3: sources[2],
            4: sources[3],
        },
        "previews": previews,
        "backup_path": None,
    }
    _reset_editor_state()


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


def render_processor() -> None:
    st.header("Studio de imagens de autopeças")
    st.caption("Gere, revise e publique conjuntos de imagens com segurança.")
    flow_steps(1 if not st.session_state.get("result") else 3)
    with st.sidebar:
        st.subheader("Fila de SKUs")
        queue_text = st.text_area("Um SKU por linha", placeholder="PEC-001\nPEC-002")
        if st.button("Buscar próximo SKU", use_container_width=True, type="primary"):
            sku = next((item.strip() for item in re.split(r"[\n,;]+", queue_text) if item.strip()), "")
            if sku:
                try:
                    _generate_sku(sku)
                    st.rerun()
                except (BaseLinkerError, ValueError, VehicleSearchError, OSError) as error:
                    st.error(str(error))
        st.divider()
        st.metric("Status", "Em revisão" if st.session_state.get("result") else "Aguardando SKU")

    result = st.session_state.get("result")
    if not result:
        st.info("Adicione um SKU à fila para começar.")
        return

    st.success(f"SKU {result['sku']} carregado. Ajuste e revise antes de publicar.")
    for warning in result.get("warnings", []):
        st.warning(warning)
    tabs = st.tabs(["Imagem 1 · Capa", "Imagem 2", "Imagem 3", "Imagem 4"])
    with tabs[0]:
        if result.get("cover_layers"):
            preview_column, controls_column = st.columns([1.5, 1])
            with controls_column:
                st.subheader("Carro")
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
    if not base_url:
        st.info("Defina PUBLIC_IMAGE_BASE_URL no .env para habilitar o envio.")
    confirm = st.checkbox("Confirmo que revisei as quatro imagens e quero publicar este SKU.")
    publish, undo = st.columns(2)
    with publish:
        if st.button("Publicar no BaseLinker", type="primary", use_container_width=True,
                     disabled=not base_url or not confirm):
            try:
                api = client()
                backup = api.save_images_backup(result["sku"], result["product_id"], result["product_images"])
                _save_final_images(result, previews)
                urls = public_urls(result["paths"], base_url, result["sku"])
                api.update_images(result["product_id"], urls, result["product_images"][4:])
                result["backup_path"] = backup
                st.success(f"Publicado com backup em {backup}.")
            except (BaseLinkerError, ValueError, OSError) as error:
                st.error(str(error))
    with undo:
        if st.button("Desfazer última publicação", use_container_width=True,
                     disabled=not result.get("backup_path")):
            try:
                client().restore_images(result["backup_path"])
                st.success("Imagens originais restauradas.")
            except (BaseLinkerError, OSError, ValueError) as error:
                st.error(str(error))


def render_vehicle_registration() -> None:
    st.header("Veículos compatíveis")
    st.caption("Cadastre uma imagem com licença comercial e, se necessário, os anos compatíveis.")
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
