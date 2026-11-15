"""
db_cola.py — Operaciones de la cola de trabajos (cotizador.trabajos).

Qué hace, en una línea: le da al worker las cuatro cosas que necesita para
consumir la cola de forma segura — tomar un trabajo sin que otro worker se lo
robe, avisar que sigue vivo, cerrarlo, y rescatar los que quedaron colgados.

Para qué sirve: antes SvelteKit llamaba al Python por HTTP y lanzaba un proceso
suelto por cada solicitud. Si entraban tres usuarios se abrían tres procesos con
sus navegadores y el VPS sufría; y si un proceso moría, la solicitud se quedaba
"procesando" para siempre. Con esto hay UN worker que atiende de a uno, y nada
queda colgado sin que alguien lo note.

No reemplaza a db.py: lo complementa. Reutiliza su misma conexión y su
search_path (cotizador, public), así que acá tampoco hace falta escribir
'cotizador.' delante de cada tabla.
"""

from __future__ import annotations

import os
import socket

import psycopg

from db import DB_CONFIG, get_conn

# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------

# Canal de PostgreSQL por el que la base avisa "hay trabajo nuevo".
# Lo dispara el trigger trabajos_notificar_insert (migración 006).
CANAL_NOTIFY = "cotizador_trabajos"

# Minutos sin latido tras los cuales se considera que el worker que tomó un
# trabajo murió. Debe ser holgado: una búsqueda en 8 proveedores puede tardar
# varios minutos. Si se queda corto, el watchdog reencola trabajos que todavía
# estaban corriendo.
TIMEOUT_LATIDO_MIN = int(os.getenv("WORKER_TIMEOUT_MIN", "10"))


def identificador_worker() -> str:
    """Identifica al proceso que toma los trabajos: maquina:pid."""
    return f"{socket.gethostname()}:{os.getpid()}"


# =====================================================================
# TOMAR UN TRABAJO
# =====================================================================
def tomar_trabajo(worker_id: str) -> dict | None:
    """
    Toma el siguiente trabajo pendiente y lo marca 'en_proceso'.
    Devuelve el trabajo completo, o None si la cola está vacía.

    El SELECT interno usa FOR UPDATE SKIP LOCKED: si dos workers corren a la
    vez, cada uno se lleva un trabajo distinto en lugar de pelearse por el
    mismo. SKIP LOCKED es la pieza que hace que esto sea una cola de verdad y
    no una fuente de bloqueos.

    La transacción es corta a propósito — toma el trabajo y commitea. El
    trabajo queda protegido por su estado 'en_proceso', no por un lock abierto
    durante los minutos que dura el scraping.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            UPDATE trabajos t
            SET estado       = 'en_proceso',
                intentos     = t.intentos + 1,
                worker_id    = %s,
                tomado_en    = now(),
                heartbeat_en = now()
            WHERE t.id = (
                SELECT id
                FROM trabajos
                WHERE estado = 'pendiente'
                ORDER BY prioridad DESC, creado_en ASC, orden ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING t.*
            """,
            (worker_id,),
        ).fetchone()
    return row


# =====================================================================
# LATIDO
# =====================================================================
def latir(trabajo_id: str) -> None:
    """
    Refresca heartbeat_en. Lo llama un hilo aparte mientras el trabajo corre.
    Mientras late, el watchdog no lo toca; si el proceso muere, el latido se
    congela y el watchdog lo rescata.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE trabajos SET heartbeat_en = now() WHERE id = %s",
            (trabajo_id,),
        )


def anotar_progreso(trabajo_id: str, mensaje: str) -> None:
    """Guarda en qué anda el trabajo, para poder mirarlo desde la DB o el UI."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE trabajos SET mensaje_progreso = %s WHERE id = %s",
            (mensaje[:500], trabajo_id),
        )


