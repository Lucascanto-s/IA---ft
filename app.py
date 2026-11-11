import csv
import os
import re
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from PIL import Image

from baselinker import BaseLinkerClient, BaseLinkerError, extract_image_urls
from image_processor import (
    content_bands,
    compose_cover,
    create_set,
    load_image,
    prepare_vehicle,
    product_image,
    public_urls,
    reposition_image,
    editor_preview,
    validate_output,
)
from vehicle_search import VehicleSearchError, find_vehicle_image


load_dotenv()
PROJECT_DIR = Path(__file__).resolve().parent
FILES_DIR = PROJECT_DIR / "arquivos"
VEHICLE_MAP_FILE = Path(os.getenv("VEHICLE_MAP_FILE", FILES_DIR / "mapa" / "vehicle_map.csv"))
VEHICLE_LIBRARY_FILE = FILES_DIR / "mapa" / "vehicles.csv"
st.set_page_config(page_title="Imagens de autopecas", layout="wide")
st.title("Gerador de imagens de autopecas")


def vehicle_for_sku(sku: str) -> dict[str, str] | None:
    with VEHICLE_MAP_FILE.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            if row.get("sku", "").strip() == sku:
                return row
    return None


def vehicle_query_from_product(product: dict) -> str:
    text_fields = product.get("text_fields", {})
    if isinstance(text_fields, dict):
        name = text_fields.get("name", "")
        if isinstance(name, str):
            return name.strip()
    return ""


def local_vehicle_for_query(query: str) -> str:
    if not VEHICLE_LIBRARY_FILE.exists():
        return ""
    query_words = set(re.findall(r"[a-zA-Z0-9]+", query.lower()))
    with VEHICLE_LIBRARY_FILE.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            required = {
                row.get("brand", "").strip().lower(),
                row.get("model", "").strip().lower(),
            }
            required.discard("")
            if required and required.issubset(query_words):
                return row.get("image_path", "").strip()
    return ""


def client() -> BaseLinkerClient:
    token = os.getenv("BASELINKER_TOKEN", "")
    inventory_id = os.getenv("BASELINKER_INVENTORY_ID", "")
    if not token or not inventory_id:
        raise ValueError("Preencha BASELINKER_TOKEN e BASELINKER_INVENTORY_ID no arquivo .env")
    return BaseLinkerClient(
        token,
        inventory_id,
        os.getenv("BASELINKER_API_URL", "https://api.baselinker.com/connector.php"),
    )


def register_vehicle(brand: str, model: str, years: str, uploaded_file) -> Path:
    vehicle_key = f"{brand}_{model}_{years}"
    safe_key = re.sub(r"[^a-zA-Z0-9_-]+", "_", vehicle_key.strip()).strip("_").lower()
    if not safe_key:
        raise ValueError("Informe marca e modelo do veículo")
    vehicle_dir = FILES_DIR / "veiculos"
    vehicle_dir.mkdir(parents=True, exist_ok=True)
    extension = Path(uploaded_file.name).suffix.lower() or ".png"
    image_path = vehicle_dir / f"{safe_key}{extension}"
    image_path.write_bytes(uploaded_file.getvalue())

    VEHICLE_LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["brand", "model", "years", "image_path"]
    rows = []
    if VEHICLE_LIBRARY_FILE.exists():
        with VEHICLE_LIBRARY_FILE.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
    relative_path = image_path.relative_to(PROJECT_DIR).as_posix()
    new_row = {"brand": brand.strip(), "model": model.strip(), "years": years.strip(), "image_path": relative_path}
    rows = [row for row in rows if not (
        row.get("brand", "").strip().lower() == brand.strip().lower()
        and row.get("model", "").strip().lower() == model.strip().lower()
    )]
    rows.append(new_row)
    with VEHICLE_LIBRARY_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return image_path


