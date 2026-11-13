import io
import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps


PROJECT_DIR = Path(__file__).resolve().parent
CANVAS_SIZE = (1200, 1200)
WHITE = (255, 255, 255)
RULER_SIZE = 42
EDITOR_PREVIEW_SIZE = 640
_REMBG_SESSION = None
MAX_IMAGE_BYTES = 15 * 1024 * 1024


@dataclass
class SetResult:
    paths: list[Path]
    cover_layers: list[dict] | None
    source_images: list[Image.Image]
    warnings: list[str]

    def __iter__(self):
        return iter(self.paths)

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, index):
        return self.paths[index]


def _rembg_session():
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        from rembg import new_session

        # isnet-general-use gives much cleaner edges than u2netp (downloads ~170 MB on first use).
        _REMBG_SESSION = new_session(os.getenv("REMBG_MODEL", "isnet-general-use"))
    return _REMBG_SESSION


def _keep_main_subject(image: Image.Image, min_alpha: int = 24) -> Image.Image:
    """Drop faint halos and every detached blob except the largest (e.g. a second car)."""
    from scipy import ndimage

    rgba = np.array(image.convert("RGBA"))
    alpha = rgba[:, :, 3]
    alpha[alpha < min_alpha] = 0
    labels, count = ndimage.label(alpha > 127)
    if count > 1:
        sizes = ndimage.sum(np.ones_like(labels), labels, index=range(1, count + 1))
        main = int(np.argmax(sizes)) + 1
        # Grow the main blob slightly so its soft anti-aliased edge is kept.
        keep = ndimage.binary_dilation(labels == main, iterations=3)
        alpha[~keep] = 0
    rgba[:, :, 3] = alpha
    return Image.fromarray(rgba, "RGBA")


def whiten_background(
    image: Image.Image, min_value: int = 205, max_spread: int = 22, transparent: bool = False
) -> Image.Image:
    """Turn the near-white, gray-ish background touching the borders into pure white.

    With transparent=True the background becomes transparent instead, so drop shadows
    follow the subject's outline rather than its bounding box.
    """
    from scipy import ndimage

    rgba = np.array(image.convert("RGBA"))
    rgb = rgba[:, :, :3].astype(np.int16)
    near_white = (rgb.min(axis=2) >= min_value) & (np.ptp(rgb, axis=2) <= max_spread)
    labels, _ = ndimage.label(near_white)
    border_labels = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    background = np.isin(labels, border_labels[border_labels > 0])
    # Soften the transition so the part's edges are not jagged.
    soft = ndimage.gaussian_filter(background.astype(np.float32), 1.0)[:, :, None]
    rgba[:, :, :3] = (rgba[:, :, :3] * (1 - soft) + 255 * soft).astype(np.uint8)
    if transparent:
        rgba[:, :, 3] = np.minimum(rgba[:, :, 3], ((1 - soft[:, :, 0]) * 255).astype(np.uint8))
    return Image.fromarray(rgba, "RGBA")


