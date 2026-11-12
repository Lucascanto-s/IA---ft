import json
import time
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

import requests

PROJECT_DIR = Path(__file__).resolve().parent

class BaseLinkerError(RuntimeError):
    pass


class BaseLinkerClient:
    def __init__(self, token: str, inventory_id: str, api_url: str) -> None:
        self.token = token
        self.inventory_id = inventory_id
        self.api_url = api_url

    def call(self, method: str, parameters: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "token": self.token,
            "method": method,
            "parameters": json.dumps(parameters),
        }
        response = None
        for attempt in range(3):
            try:
                response = requests.post(self.api_url, data=payload, timeout=(5, 30))
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
        result = self.call(
            "getInventoryProductsList",
            {"inventory_id": self.inventory_id, "filter_sku": sku},
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
            if isinstance(item, dict) and item.get("sku") == sku:
                product_id = str(item.get("product_id") or candidate_id)
                product = item
                break
            if isinstance(item, str) and item == sku:
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
    ) -> None:
        images = {
            str(index): f"url:{url}" if not url.startswith(("url:", "data:")) else url
            for index, url in enumerate((image_urls + (existing_images or []))[:16])
        }
        self.call(
            "updateInventoryProductsData",
            {
                "inventory_id": self.inventory_id,
                "products": {product_id: {"images": images}},
            },
        )

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
        self.call(
            "updateInventoryProductsData",
            {
                "inventory_id": self.inventory_id,
                "products": {
                    str(backup["product_id"]): {
                        "images": {
                            str(index): value if value.startswith(("url:", "data:")) else f"url:{value}"
                            for index, value in enumerate(images[:16])
                        }
                    }
                },
            },
        )


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