import re
import unicodedata


def sanitize_sku(sku: str) -> str:
    normalized = unicodedata.normalize("NFKD", sku).encode("ascii", "ignore").decode()
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", normalized.strip()).strip("._-")
    if not safe:
        raise ValueError("SKU inválido para criar arquivos")
    return safe[:120]


def normalized_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", normalized))
