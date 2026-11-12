import json

from baselinker import BaseLinkerClient, extract_image_urls


def test_extract_image_urls_sorts_numeric_positions():
    assert extract_image_urls({"10": "url:https://x/10", "2": "https://x/2", "0": "https://x/0"}) == [
        "https://x/0",
        "https://x/2",
        "https://x/10",
    ]


def test_update_images_prefixes_urls_and_only_sends_requested_images(monkeypatch):
    client = BaseLinkerClient("token", "inventory", "https://example.test")
    captured = {}

    def fake_call(method, parameters):
        captured["method"] = method
        captured["parameters"] = parameters
        return {"status": "SUCCESS"}

    monkeypatch.setattr(client, "call", fake_call)
    client.update_images("42", ["https://new/1", "url:https://new/2"])
    assert captured["method"] == "addInventoryProduct"
    images = captured["parameters"]["images"]
    assert captured["parameters"]["product_id"] == "42"
    assert images == {"0": "url:https://new/1", "1": "url:https://new/2"}


def test_update_images_preserves_extra_positions(monkeypatch):
    client = BaseLinkerClient("token", "inventory", "https://example.test")
    captured = {}
    monkeypatch.setattr(client, "call", lambda method, params: captured.update(params) or {"status": "SUCCESS"})
    client.update_images("42", ["data:AAA", "https://new/2"], ["https://old/5", "https://old/6"])
    assert captured["images"] == {
        "0": "data:AAA",
        "1": "url:https://new/2",
        "2": "url:https://old/5",
        "3": "url:https://old/6",
    }


def test_find_product_matches_sku_registered_with_leading_space(monkeypatch):
    client = BaseLinkerClient("token", "inventory", "https://example.test")
    filters = []

    def fake_call(method, parameters):
        if method == "getInventoryProductsList":
            filters.append(parameters["filter_sku"])
            if parameters["filter_sku"] == " 00010276":
                return {"status": "SUCCESS", "products": {"127": {"id": 127, "sku": " 00010276"}}}
            return {"status": "SUCCESS", "products": {}}
        return {"status": "SUCCESS", "products": {"127": {"sku": " 00010276", "images": {}}}}

    monkeypatch.setattr(client, "call", fake_call)
    product_id, product = client.find_product("00010276")
    assert product_id == "127"
    assert filters == ["00010276", " 00010276"]


def test_backup_and_restore(tmp_path, monkeypatch):
    client = BaseLinkerClient("token", "inventory", "https://example.test")
    backup = client.save_images_backup("SKU-1", "42", ["url:https://old/1"], tmp_path)
    assert json.loads(backup.read_text())["product_id"] == "42"
    captured = {}
    monkeypatch.setattr(client, "call", lambda method, params: captured.update(params) or {"status": "SUCCESS"})
    client.restore_images(backup)
    assert captured["product_id"] == "42"
    assert captured["images"]["0"] == "url:https://old/1"
