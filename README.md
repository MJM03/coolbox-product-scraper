# Coolbox Product Scraper

Extractor de catálogo para **Coolbox Perú**. Descubre productos desde los sitemaps públicos, consulta la API pública estructurada de VTEX que usa la tienda y genera un Excel normalizado.

## Salidas

- `Productos`: una fila por producto.
- `SKUs`: una fila por variante/SKU.
- `Ofertas`: precio, stock y seller por SKU.
- `Especificaciones`: todas las especificaciones publicadas.
- `Imagenes`: todas las URLs de imágenes.
- `Resumen`: cobertura y métricas de ejecución.
- `output/catalog_raw.jsonl`: respaldo estructurado completo.
- `docs/data/catalog.json`: versión ligera para el panel web.

Esta separación evita perder información cuando un producto tiene varios SKUs, vendedores, precios o imágenes.

## Ejecutar

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python scraper.py --limit 25   # prueba
python scraper.py              # catálogo completo
```

El Excel queda en `output/coolbox_catalog.xlsx`.

## Automatización

`.github/workflows/scrape.yml` incluye ejecución manual y diaria. El Excel se publica como **GitHub Actions artifact** y `docs/data/catalog.json` se actualiza automáticamente para alimentar el panel.

Horario del cron: 08:20 UTC (aprox. 03:20 en Perú).

## Panel web

`docs/index.html` incluye búsqueda, filtro de marca, filtro de disponibilidad, precios, SKU, categoría y enlace directo a Coolbox. Está preparado para GitHub Pages.

## Cobertura y seguridad

- Descubrimiento por los sitemaps de producto declarados por Coolbox.
- Detalle mediante rutas públicas de catálogo de VTEX.
- Reintentos y backoff para 429/errores temporales.
- Rate limiting básico y concurrencia configurable.
- No usa login, checkout, endpoints privados ni técnicas para evadir protecciones.
- `output/errors.json` permite auditar cualquier ficha que no haya podido extraerse.

## Nota

Proyecto independiente, no afiliado a Coolbox. La estructura pública del sitio puede cambiar; el scraper está diseñado para hacer visibles esos fallos en lugar de ocultarlos.
