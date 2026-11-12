from vehicle_search import find_vehicle_image


def test_find_vehicle_image_requests_white_background(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "query": {
                    "pages": {
                        "1": {
                            "title": "Ford Fusion",
                            "imageinfo": [{
                                "mime": "image/jpeg",
                                "thumburl": "https://example.test/fusion.jpg",
                            }],
                        }
                    }
                }
            }

    def fake_get(url, *, params, headers, timeout):
        captured["params"] = params
        return Response()

    monkeypatch.setattr("vehicle_search.requests.get", fake_get)

    assert find_vehicle_image("Ford Fusion 2006") == "https://example.test/fusion.jpg"
    assert captured["params"]["gsrsearch"] == "Ford Fusion 2006 car white background"
