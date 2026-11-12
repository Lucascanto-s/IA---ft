import io
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps


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

        _REMBG_SESSION = new_session("u2netp")
    return _REMBG_SESSION


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
    return Image.open(io.BytesIO(result)).convert("RGBA")


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
            shadow_alpha = fitted.getchannel("A").filter(ImageFilter.GaussianBlur(14))
            shadow_alpha = shadow_alpha.point(lambda value: int(value * 0.22))
            shadow = Image.new("RGBA", fitted.size, (40, 40, 40, 0))
            shadow.putalpha(shadow_alpha)
            canvas.alpha_composite(shadow, (x, y + 16))
        canvas.alpha_composite(fitted, (x, y))
    return canvas.convert("RGB")


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


def product_image(source: str) -> Image.Image:
    image = load_image(source)
    if image.getchannel("A").getextrema()[0] < 255 or has_white_background(image):
        return trim_white_borders(image)
    return trim_white_borders(remove_background(image))


def prepare_vehicle(vehicle_source: str) -> Image.Image:
    vehicle = load_image(vehicle_source)
    if vehicle.getchannel("A").getextrema()[0] == 255 and not has_white_background(vehicle):
        vehicle = remove_background(vehicle)
    return trim_white_borders(vehicle)


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


def create_cover(vehicle_source: str, product: Image.Image) -> Image.Image:
    return compose_cover(prepare_vehicle(vehicle_source), product)


def create_set(product_sources: Iterable[str], vehicle_source: str, output_dir: str) -> SetResult:
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
    if len(content_bands(first_source)) >= 2:
        cover = create_composite_cover(first_source)
    else:
        cover_product = product_images[1] if len(sources) > 1 else product_images[0]
        cover = create_cover(vehicle_source, cover_product)
    sharpen(cover).save(paths[0], quality=98, subsampling=0)
    for path, image in zip(paths[1:], product_images[1:4]):
        sharpen(fit_on_white(image, (10, 10, 1190, 1190))).save(path, quality=98, subsampling=0)
    return SetResult(paths, None, product_images, warnings)


def public_urls(paths: Iterable[Path], base_url: str, sku: str) -> list[str]:
    base = base_url.rstrip("/")
    return [f"{base}/{quote(sku, safe='')}/{quote(path.name, safe='')}" for path in paths]