def ligar_item(trabajo_id: str, solicitud_item_id: str) -> None:
    """
    Guarda qué item creó este trabajo. En los trabajos 'buscar' el item no
    existe al encolar — lo crea la IA al interpretar el texto — así que la
    columna arranca en NULL y se rellena acá.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE trabajos SET solicitud_item_id = %s WHERE id = %s",
            (solicitud_item_id, trabajo_id),
        )


# =====================================================================
# CERRAR UN TRABAJO
# =====================================================================
def cerrar_ok(trabajo_id: str) -> None:
    """Marca el trabajo como terminado bien."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE trabajos
            SET estado        = 'ok',
                terminado_en  = now(),
                error_mensaje = NULL,
                worker_id     = NULL
            WHERE id = %s
            """,
            (trabajo_id,),
        )


def cerrar_error(trabajo_id: str, mensaje: str) -> str:
    """
    Registra el fallo. Si al trabajo le quedan intentos, vuelve a 'pendiente'
    para que se reintente solo; si ya los agotó, queda en 'error'.

    Devuelve 'reencolado' o 'error' para que el log lo diga claro.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            UPDATE trabajos
            SET estado = CASE
                    WHEN intentos >= max_intentos THEN 'error'::estado_trabajo
                    ELSE 'pendiente'::estado_trabajo
                END,
                error_mensaje = %s,
                terminado_en  = CASE WHEN intentos >= max_intentos THEN now() END,
                worker_id     = NULL
            WHERE id = %s
            RETURNING estado
            """,
            (mensaje[:2000], trabajo_id),
        ).fetchone()
    estado = row["estado"] if row else "error"
    return "error" if estado == "error" else "reencolado"


# =====================================================================
# WATCHDOG — rescatar trabajos colgados
# =====================================================================
def rescatar_colgados(timeout_min: int = TIMEOUT_LATIDO_MIN) -> list[dict]:
    """
    Busca trabajos 'en_proceso' cuyo latido se enfrió y los devuelve a la cola
    (o los marca 'error' si ya agotaron los intentos).

    Esto es lo que elimina el "spinner eterno": si el proceso muere, el VPS se
    reinicia o pm2 mata al worker a mitad de un scraping, el trabajo no se
    queda en el limbo. El worker corre esto al arrancar y cada vuelta del loop,
    así que un reinicio de pm2 ya es suficiente para que todo se destrabe.

    Devuelve los trabajos afectados.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            UPDATE trabajos
            SET estado = CASE
                    WHEN intentos >= max_intentos THEN 'error'::estado_trabajo
                    ELSE 'pendiente'::estado_trabajo
                END,
                error_mensaje = COALESCE(
                    error_mensaje,
                    'El proceso que tomó este trabajo dejó de responder'
                ),
                terminado_en  = CASE WHEN intentos >= max_intentos THEN now() END,
                worker_id     = NULL
            WHERE estado = 'en_proceso'
              AND heartbeat_en < now() - make_interval(mins => %s)
            RETURNING id, solicitud_id, texto, intentos, max_intentos, estado
            """,
            (timeout_min,),
        ).fetchall()
    return rows


# =====================================================================
# ESTADO DERIVADO DE LA SOLICITUD
# =====================================================================
def sincronizar_estado_solicitud(solicitud_id: str) -> str | None:
    """
    Recalcula el estado de la solicitud a partir de sus trabajos.

    Esta es la ÚNICA función que escribe solicitudes.estado en el flujo nuevo.
    Antes lo escribían el frontend y el Python por separado, y ahí nacía el
    race condition: SvelteKit ponía 'scrapeando' después de que Python ya había
    puesto 'lista', y la solicitud quedaba en proceso para siempre. Derivando
    el estado de un solo lado, eso no puede volver a pasar.

    Reglas:
      quedan trabajos sin terminar        → 'scrapeando'
      todos terminaron, al menos uno bien → 'lista'
      todos terminaron, todos fallaron    → 'error'
      la solicitud no tiene trabajos      → no se toca (viene del flujo viejo)
    """
    with get_conn() as conn:
        c = conn.execute(
            """
            SELECT
                count(*)::int                                             AS total,
                count(*) FILTER (WHERE estado IN ('pendiente','en_proceso'))::int AS abiertos,
                count(*) FILTER (WHERE estado = 'ok')::int                AS ok
            FROM trabajos
            WHERE solicitud_id = %s
            """,
            (solicitud_id,),
        ).fetchone()

        if not c or c["total"] == 0:
            return None

        if c["abiertos"] > 0:
            nuevo = "scrapeando"
        elif c["ok"] > 0:
            nuevo = "lista"
        else:
            nuevo = "error"

        conn.execute(
            """
            UPDATE solicitudes
            SET estado      = %s::estado_solicitud,
                scrapeado_en = CASE
                    WHEN %s = 'lista' THEN now()
                    ELSE scrapeado_en
                END
            WHERE id = %s
            """,
            (nuevo, nuevo, solicitud_id),
        )

    return nuevo


def anotar_progreso_solicitud(solicitud_id: str, mensaje: str) -> None:
    """
    Escribe el mensaje de progreso de la solicitud (lo que ve el vendedor).
    Separado de sincronizar_estado_solicitud a propósito: el estado se deriva,
    el mensaje es informativo.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE solicitudes SET mensaje_progreso = %s WHERE id = %s",
            (mensaje[:500], solicitud_id),
        )


