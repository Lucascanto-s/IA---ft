from pathlib import Path

from PIL import Image

from image_processor import (
    CANVAS_SIZE,
    content_bbox,
    content_bands,
    load_image,
    render_layers,
    reposition_image,
    validate_output,
)


def test_load_image_resolves_project_relative_paths(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    Image.new("RGB", (8, 8), "white").save(source)
    monkeypatch.setattr("image_processor.PROJECT_DIR", tmp_path)
    loaded = load_image("source.png")
    assert loaded.size == (8, 8)


def test_render_layers_places_pixels_at_absolute_coordinates():
    layer = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
    result = render_layers([{
        "id": "product",
        "image": layer,
        "x": 20,
        "y": 30,
        "width": 4,
        "height": 4,
        "z_index": 0,
    }])
    assert result.getpixel((20, 30)) == (255, 0, 0)
    assert result.getpixel((19, 30)) == (255, 255, 255)


def test_reposition_image_wrapper_keeps_canvas_size():
    source = Image.new("RGB", CANVAS_SIZE, "white")
    source.paste((0, 0, 0), (500, 500, 700, 700))
    result = reposition_image(source, x_offset=30, y_offset=-20, scale=100)
    assert result.size == CANVAS_SIZE


def test_content_bbox_detects_empty_and_centered_images():
    assert content_bbox(Image.new("RGB", CANVAS_SIZE, "white")) is None
    image = Image.new("RGB", CANVAS_SIZE, "white")
    image.paste((0, 0, 0), (500, 500, 700, 700))
    assert content_bbox(image) == (500, 500, 700, 700)


def test_validate_output_reports_cut_content(tmp_path: Path):
    centered = Image.new("RGB", CANVAS_SIZE, "white")
    centered.paste((0, 0, 0), (500, 500, 700, 700))
    centered_path = tmp_path / "centered.jpg"
    centered.save(centered_path)
    assert validate_output(centered_path)["valid"] is True

    clipped = Image.new("RGB", CANVAS_SIZE, "white")
    clipped.paste((0, 0, 0), (0, 500, 100, 700))
    clipped_path = tmp_path / "clipped.jpg"
    clipped.save(clipped_path)
    assert validate_output(clipped_path)["valid"] is False
    assert validate_output(clipped_path)["clipped"] is True


def test_content_bands_finds_separate_rows():
    image = Image.new("RGB", (20, 100), "white")
    image.paste((0, 0, 0), (0, 10, 20, 35))
    image.paste((0, 0, 0), (0, 60, 20, 85))
    assert content_bands(image) == [(10, 35), (60, 85)]


def test_cutout_is_broken_detects_eaten_subject():
    from image_processor import cutout_is_broken

    solid = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    solid.paste((50, 50, 50, 255), (40, 40, 160, 160))
    assert cutout_is_broken(solid) is False

    # Poucas aberturas grandes (peça vazada): continua sendo um recorte bom.
    few_openings = solid.copy()
    few_openings.paste((0, 0, 0, 0), (60, 60, 95, 140))
    few_openings.paste((0, 0, 0, 0), (105, 60, 140, 140))
    assert cutout_is_broken(few_openings) is False

    # Muitos buracos pequenos espalhados (recorte comeu a peça): quebrado.
    eaten = solid.copy()
    for x in range(45, 158, 7):
        for y in range(45, 158, 7):
            eaten.paste((0, 0, 0, 0), (x, y, x + 3, y + 3))
    assert cutout_is_broken(eaten) is True
