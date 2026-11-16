"""
diagnosticar.py — Mira UN proveedor por dentro, sin tocar nada.

QUE HACE, EN UNA LINEA
──────────────────────
Corre un solo scraper con la busqueda que le digas y te muestra, producto por
producto, lo que trajo y lo que el matcher decidio sobre cada uno.

PARA QUE SIRVE
──────────────
Hoy, cuando un proveedor trae 20 productos y solo 4 llegan a la pantalla, no hay
forma de saber si el problema es el scraper, el precio, la blacklist o el
matcher. Esta herramienta responde esa pregunta en una corrida: ves los
productos crudos tal como vinieron, con su precio, y al lado el veredicto con su
motivo.

NO escribe en la base de datos. NO crea solicitudes. NO usa el worker. Se puede
correr todas las veces que quieras sin ensuciar nada.

COMO SE USA
───────────
  Ver un proveedor con una busqueda:
    python diagnosticar.py amazon "monitor LG 16 pulgadas"

  Sin pasar por la IA (la query va literal al buscador del proveedor):
    python diagnosticar.py amazon "monitor LG 16 pulgadas" --sin-ia

  Ver el navegador trabajando (util cuando un scraper da timeout):
    python diagnosticar.py eurocomp "laptop" --ver

  Todos los proveedores implementados, uno tras otro:
    python diagnosticar.py todos "monitor LG 16 pulgadas"

  Guardar el HTML de la pagina de resultados, para revisar los selectores:
    python diagnosticar.py bpc "laptop" --guardar-html

COMO LEER LA SALIDA
───────────────────
  OK          el producto llega a la pantalla del vendedor
  DESCARTADO  el producto se cae, y a la derecha dice por que

Los motivos de descarte mas comunes y lo que significan:

  sin_precio_visible   el proveedor no mostro precio. Suele querer decir que
                       hay que iniciar sesion para verlo.
  es un accesorio      el nombre empieza por un accesorio (cable, soporte...)
                       cuando se pidio el producto principal.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from config import config
from matcher import Matcher
from modelo import ProductoSolicitado


# ---------------------------------------------------------------------
def _cargar_scrapers() -> dict:
    """SCRAPERS_DISPONIBLES vive en main.py; lo tomamos de ahi."""
    from main import SCRAPERS_DISPONIBLES

    return SCRAPERS_DISPONIBLES


def _interpretar(texto: str, usar_ia: bool) -> ProductoSolicitado:
    """
    Reproduce exactamente lo que hace worker.py, para que lo que veas aca sea
    lo que pasa en produccion:

      · La IA EXTRAE marca, linea, modelo y specs (alimenta al matcher).
      · El CODIGO escribe la query con limpiar_query(), a partir del texto
        del vendedor.

    Con --sin-ia se salta la extraccion, para poder comparar cuanto aporta.
    """
    from matcher import limpiar_query

    if not usar_ia:
        return ProductoSolicitado(query_sugerida=limpiar_query(texto))

    from ia import interpretar_solicitud

    productos = interpretar_solicitud(texto)
    if not productos:
        print("  ⚠ La IA no devolvio nada. Uso el texto tal cual.")
        return ProductoSolicitado(query_sugerida=limpiar_query(texto))

    p = productos[0]
    p.query_sugerida = limpiar_query(texto)
    return p


def _correr_scraper(codigo: str, ScraperCls, variantes, ver_navegador: bool):
    """
    Corre UN scraper, en un solo hilo, sin buffers ni redirecciones de salida.
    Asi todo lo que imprima el scraper se ve en vivo — que es justo lo que
    necesitas cuando algo falla a mitad de camino.

    `variantes` es la lista de queries en cascada, igual que en produccion: se
    usa _buscar_en_cascada() de main.py para que lo que veas aca sea exactamente
    lo que hace el worker. Con una sola variante se comporta como una busqueda
    simple.
    """
    import sys

    from main import _buscar_en_cascada

    if isinstance(variantes, str):
        variantes = [variantes]

    headless = config.scraper_headless and not ver_navegador

    if ScraperCls.usa_browser_compartido:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=headless,
                slow_mo=100 if not headless else 0,
            )
            try:
                with ScraperCls(browser) as scraper:
                    return _buscar_en_cascada(scraper, codigo, variantes, sys.stdout)
            finally:
                browser.close()
    else:
        # Autonomo (amazon/patchright): se provisiona su propio navegador.
        with ScraperCls(browser=None) as scraper:
            return _buscar_en_cascada(scraper, codigo, variantes, sys.stdout)


def _precio_str(p) -> str:
    if p.precio is None:
        return "SIN PRECIO"
    moneda = p.moneda or "?"
    return f"{moneda} {float(p.precio):,.2f}"


# ---------------------------------------------------------------------
def diagnosticar(
    codigo: str,
    texto: str,
    usar_ia: bool = True,
    ver_navegador: bool = False,
    guardar_html: bool = False,
) -> dict:
    scrapers = _cargar_scrapers()
    if codigo not in scrapers:
        print(f"✗ '{codigo}' no esta en SCRAPERS_DISPONIBLES.")
        print(f"  Implementados: {', '.join(sorted(scrapers))}")
        return {}

    print("=" * 78)
    print(f"PROVEEDOR: {codigo}")
    print(f"TEXTO DEL VENDEDOR: {texto!r}")
    print("=" * 78)

    solicitado = _interpretar(texto, usar_ia)
    query = solicitado.query_sugerida

    print(f"\nQuery que se manda al buscador: {query!r}")
    if usar_ia:
        print(
            f"La IA extrajo → categoria={solicitado.categoria} "
            f"marca={solicitado.marca} linea={solicitado.linea} "
            f"modelo={solicitado.modelo} specs={solicitado.specs}"
        )
        print(
            "  (la query la arma limpiar_query() con el texto del vendedor; "
            "la IA solo extrae)"
        )

    from matcher import variantes_query

    variantes = variantes_query(texto, solicitado)
    if len(variantes) > 1:
        print(f"Variantes en cascada: {variantes}")

    print("\nCorriendo el scraper...")
    resultado = _correr_scraper(codigo, scrapers[codigo], variantes, ver_navegador)

    if not resultado.exitoso:
        print(f"\n✗ EL SCRAPER FALLO en {resultado.duracion_ms} ms")
        print(f"  {(resultado.error or '').strip()[:800]}")
        return {"crudos": 0, "ok": 0}

    crudos = resultado.productos
    print(f"\n✓ El scraper trajo {len(crudos)} productos en {resultado.duracion_ms} ms")

    if guardar_html:
        print("  (--guardar-html: revisa la carpeta debug/ del scraper, si la usa)")

    if not crudos:
        print("\nNo hay nada que analizar: el proveedor devolvio cero resultados.")
        print("Cosas que vale revisar en este caso:")
        print("  · ¿La query es demasiado especifica? Prueba con una palabra sola.")
        print("  · ¿El sitio pide iniciar sesion para buscar?")
        print("  · Corre con --ver para mirar que pasa en la pagina.")
        return {"crudos": 0, "ok": 0}

    # ── Veredicto del matcher ──
    matcher = Matcher()
    resultados = matcher.calcular_todos(solicitado, crudos)

    sin_precio = sum(1 for p in crudos if p.precio is None)
    con_imagen = sum(1 for p in crudos if p.url_imagen)
    con_marca = sum(1 for p in crudos if p.marca)

    print(f"\n{'-' * 78}")
    print("LO QUE TRAJO EL SCRAPER (antes del matcher)")
    print(f"{'-' * 78}")
    print(f"  productos .............. {len(crudos)}")
    print(f"  con precio ............. {len(crudos) - sin_precio}")
    print(
        f"  SIN precio ............. {sin_precio}"
        + ("   ← estos no se pueden cotizar" if sin_precio else "")
    )
    print(f"  con imagen ............. {con_imagen}")
    print(f"  con marca extraida ..... {con_marca}")

    print(f"\n{'-' * 78}")
    print("VEREDICTO PRODUCTO POR PRODUCTO")
    print(f"{'-' * 78}")

    ok = 0
    motivos: dict[str, int] = {}
    for i, r in enumerate(resultados, 1):
        if r.descartado:
            etiqueta = "DESCARTADO"
            nota = r.razon_descarte or ""
            motivos[nota] = motivos.get(nota, 0) + 1
        else:
            etiqueta = "OK"
            ok += 1
            nota = f"{r.tier} {r.score_total:.0f}"
            if r.palabras_faltantes:
                nota += f"  faltan: {', '.join(r.palabras_faltantes)}"
            if r.etiquetas_variante:
                nota += f"  [{', '.join(r.etiquetas_variante)}]"

        print(
            f"{i:3d}. {etiqueta:10s} {_precio_str(r.producto):>16s}  {r.producto.nombre[:46]:46s}  {nota}"
        )

    print(f"\n{'-' * 78}")
    print(f"RESUMEN: de {len(crudos)} productos, {ok} llegarian a la pantalla")
    print(f"{'-' * 78}")
    if motivos:
        for motivo, n in sorted(motivos.items(), key=lambda x: -x[1]):
            print(f"  {n:3d} descartados por: {motivo}")

    if sin_precio == len(crudos):
        print(
            "\n⚠ NINGUN producto trae precio. Este proveedor casi seguro los\n"
            "  esconde detras del login. Mientras siga asi, no aporta nada a una\n"
            "  cotizacion: o se resuelve la sesion, o conviene apagarlo."
        )

    return {"crudos": len(crudos), "ok": ok}


# ---------------------------------------------------------------------
def probar_cascada(codigo: str, texto: str, usar_ia: bool, ver_navegador: bool) -> None:
    """
    Prueba la query completa y sus versiones mas cortas en el MISMO proveedor,
    para ver cuantas palabras tolera su buscador antes de quedarse en cero.

    De aca sale el dato para decidir la busqueda en cascada: si "laptop" trae 20
    y "laptop dell 16gb ram" trae 3, conviene reintentar con menos palabras
    cuando la primera query no rinde.
    """
    from matcher import variantes_query

    scrapers = _cargar_scrapers()
    if codigo not in scrapers:
        print(f"✗ '{codigo}' no esta en SCRAPERS_DISPONIBLES.")
        print(f"  Implementados: {', '.join(sorted(scrapers))}")
        return

    solicitado = _interpretar(texto, usar_ia)
    variantes = variantes_query(texto, solicitado)

    print("=" * 78)
    print(f"CASCADA DE BUSQUEDA — {codigo}")
    print(f"TEXTO DEL VENDEDOR: {texto!r}")
    print("=" * 78)
    print(
        f"\nSe van a probar {len(variantes)} variantes, de mas especifica a mas general:"
    )
    for i, v in enumerate(variantes, 1):
        print(f"  {i}. {v!r}")

    resultados: list[tuple[str, int, int, int]] = []
    for i, variante in enumerate(variantes, 1):
        print(f"\n{'-' * 78}")
        print(f"[{i}/{len(variantes)}] Buscando {variante!r}")
        print(f"{'-' * 78}")

        prueba = ProductoSolicitado(
            categoria=solicitado.categoria,
            marca=solicitado.marca,
            linea=solicitado.linea,
            modelo=solicitado.modelo,
            specs=solicitado.specs,
            cantidad=solicitado.cantidad,
            query_sugerida=variante,
        )

        try:
            resultado = _correr_scraper(
                codigo, scrapers[codigo], [variante], ver_navegador
            )
        except Exception as e:
            print(f"  ✗ revento: {e}")
            resultados.append((variante, 0, 0, 0))
            continue

        if not resultado.exitoso:
            print(f"  ✗ fallo: {(resultado.error or '').strip()[:200]}")
            resultados.append((variante, 0, 0, resultado.duracion_ms))
            continue

        crudos = resultado.productos
        matches = Matcher().calcular_todos(prueba, crudos)
        ok = sum(1 for m in matches if not m.descartado)
        print(
            f"  ✓ {len(crudos)} crudos → {ok} llegarian a la pantalla ({resultado.duracion_ms} ms)"
        )
        for m in matches[:3]:
            if not m.descartado:
                print(f"      · {m.tier:8s} {m.producto.nombre[:58]}")
        resultados.append((variante, len(crudos), ok, resultado.duracion_ms))

    print(f"\n{'=' * 78}")
    print(f"RESUMEN DE LA CASCADA — {codigo}")
    print(f"{'=' * 78}")
    print(f"  {'query':<42}{'crudos':>8}{'a pantalla':>13}{'ms':>9}")
    for variante, crudos, ok, ms in resultados:
        print(f"  {variante[:40]:<42}{crudos:>8}{ok:>13}{ms:>9}")

    con_resultados = [r for r in resultados if r[2] > 0]
    if not con_resultados:
        print("\n  Ninguna variante trajo nada. Este proveedor no tiene el producto,")
        print("  o el scraper tiene un problema aparte de la query.")
    elif con_resultados[0][0] == resultados[0][0]:
        print("\n  La query completa ya rinde: la cascada no aporta en este caso.")
    else:
        mejor = max(con_resultados, key=lambda r: r[2])
        print(
            f"\n  La query completa no rinde, pero {mejor[0]!r} trae {mejor[2]} productos."
        )
        print("  Acá la cascada SÍ aporta: vale reintentar con menos palabras.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnostica un proveedor del cotizador sin tocar la base de datos"
    )
    parser.add_argument("proveedor", help="codigo del proveedor, o 'todos'")
    parser.add_argument("texto", help="lo que escribiria el vendedor")
    parser.add_argument(
        "--cascada",
        action="store_true",
        help="prueba la query completa y sus versiones mas cortas, y compara",
    )
    parser.add_argument(
        "--sin-ia",
        action="store_true",
        help="manda el texto literal al buscador, sin pasar por la IA",
    )
    parser.add_argument(
        "--ver",
        action="store_true",
        help="muestra el navegador (para ver donde se traba un scraper)",
    )
    parser.add_argument(
        "--guardar-html",
        action="store_true",
        help="pide al scraper que deje el HTML en debug/, si lo soporta",
    )
    args = parser.parse_args()

    if args.cascada:
        probar_cascada(
            args.proveedor,
            args.texto,
            usar_ia=not args.sin_ia,
            ver_navegador=args.ver,
        )
        return 0

    scrapers = _cargar_scrapers()
    codigos = sorted(scrapers) if args.proveedor == "todos" else [args.proveedor]

    totales: dict[str, dict] = {}
    for codigo in codigos:
        try:
            totales[codigo] = diagnosticar(
                codigo,
                args.texto,
                usar_ia=not args.sin_ia,
                ver_navegador=args.ver,
                guardar_html=args.guardar_html,
            )
        except KeyboardInterrupt:
            print("\n⏹ Interrumpido.")
            break
        except Exception as e:
            import traceback

            print(f"\n✗ {codigo} revento fuera del scraper: {e}")
            traceback.print_exc()
            totales[codigo] = {"crudos": 0, "ok": 0}
        print()

    if len(totales) > 1:
        print("=" * 78)
        print("COMPARATIVA")
        print("=" * 78)
        print(f"  {'proveedor':<16}{'crudos':>8}{'a pantalla':>13}")
        for codigo, t in totales.items():
            print(f"  {codigo:<16}{t.get('crudos', 0):>8}{t.get('ok', 0):>13}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
