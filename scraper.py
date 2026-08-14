from __future__ import annotations

import argparse
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    BASE_URL,
    DEFAULT_WORKERS,
    MAX_RETRIES,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT,
    ROBOTS_URL,
    USER_AGENT,
)

LOG = logging.getLogger("coolbox")
_thread_local = threading.local()


@dataclass(frozen=True)
class ProductTarget:
    url: str
    slug: str


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        status=MAX_RETRIES,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20))
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json,text/xml,application/xml,text/plain,*/*"})
    return session


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = build_session()
    return _thread_local.session


def get_text(url: str) -> str:
    r = get_session().get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


def discover_product_sitemaps() -> list[str]:
    robots = get_text(ROBOTS_URL)
    urls = []
    for line in robots.splitlines():
        if not line.lower().startswith("sitemap:"):
            continue
        url = line.split(":", 1)[1].strip()
        if re.search(r"/sitemap/product-\d+\.xml$", url):
            urls.append(url)
    if not urls:
        urls = [f"{BASE_URL}/sitemap/product-{i}.xml" for i in range(14)]
    return sorted(set(urls), key=lambda u: int(re.search(r"product-(\d+)", u).group(1)))


def parse_sitemap(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    urls = []
    for node in root.iter():
        if node.tag.endswith("loc") and node.text:
            url = node.text.strip()
            if url.endswith("/p") or "/p?" in url:
                urls.append(url.split("?", 1)[0])
    return urls


def slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    if path.endswith("/p"):
        path = path[:-2].rstrip("/")
    return path.rsplit("/", 1)[-1]


def discover_targets(limit: int | None = None) -> list[ProductTarget]:
    all_urls: set[str] = set()
    for sitemap in discover_product_sitemaps():
        try:
            urls = parse_sitemap(get_text(sitemap))
            LOG.info("%s -> %s URLs", sitemap.rsplit("/", 1)[-1], len(urls))
            all_urls.update(urls)
        except Exception as exc:
            LOG.warning("No se pudo leer %s: %s", sitemap, exc)
    targets = [ProductTarget(url=u, slug=slug_from_url(u)) for u in sorted(all_urls)]
    return targets[:limit] if limit else targets


def fetch_structured_product(target: ProductTarget) -> dict[str, Any]:
    endpoint = f"{BASE_URL}/api/catalog_system/pub/products/search/{target.slug}/p"
    time.sleep(REQUEST_DELAY_SECONDS)
    r = get_session().get(endpoint, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        raise ValueError("API sin producto")
    product = data[0]
    product["_source_url"] = target.url
    product["_api_url"] = endpoint
    return product


def clean_htmlish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def seller_offer(product: dict[str, Any], item: dict[str, Any], seller: dict[str, Any]) -> dict[str, Any]:
    offer = seller.get("commertialOffer") or {}
    return {
        "productId": product.get("productId"),
        "skuId": item.get("itemId"),
        "sellerId": seller.get("sellerId"),
        "sellerName": seller.get("sellerName"),
        "sellerDefault": seller.get("sellerDefault"),
        "ListPrice": offer.get("ListPrice"),
        "Price": offer.get("Price"),
        "PriceWithoutDiscount": offer.get("PriceWithoutDiscount"),
        "AvailableQuantity": offer.get("AvailableQuantity"),
        "IsAvailable": offer.get("IsAvailable"),
        "RewardValue": offer.get("RewardValue"),
        "spotPrice": offer.get("spotPrice"),
        "taxPercentage": offer.get("taxPercentage"),
        "taxes": json.dumps(offer.get("Tax") or [], ensure_ascii=False),
        "installments": json.dumps(offer.get("Installments") or [], ensure_ascii=False),
        "teasers": json.dumps(offer.get("teasers") or [], ensure_ascii=False),
        "deliverySlaSamples": json.dumps(offer.get("DeliverySlaSamplesPerRegion") or {}, ensure_ascii=False),
    }


def flatten_product(product: dict[str, Any], extracted_at: str) -> tuple[dict, list[dict], list[dict], list[dict], list[dict]]:
    items = product.get("items") or []
    first_offer = None
    for item in items:
        for seller in item.get("sellers") or []:
            offer = seller.get("commertialOffer") or {}
            if first_offer is None or offer.get("IsAvailable"):
                first_offer = offer
                if offer.get("IsAvailable"):
                    break
        if first_offer and first_offer.get("IsAvailable"):
            break
    first_offer = first_offer or {}

    known = {
        "productId", "productName", "brand", "brandId", "linkText", "productReference",
        "description", "categories", "categoriesIds", "categoryId", "items", "link",
        "productTitle", "metaTagDescription", "releaseDate", "clusterHighlights",
        "productClusters", "searchableClusters", "categoriesMap", "_source_url", "_api_url",
    }
    dynamic_specs = {k: v for k, v in product.items() if k not in known and isinstance(v, list)}

    row = {
        "productId": product.get("productId"),
        "productReference": product.get("productReference"),
        "productName": product.get("productName"),
        "productTitle": product.get("productTitle"),
        "brand": product.get("brand"),
        "brandId": product.get("brandId"),
        "categoryId": product.get("categoryId"),
        "categories": " | ".join(product.get("categories") or []),
        "categoriesIds": " | ".join(product.get("categoriesIds") or []),
        "description": clean_htmlish(product.get("description")),
        "metaTagDescription": product.get("metaTagDescription"),
        "releaseDate": product.get("releaseDate"),
        "url": product.get("_source_url") or product.get("link"),
        "apiUrl": product.get("_api_url"),
        "skuCount": len(items),
        "listPrice": first_offer.get("ListPrice"),
        "price": first_offer.get("Price"),
        "availableQuantity": first_offer.get("AvailableQuantity"),
        "isAvailable": first_offer.get("IsAvailable"),
        "specificationFieldCount": len(dynamic_specs),
        "extractedAt": extracted_at,
    }

    sku_rows: list[dict] = []
    offer_rows: list[dict] = []
    spec_rows: list[dict] = []
    image_rows: list[dict] = []

    for item in items:
        sku_rows.append({
            "productId": product.get("productId"),
            "skuId": item.get("itemId"),
            "name": item.get("name"),
            "nameComplete": item.get("nameComplete"),
            "ean": item.get("ean"),
            "referenceId": " | ".join(str(x.get("Value")) for x in (item.get("referenceId") or []) if x.get("Value")),
            "measurementUnit": item.get("measurementUnit"),
            "unitMultiplier": item.get("unitMultiplier"),
            "modalType": item.get("modalType"),
            "kitItems": json.dumps(item.get("kitItems") or [], ensure_ascii=False),
            "variations": json.dumps(item.get("variations") or [], ensure_ascii=False),
            "skuSpecifications": json.dumps(item.get("skuSpecifications") or [], ensure_ascii=False),
        })
        for seller in item.get("sellers") or []:
            offer_rows.append(seller_offer(product, item, seller))
        for idx, image in enumerate(item.get("images") or [], start=1):
            image_rows.append({
                "productId": product.get("productId"),
                "skuId": item.get("itemId"),
                "position": idx,
                "imageId": image.get("imageId"),
                "imageLabel": image.get("imageLabel"),
                "imageUrl": image.get("imageUrl"),
                "imageText": image.get("imageText"),
                "imageLastModified": image.get("imageLastModified"),
            })

    for field, values in dynamic_specs.items():
        for value in values:
            spec_rows.append({
                "productId": product.get("productId"),
                "field": field,
                "value": clean_htmlish(value),
            })

    return row, sku_rows, offer_rows, spec_rows, image_rows


def auto_width(ws, max_width: int = 60) -> None:
    for column_cells in ws.columns:
        letter = column_cells[0].column_letter
        length = min(max((len(str(c.value)) if c.value is not None else 0) for c in column_cells) + 2, max_width)
        ws.column_dimensions[letter].width = max(length, 10)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def export_excel(products, skus, offers, specs, images, summary: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(list(summary.items()), columns=["Campo", "Valor"]).to_excel(writer, "Resumen", index=False)
        pd.DataFrame(products).to_excel(writer, "Productos", index=False)
        pd.DataFrame(skus).to_excel(writer, "SKUs", index=False)
        pd.DataFrame(offers).to_excel(writer, "Ofertas", index=False)
        pd.DataFrame(specs).to_excel(writer, "Especificaciones", index=False)
        pd.DataFrame(images).to_excel(writer, "Imagenes", index=False)
        for ws in writer.book.worksheets:
            auto_width(ws)
            if ws.max_row >= 1:
                for cell in ws[1]:
                    cell.font = cell.font.copy(bold=True)


def write_dashboard_data(products: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    compact = []
    for p in products:
        compact.append({
            "id": p.get("productId"), "sku": p.get("productReference"), "name": p.get("productName"),
            "brand": p.get("brand"), "category": p.get("categories"), "price": p.get("price"),
            "listPrice": p.get("listPrice"), "stock": p.get("availableQuantity"), "available": p.get("isAvailable"),
            "url": p.get("url"), "updated": p.get("extractedAt"),
        })
    path.write_text(json.dumps({"products": compact}, ensure_ascii=False), encoding="utf-8")


def run(limit: int | None, workers: int, output: Path) -> int:
    started = datetime.now(timezone.utc)
    extracted_at = started.isoformat()
    targets = discover_targets(limit)
    LOG.info("Productos descubiertos: %s", len(targets))

    raw_path = output.parent / "catalog_raw.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[dict] = []
    raws: list[dict] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_structured_product, t): t for t in targets}
        for i, future in enumerate(as_completed(futures), 1):
            target = futures[future]
            try:
                raws.append(future.result())
            except Exception as exc:
                errors.append({"url": target.url, "error": str(exc)})
            if i % 100 == 0 or i == len(targets):
                LOG.info("Procesados %s/%s | OK %s | errores %s", i, len(targets), len(raws), len(errors))

    unique: dict[str, dict] = {}
    for raw in raws:
        key = str(raw.get("productId") or raw.get("_source_url"))
        unique.setdefault(key, raw)
    raws = list(unique.values())

    products: list[dict] = []
    skus: list[dict] = []
    offers: list[dict] = []
    specs: list[dict] = []
    images: list[dict] = []
    for raw in raws:
        p, s, o, sp, im = flatten_product(raw, extracted_at)
        products.append(p)
        skus.extend(s)
        offers.extend(o)
        specs.extend(sp)
        images.extend(im)

    with raw_path.open("w", encoding="utf-8") as f:
        for raw in raws:
            f.write(json.dumps(raw, ensure_ascii=False) + "\n")

    finished = datetime.now(timezone.utc)
    summary = {
        "Inicio UTC": started.isoformat(),
        "Fin UTC": finished.isoformat(),
        "Duracion segundos": round((finished - started).total_seconds(), 2),
        "URLs descubiertas/procesadas": len(targets),
        "Productos unicos extraidos": len(products),
        "SKUs": len(skus),
        "Ofertas seller": len(offers),
        "Especificaciones": len(specs),
        "Imagenes": len(images),
        "Errores": len(errors),
        "Cobertura sobre URLs %": round((len(products) / len(targets) * 100), 2) if targets else 0,
    }
    export_excel(products, skus, offers, specs, images, summary, output)
    write_dashboard_data(products, Path("docs/data/catalog.json"))
    Path("output/errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("output/summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Excel: %s", output)
    return 0 if products else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extrae el catálogo público de Coolbox Perú a Excel")
    parser.add_argument("--limit", type=int, default=None, help="Procesar solo N URLs para pruebas")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Número de descargas concurrentes")
    parser.add_argument("--output", type=Path, default=Path("output/coolbox_catalog.xlsx"))
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(run(args.limit, max(1, args.workers), args.output))
