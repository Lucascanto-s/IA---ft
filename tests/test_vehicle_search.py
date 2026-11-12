import io

import pytest
from PIL import Image

import vehicle_search
from vehicle_search import VehicleSearchError, _matches_vehicle_label, find_vehicle_image


def _png(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (160, 120), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _fake_requests(monkeypatch, pages_by_query, image_colors):
    searches = []

    class Response:
        def __init__(self, payload=None, content=b""):
            self._payload = payload
            self.content = content

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_get(url, *, params=None, headers, timeout):
        if url == vehicle_search.COMMONS_API:
            searches.append(params["gsrsearch"])
            pages = {
                str(index): {"index": index, "title": title, "imageinfo": [{"mime": "image/jpeg", "thumburl": image_url}]}
                for index, (title, image_url) in enumerate(pages_by_query.get(params["gsrsearch"], []))
            }
            return Response({"query": {"pages": pages}})
        return Response(content=_png(image_colors[url]))

    monkeypatch.setattr("vehicle_search.requests.get", fake_get)
    return searches


def test_searches_one_year_at_a_time_and_prefers_white_background(monkeypatch):
    searches = _fake_requests(
        monkeypatch,
        {
            "Subaru Impreza 2007": [
                ("File:2007 Subaru Impreza R.jpg", "https://img/street"),
                ("File:2007 Subaru Impreza WRX studio.jpg", "https://img/white"),
            ],
            "Subaru Impreza 2008": [("File:2014 Subaru Impreza WRC.jpg", "https://img/wrong-year")],
        },
        {"https://img/street": "gray", "https://img/white": "white", "https://img/wrong-year": "white"},
    )
    assert find_vehicle_image("Subaru Impreza 2007-2008") == "https://img/white"
    assert searches == ["Subaru Impreza 2007", "Subaru Impreza 2008", "Subaru Impreza"]


def test_accepts_photo_without_white_background_when_no_white_one_exists(monkeypatch):
    _fake_requests(
        monkeypatch,
        {"Ford Fusion 2006": [("File:2006 Ford Fusion SE.jpg", "https://img/street")]},
        {"https://img/street": "gray"},
    )
    assert find_vehicle_image("Ford Fusion 2006") == "https://img/street"


def test_raises_when_nothing_matches(monkeypatch):
    _fake_requests(monkeypatch, {}, {})
    with pytest.raises(VehicleSearchError):
        find_vehicle_image("Subaru Impreza 2007")


def test_label_rules():
    years = {2007, 2008, 2009}
    assert _matches_vehicle_label("File:2007 Subaru Impreza R.jpg", "Subaru Impreza", years)
    assert _matches_vehicle_label("File:Subaru Impreza hatchback.jpg", "Subaru Impreza", years)
    assert not _matches_vehicle_label("File:2014 Subaru Impreza WRC.jpg", "Subaru Impreza", years)
    assert not _matches_vehicle_label("File:2007 Subaru Forester.jpg", "Subaru Impreza", years)
    assert not _matches_vehicle_label("File:2007 Ford Fusion NASCAR at FOS21.jpg", "Ford Fusion", years)
