"""
Punto de entrada del RPA Cotizador SX.

En producción el que manda es worker.py: consume la cola cotizador.trabajos y
llama a las funciones de este archivo. main.py se conserva para el modo CLI
(desarrollo y pruebas puntuales) y porque aquí viven las piezas compartidas:
interpretar_y_armar_query(), ejecutar_scrapers() y procesar_item().

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
import io
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from dataformas import DataformasScraper
import db
from config import config
from ia import interpretar_solicitud
from intelec import IntelecScraper
from amazon import AmazonScraper
from bpc import BPCScraper
from compunomicos import CompunomicosScraper
from cococo import CococoScraper
from eurocomp import EurocompScraper
from intcomex import IntcomexScraper
from cdc import CDCScraper
from matcher import Matcher, limpiar_query
from modelo import ProductoSolicitado, ResultadoScraper

# ---------------------------------------------------------------------
# Registro de scrapers disponibles (código → clase)
# Que se ejecuten o no lo decide la DB (proveedores.activo)
# ---------------------------------------------------------------------
SCRAPERS_DISPONIBLES = {
    # "intelec": IntelecScraper,  # revisar mejor porque salio verificacion de no ser robot de cloudflare
    "amazon": AmazonScraper,
    "bpc": BPCScraper,
    "compunomicos": CompunomicosScraper,  # este creo que lo que vi iba producto a producto
    "eurocomp": EurocompScraper,
    # "intcomex": IntcomexScraper,  # pidio verificacion de dos pasos dificil a automatizar probar en el vps por un tema de ip
    # "cococo": CococoScraper,  # pagina rara
    # "dataformas": DataformasScraper,  # pidio restablecer la contraseña
    "cdc": CDCScraper,
}

# Umbral del matcher. Vive acá para que el worker y el modo CLI usen el mismo.
UMBRAL_MATCHER = 45.0

# Tiers que se persisten. 'dudoso' se guarda a propósito: son resultados que el
# proveedor consideró relevantes y que el vendedor puede querer mirar. Lo que NO
# se guarda es lo que el matcher descarta (sin precio, accesorios, relleno).
TIERS_A_GUARDAR = ("exacto", "bueno", "similar", "dudoso")


# ---------------------------------------------------------------------
# INTERPRETACIÓN: la IA extrae, el código arma la query
# ---------------------------------------------------------------------
def interpretar_y_armar_query(texto: str) -> list[ProductoSolicitado]:
    """
    Convierte el texto del vendedor en uno o más ProductoSolicitado listos
    para buscar. Es la ÚNICA puerta de entrada a la IA: la usan tanto el worker
    como el modo CLI, para que los dos se comporten igual.

    División de trabajo, y es lo importante de esta función:

      · La IA EXTRAE: marca, línea, modelo, specs, cantidad. Eso es lo que
        ningún buscador puede hacer con un correo de un cliente, y es lo que
        le permite al matcher distinguir un LG de 16" de un ARZOPA de 16".
      · El CÓDIGO escribe la query, con limpiar_query(), a partir del texto
        del vendedor y nada más.

    Antes la query la escribía la IA y le agregaba palabras que el catálogo del
    proveedor no usa: "laptop" se volvía "laptop portatil" y Eurocomp pasaba de
    devolver laptops a devolver parlantes; "calculadora" se volvía "calculadora
    electronica" y Amazon devolvía cero. Los buscadores de e-commerce funcionan
    con AND: cada palabra de más achica el resultado.

    Si la IA falla del todo (modelo caído, sin cuota, texto raro), se busca el
    texto tal cual: el vendedor SIEMPRE tiene que ver algo.
    """
    productos = interpretar_solicitud(texto)

    if not productos:
        print("  ⚠ La IA no devolvió productos. Uso el texto tal cual como búsqueda.")
        return [
            ProductoSolicitado(
                categoria=None,
                marca=None,
                linea=None,
                modelo=None,
                specs={},
                cantidad=1,
                query_sugerida=limpiar_query(texto),
            )
        ]

    if len(productos) == 1:
        # Un solo producto: la query es lo que escribió el vendedor, limpio.
        # Lo que la IA entendió se conserva en marca/modelo/specs, que es donde
        # realmente sirve.
        productos[0].query_sugerida = limpiar_query(texto)
    else:
        # Varios productos en un mismo renglón ("5 laptops y 3 monitores"):
        # acá sí hace falta la separación que hizo la IA, pero igual se limpia.
        for p in productos:
            p.query_sugerida = limpiar_query(p.query_sugerida or texto)

    return productos


def ejecutar_scrapers_OLD(
    query: str, codigos_activos: list[str]
) -> list[ResultadoScraper]:
    """Corre los scrapers indicados con la query dada.

    Cada proveedor está TOTALMENTE aislado: si uno falla al abrir el navegador,
    al hacer login, al buscar o al cerrar, se registra el fallo y se continúa
    con el siguiente. Un proveedor caído NUNCA tumba a los demás.
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

    # Avisar en el log qué proveedores activos NO tienen scraper implementado
    sin_implementar = [c for c in codigos_activos if c not in SCRAPERS_DISPONIBLES]
    if sin_implementar:
        print(
            f"  ⚠ Activos en DB pero sin scraper implementado (se omiten): {sin_implementar}"
        )

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
                    resultados.append(
                        _correr_un_scraper_OLD(codigo, ScraperCls, query, browser)
                    )
            finally:
                browser.close()

    # ---- Tanda 2: scrapers autonomos (fuera del sync_playwright) ----
    for codigo, ScraperCls in autonomos:
        resultados.append(
            _correr_un_scraper_OLD(codigo, ScraperCls, query, browser=None)
        )
    return resultados


