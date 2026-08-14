from __future__ import annotations

import argparse, json, logging, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
import scraper as v1

LOG = logging.getLogger("coolbox-v2")


def hinted_id(target: v1.ProductTarget) -> str | None:
    m = re.search(r"(?:^|-)(\d{4,})$", target.slug)
    return m.group(1) if m else None


def api_get(url: str) -> list[dict[str, Any]]:
    time.sleep(v1.REQUEST_DELAY_SECONDS)
    r = v1.get_session().get(url, timeout=v1.REQUEST_TIMEOUT, headers={"Accept": "application/json"})
    if r.status_code in (400, 404):
        return []
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def pick(rows: list[dict[str, Any]], pid: str | None) -> dict[str, Any] | None:
    if pid:
        for row in rows:
            if str(row.get("productId")) == pid:
                return row
    return rows[0] if rows else None


def page_state(target: v1.ProductTarget) -> dict[str, Any]:
    try:
        time.sleep(v1.REQUEST_DELAY_SECONDS)
        r = v1.get_session().get(target.url, timeout=v1.REQUEST_TIMEOUT, headers={"Accept": "text/html"})
        status = r.status_code
        text = r.text.lower()
    except Exception as exc:
        return {"httpStatus": None, "pageState": "request_error", "pageError": str(exc)}
    if status >= 400:
        return {"httpStatus": status, "pageState": "http_error"}
    state = "agotado" if "agotado" in text else "page_present"
    if any(x in text for x in ("página no encontrada", "pagina no encontrada", "not found")):
        state = "not_found"
    return {"httpStatus": status, "pageState": state}


def fetch_v2(target: v1.ProductTarget) -> dict[str, Any]:
    pid = hinted_id(target)
    attempts = [
        ("slug", f"{v1.BASE_URL}/api/catalog_system/pub/products/search/{quote(target.slug)}/p"),
    ]
    if pid:
        attempts += [
            ("productId", f"{v1.BASE_URL}/api/catalog_system/pub/products/search?fq=productId:{quote(pid)}"),
            ("fullText", f"{v1.BASE_URL}/api/catalog_system/pub/products/search?ft={quote(pid)}"),
        ]
    for method, endpoint in attempts:
        product = pick(api_get(endpoint), pid)
        if product:
            product["_source_url"] = target.url
            product["_api_url"] = endpoint
            product["_resolution_method"] = method
            product["_hinted_id"] = pid
            return product
    raise LookupError(json.dumps({"reason": "API sin producto", "hintedId": pid, **page_state(target)}, ensure_ascii=False))


def main_image(raw: dict[str, Any]) -> str | None:
    for item in raw.get("items") or []:
        imgs = item.get("images") or []
        if imgs and imgs[0].get("imageUrl"):
            return imgs[0]["imageUrl"]
    return None


def write_dashboard(products: list[dict], summary: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    compact = [{
        "id": p.get("productId"), "sku": p.get("productReference"), "name": p.get("productName"),
        "brand": p.get("brand"), "category": p.get("categories"), "price": p.get("price"),
        "listPrice": p.get("listPrice"), "stock": p.get("availableQuantity"), "available": p.get("isAvailable"),
        "image": p.get("imageUrl"), "url": p.get("url"), "resolution": p.get("resolutionMethod"),
        "updated": p.get("extractedAt"),
    } for p in products]
    path.write_text(json.dumps({"summary": summary, "products": compact}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def run(limit: int | None, workers: int, output: Path) -> int:
    started = datetime.now(timezone.utc); extracted_at = started.isoformat()
    targets = v1.discover_targets(limit); LOG.info("Productos descubiertos: %s", len(targets))
    raws, errors, rescued = [], [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_v2, t): t for t in targets}
        for i, future in enumerate(as_completed(futures), 1):
            target = futures[future]
            try:
                raw = future.result(); raws.append(raw)
                rescued += raw.get("_resolution_method") != "slug"
            except Exception as exc:
                info = {"reason": str(exc)}
                try: info = json.loads(str(exc))
                except Exception: pass
                errors.append({"url": target.url, "slug": target.slug, "hintedId": hinted_id(target), **info})
            if i % 100 == 0 or i == len(targets):
                LOG.info("Procesados %s/%s | OK %s | rescatados %s | errores %s", i, len(targets), len(raws), rescued, len(errors))

    unique = {}
    for raw in raws:
        key = str(raw.get("productId") or raw.get("_source_url"))
        if key not in unique or raw.get("_resolution_method") == "slug": unique[key] = raw
    raws = list(unique.values())

    products=[]; skus=[]; offers=[]; specs=[]; images=[]
    for raw in raws:
        p,s,o,sp,im = v1.flatten_product(raw, extracted_at)
        p["resolutionMethod"] = raw.get("_resolution_method", "slug")
        p["hintedId"] = raw.get("_hinted_id")
        p["imageUrl"] = main_image(raw)
        p["catalogStatus"] = "active" if p.get("isAvailable") else "out_of_stock"
        products.append(p); skus.extend(s); offers.extend(o); specs.extend(sp); images.extend(im)

    output.parent.mkdir(parents=True, exist_ok=True)
    finished = datetime.now(timezone.utc)
    visible_unresolved = sum(e.get("pageState") in ("page_present","agotado") for e in errors)
    summary = {
        "Version":"2.0", "Inicio UTC":started.isoformat(), "Fin UTC":finished.isoformat(),
        "Duracion segundos":round((finished-started).total_seconds(),2), "URLs descubiertas/procesadas":len(targets),
        "Productos unicos extraidos":len(products), "Productos rescatados V2":rescued, "SKUs":len(skus),
        "Ofertas seller":len(offers), "Especificaciones":len(specs), "Imagenes":len(images), "No resueltos":len(errors),
        "No resueltos con pagina visible":visible_unresolved,
        "Cobertura sobre URLs %":round(len(products)/len(targets)*100,2) if targets else 0,
    }
    v1.export_excel(products, skus, offers, specs, images, summary, output)
    with pd.ExcelWriter(output, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        pd.DataFrame(errors).to_excel(writer, sheet_name="No_resueltos", index=False)
        v1.auto_width(writer.book["No_resueltos"])
    write_dashboard(products, summary, Path("docs/data/catalog.json"))
    Path("output/errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("output/summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Excel V2: %s", output)
    return 0 if products else 2


def parse_args():
    p=argparse.ArgumentParser(description="Coolbox scraper V2")
    p.add_argument("--limit",type=int,default=None);p.add_argument("--workers",type=int,default=v1.DEFAULT_WORKERS)
    p.add_argument("--output",type=Path,default=Path("output/coolbox_catalog.xlsx"));p.add_argument("--verbose",action="store_true")
    return p.parse_args()

if __name__ == "__main__":
    a=parse_args();logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(run(a.limit,max(1,a.workers),a.output))
