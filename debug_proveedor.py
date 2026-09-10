"""
Script de reconocimiento para inspeccionar sitios de proveedores.

Uso:
    python debug_proveedor.py eurocomp "mouse logitech"

Que hace:
    1. Abre el navegador visible (Chromium).
    2. Va al home del proveedor, escribe en el buscador y hace submit.
    3. Espera a que la pagina cargue completa.
    4. Hace scroll hasta el final para forzar carga de contenido lazy.
    5. Guarda el HTML final en debug/<proveedor>_<query>.html
    6. Toma un screenshot en debug/<proveedor>_<query>.png
    7. Deja el navegador abierto para inspeccion manual.
    8. Al presionar Enter en la consola, cierra todo.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from config import config

# ---------------------------------------------------------------------
# Configuracion por proveedor: como buscar en cada uno.
# ---------------------------------------------------------------------
# tipo:
#   "url"  -> se arma la URL con el parametro y se navega directamente
#   "form" -> se navega al home, se rellena el input y se hace submit
# ---------------------------------------------------------------------
PROVEEDORES = {
    "eurocomp": {
        "tipo": "form",
        "home": "https://eurocompcr.com",
        "input_selector": "#ed",
        "submit_selector": "#catalogo_submit",
    },
    "intelec": {
        # Con post_type=product limitamos a solo productos (no posts de blog)
        "tipo": "url",
        "url": "https://www.intelec.co.cr/?s={query}&post_type=product",
    },
    "intcomex": {
        # URL real de resultados (la anterior daba 404)
        "tipo": "url",
        "url": "https://store.intcomex.com/Products/ByKeyword?term={query}&typeSearch=&r=true",
    },
    "amazon": {
        "tipo": "url",
        "url": "https://www.amazon.com/s?k={query}",
    },
}


def slugify(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")


def buscar_por_form(page: Page, cfg: dict, query: str) -> None:
    """Llena el input del buscador y hace submit."""
    print(f"→ Cargando home: {cfg['home']}")
    page.goto(
        cfg["home"], wait_until="domcontentloaded", timeout=config.scraper_timeout_ms
    )

    print(f"→ Escribiendo '{query}' en el input {cfg['input_selector']}")
    page.wait_for_selector(cfg["input_selector"], timeout=10000)
    page.fill(cfg["input_selector"], query)

    print(f"→ Haciendo click en {cfg['submit_selector']}")
    # Esperamos a que la navegacion suceda al hacer click
    with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
        page.click(cfg["submit_selector"])

    print(f"→ URL de resultados: {page.url}")


def buscar_por_url(page: Page, cfg: dict, query: str) -> None:
    """Va directo a la URL de busqueda."""
    from urllib.parse import quote_plus

    url = cfg["url"].format(query=quote_plus(query))
    print(f"→ Cargando: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=config.scraper_timeout_ms)


def hacer_scroll_completo(page: Page) -> None:
    """Scroll progresivo hasta el final para disparar contenido lazy."""
    try:
        page.evaluate("""
            async () => {
                await new Promise((resolve) => {
                    let totalHeight = 0;
                    const distance = 400;
                    const timer = setInterval(() => {
                        const scrollHeight = document.body.scrollHeight;
                        window.scrollBy(0, distance);
                        totalHeight += distance;
                        if (totalHeight >= scrollHeight - window.innerHeight) {
                            clearInterval(timer);
                            resolve();
                        }
                    }, 200);
                });
            }
        """)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"⚠ Error en scroll: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug de sitios proveedor")
    parser.add_argument("proveedor", choices=list(PROVEEDORES.keys()))
    parser.add_argument("query", help="Texto de busqueda (entre comillas)")
    args = parser.parse_args()

    cfg = PROVEEDORES[args.proveedor]

    debug_dir = Path("debug")
    debug_dir.mkdir(exist_ok=True)
    base_name = f"{args.proveedor}_{slugify(args.query)}"
    html_path = debug_dir / f"{base_name}.html"
    png_path = debug_dir / f"{base_name}.png"

    print(f"\n▶ Proveedor: {args.proveedor}")
    print(f"▶ Query:     {args.query}")
    print(f"▶ Tipo:      {cfg['tipo']}\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=200)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="es-CR",
        )
        page = context.new_page()

        try:
            if cfg["tipo"] == "form":
                buscar_por_form(page, cfg, args.query)
            else:
                buscar_por_url(page, cfg, args.query)
        except Exception as e:
            print(f"⚠ Error durante la busqueda: {e}")
            print("  El navegador queda abierto para revision manual.")

        # Esperar red inactiva
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            print("⚠ networkidle no se alcanzo en 15s (puede ser normal)")

        # Scroll para forzar lazy loading
        print("→ Haciendo scroll para forzar carga de contenido lazy...")
        hacer_scroll_completo(page)

        # Volver arriba para el screenshot
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

        # Guardar HTML y screenshot
        print(f"→ Guardando HTML en {html_path}")
        html = page.content()
        html_path.write_text(html, encoding="utf-8")

        print(f"→ Guardando screenshot en {png_path}")
        page.screenshot(path=str(png_path), full_page=True)

        tam_kb = html_path.stat().st_size / 1024
        print(f"\n✓ HTML: {tam_kb:,.0f} KB ({len(html):,} caracteres)")
        print(f"✓ URL final: {page.url}")

        print("\n" + "=" * 60)
        print("NAVEGADOR ABIERTO — Puedes inspeccionar con F12")
        print("=" * 60)
        print("Presiona Enter aqui en la consola para cerrar el navegador...")
        input()

        browser.close()

    print("\n✓ Listo. Archivos guardados en debug/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
