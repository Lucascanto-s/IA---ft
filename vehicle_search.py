import io
import re
import unicodedata

import requests
from PIL import Image


COMMONS_API = "https://commons.wikimedia.org/w/api.php"
SEARCH_HEADERS = {"User-Agent": "autopecas-image-generator/1.0"}
MAX_BACKGROUND_CHECKS = 6
# Photos that do not show the stock car from outside (race cars, interiors, engines, wrecks).
EXCLUDED_LABEL_WORDS = {
    "nascar", "wrc", "rally", "rallye", "race", "racing", "racecar", "dtm", "stock", "drift",
    "rear", "interior", "dashboard", "cockpit", "engine", "motor", "trunk", "wheel", "badge", "logo",
    "emblem", "crash", "wreck", "damaged", "police", "taxi", "model", "toy", "diecast", "concept",
}


class VehicleSearchError(RuntimeError):
    pass


def _normalized_words(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", normalized))


def _requested_years(query: str) -> set[int]:
    years = sorted({int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", query)})
    if len(years) >= 2 and years[-1] - years[0] <= 15:
        return set(range(years[0], years[-1] + 1))
    return set(years)


def _matches_vehicle_label(label: str, query: str, years: set[int]) -> bool:
    """The label must name the make and model; if it names a year, it must be in range."""
    label_words = _normalized_words(label)
    query_words = _normalized_words(query) - {str(year) for year in years}
    generic = {"car", "cars", "vehicle", "white", "background", "isolated", "photo", "image"}
    required = query_words - generic
    if not required or not required.issubset(label_words):
        return False
    if label_words & EXCLUDED_LABEL_WORDS:
        return False
    label_years = {int(word) for word in label_words if re.fullmatch(r"(?:19|20)\d{2}", word)}
    return not (years and label_years and not years & label_years)


def _label_has_year(label: str, years: set[int]) -> bool:
    return bool(years & {int(word) for word in _normalized_words(label) if word.isdigit()})


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


def _commons_search(search_query: str) -> list[tuple[str, str]]:
    try:
        response = requests.get(
            COMMONS_API,
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": search_query,
                "gsrnamespace": 6,
                "gsrlimit": 20,
                "prop": "imageinfo",
                "iiprop": "url|mime",
                "iiurlwidth": 1200,
            },
            headers=SEARCH_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {}).values()
    except (requests.RequestException, ValueError):
        return []
    candidates = []
    for page in sorted(pages, key=lambda item: item.get("index", 0)):
        image_info = (page.get("imageinfo") or [{}])[0]
        url = image_info.get("thumburl") or image_info.get("url", "")
        if image_info.get("mime", "") in ("image/jpeg", "image/png", "image/webp") and url:
            candidates.append((url, page.get("title", "")))
    return candidates


def _commons_candidates(query: str, years: set[int]) -> list[tuple[str, str]]:
    """Search Wikimedia Commons one year at a time (multi-year queries return nothing)."""
    searches = [f"{query} {year}" for year in sorted(years)] + [query]
    candidates: list[tuple[str, str]] = []
    seen = set()
    for search in searches:
        for url, label in _commons_search(search):
            if url not in seen:
                candidates.append((url, label))
                seen.add(url)
    return candidates


def find_vehicle_image(query: str) -> str:
    """Return the best matching vehicle photo."""
    return find_vehicle_images(query, limit=1)[0]


def find_vehicle_images(query: str, limit: int = 10) -> list[str]:
    """Return matching vehicle photos, best first: year matches, then white backgrounds.

    The cover pipeline removes the background automatically, so photos without a white
    background are still acceptable; the user picks and approves one before it is used.
    """
    years = _requested_years(query)
    query_without_years = re.sub(r"\b(?:19|20)\d{2}\b", " ", query)
    # Drop range connectors left behind: "2007-2009", "2007 a 2009", "2007 até 2009".
    query_without_years = re.sub(r"(?:^|\s)(?:[-–/]|a|ate|até|to)(?=\s|$)", " ", query_without_years,
                                 flags=re.IGNORECASE)
    query_without_years = re.sub(r"\s+", " ", query_without_years).strip(" -–/")
    matches = [
        (url, label)
        for url, label in _commons_candidates(query_without_years, years)
        if _matches_vehicle_label(label, query_without_years, years)
    ]
    if years:
        with_year = [item for item in matches if _label_has_year(item[1], years)]
        matches = with_year or matches
    if not matches:
        raise VehicleSearchError(
            f"Não encontrei imagem de {query} na internet. "
            "Envie uma imagem manualmente para continuar."
        )
    urls = [url for url, _ in matches]
    white = [url for url in urls[:MAX_BACKGROUND_CHECKS] if _download_has_white_background(url)]
    return (white + [url for url in urls if url not in white])[:limit]
