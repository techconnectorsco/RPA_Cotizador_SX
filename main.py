"""
Punto de entrada del RPA Cotizador SX.

Tres modos de uso:

  Modo CLI (desarrollo/testing):
    python main.py "mouse logitech"
    python main.py "necesito 5 laptops lenovo 16gb"
    python main.py --guardar-json "mouse logitech"

  Modo invocado por SvelteKit (producción, solicitud nueva):
    python main.py --solicitud-id <uuid>
    (SvelteKit ya creó la solicitud en la DB, Python solo la procesa)

  Modo reintento (producción, item ya existente):
    python main.py --reintentar-item <uuid-item> --nuevo-texto "texto libre"
    (Reprocesa UN item con un texto nuevo, generando un intento_numero + 1.
     Los productos y ejecuciones del intento anterior quedan intactos.)

Flujo interno (modo 1 y 2):
  1. Interpretar con IA
  2. Ejecutar scrapers activos (según cotizador.proveedores.activo)
  3. Aplicar matcher con tiers
  4. Persistir todo en cotizador.* (schema)
  5. Marcar solicitud como 'lista'

Flujo interno (modo 3 — reintento):
  1. Localizar item + solicitud padre
  2. Marcar solicitud como 'scrapeando' (para el polling del frontend)
  3. Reinterpretar texto libre con IA (o fallback)
  4. Calcular siguiente intento_numero
  5. Ejecutar scrapers + matcher para ESE item
  6. Persistir con nuevo intento_numero
  7. Actualizar intento_actual del item
  8. Marcar solicitud como 'lista'
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import db
from config import config
from ia import interpretar_solicitud
from intelec import IntelecScraper
from amazon import AmazonScraper
from matcher import Matcher
from modelo import ProductoSolicitado, ResultadoScraper

# ---------------------------------------------------------------------
# Registro de scrapers disponibles (código → clase)
# Que se ejecuten o no lo decide la DB (proveedores.activo)
# ---------------------------------------------------------------------
SCRAPERS_DISPONIBLES = {
    # "intelec": IntelecScraper,
    # "amazon": AmazonScraper,
    "eurocomp": EurocompScraper,  # pendiente credenciales
    # "intcomex": IntcomexScraper,  # pendiente credenciales
    # "cdc":      CDCScraper,       # pendiente aprobacion
}


def ejecutar_scrapers(query: str, codigos_activos: list[str]) -> list[ResultadoScraper]:
    """Corre los scrapers indicados con la query dada.

    Los scrapers se dividen en dos grupos:
      - Los que usan el browser Playwright compartido (default)
      - Los que se auto-provisionan su propio browser (ej: Amazon con Patchright)
    Estos ultimos DEBEN correr fuera del context de sync_playwright() para
    evitar el conflicto de loops entre Playwright y Patchright en el mismo proceso.
    """
    resultados: list[ResultadoScraper] = []

    a_correr = [
        (c, SCRAPERS_DISPONIBLES[c])
        for c in codigos_activos
        if c in SCRAPERS_DISPONIBLES
    ]

    if not a_correr:
        print("  ⚠ Sin scrapers implementados activos")
        return resultados

    # Separar por tipo
    compartidos = [(c, cls) for c, cls in a_correr if cls.usa_browser_compartido]
    autonomos = [(c, cls) for c, cls in a_correr if not cls.usa_browser_compartido]

    # ---- Tanda 1: scrapers con browser compartido ----
    if compartidos:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=config.scraper_headless,
                slow_mo=100 if not config.scraper_headless else 0,
            )
            try:
                for codigo, ScraperCls in compartidos:
                    print(f"\n▶ Ejecutando scraper: {codigo}")
                    with ScraperCls(browser) as scraper:
                        resultado = scraper.ejecutar(query)
                        resultados.append(resultado)
                        _log_resultado(resultado)
            finally:
                browser.close()

    # ---- Tanda 2: scrapers autonomos (fuera del sync_playwright) ----
    for codigo, ScraperCls in autonomos:
        print(f"\n▶ Ejecutando scraper: {codigo}")
        with ScraperCls(browser=None) as scraper:
            resultado = scraper.ejecutar(query)
            resultados.append(resultado)
            _log_resultado(resultado)

    return resultados


def _log_resultado(resultado: ResultadoScraper) -> None:
    if resultado.exitoso:
        print(
            f"  ✓ {len(resultado.productos)} productos " f"({resultado.duracion_ms} ms)"
        )
    else:
        err = resultado.error[:100] if resultado.error else ""
        print(f"  ✗ Fallo ({resultado.duracion_ms} ms): {err}")


def procesar_item(
    item_id: str,
    solicitado: ProductoSolicitado,
    matcher: Matcher,
    codigos_activos: list[str],
    intento_numero: int = 1,
) -> None:
    """
    Corre scrapers + matcher + persistencia para UN item.
    Aislado para poder reusarse en el flujo original y en el flujo de reintento.

    `intento_numero` marca a qué intento pertenecen las ejecuciones y productos
    guardados. Default = 1 mantiene compatibilidad con el flujo original.
    """
    query = solicitado.query_sugerida
    print(f"\n  → Item {item_id}  (intento #{intento_numero})  query: '{query}'")

    resultados_scrapers = ejecutar_scrapers(query, codigos_activos)

    for r in resultados_scrapers:
        # 1. Guardar la ejecución del scraper (siempre, exitosa o no)
        ejecucion_id = db.guardar_ejecucion_scraper(
            solicitud_item_id=item_id,
            proveedor_codigo=r.proveedor,
            query_usada=r.query,
            exitoso=r.exitoso,
            duracion_ms=r.duracion_ms,
            total_crudos=len(r.productos),
            error_mensaje=r.error,
            intento_numero=intento_numero,
        )

        if not r.exitoso or not r.productos:
            continue

        # 2. Aplicar matcher
        matches_todos = matcher.calcular_todos(solicitado, r.productos)

        # 3. Convertir a formato dict
        productos_con_match = [m.to_dict() for m in matches_todos]

        # 4. Persistir solo tiers exacto/bueno/similar
        guardados = db.guardar_productos_encontrados(
            solicitud_item_id=item_id,
            scraper_ejecucion_id=ejecucion_id,
            productos_con_match=productos_con_match,
            tiers_a_guardar=("exacto", "bueno", "similar"),
            intento_numero=intento_numero,
        )
        db.actualizar_total_guardados(ejecucion_id, guardados)

        # Contadores por tier para log
        tiers = Counter(m.tier for m in matches_todos if not m.descartado)
        print(
            f"    [{r.proveedor}] {len(r.productos)} crudos → "
            f"guardados {guardados} "
            f"(exacto:{tiers.get('exacto',0)} "
            f"bueno:{tiers.get('bueno',0)} "
            f"similar:{tiers.get('similar',0)} "
            f"dudoso:{tiers.get('dudoso',0)})"
        )


def procesar_solicitud(solicitud_id: str, texto_original: str) -> dict:
    """
    Pipeline completo para UNA solicitud (ya existente en DB).
    Actualiza el estado en cada fase y persiste todo.
    Devuelve el resumen final.
    """
    print(f"\n{'='*70}")
    print(f"PROCESANDO SOLICITUD: {solicitud_id}")
    print(f"TEXTO: {texto_original}")
    print("=" * 70)

    resumen = {
        "solicitud_id": solicitud_id,
        "texto_original": texto_original,
        "es_cotizacion": False,
        "items": [],
    }

    try:
        # ---- FASE 1: IA ----
        db.actualizar_estado_solicitud(solicitud_id, "normalizando")
        print("\n[1/3] Interpretando con IA...")

        productos_solicitados = interpretar_solicitud(texto_original)

        if not productos_solicitados:
            print("  ⚠ La IA determinó que NO es cotización.")
            db.guardar_interpretacion_ia(
                solicitud_id,
                es_cotizacion=False,
                interpretacion_json={"es_cotizacion": False, "productos": []},
                modelo_usado=config.gemini_model,
            )
            db.actualizar_estado_solicitud(solicitud_id, "cerrada")
            return resumen

        # Guardar interpretación completa
        interpretacion_json = {
            "es_cotizacion": True,
            "productos": [p.to_dict() for p in productos_solicitados],
        }
        db.guardar_interpretacion_ia(
            solicitud_id,
            es_cotizacion=True,
            interpretacion_json=interpretacion_json,
            modelo_usado=config.gemini_model,
        )
        resumen["es_cotizacion"] = True

        print(f"  ✓ IA detectó {len(productos_solicitados)} producto(s):")
        for i, p in enumerate(productos_solicitados, 1):
            print(
                f"    {i}. [{p.categoria}] {p.marca or ''} {p.linea or ''} "
                f"{p.modelo or ''} — query: '{p.query_sugerida}' (x{p.cantidad})"
            )

        # Guardar items y obtener sus IDs
        item_ids = db.guardar_items(
            solicitud_id,
            [p.to_dict() for p in productos_solicitados],
        )

        # ---- FASE 2 y 3: Scrapers + Matcher + persistencia ----
        db.actualizar_estado_solicitud(solicitud_id, "scrapeando")
        print(f"\n[2/3] Corriendo scrapers...")

        codigos_activos = db.listar_proveedores_activos()
        print(f"  Proveedores activos: {codigos_activos}")

        matcher = Matcher(umbral_minimo=55.0)

        for item_id, solicitado in zip(item_ids, productos_solicitados):
            procesar_item(
                item_id=item_id,
                solicitado=solicitado,
                matcher=matcher,
                codigos_activos=codigos_activos,
                intento_numero=1,
            )

        # ---- Cierre ----
        db.actualizar_estado_solicitud(solicitud_id, "lista")

        # Resumen final desde DB
        r = db.resumen_solicitud(solicitud_id)
        if r:
            print(f"\n{'='*70}")
            print("RESUMEN")
            print("=" * 70)
            print(f"  Estado:              {r['estado']}")
            print(f"  Items solicitados:   {r['total_items']}")
            print(f"  Productos guardados: {r['total_productos']}")
            print(f"    exactos:  {r['total_exactos']}")
            print(f"    buenos:   {r['total_buenos']}")

            resumen["items"] = r["total_items"]
            resumen["productos_guardados"] = r["total_productos"]
            resumen["exactos"] = r["total_exactos"]
            resumen["buenos"] = r["total_buenos"]

        return resumen

    except Exception as e:
        print(f"\n✗ ERROR en el pipeline: {e}")
        import traceback

        traceback.print_exc()
        db.actualizar_estado_solicitud(
            solicitud_id, "error", error_mensaje=str(e)[:1000]
        )
        raise


def procesar_reintento(item_id: str, nuevo_texto: str) -> dict:
    """
    Pipeline de reintento para UN item ya existente.
    Genera un nuevo intento_numero — los productos anteriores quedan intactos.
    """
    print(f"\n{'='*70}")
    print(f"REINTENTANDO ITEM: {item_id}")
    print(f"NUEVO TEXTO: {nuevo_texto}")
    print("=" * 70)

    # 1. Localizar item + solicitud padre
    item = db.obtener_item(item_id)
    if not item:
        raise ValueError(f"Item {item_id} no existe")
    solicitud_id = str(item["solicitud_id"])
    print(f"  Solicitud padre: {solicitud_id}")

    resumen: dict = {
        "solicitud_id": solicitud_id,
        "item_id": item_id,
        "nuevo_texto": nuevo_texto,
    }

    try:
        # 2. Marcar solicitud como scrapeando (para el polling del frontend)
        db.actualizar_estado_solicitud(solicitud_id, "scrapeando")

        # 3. Guardar el texto libre que el vendedor escribió
        db.guardar_texto_reinterpretacion(item_id, nuevo_texto)

        # 4. Reinterpretar con IA
        print("\n[1/3] Reinterpretando con IA...")
        productos_solicitados = interpretar_solicitud(nuevo_texto)

        # Si la IA no devolvió nada, hacemos fallback: usar el texto tal cual
        if not productos_solicitados:
            print("  ⚠ La IA no devolvió productos. Usando texto tal cual como query.")
            productos_solicitados = [
                ProductoSolicitado(
                    categoria=None,
                    marca=None,
                    linea=None,
                    modelo=None,
                    specs={},
                    cantidad=1,
                    query_sugerida=nuevo_texto.strip()[:120],
                )
            ]

        # Reintento = 1 item. Si la IA devolvió varios, nos quedamos con el primero
        solicitado = productos_solicitados[0]
        if len(productos_solicitados) > 1:
            print(
                f"  ⚠ IA devolvió {len(productos_solicitados)} productos, "
                f"tomo solo el primero para el reintento."
            )

        print(
            f"  ✓ Interpretado: [{solicitado.categoria}] "
            f"{solicitado.marca or ''} {solicitado.linea or ''} "
            f"{solicitado.modelo or ''} — query: '{solicitado.query_sugerida}'"
        )

        # 5. Calcular siguiente intento_numero
        intento_numero = db.siguiente_intento_numero(item_id)
        print(f"\n[2/3] Este será el intento #{intento_numero}")

        # 6. Correr scrapers
        codigos_activos = db.listar_proveedores_activos()
        print(f"  Proveedores activos: {codigos_activos}")

        matcher = Matcher(umbral_minimo=55.0)

        print(f"\n[3/3] Corriendo scrapers...")
        procesar_item(
            item_id=item_id,
            solicitado=solicitado,
            matcher=matcher,
            codigos_activos=codigos_activos,
            intento_numero=intento_numero,
        )

        # 7. Marcar este intento como el vigente
        db.actualizar_intento_actual(item_id, intento_numero)

        # 8. Cerrar solicitud
        db.actualizar_estado_solicitud(solicitud_id, "lista")

        resumen["intento_numero"] = intento_numero

        print(f"\n{'='*70}")
        print(f"REINTENTO COMPLETADO — intento #{intento_numero} guardado")
        print("=" * 70)

        return resumen

    except Exception as e:
        print(f"\n✗ ERROR en el reintento: {e}")
        import traceback

        traceback.print_exc()
        db.actualizar_estado_solicitud(
            solicitud_id, "error", error_mensaje=str(e)[:1000]
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="RPA Cotizador SX")

    # Modo CLI: texto libre
    parser.add_argument(
        "peticion",
        nargs="?",
        help="Texto de la petición (modo CLI/desarrollo)",
    )

    # Modo invocado por SvelteKit: solicitud ya en DB
    parser.add_argument(
        "--solicitud-id",
        help="UUID de una solicitud existente en DB (modo producción)",
    )

    # Modo reintento: item existente + texto nuevo
    parser.add_argument(
        "--reintentar-item",
        help="UUID de un solicitud_item para reintentar la búsqueda",
    )
    parser.add_argument(
        "--nuevo-texto",
        help="Texto libre para reinterpretar (obligatorio con --reintentar-item)",
    )

    # Otros
    parser.add_argument(
        "--canal",
        default="busqueda_rapida",
        choices=["formulario", "busqueda_rapida", "correo", "whatsapp_manual", "cron"],
        help="Canal de origen (solo modo CLI)",
    )
    parser.add_argument(
        "--guardar-json",
        action="store_true",
        help="Además de la DB, guarda copia del resumen en salidas/",
    )
    args = parser.parse_args()

    # --- Determinar modo ---
    if args.reintentar_item:
        # MODO 3: reintento de un item existente
        if not args.nuevo_texto:
            print("✗ --reintentar-item requiere también --nuevo-texto")
            return 1
        print(f"Modo: reintento del item {args.reintentar_item}")
        resumen = procesar_reintento(args.reintentar_item, args.nuevo_texto)

    elif args.solicitud_id:
        # MODO 2: procesar solicitud existente
        solicitud = db.obtener_solicitud(args.solicitud_id)
        if not solicitud:
            print(f"✗ Solicitud {args.solicitud_id} no encontrada")
            return 1
        solicitud_id = str(solicitud["id"])
        texto = solicitud["texto_original"]
        print(f"Modo: procesando solicitud existente {solicitud_id}")
        resumen = procesar_solicitud(solicitud_id, texto)

    elif args.peticion:
        # MODO 1: crear solicitud desde CLI
        print(f"Modo: creando solicitud nueva desde CLI")
        solicitud_id = db.crear_solicitud(
            texto_original=args.peticion,
            canal_origen=args.canal,
        )
        texto = args.peticion
        print(f"  Solicitud creada: {solicitud_id}")
        resumen = procesar_solicitud(solicitud_id, texto)

    else:
        parser.print_help()
        print("\n⚠ Debes pasar un texto, --solicitud-id o --reintentar-item")
        return 1

    # --- Guardar JSON opcional ---
    if args.guardar_json:
        Path("salidas").mkdir(exist_ok=True)
        fname = f"salidas/resumen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        Path(fname).write_text(
            json.dumps(resumen, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\n✓ Resumen guardado en {fname}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
