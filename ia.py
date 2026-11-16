"""
Wrapper de IA con estrategia de resiliencia por capas:

  1. Cache: si ya interpretamos ese texto recientemente, no llamamos a la IA
  2. Modelo primario: intenta con el modelo configurado (con reintentos)
  3. Modelos fallback: si el primario se satura, prueba modelos alternos
  4. Fallback determinístico: si TODOS los modelos fallan, extrae con reglas
     simples (el texto tal cual es la query). Nunca deja al usuario sin respuesta.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import warnings
from typing import Any

from google import genai
from google.genai import errors, types

from config import config
from modelo import ProductoSolicitado

warnings.filterwarnings("ignore", message=".*automatic function calling.*")


# Orden de fallback de modelos Gemini (del preferido al mas resiliente)
GEMINI_MODELOS_FALLBACK = [
    "gemini-3.5-flash-lite",  # más liviano, menos demanda
    "gemini-3.5-flash",  # estable
]

# Interruptor general. Con USAR_IA=false el pipeline no llama a Gemini:
# arma la query con el texto del vendedor y sigue de largo.
USAR_IA = os.getenv("USAR_IA", "true").strip().lower() not in ("false", "0", "no")

# Tiempo máximo por petición. Sin esto el SDK espera indefinidamente y un
# modelo que no responde congela el worker completo.
IA_TIMEOUT_S = float(os.getenv("IA_TIMEOUT_S", "20"))


PROMPT_SISTEMA = """Eres un asistente que extrae peticiones de cotizacion de productos \
de tecnologia a partir de texto libre en español.

Analiza el texto y devuelve UNICAMENTE un JSON valido con esta estructura exacta:

{
  "es_cotizacion": true,
  "productos": [
    {
      "categoria": "laptop",
      "marca": "Lenovo",
      "linea": "ThinkPad",
      "modelo": "T14 Gen 3",
      "specs": {
        "ram_gb": 16,
        "cpu": "i7",
        "almacenamiento_gb": 512,
        "almacenamiento_tipo": "SSD",
        "pantalla_pulgadas": 14
      },
      "cantidad": 5,
      "query_sugerida": "Lenovo ThinkPad i7 16GB"
    }
  ]
}

