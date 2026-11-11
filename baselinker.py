import json
from typing import Any

import requests


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
        response = requests.post(self.api_url, data=payload, timeout=60)
        response.raise_for_status()
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

    def update_images(self, product_id: str, image_urls: list[str]) -> None:
        images = {str(index): url for index, url in enumerate(image_urls)}
        self.call(
            "updateInventoryProductsData",
            {
                "inventory_id": self.inventory_id,
                "products": {product_id: {"images": images}},
            },
        )


def extract_image_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return [value]
    if isinstance(value, dict):
        for item in value.values():
            urls.extend(extract_image_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(extract_image_urls(item))
    return list(dict.fromkeys(urls))