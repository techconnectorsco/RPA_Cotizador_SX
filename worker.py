"""
worker.py — Proceso que consume la cola del cotizador.

Qué hace, en una línea: se queda corriendo, toma de a UN trabajo de
cotizador.trabajos, lo procesa con el mismo pipeline que ya tenías
(IA → scrapers → matcher → persistencia) y pasa al siguiente.

Para qué sirve: reemplaza el disparo por HTTP (api.py + fetch desde SvelteKit).
Ahora SvelteKit solo inserta el trabajo en la base y responde al vendedor al
instante. El worker lo recoge por su cuenta. Las ventajas concretas:

  - Si tres usuarios mandan solicitudes a la vez, se encolan. Antes se abrían
    tres procesos con sus navegadores y el VPS se ahogaba.
  - El vendedor nunca espera: manda su lista y sigue trabajando.
  - Nada queda colgado. Si este proceso muere a mitad de un scraping, el
    watchdog devuelve el trabajo a la cola en el próximo arranque.
  - No hace falta exponer ningún puerto, ni ngrok, ni autenticar un endpoint.

Cómo se corre:

  Normal (es lo que va en pm2, se queda corriendo):
    python worker.py

  Una sola vuelta y salir (para probar que todo funciona):
    python worker.py --una-vez

  Solo rescatar colgados y salir (para destrabar a mano):
    python worker.py --solo-watchdog

Variables de entorno opcionales (ninguna es obligatoria):
  WORKER_POLL_S      segundos entre revisiones de la cola cuando está vacía (5)
  WORKER_TIMEOUT_MIN minutos sin latido para dar un trabajo por muerto (10)

OJO: Python lee los .py al arrancar el proceso. Si cambias matcher.py, main.py
o cualquier otro archivo del RPA, hay que REINICIAR el worker para que tome la
versión nueva (en pm2: pm2 restart <nombre>).
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import traceback

import db
import db_cola
from main import UMBRAL_MATCHER, interpretar_y_armar_query, procesar_item
from matcher import Matcher

# Segundos que el worker espera antes de volver a revisar la cola cuando no
# hay nada. Es solo la red de seguridad: con LISTEN/NOTIFY se despierta al
# instante en que entra un trabajo.
POLL_S = float(os.getenv("WORKER_POLL_S", "5"))

# Cada cuánto late el worker mientras procesa. Tiene que ser bastante menor
# que WORKER_TIMEOUT_MIN o el watchdog reencolaría trabajos que están vivos.
LATIDO_CADA_S = 20.0

# Lo levanta el manejador de señales para que el worker termine el trabajo que
# tiene en mano antes de salir, en lugar de cortarlo a la mitad.
_salir = threading.Event()


# =====================================================================
# LATIDO EN SEGUNDO PLANO
# =====================================================================
class Latido:
    """
    Mientras el bloque esté abierto, un hilo refresca heartbeat_en.

    Uso:
        with Latido(trabajo_id):
            ...trabajo largo...

    El hilo es daemon: si el proceso muere de golpe, el latido se detiene con
    él y el watchdog se da cuenta. Eso es exactamente lo que queremos.
    """

    def __init__(self, trabajo_id: str, cada_s: float = LATIDO_CADA_S):
        self._trabajo_id = trabajo_id
        self._cada_s = cada_s
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    def _bucle(self) -> None:
        while not self._parar.wait(self._cada_s):
            try:
                db_cola.latir(self._trabajo_id)
            except Exception as e:
                # Un fallo al latir no debe tumbar el trabajo. Si la base está
                # caída, el trabajo va a fallar por su cuenta más adelante.
                print(f"  ⚠ No pude latir: {e}")

    def __enter__(self) -> "Latido":
        self._hilo = threading.Thread(target=self._bucle, daemon=True)
        self._hilo.start()
        return self

    def __exit__(self, *_) -> None:
        self._parar.set()
        if self._hilo:
            self._hilo.join(timeout=2)


# =====================================================================
# PROCESAR UN TRABAJO DE TIPO 'buscar'
# =====================================================================
def procesar_buscar(trabajo: dict) -> None:
    """
    Un renglón nuevo: interpretar con IA, crear el/los item(s) y scrapear.

    Normalmente un renglón es un producto, pero si el vendedor escribió dos
    cosas en la misma línea ("5 laptops y 3 monitores"), la IA devuelve dos y
    acá se crean los dos items. El trabajo se cierra cuando todos terminaron.

    La interpretación la hace interpretar_y_armar_query() (en main.py): la IA
    extrae marca/modelo/specs y el código arma la query con limpiar_query().
    """
    trabajo_id = str(trabajo["id"])
    solicitud_id = str(trabajo["solicitud_id"])
    texto = trabajo["texto"]

    db_cola.anotar_progreso_solicitud(solicitud_id, f"Interpretando: {texto[:80]}")
    db_cola.anotar_progreso(trabajo_id, "Interpretando con IA")

    print("\n[1/3] Interpretando con IA...")
    solicitados = interpretar_y_armar_query(texto)

    for i, p in enumerate(solicitados, 1):
        print(
            f"    {i}. [{p.categoria}] {p.marca or ''} {p.linea or ''} "
            f"{p.modelo or ''} — query: '{p.query_sugerida}' (x{p.cantidad})"
        )

    # Acumula la interpretación en la solicitud (no la sobrescribe: cada
    # renglón suma la suya).
    db_cola.acumular_interpretacion_ia(
        solicitud_id,
        [p.to_dict() for p in solicitados],
        config_modelo(),
    )

    # Los items nuevos continúan después de los que ya existían, así el orden
    # en pantalla coincide con el orden en que el vendedor los escribió.
    orden_inicial = db.siguiente_orden_item(solicitud_id)
    item_ids = db.guardar_items(
        solicitud_id,
        [p.to_dict() for p in solicitados],
        orden_inicial=orden_inicial,
    )

    # Dejamos constancia de qué item salió de este trabajo.
    if item_ids:
        db_cola.ligar_item(trabajo_id, item_ids[0])

    codigos_activos = db.listar_proveedores_para_solicitud(solicitud_id)
    print(f"  Proveedores (según preferencia del usuario): {codigos_activos}")
    if not codigos_activos:
        raise ValueError("No hay proveedores activos configurados para buscar.")

    db_cola.anotar_progreso_solicitud(
        solicitud_id,
        f"Consultando {len(codigos_activos)} proveedores: {texto[:60]}",
    )
    db_cola.anotar_progreso(
        trabajo_id, f"Scrapeando {len(codigos_activos)} proveedores"
    )

    print("\n[2/3] Corriendo scrapers...")
    matcher = Matcher(umbral_minimo=UMBRAL_MATCHER)
    for item_id, solicitado in zip(item_ids, solicitados):
        procesar_item(
            item_id=item_id,
            solicitado=solicitado,
            matcher=matcher,
            codigos_activos=codigos_activos,
            intento_numero=1,
        )

    print("\n[3/3] Renglón terminado.")


def config_modelo() -> str:
    """Nombre del modelo de IA configurado, para guardarlo en la solicitud."""
    from config import config

    return config.gemini_model


# =====================================================================
# PROCESAR UN TRABAJO DE TIPO 'reintento'
# =====================================================================
def procesar_reintento(trabajo: dict) -> None:
    """
    Reinterpreta un item que ya existe y lo vuelve a scrapear, generando un
    intento_numero + 1. Los productos del intento anterior quedan intactos.
    """
    trabajo_id = str(trabajo["id"])
    solicitud_id = str(trabajo["solicitud_id"])
    item_id = str(trabajo["solicitud_item_id"])
    texto = trabajo["texto"]

    item = db.obtener_item(item_id)
    if not item:
        raise ValueError(f"El item {item_id} ya no existe")

    db.guardar_texto_reinterpretacion(item_id, texto)
    db_cola.anotar_progreso_solicitud(solicitud_id, f"Reintentando: {texto[:80]}")
    db_cola.anotar_progreso(trabajo_id, "Reinterpretando con IA")

    print("\n[1/2] Reinterpretando con IA...")
    solicitados = interpretar_y_armar_query(texto)

    # Un reintento corrige UN item. Si la IA devolvió varios, tomamos el primero.
    solicitado = solicitados[0]
    if len(solicitados) > 1:
        print(
            f"  ⚠ La IA devolvió {len(solicitados)} productos; "
            f"para el reintento uso solo el primero."
        )

    print(
        f"  ✓ Interpretado: [{solicitado.categoria}] "
        f"{solicitado.marca or ''} {solicitado.linea or ''} "
        f"{solicitado.modelo or ''} — query: '{solicitado.query_sugerida}'"
    )

    intento_numero = db.siguiente_intento_numero(item_id)
    codigos_activos = db.listar_proveedores_para_solicitud(solicitud_id)
    print(f"  Proveedores: {codigos_activos}")
    if not codigos_activos:
        raise ValueError("No hay proveedores activos configurados para buscar.")

    db_cola.anotar_progreso(trabajo_id, f"Scrapeando (intento #{intento_numero})")

    print(f"\n[2/2] Corriendo scrapers — intento #{intento_numero}...")
    matcher = Matcher(umbral_minimo=UMBRAL_MATCHER)
    procesar_item(
        item_id=item_id,
        solicitado=solicitado,
        matcher=matcher,
        codigos_activos=codigos_activos,
        intento_numero=intento_numero,
    )

    # Este intento pasa a ser el vigente: es el que el UI abre por defecto.
    db.actualizar_intento_actual(item_id, intento_numero)


# =====================================================================
# DESPACHADOR
# =====================================================================
def procesar_trabajo(trabajo: dict) -> None:
    """
    Corre UN trabajo de punta a punta, con aislamiento total.

    Pase lo que pase, el trabajo se cierra (ok o error) y el estado de la
    solicitud se recalcula. Una excepción acá nunca tumba al worker ni deja la
    solicitud en el limbo.
    """
    trabajo_id = str(trabajo["id"])
    solicitud_id = str(trabajo["solicitud_id"])
    tipo = trabajo["tipo"]
    intento = trabajo["intentos"]
    maximo = trabajo["max_intentos"]

    print(f"\n{'=' * 70}")
    print(f"TRABAJO {trabajo_id}  [{tipo}]  intento {intento}/{maximo}")
    print(f"SOLICITUD {solicitud_id}")
    print(f"TEXTO: {trabajo['texto']}")
    print("=" * 70)

    inicio = time.perf_counter()

    # La solicitud pasa a 'scrapeando' en cuanto se toma el primer trabajo.
    db_cola.sincronizar_estado_solicitud(solicitud_id)

    try:
        with Latido(trabajo_id):
            if tipo == "reintento":
                procesar_reintento(trabajo)
            else:
                procesar_buscar(trabajo)

        db_cola.cerrar_ok(trabajo_id)
        duracion = time.perf_counter() - inicio
        print(f"\n✓ Trabajo terminado en {duracion:.1f}s")

    except Exception as e:
        detalle = f"{type(e).__name__}: {e}"
        print(f"\n✗ Falló el trabajo: {detalle}")
        traceback.print_exc()

        desenlace = db_cola.cerrar_error(
            trabajo_id, f"{detalle}\n{traceback.format_exc()}"
        )
        if desenlace == "reencolado":
            print("  → Vuelve a la cola para un intento más.")
        else:
            print("  → Sin intentos restantes: queda marcado como error.")

    finally:
        # El estado de la solicitud SIEMPRE se deriva de sus trabajos, incluso
        # si el trabajo explotó. Acá es donde la solicitud pasa a 'lista'.
        try:
            estado = db_cola.sincronizar_estado_solicitud(solicitud_id)
            if estado:
                print(f"  Estado de la solicitud: {estado}")
            if estado == "lista":
                db_cola.anotar_progreso_solicitud(solicitud_id, "Búsqueda completada")
        except Exception as e:
            print(f"  ⚠ No pude recalcular el estado de la solicitud: {e}")


# =====================================================================
# WATCHDOG
# =====================================================================
def correr_watchdog() -> int:
    """Rescata los trabajos colgados. Devuelve cuántos tocó."""
    try:
        rescatados = db_cola.rescatar_colgados()
    except Exception as e:
        print(f"⚠ El watchdog falló: {e}")
        return 0

    for t in rescatados:
        destino = "la cola" if t["estado"] == "pendiente" else "error"
        print(
            f"⚠ Rescatado trabajo {t['id']} (intento {t['intentos']}/"
            f"{t['max_intentos']}) → {destino}: {t['texto'][:60]}"
        )
        try:
            db_cola.sincronizar_estado_solicitud(str(t["solicitud_id"]))
        except Exception:
            pass

    return len(rescatados)


# =====================================================================
# LOOP PRINCIPAL
# =====================================================================
def _manejar_senal(numero, _marco) -> None:
    print(f"\n⏹ Señal {numero} recibida. Termino el trabajo en curso y salgo.")
    _salir.set()


def correr(una_vez: bool = False) -> int:
    worker_id = db_cola.identificador_worker()
    espera = db_cola.EsperaDeTrabajos()

    print(f"Worker del cotizador arriba — id: {worker_id}")
    print(
        f"  Revisión de la cola cada {POLL_S:g}s (o al instante por aviso de la base)"
    )
    print(
        f"  Trabajos sin latido por más de {db_cola.TIMEOUT_LATIDO_MIN} min se rescatan"
    )

    # Al arrancar, destrabar lo que haya quedado colgado del arranque anterior.
    # Esto es lo que hace que un simple reinicio de pm2 destrabe el sistema.
    rescatados = correr_watchdog()
    if rescatados:
        print(f"  {rescatados} trabajo(s) rescatados del arranque anterior")

    ultimo_watchdog = time.monotonic()

    try:
        while not _salir.is_set():
            try:
                trabajo = db_cola.tomar_trabajo(worker_id)
            except Exception as e:
                print(f"⚠ No pude leer la cola ({e}). Reintento en {POLL_S:g}s.")
                _salir.wait(POLL_S)
                continue

            if trabajo:
                procesar_trabajo(trabajo)
                if una_vez:
                    print("\n--una-vez: procesé un trabajo, salgo.")
                    break
                # Seguimos de inmediato: puede haber más renglones esperando.
                continue

            if una_vez:
                print("\n--una-vez: la cola está vacía, salgo.")
                break

            # Cola vacía. Pasamos el watchdog de vez en cuando y nos dormimos
            # hasta que la base avise o se cumpla el tiempo.
            if time.monotonic() - ultimo_watchdog > 60:
                correr_watchdog()
                ultimo_watchdog = time.monotonic()

            espera.esperar(POLL_S)

    finally:
        espera.cerrar()

    print("Worker detenido limpiamente.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Worker de la cola del cotizador SX")
    parser.add_argument(
        "--una-vez",
        action="store_true",
        help="Procesa un solo trabajo (o sale si la cola está vacía) y termina",
    )
    parser.add_argument(
        "--solo-watchdog",
        action="store_true",
        help="Solo rescata trabajos colgados y termina",
    )
    args = parser.parse_args()

    for s in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if s is not None:
            try:
                signal.signal(s, _manejar_senal)
            except (ValueError, OSError):
                # En Windows algunas señales no se pueden capturar. No es grave.
                pass

    if args.solo_watchdog:
        total = correr_watchdog()
        print(f"Watchdog: {total} trabajo(s) rescatados.")
        return 0

    return correr(una_vez=args.una_vez)


if __name__ == "__main__":
    from logger import configurar_logging

    configurar_logging("worker")
    sys.exit(main())