def render_processor() -> None:
    sku = st.text_input("SKU")
    if st.button("Buscar e gerar imagens", type="primary") and sku:
        try:
            product_id, product = client().find_product(sku.strip())
            vehicle = vehicle_for_sku(sku.strip())
            vehicle_query = vehicle.get("vehicle", "").strip() if vehicle else ""
            vehicle_source = vehicle.get("vehicle_image_url", "").strip() if vehicle else ""
            if not vehicle_source:
                vehicle_query = vehicle_query or vehicle_query_from_product(product)
                if not vehicle_query:
                    raise ValueError("Cadastre o veículo na aba Veículos ou no nome do produto")
                vehicle_source = local_vehicle_for_query(vehicle_query) or find_vehicle_image(vehicle_query)
            image_urls = extract_image_urls(product.get("images", product))
            if not image_urls:
                raise ValueError("Nenhuma imagem foi encontrada no produto")
            output_dir = Path(os.getenv("OUTPUT_DIR", FILES_DIR / "saida")) / sku.strip()
            paths = create_set(image_urls, vehicle_source, str(output_dir))
            cover_layers = None
            first_source = load_image(image_urls[0])
            if len(content_bands(first_source)) < 2:
                cover_product_source = image_urls[1] if len(image_urls) > 1 else image_urls[0]
                cover_layers = {
                    "vehicle": prepare_vehicle(vehicle_source),
                    "product": product_image(cover_product_source),
                }
            for key in (
                "cover_vehicle_x",
                "cover_vehicle_y",
                "cover_vehicle_scale",
                "cover_product_x",
                "cover_product_y",
                "cover_product_scale",
            ):
                st.session_state.pop(key, None)
            for index in range(2, 5):
                for suffix in ("x", "y", "scale"):
                    st.session_state.pop(f"image_{index}_{suffix}", None)
            st.session_state["result"] = {
                "sku": sku.strip(),
                "product_id": product_id,
                "paths": paths,
                "cover_layers": cover_layers,
                "editor_sources": {path.name: load_image(str(path)) for path in paths[1:]},
            }
            st.success(f"Imagens geradas em {output_dir}")
        except (BaseLinkerError, ValueError, VehicleSearchError, OSError) as error:
            st.error(str(error))

    result = st.session_state.get("result")
    if result:
        if result.get("cover_layers"):
            st.subheader("Ajustar imagem 1: carro e peça")
            preview_column, controls_column = st.columns([1.3, 1])
            with controls_column:
                st.caption("Carro")
                vehicle_x = st.slider("Lado", -400, 400, 0, 10, key="cover_vehicle_x")
                vehicle_y = st.slider("Altura", -250, 250, 0, 10, key="cover_vehicle_y")
                vehicle_scale = st.slider("Zoom", 50, 150, 100, 5, key="cover_vehicle_scale")
                st.caption("Peça")
                product_x = st.slider("Lado", -400, 400, 0, 10, key="cover_product_x")
                product_y = st.slider("Altura", -350, 250, 0, 10, key="cover_product_y")
                product_scale = st.slider("Zoom", 50, 150, 100, 5, key="cover_product_scale")
            cover = compose_cover(
                result["cover_layers"]["vehicle"],
                result["cover_layers"]["product"],
                vehicle_x,
                vehicle_y,
                vehicle_scale,
                product_x,
                product_y,
                product_scale,
                resampling=Image.Resampling.BILINEAR,
            )
            cover.save(result["paths"][0], quality=98, subsampling=0)
            with preview_column:
                st.image(
                    editor_preview(cover),
                    caption="Imagem 1 - prévia de edição",
                    width="stretch",
                )

        st.subheader("Ajustar imagens da peça")
        editor_sources = result.setdefault("editor_sources", {})
        for index, path in enumerate(result["paths"][1:], start=2):
            if path.name not in editor_sources:
                editor_sources[path.name] = load_image(str(path))
            with st.expander(f"Imagem {index}"):
                preview_column, controls_column = st.columns([1.3, 1])
                with controls_column:
                    st.caption("Comandos de edição")
                    x_offset = st.slider("Lado", -400, 400, 0, 10, key=f"image_{index}_x")
                    y_offset = st.slider("Altura", -400, 400, 0, 10, key=f"image_{index}_y")
                    scale = st.slider("Zoom", 50, 150, 100, 5, key=f"image_{index}_scale")
                adjusted = reposition_image(
                    editor_sources[path.name],
                    x_offset,
                    y_offset,
                    scale,
                    resampling=Image.Resampling.BILINEAR,
                )
                adjusted.save(path, quality=98, subsampling=0)
                with preview_column:
                    st.image(
                        editor_preview(adjusted),
                        caption=f"{path.name} - prévia de edição",
                        width="stretch",
                    )

        validation = [validate_output(path) for path in result["paths"]]
        invalid = [path.name for path, report in zip(result["paths"], validation) if not report["valid"]]
        if invalid:
            st.warning(f"Revise o enquadramento de: {', '.join(invalid)}")
        else:
            st.success("Validação de pixels: imagens centralizadas e sem cortes.")
        st.warning("Revise as quatro imagens antes de gravar no BaseLinker.")
        base_url = os.getenv("PUBLIC_IMAGE_BASE_URL", "").strip()
        if not base_url:
            st.info("Defina PUBLIC_IMAGE_BASE_URL no .env para habilitar o envio.")
        elif st.button("Aprovar e gravar no BaseLinker", type="primary"):
            urls = public_urls(result["paths"], base_url, result["sku"])
            try:
                client().update_images(result["product_id"], urls)
                st.success("Imagens gravadas nas posições 0 a 3.")
            except (BaseLinkerError, ValueError) as error:
                st.error(str(error))


def render_vehicle_registration() -> None:
    st.subheader("Cadastrar carro compatível")
    st.caption("A imagem cadastrada será usada na capa, evitando buscas automáticas com resultados incorretos.")
    with st.form("vehicle_form"):
        uploaded_file = st.file_uploader("Imagem do veículo", type=["png", "jpg", "jpeg", "webp"])
        brand = st.text_input("Marca", placeholder="Chevrolet")
        model = st.text_input("Modelo", placeholder="Cruze")
        years = st.text_input("Anos", placeholder="2014 a 2016")
        submitted = st.form_submit_button("Salvar veículo", type="primary")
    if uploaded_file:
        st.image(uploaded_file, caption="Pré-visualização", width=320)
    if submitted:
        if not uploaded_file or not brand.strip() or not model.strip():
            st.error("Envie uma imagem e informe a marca e o modelo.")
        else:
            try:
                image_path = register_vehicle(brand, model, years, uploaded_file)
                st.success(f"Veículo salvo em {image_path}")
            except (OSError, ValueError) as error:
                st.error(str(error))


process_tab, vehicle_tab = st.tabs(["Processar SKU", "Veículos"])
with process_tab:
    render_processor()
with vehicle_tab:
    render_vehicle_registration()