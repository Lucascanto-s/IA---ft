import json

from baselinker import BaseLinkerClient, extract_image_urls


def test_extract_image_urls_sorts_numeric_positions():
    assert extract_image_urls({"10": "url:https://x/10", "2": "https://x/2", "0": "https://x/0"}) == [
        "https://x/0",
        "https://x/2",
        "https://x/10",
    ]


def test_update_images_prefixes_urls_and_preserves_extra(monkeypatch):
    client = BaseLinkerClient("token", "inventory", "https://example.test")
    captured = {}

    def fake_call(method, parameters):
        captured["method"] = method
        captured["parameters"] = parameters
        return {"status": "SUCCESS"}

    monkeypatch.setattr(client, "call", fake_call)
    client.update_images("42", ["https://new/1", "url:https://new/2"], ["https://old/3"])
    images = captured["parameters"]["products"]["42"]["images"]
    assert images == {"0": "url:https://new/1", "1": "url:https://new/2", "2": "url:https://old/3"}


def test_backup_and_restore(tmp_path, monkeypatch):
    client = BaseLinkerClient("token", "inventory", "https://example.test")
    backup = client.save_images_backup("SKU-1", "42", ["url:https://old/1"], tmp_path)
    assert json.loads(backup.read_text())["product_id"] == "42"
    captured = {}
    monkeypatch.setattr(client, "call", lambda method, params: captured.update(params) or {"status": "SUCCESS"})
    client.restore_images(backup)
    assert captured["products"]["42"]["images"]["0"] == "url:https://old/1"
