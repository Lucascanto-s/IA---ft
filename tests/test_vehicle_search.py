import io

from PIL import Image

from vehicle_search import find_vehicle_image


def test_find_vehicle_image_requests_white_background(monkeypatch):
    captured = {}

    white_image = io.BytesIO()
    Image.new("RGB", (160, 120), "white").save(white_image, format="PNG")

    class SearchResponse:
        ok = True
        text = '<img alt="Ford Fusion 2006 white background" src="https://encrypted-tbn0.gstatic.com/images?q=fusion">'

    class ImageResponse:
        content = white_image.getvalue()

        def raise_for_status(self):
            pass

    def fake_get(url, *, params=None, headers, timeout):
        if url == "https://www.google.com/search":
            captured["url"] = url
            captured["params"] = params
            return SearchResponse()
        return ImageResponse()

    monkeypatch.setattr("vehicle_search.requests.get", fake_get)

    assert find_vehicle_image("Ford Fusion 2006") == "https://encrypted-tbn0.gstatic.com/images?q=fusion"
    assert captured["url"] == "https://www.google.com/search"
    assert captured["params"] == {
        "tbm": "isch",
        "q": "Ford Fusion 2006 car isolated white background",
    }
