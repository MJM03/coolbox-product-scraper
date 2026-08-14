from scraper import slug_from_url, flatten_product


def test_slug_from_url():
    assert slug_from_url("https://www.coolbox.pe/producto-ejemplo/p") == "producto-ejemplo"


def test_flatten_minimal_product():
    raw = {
        "productId": "1", "productName": "Demo", "brand": "Marca", "categories": ["/Cat/"],
        "items": [{"itemId": "10", "name": "Demo SKU", "sellers": [{"sellerId": "1", "sellerName": "Coolbox", "commertialOffer": {"Price": 10, "ListPrice": 12, "AvailableQuantity": 3, "IsAvailable": True}}], "images": []}],
        "_source_url": "https://www.coolbox.pe/demo/p",
        "Color": ["Negro"],
    }
    product, skus, offers, specs, images = flatten_product(raw, "2026-01-01T00:00:00+00:00")
    assert product["price"] == 10
    assert len(skus) == 1
    assert offers[0]["AvailableQuantity"] == 3
    assert specs[0]["field"] == "Color"
    assert images == []