def acumular_interpretacion_ia(
    solicitud_id: str,
    productos: list[dict],
    modelo_usado: str,
) -> None:
    """
    Suma los productos que la IA interpretó al jsonb de la solicitud.

    Con el flujo nuevo cada renglón se interpreta por separado, así que no se
    puede sobrescribir el campo de una sola vez como hacía main.py: hay que
    ACUMULAR, o el último renglón borraría la interpretación de los anteriores.
    """
    from psycopg.types.json import Jsonb

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE solicitudes
            SET ia_es_cotizacion = true,
                ia_modelo_usado  = %s,
                ia_interpretacion = jsonb_build_object(
                    'es_cotizacion', true,
                    'productos',
                    COALESCE(ia_interpretacion -> 'productos', '[]'::jsonb) || %s
                ),
                normalizado_en = COALESCE(normalizado_en, now())
            WHERE id = %s
            """,
            (modelo_usado, Jsonb(productos), solicitud_id),
        )


# =====================================================================
# ESPERA POR NOTIFICACIÓN
# =====================================================================
class EsperaDeTrabajos:
    """
    Espera a que la base avise que hay trabajo nuevo, con LISTEN/NOTIFY.

    Para qué sirve: sin esto el worker tendría que preguntar "¿hay algo?" cada
    pocos segundos. Con esto se queda dormido sin consumir nada y PostgreSQL lo
    despierta en el instante en que SvelteKit inserta un trabajo. El timeout es
    la red de seguridad: aunque el aviso se pierda, el worker igual revisa la
    cola cada tantos segundos.

    Mantiene su propia conexión (aparte de las de db.py) y la reabre sola si se
    cae, porque es una conexión de larga vida y el VPS puede cortarla.
    """

    def __init__(self, canal: str = CANAL_NOTIFY):
        self._canal = canal
        self._conn: psycopg.Connection | None = None

    def _asegurar_conexion(self) -> None:
        if self._conn is not None and not self._conn.closed:
            return
        # autocommit=True es obligatorio para LISTEN: dentro de una transacción
        # abierta las notificaciones no llegan hasta el commit.
        self._conn = psycopg.connect(**DB_CONFIG, autocommit=True)
        self._conn.execute(f"LISTEN {self._canal}")

    def esperar(self, timeout_s: float) -> bool:
        """True si llegó un aviso; False si se agotó el tiempo."""
        try:
            self._asegurar_conexion()
            assert self._conn is not None
            # stop_after=1: con un solo aviso ya alcanza. Si entraron 5
            # renglones quedan 4 notificaciones pendientes, y no importa: el
            # worker vacía la cola completa antes de volver a esperar, y esas
            # notificaciones sobrantes solo hacen que la próxima espera
            # retorne de inmediato.
            for _ in self._conn.notifies(timeout=timeout_s, stop_after=1):
                return True
            return False
        except Exception as e:
            print(f"  ⚠ Se cayó la escucha de avisos ({e}). Sigo por tiempo.")
            self.cerrar()
            return False

    def cerrar(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
