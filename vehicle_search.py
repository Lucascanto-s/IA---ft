import re
import unicodedata

import requests


COMMONS_API = "https://commons.wikimedia.org/w/api.php"


class VehicleSearchError(RuntimeError):
    pass


def _normalized_words(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return {word for word in re.findall(r"[a-z]{3,}", normalized)}


def _is_relevant_vehicle_page(title: str, query: str) -> bool:
    title_words = _normalized_words(title)
    blocked_words = {"train", "locomotive", "railway", "locomotiva", "trem", "steam"}
    if title_words & blocked_words:
        return False
    query_words = _normalized_words(query)
    generic_words = {"suporte", "coxim", "cambio", "peca", "carro", "vehicle", "automovel"}
    relevant_words = query_words - generic_words
    return bool(relevant_words & title_words)


def find_vehicle_image(query: str) -> str:
    """Find a Wikimedia Commons vehicle image URL with a white background."""
    queries = [query]
    without_years = re.sub(r"\b20\d{2}\b", "", query).strip()
    without_specs = re.sub(r"\b(?:20\d{2}|\d+(?:[.,]\d+)?)\b", "", query).strip()
    for candidate in (without_years, without_specs):
        if candidate and candidate not in queries:
            queries.append(candidate)
    words = without_specs.split()
    for size in (2, 3, 4):
        candidate = " ".join(words[-size:]).strip()
        if candidate and candidate not in queries:
            queries.append(candidate)

    for candidate in queries:
        response = requests.get(
            COMMONS_API,
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"{candidate} car white background",
                "gsrnamespace": 6,
                "gsrlimit": 10,
                "prop": "imageinfo",
                "iiprop": "url|mime|thumburl",
                "iiurlwidth": 1200,
            },
            headers={"User-Agent": "autopecas-image-generator/1.0"},
            timeout=30,
        )
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {})
        for page in pages.values():
            if not _is_relevant_vehicle_page(page.get("title", ""), candidate):
                continue
            image_info = page.get("imageinfo", [{}])[0]
            mime = image_info.get("mime", "")
            url = image_info.get("thumburl") or image_info.get("url", "")
            if mime.startswith("image/") and url:
                return url
        if candidate != query:
            response = requests.get(
                COMMONS_API,
                params={
                    "action": "query",
                    "format": "json",
                    "generator": "search",
                    "gsrsearch": f"{candidate} vehicle",
                    "gsrnamespace": 6,
                    "gsrlimit": 10,
                    "prop": "imageinfo",
                    "iiprop": "url|mime|thumburl",
                    "iiurlwidth": 1200,
                },
                headers={"User-Agent": "autopecas-image-generator/1.0"},
                timeout=30,
            )
            response.raise_for_status()
            pages = response.json().get("query", {}).get("pages", {})
            for page in pages.values():
                if not _is_relevant_vehicle_page(page.get("title", ""), candidate):
                    continue
                image_info = page.get("imageinfo", [{}])[0]
                mime = image_info.get("mime", "")
                url = image_info.get("thumburl") or image_info.get("url", "")
                if mime.startswith("image/") and url:
                    return url
    raise VehicleSearchError(f"Nenhuma imagem licenciada encontrada para: {query}")