def ejecutar_scrapers(query: str, codigos_activos: list[str]) -> list[ResultadoScraper]:
    """Corre los scrapers EN PARALELO, en tandas de hasta MAX_PARALELO a la vez.

    Cada scraper abre su PROPIO navegador dentro de su PROPIO hilo. Nada se
    comparte entre ellos, así que un scraper no interfiere con otro ni con el
    aislamiento de errores. Con 8 activos y MAX_PARALELO=3, corren 3+3+2.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Cuántos navegadores a la vez. Por defecto 1: con 3 simultáneos, BPC,
    # Eurocomp y CDC fallaban por timeout y parecía problema de los sitios,
    # cuando en realidad era saturación de la máquina. Se puede subir por
    # SCRAPER_MAX_PARALELO en el .env si el VPS lo aguanta — pero súbelo de a
    # uno y comprobando el log, no a ciegas.
    MAX_PARALELO = int(os.getenv("SCRAPER_MAX_PARALELO", "1"))

    resultados: list[ResultadoScraper] = []

    a_correr = [
        (c, SCRAPERS_DISPONIBLES[c])
        for c in codigos_activos
        if c in SCRAPERS_DISPONIBLES
    ]

    if not a_correr:
        print("  ⚠ Sin scrapers implementados activos")
        return resultados

    # Avisar qué proveedores activos NO tienen scraper implementado
    sin_implementar = [c for c in codigos_activos if c not in SCRAPERS_DISPONIBLES]
    if sin_implementar:
        print(
            f"  ⚠ Activos en DB pero sin scraper implementado (se omiten): {sin_implementar}"
        )

    print(
        f"  Corriendo {len(a_correr)} scrapers en paralelo (máx {MAX_PARALELO} a la vez)..."
    )

    with ThreadPoolExecutor(max_workers=MAX_PARALELO) as pool:
        futuros = {
            pool.submit(_correr_un_scraper, codigo, ScraperCls, query): codigo
            for codigo, ScraperCls in a_correr
        }
        for futuro in as_completed(futuros):
            resultados.append(futuro.result())

    return resultados


def _correr_un_scraper_OLD(codigo, ScraperCls, query, browser) -> ResultadoScraper:
    """
    Corre UN scraper de punta a punta con aislamiento total.

    Envuelve el ciclo de vida completo (abrir contexto + login + buscar + cerrar)
    en un try/except. Cualquier fallo se convierte en un ResultadoScraper fallido
    — nunca se propaga ni tumba a los demás proveedores.
    """
    import time
    import traceback

    print(f"\n▶ Ejecutando scraper: {codigo}")
    inicio = time.perf_counter()
    try:
        with ScraperCls(browser) as scraper:
            resultado = scraper.ejecutar(query)
        _log_resultado(resultado)
        return resultado
    except Exception as e:
        # Falló fuera de buscar(): típicamente al abrir el navegador o en el
        # login (Intelec sin password, MFA de Intcomex, Dataformas, etc.).
        duracion = int((time.perf_counter() - inicio) * 1000)
        error_full = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        print(f"  ✗ [{codigo}] Falló al iniciar/cerrar ({duracion} ms): {e}")
        return ResultadoScraper(
            proveedor=codigo,
            query=query,
            exitoso=False,
            productos=[],
            duracion_ms=duracion,
            error=error_full,
        )


def _correr_un_scraper(codigo, ScraperCls, query) -> ResultadoScraper:
    """
    Corre UN scraper de punta a punta, con su PROPIO navegador, aislado.

    Abre su propio sync_playwright (cada hilo el suyo — Playwright sync no se
    puede compartir entre hilos). Cualquier fallo (abrir navegador, login,
    buscar, cerrar) se convierte en un ResultadoScraper fallido; nunca se
    propaga ni tumba a los demás.

    Para que el log sea legible pese al paralelismo, este scraper acumula sus
    mensajes y los imprime todos juntos al final, en un bloque.

    IMPORTANTE: la captura usa log_paralelo.capturar_salida_del_hilo() y NO
    contextlib.redirect_stdout. redirect_stdout cambia sys.stdout globalmente,
    así que con varios hilos los buffers se pisan y sys.stdout termina
    apuntando a un buffer descartado: desde ahí el proceso sigue trabajando
    pero deja de loguear, para siempre. Con el worker, que vive horas, eso
    significaba quedarse ciego a mitad del trabajo. Ver log_paralelo.py.
    """
    import time
    import traceback

    from log_paralelo import capturar_salida_del_hilo

    inicio = time.perf_counter()
    resultado: ResultadoScraper | None = None

    with capturar_salida_del_hilo() as buffer:
        try:
            # Patchright (amazon) ignora el browser que reciba; los demás
            # abren su propio Chromium aquí. Cada hilo, su propio Playwright.
            if ScraperCls.usa_browser_compartido:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(
                        headless=config.scraper_headless,
                        slow_mo=100 if not config.scraper_headless else 0,
                    )
                    try:
                        with ScraperCls(browser) as scraper:
                            resultado = scraper.ejecutar(query)
                    finally:
                        browser.close()
            else:
                # Autónomo (amazon/patchright): se provisiona su propio browser
                with ScraperCls(browser=None) as scraper:
                    resultado = scraper.ejecutar(query)

            _log_resultado_en(buffer, resultado)

        except Exception as e:
            duracion = int((time.perf_counter() - inicio) * 1000)
            error_full = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            print(
                f"  ✗ [{codigo}] Falló al iniciar/cerrar ({duracion} ms): {e}",
                file=buffer,
            )
            resultado = ResultadoScraper(
                proveedor=codigo,
                query=query,
                exitoso=False,
                productos=[],
                duracion_ms=duracion,
                error=error_full,
            )

    # Ya fuera de la captura: la salida de este hilo volvió a la normalidad y el
    # bloque se imprime completo, de una sola vez.
    print(f"\n▶ {codigo}")
    print(buffer.getvalue(), end="")
    return resultado


def _log_resultado_en(buffer, resultado: ResultadoScraper) -> None:
    """Como _log_resultado pero escribe en un buffer (para logs en paralelo)."""
    if resultado.exitoso:
        print(
            f"  ✓ {len(resultado.productos)} productos ({resultado.duracion_ms} ms)",
            file=buffer,
        )
    else:
        err = resultado.error[:100] if resultado.error else ""
        print(f"  ✗ Fallo ({resultado.duracion_ms} ms): {err}", file=buffer)


##ojo esta quedo para las _old por si acaso
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

    El log explica POR QUÉ se descartó cada producto que no llegó a la pantalla.
    Sin ese desglose, un proveedor que trae 20 productos y guarda 0 se ve igual
    que uno que no trajo nada, y no hay manera de saber si el problema es el
    precio, la blacklist, el matcher o el scraper.
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

        if not r.exitoso:
            continue
        if not r.productos:
            print(f"    [{r.proveedor}] 0 crudos — el scraper no encontró nada")
            continue

        # 2. Aplicar matcher
        matches_todos = matcher.calcular_todos(solicitado, r.productos)

        # 3. Contar POR QUÉ se cae cada producto, antes de guardar
        motivos = Counter()
        for m in matches_todos:
            if m.descartado:
                motivos[m.razon_descarte or "descartado"] += 1
            elif m.tier not in TIERS_A_GUARDAR:
                motivos[f"tier {m.tier} no se guarda"] += 1

        # 4. Persistir
        productos_con_match = [m.to_dict() for m in matches_todos]
        guardados = db.guardar_productos_encontrados(
            solicitud_item_id=item_id,
            scraper_ejecucion_id=ejecucion_id,
            productos_con_match=productos_con_match,
            tiers_a_guardar=TIERS_A_GUARDAR,
            intento_numero=intento_numero,
        )
        db.actualizar_total_guardados(ejecucion_id, guardados)

        # 5. Log: tiers de lo que entró + motivos de lo que no
        tiers = Counter(m.tier for m in matches_todos if not m.descartado)
        print(
            f"    [{r.proveedor}] {len(r.productos)} crudos → "
            f"guardados {guardados} "
            f"(exacto:{tiers.get('exacto', 0)} "
            f"bueno:{tiers.get('bueno', 0)} "
            f"similar:{tiers.get('similar', 0)} "
            f"dudoso:{tiers.get('dudoso', 0)})"
        )

        perdidos = len(r.productos) - guardados
        if perdidos > 0:
            detalle = ", ".join(f"{n} {motivo}" for motivo, n in motivos.most_common())
            print(
                f"      ↳ {perdidos} no llegaron a la pantalla: "
                f"{detalle or 'sin motivo registrado'}"
            )
            # Los primeros descartados, con nombre, para poder juzgar a ojo si
            # el descarte fue correcto o si el filtro está de más.
            for m in [m for m in matches_todos if m.descartado][:3]:
                print(f"         · [{m.razon_descarte}] {m.producto.nombre[:70]}")


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
        db.actualizar_estado_solicitud(
            solicitud_id,
            "normalizando",
            mensaje_progreso="Interpretando producto(s) con IA...",
        )
        print("\n[1/3] Interpretando con IA...")

        # La IA extrae marca/modelo/specs; la query la arma limpiar_query().
        productos_solicitados = interpretar_y_armar_query(texto_original)

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
        codigos_activos = db.listar_proveedores_para_solicitud(solicitud_id)

        db.actualizar_estado_solicitud(
            solicitud_id,
            "scrapeando",
            mensaje_progreso=f"Consultando {len(codigos_activos)} proveedores para {len(productos_solicitados)} producto(s)...",
        )
        print(f"\n[2/3] Corriendo scrapers...")
        print(f"  Proveedores (según preferencia del usuario): {codigos_activos}")

        if not codigos_activos:
            raise ValueError("No hay proveedores activos configurados para buscar.")

        matcher = Matcher(umbral_minimo=UMBRAL_MATCHER)

        for item_id, solicitado in zip(item_ids, productos_solicitados):
            procesar_item(
                item_id=item_id,
                solicitado=solicitado,
                matcher=matcher,
                codigos_activos=codigos_activos,
                intento_numero=1,
            )

        # ---- Cierre ----
        db.actualizar_estado_solicitud(
            solicitud_id, "lista", mensaje_progreso="Búsqueda completada"
        )

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
        db.actualizar_estado_solicitud(
            solicitud_id,
            "scrapeando",
            mensaje_progreso="Reintentando búsqueda con nuevo texto...",
        )

        # 3. Guardar el texto libre que el vendedor escribió
        db.guardar_texto_reinterpretacion(item_id, nuevo_texto)

        # 4. Reinterpretar con IA (la query la arma limpiar_query)
        print("\n[1/3] Reinterpretando con IA...")
        productos_solicitados = interpretar_y_armar_query(nuevo_texto)

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

        # 6. Correr scrapers (según la preferencia del usuario de la solicitud)
        codigos_activos = db.listar_proveedores_para_solicitud(solicitud_id)
        print(f"  Proveedores (según preferencia del usuario): {codigos_activos}")

        if not codigos_activos:
            raise ValueError("No hay proveedores activos configurados para buscar.")

        matcher = Matcher(umbral_minimo=UMBRAL_MATCHER)

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
        db.actualizar_estado_solicitud(
            solicitud_id, "lista", mensaje_progreso="Reintento completado"
        )

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


def procesar_agregar_busqueda(solicitud_id: str, texto: str) -> dict:
    """
    Modo 4: agrega UNO o MÁS renglones nuevos a una solicitud YA existente.

    A diferencia del reintento (que corrige UN item), esto SUMA items a la
    solicitud sin tocar los que ya estaban. Cada item nuevo arranca en intento #1.
    """
    print(f"\n{'='*70}")
    print(f"AGREGANDO BÚSQUEDA A SOLICITUD: {solicitud_id}")
    print(f"TEXTO NUEVO: {texto}")
    print("=" * 70)

    # Validar que la solicitud exista antes de tocar nada
    solicitud = db.obtener_solicitud(solicitud_id)
    if not solicitud:
        raise ValueError(f"Solicitud {solicitud_id} no existe")

    resumen: dict = {
        "solicitud_id": solicitud_id,
        "texto_agregado": texto,
        "items_nuevos": [],
    }

    try:
        # Marcar scrapeando (para que el polling del frontend despierte)
        db.actualizar_estado_solicitud(
            solicitud_id,
            "scrapeando",
            mensaje_progreso="Agregando producto y buscando en proveedores...",
        )

        # Interpretar con IA (la query la arma limpiar_query)
        print("\n[1/3] Interpretando con IA...")
        productos_solicitados = interpretar_y_armar_query(texto)

        print(f"  ✓ Se agregarán {len(productos_solicitados)} renglón(es):")
        for i, p in enumerate(productos_solicitados, 1):
            print(
                f"    {i}. [{p.categoria}] {p.marca or ''} {p.linea or ''} "
                f"{p.modelo or ''} — query: '{p.query_sugerida}' (x{p.cantidad})"
            )

        # Crear items NUEVOS: su orden continúa después de los existentes
        orden_inicial = db.siguiente_orden_item(solicitud_id)
        item_ids = db.guardar_items(
            solicitud_id,
            [p.to_dict() for p in productos_solicitados],
            orden_inicial=orden_inicial,
        )
        resumen["items_nuevos"] = item_ids

        # Scrapers + matcher + persistencia para cada item nuevo
        print(f"\n[2/3] Corriendo scrapers para los renglones nuevos...")
        codigos_activos = db.listar_proveedores_para_solicitud(solicitud_id)
        print(f"  Proveedores (según preferencia del usuario): {codigos_activos}")

        if not codigos_activos:
            raise ValueError("No hay proveedores activos configurados para buscar.")

        matcher = Matcher(umbral_minimo=UMBRAL_MATCHER)

        for item_id, solicitado in zip(item_ids, productos_solicitados):
            procesar_item(
                item_id=item_id,
                solicitado=solicitado,
                matcher=matcher,
                codigos_activos=codigos_activos,
                intento_numero=1,
            )

        # Cerrar
        db.actualizar_estado_solicitud(
            solicitud_id, "lista", mensaje_progreso="Producto agregado"
        )

        print(f"\n{'='*70}")
        print(f"BÚSQUEDA AGREGADA — {len(item_ids)} renglón(es) nuevo(s)")
        print("=" * 70)

        return resumen

    except Exception as e:
        print(f"\n✗ ERROR al agregar búsqueda: {e}")
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

    # Modo 4: agregar búsqueda(s) a una solicitud existente
    parser.add_argument(
        "--agregar-busqueda",
        help="UUID de una solicitud existente a la que se le suman renglones nuevos",
    )
    parser.add_argument(
        "--texto",
        help="Texto libre de la nueva búsqueda (obligatorio con --agregar-busqueda)",
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

    if args.agregar_busqueda:
        # MODO 4: agregar renglones a una solicitud existente
        if not args.texto:
            print("✗ --agregar-busqueda requiere también --texto")
            return 1
        print(f"Modo: agregar búsqueda a la solicitud {args.agregar_busqueda}")
        resumen = procesar_agregar_busqueda(args.agregar_busqueda, args.texto)

    elif args.reintentar_item:
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
    from logger import configurar_logging

    configurar_logging()
    sys.exit(main())
