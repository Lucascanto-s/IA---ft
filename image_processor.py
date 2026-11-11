import io
import os
from pathlib import Path
from typing import Iterable

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
from rembg import new_session, remove


CANVAS_SIZE = (1200, 1200)
WHITE = (255, 255, 255)
RULER_SIZE = 42
EDITOR_PREVIEW_SIZE = 640
REMBG_SESSION = new_session("u2netp")


def load_image(source: str) -> Image.Image:
    if source.startswith(("http://", "https://")):
        response = requests.get(
            source,
            headers={"User-Agent": "autopecas-image-generator/1.0"},
            timeout=60,
        )
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGBA")
    return Image.open(source).convert("RGBA")


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
    rgb = image.convert("RGB")
    pixels = rgb.load()
    for y in range(rgb.height):
        for x in range(rgb.width):
            red, green, blue = pixels[x, y]
            if red > 150 and green < 90 and blue < 90:
                pixels[x, y] = WHITE
    return rgb.convert("RGBA")


def content_bands(image: Image.Image) -> list[tuple[int, int]]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    rows = []
    for y in range(height):
        row = rgb.crop((0, y, width, y + 1)).getdata()
        rows.append(sum(min(pixel) < 245 for pixel in row) > max(3, width // 200))
    bands = []
    start = None
    for index, has_content in enumerate(rows + [False]):
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
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    result = remove(
        encoded.getvalue(),
        session=REMBG_SESSION,
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
    canvas = Image.new("RGB", CANVAS_SIZE, WHITE)
    x = (CANVAS_SIZE[0] - content.width) // 2 + x_offset
    y = (CANVAS_SIZE[1] - content.height) // 2 + y_offset
    canvas.paste(content, (x, y))
    return canvas


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


def sharpen(image: Image.Image) -> Image.Image:
    return image.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3))


def content_bbox(image: Image.Image, threshold: int = 5) -> tuple[int, int, int, int] | None:
    rgb = image.convert("RGB")
    difference = ImageChops.difference(rgb, Image.new("RGB", rgb.size, WHITE)).convert("L")
    return difference.point(lambda value: 255 if value > threshold else 0).getbbox()


def validate_output(path: Path, tolerance: int = 24) -> dict[str, int | bool]:
    image = Image.open(path).convert("RGB")
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
    has_vehicle_mask = vehicle.getchannel("A").getextrema()[0] < 255
    canvas = Image.new("RGBA", CANVAS_SIZE, WHITE + (255,))
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
    vehicle_x = (1200 - vehicle_fitted.width) // 2 + vehicle_x_offset
    vehicle_y = 25 + vehicle_y_offset
    if has_vehicle_mask:
        shadow_alpha = vehicle_fitted.getchannel("A").filter(ImageFilter.GaussianBlur(14))
        shadow_alpha = shadow_alpha.point(lambda value: int(value * 0.22))
        shadow = Image.new("RGBA", vehicle_fitted.size, (40, 40, 40, 0))
        shadow.putalpha(shadow_alpha)
        canvas.alpha_composite(shadow, (vehicle_x, vehicle_y + 16))
    canvas.alpha_composite(vehicle_fitted, (vehicle_x, vehicle_y))
    product_x = (1200 - product_fitted.width) // 2 + product_x_offset
    product_y = 500 + product_y_offset
    canvas.alpha_composite(product_fitted, (product_x, product_y))
    return canvas.convert("RGB")


def create_cover(vehicle_source: str, product: Image.Image) -> Image.Image:
    return compose_cover(prepare_vehicle(vehicle_source), product)


def create_set(product_sources: Iterable[str], vehicle_source: str, output_dir: str) -> list[Path]:
    sources = list(product_sources)[:4]
    if not sources:
        raise ValueError("O produto nao possui imagens")
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
    return paths


def public_urls(paths: Iterable[Path], base_url: str, sku: str) -> list[str]:
    base = base_url.rstrip("/")
    return [f"{base}/{sku}/{path.name}" for path in paths]