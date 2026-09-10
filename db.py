"""
Módulo de persistencia a PostgreSQL.

Todo lo que hoy sale al JSON, aquí se guarda en la base de datos.
Usa psycopg (v3) sin ORM para mantenerlo simple.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------
# Configuración de conexión
# ---------------------------------------------------------------------
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "facturacion_db"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


@contextmanager
def get_conn():
    """Context manager para conexiones. Uso: with get_conn() as conn:"""
    conn = psycopg.connect(**DB_CONFIG, row_factory=dict_row)
    # Setear search_path para no tener que escribir 'cotizador.' todo el tiempo
    conn.execute("SET search_path TO cotizador, public")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =====================================================================
# PROVEEDORES
# =====================================================================
def listar_proveedores_activos() -> list[str]:
    """
    Devuelve los códigos de proveedores marcados como activos en la DB.
    Esta es la fuente de verdad para saber qué scrapers correr.
    El frontend puede activar/desactivar cambiando el campo `activo`.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT codigo FROM proveedores WHERE activo = true ORDER BY codigo"
        ).fetchall()
    return [r["codigo"] for r in rows]


# =====================================================================
# SOLICITUDES
# =====================================================================
def crear_solicitud(
    texto_original: str,
    canal_origen: str = "busqueda_rapida",
    cliente_id: str | None = None,
    usuario_creador_id: str | None = None,
    empresa_id: str | None = None,
    correo_remitente: str | None = None,
    asunto_correo: str | None = None,
    message_id_correo: str | None = None,
    urgencia: str = "normal",
) -> str:
    """
    Crea una solicitud nueva en estado 'pendiente'.
    Devuelve el UUID como string.

    Uso desde CLI:      crear_solicitud("mouse logitech")
    Uso desde SvelteKit: la crea el frontend antes de invocar main.py
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO solicitudes (
                texto_original, canal_origen, cliente_id, usuario_creador_id,
                empresa_id, correo_remitente, asunto_correo, message_id_correo,
                urgencia, estado
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pendiente')
            RETURNING id
            """,
            (
                texto_original,
                canal_origen,
                cliente_id,
                usuario_creador_id,
                empresa_id,
                correo_remitente,
                asunto_correo,
                message_id_correo,
                urgencia,
            ),
        ).fetchone()
    return str(row["id"])


def obtener_solicitud(solicitud_id: str) -> dict | None:
    """
    Obtiene una solicitud por ID.
    Usado por main.py en modo 2 (cuando SvelteKit ya creó la solicitud).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM solicitudes WHERE id = %s",
            (solicitud_id,),
        ).fetchone()
    return row


def actualizar_estado_solicitud(
    solicitud_id: str,
    estado: str,
    error_mensaje: str | None = None,
) -> None:
    """
    Cambia el estado. También setea timestamps según el estado.
    Estados: pendiente, normalizando, scrapeando, lista, error, cerrada
    """
    # Mapear estado → campo timestamp a actualizar
    ts_field_map = {
        "normalizando": "normalizado_en",
        "scrapeando": None,  # el timestamp scrapeado_en se pone al final
        "lista": "scrapeado_en",
        "cerrada": "cerrado_en",
    }
    ts_field = ts_field_map.get(estado)

    if ts_field:
        sql = f"""UPDATE solicitudes
                  SET estado = %s, error_mensaje = %s, {ts_field} = now()
                  WHERE id = %s"""
    else:
        sql = """UPDATE solicitudes
                 SET estado = %s, error_mensaje = %s
                 WHERE id = %s"""

    with get_conn() as conn:
        conn.execute(sql, (estado, error_mensaje, solicitud_id))


def guardar_interpretacion_ia(
    solicitud_id: str,
    es_cotizacion: bool,
    interpretacion_json: dict,
    modelo_usado: str,
) -> None:
    """Guarda el resultado de la IA sobre la solicitud."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE solicitudes
            SET ia_es_cotizacion  = %s,
                ia_interpretacion = %s,
                ia_modelo_usado   = %s
            WHERE id = %s
            """,
            (es_cotizacion, Jsonb(interpretacion_json), modelo_usado, solicitud_id),
        )


# =====================================================================
# ITEMS DE SOLICITUD
# =====================================================================
def guardar_items(solicitud_id: str, productos_solicitados: list[dict]) -> list[str]:
    """
    Guarda los items (productos que la IA identificó en la solicitud).
    Devuelve lista de UUIDs generados, en el mismo orden.
    """
    ids: list[str] = []
    with get_conn() as conn:
        for orden, p in enumerate(productos_solicitados):
            row = conn.execute(
                """
                INSERT INTO solicitud_items (
                    solicitud_id, categoria, marca, linea, modelo,
                    specs, cantidad, query_scraper, orden
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    solicitud_id,
                    p.get("categoria"),
                    p.get("marca"),
                    p.get("linea"),
                    p.get("modelo"),
                    Jsonb(p.get("specs") or {}),
                    p.get("cantidad", 1),
                    p.get("query_sugerida", ""),
                    orden,
                ),
            ).fetchone()
            ids.append(str(row["id"]))
    return ids