def _download_path(source: str) -> Path:
    cache_dir = Path(os.getenv("DOWNLOAD_CACHE_DIR", PROJECT_DIR / "arquivos" / "downloads"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{hashlib.sha256(source.encode()).hexdigest()}.img"


def load_image(source: str) -> Image.Image:
    if source.startswith(("http://", "https://")):
        cached_path = _download_path(source)
        for attempt in range(2):
            try:
                if not cached_path.exists():
                    partial_path = cached_path.with_suffix(".part")
                    partial_path.unlink(missing_ok=True)
                    with requests.get(
                        source,
                        headers={"User-Agent": "autopecas-image-generator/1.0"},
                        timeout=(5, 20),
                        stream=True,
                    ) as response:
                        response.raise_for_status()
                        content_length = int(response.headers.get("Content-Length", 0) or 0)
                        if content_length > MAX_IMAGE_BYTES:
                            raise ValueError("A imagem baixada excede o limite de 15 MB")
                        downloaded = 0
                        with partial_path.open("wb") as file:
                            for chunk in response.iter_content(chunk_size=64 * 1024):
                                downloaded += len(chunk)
                                if downloaded > MAX_IMAGE_BYTES:
                                    raise ValueError("A imagem baixada excede o limite de 15 MB")
                                file.write(chunk)
                    os.replace(partial_path, cached_path)
                source = str(cached_path)
                with Image.open(source) as image:
                    image.verify()
                return Image.open(source).convert("RGBA")
            except (OSError, ValueError, requests.RequestException):
                cached_path.unlink(missing_ok=True)
                cached_path.with_suffix(".part").unlink(missing_ok=True)
                if attempt == 1:
                    raise ValueError("Não foi possível baixar uma imagem válida após duas tentativas")
    local_source = Path(source).expanduser()
    if not local_source.is_absolute():
        project_source = PROJECT_DIR / local_source
        if project_source.exists():
            local_source = project_source
    try:
        with Image.open(local_source) as image:
            image.verify()
        return Image.open(local_source).convert("RGBA")
    except (OSError, ValueError) as error:
        raise ValueError("O arquivo informado não é uma imagem válida") from error


def has_white_background(image: Image.Image) -> bool:
    rgb = image.convert("RGB")
    width, height = rgb.size
    sample_size = max(8, min(width, height) // 20)
    corners = (
        (0, 0, sample_size, sample_size),
        (width - sample_size, 0, width, sample_size),
        (0, height - sample_size, sample_size, height),
        (width - sample_size, height - sample_size, width, height),
    )
    pixels = []
    for box in corners:
        pixels.extend(rgb.crop(box).getdata())
    return sum(min(pixel) >= 245 for pixel in pixels) / len(pixels) >= 0.9


def trim_white_borders(image: Image.Image) -> Image.Image:
    if image.getchannel("A").getextrema()[0] < 255:
        bbox = image.getchannel("A").getbbox()
    else:
        rgb = image.convert("RGB")
        white = Image.new("RGB", rgb.size, WHITE)
        difference = ImageChops.difference(rgb, white).convert("L")
        mask = difference.point(lambda value: 255 if value > 12 else 0)
        bbox = mask.getbbox()
    return image.crop(bbox) if bbox else image


def remove_red_guides(image: Image.Image) -> Image.Image:
    pixels = np.array(image.convert("RGB"))
    mask = (pixels[:, :, 0] > 150) & (pixels[:, :, 1] < 90) & (pixels[:, :, 2] < 90)
    pixels[mask] = WHITE
    return Image.fromarray(pixels).convert("RGBA")


def content_bands(image: Image.Image) -> list[tuple[int, int]]:
    pixels = np.array(image.convert("RGB"))
    width, height = image.size
    rows = (pixels.min(axis=2) < 245).sum(axis=1) > max(3, width // 200)
    bands = []
    start = None
    for index, has_content in enumerate(np.append(rows, False)):
        if has_content and start is None:
            start = index
        elif not has_content and start is not None:
            bands.append((start, index))
            start = None
    minimum_band_height = max(20, height // 100)
    return [band for band in bands if band[1] - band[0] >= minimum_band_height]


def extract_vehicle_from_cover(image: Image.Image, allow_multiple: bool = False) -> Image.Image | None:
    """Crop the vehicle strip from an edited cover (vehicle(s) on top, part below, white background).

    Returns None when the image does not look like such a cover. With allow_multiple, a strip
    holding several cars side by side is returned whole.
    """
    found = _cover_vehicle_strip(image, allow_multiple)
    return found[0] if found else None


def extract_part_from_cover(image: Image.Image) -> Image.Image | None:
    """Part below the car(s) when a product photo is actually an old cover (car on top, part below).

    Returns the part cropped from the original image, or None when the photo is not such a cover.
    """
    found = _cover_vehicle_strip(image, allow_multiple=True)
    if not found:
        return None
    _, strip_bottom = found
    rgba = image.convert("RGBA")
    cut = min(rgba.height - 1, round(strip_bottom * rgba.height / CANVAS_SIZE[1]))
    below = rgba.crop((0, cut, rgba.width, rgba.height))
    bbox = content_bbox(below.convert("RGB"), threshold=12)
    # Something substantial must be left below the car, otherwise it is not a car + part cover.
    if not bbox or (bbox[2] - bbox[0]) < rgba.width * 0.08 or (bbox[3] - bbox[1]) < rgba.height * 0.08:
        return None
    return below.crop(bbox)


def _cover_vehicle_strip(image: Image.Image, allow_multiple: bool) -> tuple[Image.Image, int] | None:
    """(vehicle strip, bottom row of the strip on the 1200 canvas) of a car-on-top cover, or None."""
    rgb = image.convert("RGB")
    if rgb.size != CANVAS_SIZE:
        rgb = rgb.resize(CANVAS_SIZE)
    pixels = np.array(rgb)
    if not has_white_background(rgb):
        return None
    width, height = rgb.size
    # Ignore thin decorative strips glued to the top/bottom edges.
    bands = [band for band in content_bands(rgb) if not (band[1] - band[0] < 40 and (band[0] < 5 or band[1] > height - 5))]
    if not bands or bands[0][0] > height * 0.3:
        return None
    top, bottom = bands[0]
    if bottom > height * 0.5:
        # Vehicle and part touch: cut at the emptiest row between them.
        counts = (pixels.min(axis=2) < 245).sum(axis=1)
        low, high = int(height * 0.25), int(height * 0.45)
        bottom = low + int(np.argmin(counts[low:high]))
        if counts[bottom] > width * 0.04 or bottom - top < 90:
            return None
    elif len(bands) < 2:
        return None  # a single strip on top is a plain photo, not a cover with a part below
    strip = rgb.crop((0, max(0, top - 4), width, min(height, bottom + 4)))
    bbox = content_bbox(strip, threshold=12)
    if not bbox:
        return None
    vehicle = strip.crop(bbox)
    ratio = vehicle.width / vehicle.height
    # One car is roughly 1.4-2.5x wider than tall; wider strips usually hold several cars.
    max_ratio = 8.0 if allow_multiple else 2.7
    if vehicle.width < 250 or vehicle.height < 90 or not 1.3 <= ratio <= max_ratio:
        return None
    return vehicle.convert("RGBA"), min(height, bottom + 4)


def create_composite_cover(image: Image.Image) -> Image.Image:
    canvas = Image.new("RGBA", CANVAS_SIZE, WHITE + (255,))
    composite = ImageOps.contain(remove_red_guides(image), CANVAS_SIZE)
    canvas.alpha_composite(composite, ((1200 - composite.width) // 2, (1200 - composite.height) // 2))
    return canvas.convert("RGB")


def remove_background(image: Image.Image) -> Image.Image:
    from rembg import remove

    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    result = remove(
        encoded.getvalue(),
        session=_rembg_session(),
        force_return_bytes=True,
    )
    return _keep_main_subject(Image.open(io.BytesIO(result)).convert("RGBA"))


def fit_on_white(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    image = trim_white_borders(image)
    target = Image.new("RGBA", CANVAS_SIZE, WHITE + (255,))
    fitted = ImageOps.contain(image, (box[2] - box[0], box[3] - box[1]))
    x = box[0] + (box[2] - box[0] - fitted.width) // 2
    y = box[1] + (box[3] - box[1] - fitted.height) // 2
    target.alpha_composite(fitted, (x, y))
    return target.convert("RGB")


_CONTENT_LAYER_CACHE: dict[int, tuple[Image.Image, Image.Image]] = {}


def _content_layer(image: Image.Image) -> Image.Image:
    cached = _CONTENT_LAYER_CACHE.get(id(image))
    if cached and cached[0] is image:
        return cached[1]
    bbox = content_bbox(image)
    content = image.crop(bbox) if bbox else image
    if len(_CONTENT_LAYER_CACHE) >= 32:
        _CONTENT_LAYER_CACHE.pop(next(iter(_CONTENT_LAYER_CACHE)))
    _CONTENT_LAYER_CACHE[id(image)] = (image, content)
    return content


def product_editor_layer(image: Image.Image) -> dict:
    content = _content_layer(image)
    return {
        "id": "product",
        "image": content,
        "x": (CANVAS_SIZE[0] - content.width) // 2,
        "y": (CANVAS_SIZE[1] - content.height) // 2,
        "width": content.width,
        "height": content.height,
        "z_index": 0,
        "shadow": False,
        "locked": False,
    }


def reposition_image(
    image: Image.Image,
    x_offset: int = 0,
    y_offset: int = 0,
    scale: int = 100,
    *,
    resampling: Image.Resampling = Image.Resampling.LANCZOS,
) -> Image.Image:
    """Recompose a generated image on the white canvas using manual adjustments."""
    content = _content_layer(image)
    width = max(1, round(content.width * scale / 100))
    height = max(1, round(content.height * scale / 100))
    content = content.resize((width, height), resampling)
    x = (CANVAS_SIZE[0] - content.width) // 2 + x_offset
    y = (CANVAS_SIZE[1] - content.height) // 2 + y_offset
    return render_layers(
        [{"id": "product", "image": content, "x": x, "y": y, "width": width, "height": height, "z_index": 0}],
        CANVAS_SIZE,
        resampling=resampling,
    )


def editor_preview(
    image: Image.Image,
    ruler_min: int = -400,
    ruler_max: int = 400,
) -> Image.Image:
    """Add screen-only rulers using the same scale as the position controls."""
    image = ImageOps.contain(image.convert("RGB"), (EDITOR_PREVIEW_SIZE, EDITOR_PREVIEW_SIZE))
    preview = Image.new(
        "RGB",
        (EDITOR_PREVIEW_SIZE + RULER_SIZE, EDITOR_PREVIEW_SIZE + RULER_SIZE),
        (38, 38, 38),
    )
    image_left = RULER_SIZE + (EDITOR_PREVIEW_SIZE - image.width) // 2
    image_top = RULER_SIZE + (EDITOR_PREVIEW_SIZE - image.height) // 2
    preview.paste(image, (image_left, image_top))
    draw = ImageDraw.Draw(preview)
    ruler_color = (190, 190, 190)
    text_color = (245, 245, 245)
    ruler_range = ruler_max - ruler_min
    for value in range(ruler_min, ruler_max + 1, 100):
        x = RULER_SIZE + round((value - ruler_min) * EDITOR_PREVIEW_SIZE / ruler_range)
        tick = 18 if value % 500 == 0 else 11
        draw.line((x, RULER_SIZE - tick, x, RULER_SIZE), fill=ruler_color, width=1)
        label_x = max(RULER_SIZE + 2, min(x - 12, preview.width - 30))
        draw.text((label_x, 7), str(value), fill=text_color)
    for value in range(ruler_min, ruler_max + 1, 100):
        y = RULER_SIZE + round((value - ruler_min) * EDITOR_PREVIEW_SIZE / ruler_range)
        tick = 18 if value % 500 == 0 else 11
        draw.line((RULER_SIZE - tick, y, RULER_SIZE, y), fill=ruler_color, width=1)
        draw.text((6, max(RULER_SIZE + 2, y - 6)), str(value), fill=text_color)
    center = RULER_SIZE + EDITOR_PREVIEW_SIZE // 2
    draw.line((center, RULER_SIZE, center, preview.height), fill=(120, 120, 120), width=1)
    draw.line((RULER_SIZE, center, preview.width, center), fill=(120, 120, 120), width=1)
    return preview


def render_layers(
    layers: Iterable[dict],
    canvas_size: tuple[int, int] = CANVAS_SIZE,
    *,
    resampling: Image.Resampling = Image.Resampling.LANCZOS,
) -> Image.Image:
    """Render absolute pixel layers onto the final white canvas."""
    canvas = Image.new("RGBA", canvas_size, WHITE + (255,))
    for layer in sorted(layers, key=lambda item: item.get("z_index", 0)):
        image = layer["image"].convert("RGBA")
        width = max(1, int(round(layer["width"])))
        height = max(1, int(round(layer["height"])))
        fitted = image.resize((width, height), resampling)
        x = int(round(layer["x"]))
        y = int(round(layer["y"]))
        if layer.get("shadow"):
            alpha = fitted.getchannel("A")
            if alpha.getextrema()[0] == 255:
                # Opaque image on white: cast the shadow from the subject, not from the box.
                difference = ImageChops.difference(fitted.convert("RGB"), Image.new("RGB", fitted.size, WHITE))
                alpha = difference.convert("L").point(lambda value: 255 if value > 18 else 0)
            shadow_alpha = alpha.filter(ImageFilter.GaussianBlur(14))
            shadow_alpha = shadow_alpha.point(lambda value: int(value * 0.22))
            shadow = Image.new("RGBA", fitted.size, (40, 40, 40, 0))
            shadow.putalpha(shadow_alpha)
            canvas.alpha_composite(shadow, (x, y + 16))
        canvas.alpha_composite(fitted, (x, y))
    return canvas.convert("RGB")


def contain_size(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """Same size ImageOps.contain would produce, without resizing anything."""
    width, height = size
    if not width or not height:
        return 0, 0
    if width / height > box[0] / box[1]:
        return box[0], round(height / width * box[0])
    if width / height < box[0] / box[1]:
        return round(width / height * box[1]), box[1]
    return box


def cover_base_sizes(
    vehicle_size: tuple[int, int], product_size: tuple[int, int], vehicle_scale: int = 100
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Vehicle size at zoom 100 and product size at zoom 100 (it depends on the vehicle zoom),
    matching cover_editor_layers."""
    vehicle_base = contain_size(vehicle_size, (1060, 390))
    vehicle_width = round(vehicle_base[0] * vehicle_scale / 100)
    product_width = min(1100, max(900, int(vehicle_width * 1.03)))
    return vehicle_base, contain_size(product_size, (product_width, 680))


def cover_editor_layers(
    vehicle: Image.Image,
    product: Image.Image,
    vehicle_x_offset: int = 0,
    vehicle_y_offset: int = 0,
    vehicle_scale: int = 100,
    product_x_offset: int = 0,
    product_y_offset: int = 0,
    product_scale: int = 100,
    *,
    resampling: Image.Resampling = Image.Resampling.LANCZOS,
) -> list[dict]:
    vehicle_fitted = ImageOps.contain(vehicle, (1060, 390))
    vehicle_fitted = vehicle_fitted.resize(
        (round(vehicle_fitted.width * vehicle_scale / 100), round(vehicle_fitted.height * vehicle_scale / 100)),
        resampling,
    )
    product_width = min(1100, max(900, int(vehicle_fitted.width * 1.03)))
    product_fitted = ImageOps.contain(product, (product_width, 680))
    product_fitted = product_fitted.resize(
        (round(product_fitted.width * product_scale / 100), round(product_fitted.height * product_scale / 100)),
        resampling,
    )
    return [
        {
            "id": "vehicle",
            "image": vehicle_fitted,
            "x": (1200 - vehicle_fitted.width) // 2 + vehicle_x_offset,
            "y": 25 + vehicle_y_offset,
            "width": vehicle_fitted.width,
            "height": vehicle_fitted.height,
            "z_index": 0,
            "shadow": True,
            "locked": False,
        },
        {
            "id": "product",
            "image": product_fitted,
            "x": (1200 - product_fitted.width) // 2 + product_x_offset,
            "y": 500 + product_y_offset,
            "width": product_fitted.width,
            "height": product_fitted.height,
            "z_index": 1,
            "shadow": False,
            "locked": False,
        },
    ]


def sharpen(image: Image.Image) -> Image.Image:
    return image.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3))


def content_bbox(image: Image.Image, threshold: int = 5) -> tuple[int, int, int, int] | None:
    rgb = image.convert("RGB")
    difference = ImageChops.difference(rgb, Image.new("RGB", rgb.size, WHITE)).convert("L")
    return difference.point(lambda value: 255 if value > threshold else 0).getbbox()


def validate_output(path: Path | Image.Image, tolerance: int = 24) -> dict[str, int | bool]:
    image = path.convert("RGB") if isinstance(path, Image.Image) else Image.open(path).convert("RGB")
    bbox = content_bbox(image)
    if not bbox:
        return {"valid": False, "center_offset_x": 0, "center_offset_y": 0, "clipped": True}
    left, top, right, bottom = bbox
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    target_x = image.width / 2
    target_y = image.height / 2
    clipped = left <= 1 or top <= 1 or right >= image.width - 1 or bottom >= image.height - 1
    return {
        "valid": image.size == CANVAS_SIZE and not clipped
        and abs(center_x - target_x) <= tolerance
        and abs(center_y - target_y) <= tolerance,
        "center_offset_x": round(center_x - target_x),
        "center_offset_y": round(center_y - target_y),
        "clipped": clipped,
    }


PRODUCT_MIN_SIDE = 1400
DEFAULT_CONTRAST = 1.08
DEFAULT_SHARPNESS = 1.0


def enhance_product(
    image: Image.Image, contrast: float = DEFAULT_CONTRAST, sharpness: float = DEFAULT_SHARPNESS
) -> Image.Image:
    """Make the part photo crisper without touching the white background.

    Upscales small photos before they are fitted on the 1200px canvas (so the final resize
    shrinks instead of blurring), adds a little contrast/colour and sharpens edges and texture.
    contrast: 1.0 = unchanged. sharpness: 0 = no extra sharpening, 1 = default, 2 = double.
    """
    image = image.convert("RGBA")
    longest = max(image.size)
    if longest < PRODUCT_MIN_SIDE:
        factor = PRODUCT_MIN_SIDE / longest
        image = image.resize((round(image.width * factor), round(image.height * factor)),
                             Image.Resampling.LANCZOS)
    alpha = image.getchannel("A")
    rgb = image.convert("RGB")
    background = np.array(rgb).min(axis=2) >= 250

    if sharpness > 0:
        # Smooth JPEG blocks/noise first so sharpening does not turn them into speckles.
        rgb = rgb.filter(ImageFilter.MedianFilter(3)).filter(ImageFilter.GaussianBlur(0.5))
    if contrast != 1:
        rgb = ImageEnhance.Contrast(rgb).enhance(contrast)
    rgb = ImageEnhance.Color(rgb).enhance(1.05)
    if sharpness > 0:
        rgb = rgb.filter(ImageFilter.UnsharpMask(radius=2.5, percent=round(110 * sharpness), threshold=4))
        rgb = rgb.filter(ImageFilter.UnsharpMask(radius=1.0, percent=round(50 * sharpness), threshold=6))

    pixels = np.array(rgb)
    pixels[background] = WHITE  # sem halo cinza no fundo branco
    result = Image.fromarray(pixels).convert("RGBA")
    result.putalpha(alpha)
    return result


@lru_cache(maxsize=16)
def _clean_product(source: str) -> Image.Image:
    """Part cut out on white/transparent, cached so the editor does not rerun background removal."""
    image = load_image(source)
    if image.getchannel("A").getextrema()[0] < 255:
        return trim_white_borders(image)
    if has_white_background(image):
        # Some listings reuse an old cover (car on top, part below) as a part photo: keep only the part.
        part = extract_part_from_cover(image)
        if part is not None:
            cleaned = trim_white_borders(whiten_background(part))
            cleaned.info["from_cover"] = True
            return cleaned
        return trim_white_borders(whiten_background(image))
    cutout = remove_background(image)
    if cutout_is_broken(cutout):
        # Close-ups (labels, plates) fill the frame: the cutout eats parts of them. Keep the photo whole.
        return image
    return trim_white_borders(cutout)


def cutout_is_broken(cutout: Image.Image, max_holes: float = 0.08, max_fragments: int = 120) -> bool:
    """True when background removal ate pieces of the subject (e.g. a label photographed up close).

    A few large holes are real openings of the part (a window regulator, a bracket) and are fine.
    A broken cutout instead has the subject eaten into many small scattered holes ("swiss cheese").
    """
    from scipy.ndimage import binary_fill_holes, label

    solid = np.array(cutout.getchannel("A")) > 128
    if solid.sum() < solid.size * 0.02:
        return True  # almost nothing left: the subject was removed
    filled = binary_fill_holes(solid)
    holes = filled & ~solid
    if holes.sum() / filled.sum() <= max_holes:
        return False  # solid shape, good cutout
    # Significant holes: real openings are few and large; an eaten subject leaves many small ones.
    _, fragments = label(holes)
    return bool(fragments > max_fragments)


def product_image(source: str, enhance: bool = True) -> Image.Image:
    product = _clean_product(source).copy()
    return enhance_product(product) if enhance else product


def prepare_vehicle(vehicle_source: str) -> Image.Image:
    vehicle = load_image(vehicle_source)
    return prepare_vehicle_image(vehicle)


def prepare_vehicle_image(vehicle: Image.Image) -> Image.Image:
    vehicle = vehicle.convert("RGBA")
    if vehicle.getchannel("A").getextrema()[0] == 255:
        if has_white_background(vehicle):
            vehicle = whiten_background(vehicle, transparent=True)
        else:
            vehicle = remove_background(vehicle)
    return trim_white_borders(vehicle)


def is_composite_cover(image: Image.Image) -> bool:
    """Photo 1 already is an old cover (cars on top + part below)."""
    return len(content_bands(image)) >= 2


def cover_product_index(first_source: Image.Image, total: int) -> int:
    """Which product photo goes on the cover: photo 1, unless it is an old composite cover."""
    return 1 if is_composite_cover(first_source) and total > 1 else 0


def compose_vehicle_group(vehicles: Iterable[Image.Image]) -> Image.Image:
    """Place approved vehicle images in a single horizontal group on white."""
    images = [prepare_vehicle_image(image) for image in vehicles]
    if not images:
        raise ValueError("A capa precisa de pelo menos um veículo aprovado")
    if len(images) == 1:
        return images[0]

    canvas_width, _ = CANVAS_SIZE
    top_height = 420
    gap = 6
    slot_width = (canvas_width - gap * (len(images) - 1)) // len(images)
    # Transparent canvas: an opaque white one would cast a rectangular shadow on the cover.
    canvas = Image.new("RGBA", (canvas_width, top_height), WHITE + (0,))
    for index, image in enumerate(images):
        fitted = ImageOps.contain(image, (max(1, slot_width), top_height - 24))
        x = index * (slot_width + gap) + (slot_width - fitted.width) // 2
        y = (top_height - fitted.height) // 2
        canvas.alpha_composite(fitted.convert("RGBA"), (x, y))
    # Crop the empty sides so the cars fill the whole vehicle area of the cover (bigger cars).
    bbox = canvas.getchannel("A").getbbox()
    return canvas.crop(bbox) if bbox else canvas


def compose_cover(
    vehicle: Image.Image,
    product: Image.Image,
    vehicle_x_offset: int = 0,
    vehicle_y_offset: int = 0,
    vehicle_scale: int = 100,
    product_x_offset: int = 0,
    product_y_offset: int = 0,
    product_scale: int = 100,
    *,
    resampling: Image.Resampling = Image.Resampling.LANCZOS,
) -> Image.Image:
    layers = cover_editor_layers(
        vehicle,
        product,
        vehicle_x_offset,
        vehicle_y_offset,
        vehicle_scale,
        product_x_offset,
        product_y_offset,
        product_scale,
        resampling=resampling,
    )
    return render_layers(
        layers,
        CANVAS_SIZE,
        resampling=resampling,
    )


def create_cover(vehicle_source: str | Image.Image, product: Image.Image) -> Image.Image:
    vehicle = (
        prepare_vehicle(vehicle_source)
        if isinstance(vehicle_source, str)
        else prepare_vehicle_image(vehicle_source)
    )
    return compose_cover(vehicle, product)


def create_set(
    product_sources: Iterable[str], vehicle_source: str | Image.Image, output_dir: str
) -> SetResult:
    sources = list(product_sources)[:4]
    if not sources:
        raise ValueError("O produto nao possui imagens")
    warnings = []
    if len(sources) < 4:
        warnings.append(
            f"O produto possui {len(sources)} foto(s); a última foi repetida para completar quatro posições."
        )
    product_images = [product_image(source) for source in sources]
    while len(product_images) < 4:
        product_images.append(product_images[-1].copy())
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths = [output / "1.jpg", output / "2.jpg", output / "3.jpg", output / "4.jpg"]
    first_source = load_image(sources[0])
    if is_composite_cover(first_source):
        cover = create_composite_cover(first_source)
    else:
        cover = create_cover(vehicle_source, product_images[cover_product_index(first_source, len(sources))])
    sharpen(cover).save(paths[0], quality=98, subsampling=0)
    for path, image in zip(paths[1:], product_images[1:4]):
        sharpen(fit_on_white(image, (10, 10, 1190, 1190))).save(path, quality=98, subsampling=0)
    return SetResult(paths, None, product_images, warnings)


def public_urls(paths: Iterable[Path], base_url: str, sku: str) -> list[str]:
    base = base_url.rstrip("/")
    return [f"{base}/{quote(sku, safe='')}/{quote(path.name, safe='')}" for path in paths]
