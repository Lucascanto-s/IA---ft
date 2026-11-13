from baselinker import BaseLinkerClient, sku_number


class FakeClient(BaseLinkerClient):
    """Cliente com um inventário falso, sem rede."""
    def __init__(self, products):
        super().__init__("t", "1", "http://x")
        self._products = products  # {id: {"sku":..., "star":..., "name":...}}

    def call(self, method, parameters):
        if method == "getInventoryProductsList":
            page = parameters.get("page", 1)
            items = list(self._products.items())[(page - 1) * 1000: page * 1000]
            return {"products": {pid: {"sku": info["sku"]} for pid, info in items}}
        if method == "getInventoryProductsData":
            wanted = {str(p) for p in parameters["products"]}
            return {"products": {pid: {"sku": info["sku"], "star": info["star"],
                                       "text_fields": {"name": info.get("name", "")}}
                                 for pid, info in self._products.items() if pid in wanted}}
        raise AssertionError(method)


def _inventory():
    return {
        "1": {"sku": "00000500", "star": 4},
        "2": {"sku": "00009000", "star": 4},
        "3": {"sku": "00010323", "star": 4},
        "4": {"sku": "00020000", "star": 5},
        "5": {"sku": "ABC", "star": 4},
    }


def test_sku_number():
    assert sku_number("00009000") == 9000
    assert sku_number("ABC") is None


def test_find_starred_without_range():
    found = FakeClient(_inventory()).find_starred(4)
    assert [item["sku"] for item in found] == ["00000500", "00009000", "00010323", "ABC"]


def test_find_starred_from_sku():
    found = FakeClient(_inventory()).find_starred(4, sku_from=9000)
    assert [item["sku"] for item in found] == ["00009000", "00010323"]


def test_find_starred_sku_range():
    found = FakeClient(_inventory()).find_starred(4, sku_from=9000, sku_to=15000)
    assert [item["sku"] for item in found] == ["00009000", "00010323"]
    tight = FakeClient(_inventory()).find_starred(4, sku_from=9000, sku_to=9500)
    assert [item["sku"] for item in tight] == ["00009000"]
