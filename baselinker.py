import base64
import io
import json
import time
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

import requests
from PIL import Image

PROJECT_DIR = Path(__file__).resolve().parent
MAX_IMAGE_BYTES = 2 * 1024 * 1024

class BaseLinkerError(RuntimeError):
    pass


def sku_number(sku: str) -> int | None:
    """Número do SKU para comparar faixas (ex.: '00009000' -> 9000). None se não for numérico."""
    text = str(sku).strip()
    return int(text) if text.isdigit() else None


class BaseLinkerClient:
    def __init__(self, token: str, inventory_id: str, api_url: str) -> None:
        self.token = token
        self.inventory_id = inventory_id
        self.api_url = api_url

    def call(self, method: str, parameters: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "method": method,
            "parameters": json.dumps(parameters),
        }
        response = None
        for attempt in range(3):
            try:
                response = requests.post(
                    self.api_url,
                    data=payload,
                    headers={"X-BLToken": self.token},
                    timeout=(5, 30),
                )
                if response.status_code != 429:
                    response.raise_for_status()
                    break
            except requests.RequestException:
                if attempt == 2:
                    raise BaseLinkerError("Falha de comunicação com o BaseLinker. Tente novamente.")
            if attempt < 2:
                time.sleep(2**attempt)
        if response is None:
            raise BaseLinkerError("Falha de comunicação com o BaseLinker. Tente novamente.")
        if response.status_code == 429:
            raise BaseLinkerError("O BaseLinker limitou as requisições. Aguarde e tente novamente.")
        result = response.json()
        if result.get("status") != "SUCCESS":
            raise BaseLinkerError(result.get("error_message", "Erro desconhecido na API do BaseLinker"))
        return result

    def find_product(self, sku: str) -> tuple[str, dict[str, Any]]:
        sku = sku.strip()
        # filter_sku is an exact match, so also try SKUs registered with stray spaces.
        for filter_value in (sku, f" {sku}", f"{sku} ", f" {sku} "):
            try:
                return self._find_product_exact(sku, filter_value)
            except BaseLinkerError as error:
                if not str(error).startswith("SKU nao encontrado"):
                    raise
        raise BaseLinkerError(f"SKU nao encontrado: {sku}")

    def _find_product_exact(self, sku: str, filter_value: str) -> tuple[str, dict[str, Any]]:
        result = self.call(
            "getInventoryProductsList",
            {"inventory_id": self.inventory_id, "filter_sku": filter_value},
        )
        products_value = result.get("products", [])
        if isinstance(products_value, dict):
            candidates = list(products_value.items())
        elif isinstance(products_value, list):
            candidates = [
                (str(item.get("product_id", "")), item)
                for item in products_value
                if isinstance(item, dict)
            ]
        else:
            candidates = []

        product_id = None
        product = None
        for candidate_id, item in candidates:
            if isinstance(item, dict) and str(item.get("sku", "")).strip() == sku:
                product_id = str(item.get("product_id") or candidate_id)
                product = item
                break
            if isinstance(item, str) and item.strip() == sku:
                product_id = str(candidate_id)
                product = {"sku": item}
                break

        if not product_id or not product:
            raise BaseLinkerError(f"SKU nao encontrado: {sku}")
        details = self.call(
            "getInventoryProductsData",
            {"inventory_id": self.inventory_id, "products": [product_id]},
        )
        return product_id, details.get("products", {}).get(product_id, product)

    def update_images(
        self,
        product_id: str,
        image_urls: list[str],
        existing_images: list[str] | None = None,
        star: int | None = None,
    ) -> None:
        """Replace the first positions and keep `existing_images` after them (max 16).

        `star` (0-5) is changed in the same call, so it only changes when the images are sent.
        """
        images = {
            str(index): f"url:{url}" if not url.startswith(("url:", "data:")) else url
            for index, url in enumerate((image_urls + (existing_images or []))[:16])
        }
        parameters: dict[str, Any] = {
            "inventory_id": self.inventory_id,
            "product_id": product_id,
            "images": images,
        }
        if star is not None:
            parameters["star"] = int(star)
        self.call("addInventoryProduct", parameters)

    def list_products(self) -> list[tuple[str, str]]:
        """(product ID, SKU) of the whole inventory. The list method is cheap and already
        returns the SKU, so a SKU range can be applied before reading the heavy product data."""
        items: list[tuple[str, str]] = []
        page = 1
        while True:
            batch = self.call("getInventoryProductsList", {"inventory_id": self.inventory_id, "page": page})
            products = batch.get("products", {}) or {}
            for product_id, info in products.items():
                sku = str((info or {}).get("sku") or "").strip() if isinstance(info, dict) else ""
                items.append((str(product_id), sku))
            if len(products) < 1000:
                return items
            page += 1

    def list_product_ids(self) -> list[str]:
        """Every product ID of the inventory."""
        return [product_id for product_id, _ in self.list_products()]

    def find_starred(self, star: int, progress=None, sku_from: int | None = None,
                     sku_to: int | None = None) -> list[dict[str, str]]:
        """SKU, name and ID of every product with this star type.

        The API cannot filter by star, so the inventory is read in blocks of 1000.
        sku_from / sku_to limit the reading to products whose numeric SKU is in that range.
        `progress(fraction, message)` is called after each block.
        """
        products = self.list_products()
        if sku_from is not None or sku_to is not None:
            low = sku_from if sku_from is not None else 0
            high = sku_to if sku_to is not None else float("inf")
            products = [
                (product_id, sku) for product_id, sku in products
                if (number := sku_number(sku)) is not None and low <= number <= high
            ]
        ids = [product_id for product_id, _ in products]
        found: list[dict[str, str]] = []
        blocks = range(0, len(ids), 1000)
        for done, start in enumerate(blocks, start=1):
            chunk = [int(product_id) for product_id in ids[start:start + 1000]]
            data = self.call("getInventoryProductsData", {"inventory_id": self.inventory_id, "products": chunk})
            for product_id, product in (data.get("products") or {}).items():
                if int(product.get("star") or 0) == int(star) and str(product.get("sku") or "").strip():
                    found.append({
                        "product_id": str(product_id),
                        "sku": str(product["sku"]).strip(),
                        "name": str((product.get("text_fields") or {}).get("name", "")).strip(),
                    })
            if progress:
                progress(done / len(blocks), f"Lendo o inventário: {min(start + 1000, len(ids))} de {len(ids)}")
        return sorted(found, key=lambda item: item["sku"])

    @staticmethod
    def image_file_to_data(path: str | Path) -> str:
        """Encode an image for the API, compressing it below BaseLinker's 2 MB limit."""
        raw = Path(path).read_bytes()
        if len(raw) > MAX_IMAGE_BYTES:
            with Image.open(path) as source:
                image = source.convert("RGB")
            quality = 90
            while quality >= 50:
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=quality, optimize=True)
                raw = buffer.getvalue()
                if len(raw) <= MAX_IMAGE_BYTES:
                    break
                quality -= 5
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"Imagem acima de 2 MB mesmo comprimida: {path}")
        return "data:" + base64.b64encode(raw).decode("ascii")

    def save_images_backup(
        self,
        sku: str,
        product_id: str,
        images: list[str],
        backup_dir: str | Path = PROJECT_DIR / "arquivos" / "backup",
    ) -> Path:
        directory = Path(backup_dir)
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{sku}_{timestamp}.json"
        path.write_text(
            json.dumps({"sku": sku, "product_id": product_id, "images": images}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def restore_images(self, backup_path: str | Path) -> None:
        backup = json.loads(Path(backup_path).read_text(encoding="utf-8"))
        images = backup.get("images", [])
        if not isinstance(images, list):
            raise BaseLinkerError("Backup de imagens inválido")
        self.update_images(str(backup["product_id"]), images[:16])


def extract_image_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        normalized = value[4:] if value.startswith("url:") else value
        if normalized.startswith(("http://", "https://")):
            return [normalized]
    if isinstance(value, dict):
        items = value.items()
        try:
            items = sorted(items, key=lambda item: int(item[0]))
        except (TypeError, ValueError):
            items = list(items)
        for _, item in items:
            urls.extend(extract_image_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(extract_image_urls(item))
    return list(dict.fromkeys(urls))