# =====================================================================
# EJECUCIONES DE SCRAPER
# =====================================================================
def guardar_ejecucion_scraper(
    solicitud_item_id: str,
    proveedor_codigo: str,
    query_usada: str,
    exitoso: bool,
    duracion_ms: int,
    total_crudos: int,
    error_mensaje: str | None = None,
) -> str:
    """
    Guarda una ejecución de scraper.
    Devuelve el ID de la ejecución (necesario para guardar los productos).
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO scraper_ejecuciones (
                solicitud_item_id, proveedor_codigo, query_usada,
                exitoso, duracion_ms, total_crudos, error_mensaje
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                solicitud_item_id,
                proveedor_codigo,
                query_usada,
                exitoso,
                duracion_ms,
                total_crudos,
                error_mensaje,
            ),
        ).fetchone()
    return str(row["id"])


def actualizar_total_guardados(ejecucion_id: str, total_guardados: int) -> None:
    """Actualiza el contador de productos que efectivamente se persistieron."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE scraper_ejecuciones SET total_guardados = %s WHERE id = %s",
            (total_guardados, ejecucion_id),
        )


# =====================================================================
# PRODUCTOS ENCONTRADOS
# =====================================================================
def guardar_productos_encontrados(
    solicitud_item_id: str,
    scraper_ejecucion_id: str,
    productos_con_match: list[dict],
    tiers_a_guardar: tuple[str, ...] = ("exacto", "bueno", "similar"),
) -> int:
    """
    Guarda los productos que pasaron el filtro de tier.
    Por defecto: exacto, bueno, similar. Los 'dudoso' se descartan.

    Cada producto es un dict que combina el producto original + el match.
    Estructura esperada:
      {
        "proveedor": "intelec",
        "nombre": "...",
        ...  (todos los campos de ProductoEncontrado)
        "match": {
          "score_total": 92.5,
          "score_texto": 85.0,
          "tier": "exacto",
          ...
        }
      }

    Devuelve la cantidad efectivamente guardada.
    """
    if not productos_con_match:
        return 0

    guardados = 0
    with get_conn() as conn:
        for p in productos_con_match:
            match = p.get("match", {})
            tier = match.get("tier")

            # Filtro: solo guardar los tiers permitidos y no descartados
            if match.get("descartado"):
                continue
            if tier not in tiers_a_guardar:
                continue

            conn.execute(
                """
                INSERT INTO productos_encontrados (
                    solicitud_item_id, scraper_ejecucion_id, proveedor_codigo,
                    nombre, sku, marca, categoria,
                    url_detalle, url_imagen,
                    precio, precio_regular, tiene_descuento, descuento_porcentaje,
                    moneda, precio_texto_original,
                    stock_texto, disponible,
                    descripcion_corta, specs, extra,
                    match_score, match_score_texto, match_score_marca, match_score_specs,
                    match_ajuste_variantes, match_bonus_identidad, match_bonus_contenida,
                    match_tier, match_etiquetas,
                    scrapeado_en
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s
                )
                """,
                (
                    solicitud_item_id,
                    scraper_ejecucion_id,
                    p.get("proveedor"),
                    p.get("nombre"),
                    p.get("sku"),
                    p.get("marca"),
                    p.get("categoria"),
                    p.get("url_detalle"),
                    p.get("url_imagen"),
                    p.get("precio"),
                    p.get("precio_regular"),
                    p.get("tiene_descuento", False),
                    p.get("descuento_porcentaje"),
                    p.get("moneda"),
                    p.get("precio_texto_original"),
                    p.get("stock_texto"),
                    p.get("disponible"),
                    p.get("descripcion_corta"),
                    Jsonb(p.get("specs") or {}),
                    Jsonb(p.get("extra") or {}),
                    match.get("score_total"),
                    match.get("score_texto"),
                    match.get("score_marca"),
                    match.get("score_specs"),
                    match.get("ajuste_variantes", 0),
                    match.get("bonus_identidad", 0),
                    match.get("bonus_contenida", 0),
                    tier,
                    match.get("etiquetas_variante") or [],
                    p.get("scrapeado_en"),
                ),
            )
            guardados += 1

    return guardados


# =====================================================================
# CONSULTAS ÚTILES (usadas por CLI para verificar / debug)
# =====================================================================
def resumen_solicitud(solicitud_id: str) -> dict | None:
    """Devuelve un resumen completo de una solicitud (usa la vista)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM v_solicitudes_resumen WHERE id = %s",
            (solicitud_id,),
        ).fetchone()
    return row


def listar_productos_solicitud(solicitud_id: str) -> list[dict]:
    """
    Todos los productos encontrados en una solicitud, ordenados por tier y score.
    Útil para verificar desde CLI.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT pe.*, si.query_scraper
            FROM productos_encontrados pe
            JOIN solicitud_items si ON si.id = pe.solicitud_item_id
            WHERE si.solicitud_id = %s
            ORDER BY pe.match_tier, pe.match_score DESC
            """,
            (solicitud_id,),
        ).fetchall()
    return rows
