import io
import re
import unicodedata
from html.parser import HTMLParser

import requests
from PIL import Image


COMMONS_API = "https://commons.wikimedia.org/w/api.php"
GOOGLE_IMAGES_URL = "https://www.google.com/search"
SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    )
}


class VehicleSearchError(RuntimeError):
    pass


class _GoogleImageParser(HTMLParser):
    """Collect result thumbnails and their labels from Google Images HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        values = dict(attrs)
        title = " ".join((values.get("alt") or "", values.get("title") or ""))
        for key in ("src", "data-src"):
            url = values.get(key) or ""
            if "encrypted-tbn" in url and "gstatic.com/images" in url:
                self.results.append((url, title))


def _normalized_words(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", normalized))


def _requested_years(query: str) -> set[int]:
    years = sorted({int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", query)})
    if len(years) >= 2 and years[-1] - years[0] <= 12:
        return set(range(years[0], years[-1] + 1))
    return set(years)


def _matches_vehicle_label(label: str, query: str, years: set[int]) -> bool:
    label_words = _normalized_words(label)
    query_words = _normalized_words(query) - {str(year) for year in years}
    generic = {"car", "cars", "vehicle", "white", "background", "isolated", "photo", "image"}
    required = query_words - generic
    if not required or not required.issubset(label_words):
        return False
    if years and not (years & {int(word) for word in label_words if word.isdigit()}):
        return False
    return True


def _has_white_corners(image_bytes: bytes) -> bool:
    if not image_bytes or len(image_bytes) > 8 * 1024 * 1024:
        return False
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            if "A" in source.getbands() or "transparency" in source.info:
                rgba = source.convert("RGBA")
                white_canvas = Image.new("RGBA", rgba.size, "white")
                white_canvas.alpha_composite(rgba)
                image = white_canvas.convert("RGB")
            else:
                image = source.convert("RGB")
            width, height = image.size
            if width < 100 or height < 80:
                return False
            sample = max(4, min(width, height) // 30)
            corners = (
                (0, 0, sample, sample),
                (width - sample, 0, width, sample),
                (0, height - sample, sample, height),
                (width - sample, height - sample, width, height),
            )
            pixels = [pixel for box in corners for pixel in image.crop(box).getdata()]
            return sum(min(pixel) >= 242 for pixel in pixels) / len(pixels) >= 0.92
    except (OSError, ValueError):
        return False


def _download_has_white_background(url: str) -> bool:
    try:
        response = requests.get(url, headers=SEARCH_HEADERS, timeout=8)
        response.raise_for_status()
        return _has_white_corners(response.content)
    except requests.RequestException:
        return False


def _google_candidates(query: str, years: set[int]) -> list[tuple[str, str]]:
    query_years = sorted(years) if years else [None]
    candidates: list[tuple[str, str]] = []
    seen = set()
    for year in query_years:
        search = f"{query} {year}" if year else query
        search = f"{search} car isolated white background"
        try:
            response = requests.get(
                GOOGLE_IMAGES_URL,
                params={"tbm": "isch", "q": search},
                headers=SEARCH_HEADERS,
                timeout=10,
            )
        except requests.RequestException:
            continue
        if not response.ok:
            continue
        parser = _GoogleImageParser()
        parser.feed(response.text)
        for url, label in parser.results:
            if url not in seen:
                candidates.append((url, label))
                seen.add(url)
    return candidates


def _commons_candidates(query: str, years: set[int]) -> list[tuple[str, str]]:
    search_query = f"{query} {' '.join(map(str, sorted(years)))} car white background".strip()
    try:
        response = requests.get(
            COMMONS_API,
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": search_query,
                "gsrnamespace": 6,
                "gsrlimit": 30,
                "prop": "imageinfo",
                "iiprop": "url|mime|thumburl",
                "iiurlwidth": 1200,
            },
            headers={"User-Agent": "autopecas-image-generator/1.0"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException:
        return []
    pages = response.json().get("query", {}).get("pages", {}).values()
    candidates = []
    for page in pages:
        image_info = page.get("imageinfo", [{}])[0]
        url = image_info.get("thumburl") or image_info.get("url", "")
        label = page.get("title", "")
        if image_info.get("mime", "").startswith("image/") and url:
            candidates.append((url, label))
    return candidates


def find_vehicle_image(query: str) -> str:
    """Return only a year-matching candidate whose downloaded image has a white background."""
    years = _requested_years(query)
    query_without_years = re.sub(r"\b(?:19|20)\d{2}\b", " ", query)
    query_without_years = re.sub(r"\s+", " ", query_without_years).strip()
    candidates = _google_candidates(query_without_years, years)
    for url, label in candidates:
        if _matches_vehicle_label(label, query_without_years, years) and _download_has_white_background(url):
            return url

    for url, label in _commons_candidates(query_without_years, years):
        if _matches_vehicle_label(label, query_without_years, years) and _download_has_white_background(url):
            return url

    raise VehicleSearchError(
        f"Não encontrei imagem validada em fundo branco para {query}. "
        "Envie uma imagem manualmente para continuar."
    )