REGLAS ESTRICTAS:
- Si el texto NO es una peticion de productos, devuelve es_cotizacion=false y productos=[].
- "query_sugerida" debe ser de 2 a 5 palabras clave optimizadas para el buscador de una \
tienda online. Incluye marca y specs distintivas. NO uses palabras como "necesito", \
"cotizar", "quiero", "por favor".
- Solo incluye specs que esten mencionadas o inferibles. NO inventes.
- Si no se especifica cantidad, usa 1.
- Puede haber varios productos en un mismo texto: cada uno va como un elemento del array.
- Responde SOLO el JSON, sin markdown, sin ```json, sin explicaciones.
"""


# =====================================================================
# Cache simple en memoria (dura mientras vive el proceso)
# =====================================================================
_cache: dict[str, dict[str, Any]] = {}


def _cache_key(texto: str) -> str:
    """Hash del texto normalizado (lowercase + strip)."""
    return hashlib.md5(texto.lower().strip().encode("utf-8")).hexdigest()


# =====================================================================
# Fallback deterministico (sin IA)
# =====================================================================
def _fallback_sin_ia(texto: str) -> dict[str, Any]:
    """
    Cuando toda la IA esta caida, degradamos elegantemente:
    - Asumimos que es una peticion de cotizacion
    - Usamos el texto tal cual como query_sugerida (limitado a 5 palabras)
    - Sin marca, categoria ni specs (la IA los habria extraido, sin ella no)

    El scraper igual traera resultados, solo que menos precisos.
    """
    print("  ⚠ Usando fallback SIN IA (query = texto tal cual)")
    palabras = texto.strip().split()[:5]
    query = " ".join(palabras)
    return {
        "es_cotizacion": True,
        "productos": [
            {
                "categoria": None,
                "marca": None,
                "linea": None,
                "modelo": None,
                "specs": {},
                "cantidad": 1,
                "query_sugerida": query,
                "_fallback_sin_ia": True,  # marca para debug
            }
        ],
    }


# =====================================================================
# Interpretador con estrategia por capas
# =====================================================================
class InterpretadorIA:

    def __init__(self) -> None:
        self.provider = config.ia_provider
        if self.provider == "gemini":
            if not config.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY vacia en .env")
            self._client = genai.Client(
                api_key=config.gemini_api_key,
                http_options=types.HttpOptions(timeout=int(IA_TIMEOUT_S * 1000)),
            )
            # Lista completa: modelo primario del .env + fallbacks
            self._modelos = [config.gemini_model] + [
                m for m in GEMINI_MODELOS_FALLBACK if m != config.gemini_model
            ]
            print(
                f"  [ia] modelo del .env: {config.gemini_model} | "
                f"cascada: {' → '.join(self._modelos)} | "
                f"timeout: {IA_TIMEOUT_S}s",
                flush=True,
            )
        else:
            raise NotImplementedError(
                f"Proveedor IA '{self.provider}' aun no implementado"
            )

    def interpretar(self, texto: str) -> dict[str, Any]:
        # ---- Capa 0: interruptor ----
        if not USAR_IA:
            print("  [ia] USAR_IA=false: no se consulta a Gemini", flush=True)
            return _fallback_sin_ia(texto)

        # ---- Capa 1: cache ----
        clave = _cache_key(texto)
        if clave in _cache:
            print("  ✓ Cache hit (sin llamar a IA)")
            return _cache[clave]

        # ---- Capa 2 y 3: intentar cada modelo con reintentos ----
        for i, modelo in enumerate(self._modelos, 1):
            try:
                print(f"  → Intentando modelo {i}/{len(self._modelos)}: {modelo}")
                data = self._intentar_modelo(modelo, texto)
                _cache[clave] = data
                return data
            except errors.ServerError as e:
                print(f"  ⚠ Modelo {modelo} con error de servidor: {e}")
                continue
            except errors.ClientError as e:
                # 4xx = error de nuestro lado. Mostramos el detalle real:
                #   429 = sin cuota (RESOURCE_EXHAUSTED)
                #   404 = modelo no existe / no disponible para esta key
                #   400 = formato / API key inválida
                print(f"  ⚠ Modelo {modelo} rechazó la petición: {e}")
                if "API_KEY" in str(e).upper() or "PERMISSION" in str(e).upper():
                    raise
                continue
            except json.JSONDecodeError:
                print(
                    f"  ⚠ Modelo {modelo} devolvio JSON invalido, probando siguiente..."
                )
                continue
            except Exception as e:
                # Red caída, DNS que no resuelve (getaddrinfo failed), timeout,
                # SSL... Cualquier fallo de conexión NO debe tumbar el proceso.
                # Probamos el siguiente modelo y, si todos fallan, cae al
                # fallback determinístico de abajo (buscar el texto tal cual).
                print(f"  ⚠ Modelo {modelo} falló por conexión u otro error: {e}")
                continue

        # ---- Capa 4: fallback determinístico ----
        print("  ✗ Todos los modelos IA fallaron. Usando fallback deterministico.")
        data = _fallback_sin_ia(texto)
        # No cacheamos el fallback: la proxima vez queremos volver a intentar la IA
        return data

    def _intentar_modelo(self, modelo: str, texto: str) -> dict[str, Any]:
        """
        Intenta un modelo especifico con hasta 3 reintentos y backoff.
        Solo reintenta ante ServerError (5xx). ClientError (4xx) sube.
        """
        max_intentos = 3
        espera = 2  # segundos

        for intento in range(1, max_intentos + 1):
            inicio = time.perf_counter()
            try:
                print(
                    f"    [ia] intento {intento}/{max_intentos} → petición a "
                    f"{modelo} (timeout {IA_TIMEOUT_S}s)...",
                    flush=True,
                )
                respuesta = self._client.models.generate_content(
                    model=modelo,
                    contents=texto,
                    config=types.GenerateContentConfig(
                        system_instruction=PROMPT_SISTEMA,
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
                print(
                    f"    [ia] respondió en {time.perf_counter() - inicio:.1f}s",
                    flush=True,
                )
                return json.loads(self._limpiar(respuesta.text))

            except errors.ServerError as error:
                tardo = time.perf_counter() - inicio
                # Un 504 DEADLINE_EXCEEDED significa que ese modelo no alcanza a
                # responder dentro del plazo de su propio backend. Reintentarlo
                # da el mismo resultado: conviene pasar al siguiente modelo ya.
                if "DEADLINE_EXCEEDED" in str(error) or "504" in str(error):
                    print(
                        f"    [ia] {modelo} agotó el plazo del servidor a los "
                        f"{tardo:.1f}s; sin reintentos, va el siguiente modelo",
                        flush=True,
                    )
                    raise
                if intento < max_intentos:
                    print(
                        f"    [ia] intento {intento}/{max_intentos} falló a los "
                        f"{tardo:.1f}s ({str(error)[:120]}); "
                        f"esperando {espera}s...",
                        flush=True,
                    )
                    time.sleep(espera)
                    espera *= 2
                else:
                    print(
                        f"    [ia] último intento falló a los {tardo:.1f}s",
                        flush=True,
                    )
                    raise  # el interpretar() de arriba se encarga
            except Exception as error:
                print(
                    f"    [ia] {type(error).__name__} a los "
                    f"{time.perf_counter() - inicio:.1f}s: {error}",
                    flush=True,
                )
                raise

    @staticmethod
    def _limpiar(texto: str) -> str:
        t = texto.strip()
        if t.startswith("```"):
            lineas = t.split("\n")
            if lineas[-1].strip().startswith("```"):
                lineas = lineas[1:-1]
            else:
                lineas = lineas[1:]
            t = "\n".join(lineas)
        return t.strip()


# =====================================================================
# API publica del modulo (no cambia)
# =====================================================================
_interpretador: InterpretadorIA | None = None


def _get_interpretador() -> InterpretadorIA:
    global _interpretador
    if _interpretador is None:
        _interpretador = InterpretadorIA()
    return _interpretador


def interpretar_solicitud(texto: str) -> list[ProductoSolicitado]:
    """
    Convierte texto libre en una lista de ProductoSolicitado.
    Nunca lanza excepcion — siempre devuelve una lista (aunque sea con fallback).
    """
    data = _get_interpretador().interpretar(texto)

    if not data.get("es_cotizacion"):
        return []

    productos = []
    for p in data.get("productos", []):
        productos.append(
            ProductoSolicitado(
                categoria=p.get("categoria"),
                marca=p.get("marca"),
                modelo=p.get("modelo"),
                linea=p.get("linea"),
                specs=p.get("specs", {}),
                cantidad=int(p.get("cantidad", 1)),
                query_sugerida=p.get("query_sugerida", ""),
            )
        )
    return productos


def limpiar_cache() -> None:
    """Util para testing o para forzar re-consulta."""
    _cache.clear()
