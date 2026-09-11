"""Test aislado del scraper de Amazon con Patchright."""

from amazon import AmazonScraper

# browser=None: AmazonScraper lo ignora y arma su propio Patchright
with AmazonScraper(browser=None) as scraper:
    productos = scraper.buscar("logitech mx master")
    print(f"\n{'='*60}")
    print(f"TOTAL ENCONTRADOS: {len(productos)}")
    print("=" * 60)
    for p in productos[:5]:
        print(f"\n  {p.nombre[:80]}")
        print(f"    ASIN: {p.sku}  |  Precio: {p.precio_texto_original}")
        print(f"    {p.url_detalle}")
