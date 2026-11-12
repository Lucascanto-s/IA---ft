from pathlib import Path

from project_utils import normalized_tokens, sanitize_sku
from image_processor import public_urls


def test_sanitize_sku_removes_path_characters():
    assert sanitize_sku(" Peça / 01 ") == "Peca_01"


def test_normalized_tokens_support_compound_models_and_accents():
    assert normalized_tokens("Land Rover Série 5") == {"land", "rover", "serie", "5"}


def test_public_urls_quote_sku():
    urls = public_urls([Path("1.jpg")], "https://example.test", "A SKU/1")
    assert urls == ["https://example.test/A%20SKU%2F1/1.jpg"]
