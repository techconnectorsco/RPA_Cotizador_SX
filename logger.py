"""
logger.py — Logging central del RPA Cotizador.

Qué hace, en una línea: manda TODO lo que el RPA imprime (los print que ya
existen en main.py y en los scrapers) a dos lados a la vez:

  - la consola (igual que siempre), y
  - un archivo en  logs/rpa_<fecha>_<hora>.log

Para qué sirve: cuando el sistema (pm2) lanza el Python en background, la
ventana de consola se abre y se cierra sin que alcances a leerla. Con esto,
no importa: todo queda guardado en el archivo de log, incluido cualquier error
no controlado con su traceback completo.

No hay que cambiar ni un solo print del proyecto. Solo se llama a
configurar_logging() una vez, al arrancar.
"""

from __future__ import annotations

import atexit
import sys
import traceback
from datetime import datetime
from pathlib import Path


class _Tee:
    """Escribe lo mismo en varios streams (consola + archivo) a la vez."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                # Nunca dejamos que un fallo al escribir el log tumbe el RPA.
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


_archivo = None  # handle del archivo de log abierto (para cerrarlo al final)


def configurar_logging(prefijo: str = "rpa") -> Path:
    """
    Activa el log a archivo + consola. Devuelve la ruta del archivo creado.
    Llamar UNA vez, lo más temprano posible en el arranque.
    """
    global _archivo

    # La carpeta logs/ se crea al lado de este archivo, NO en el directorio
    # desde donde se lanzó el proceso. Así pm2 siempre escribe en el mismo lugar.
    carpeta = Path(__file__).resolve().parent / "logs"
    carpeta.mkdir(exist_ok=True)

    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta = carpeta / f"{prefijo}_{sello}.log"

    # buffering=1 = line-buffered: cada línea se vuelca al disco al instante.
    # Si el proceso muere de golpe, no se pierde lo último que se imprimió.
    _archivo = open(ruta, "a", encoding="utf-8", buffering=1)

    sys.stdout = _Tee(sys.__stdout__, _archivo)
    sys.stderr = _Tee(sys.__stderr__, _archivo)

    # Encabezado: clave para depurar el arranque desde pm2 en el VPS.
    print("=" * 70)
    print(f"INICIO RPA — {datetime.now().isoformat(timespec='seconds')}")
    print(f"  Ejecutable : {sys.executable}")
    print(f"  Directorio : {Path.cwd()}")
    print(f"  Argumentos : {' '.join(sys.argv[1:]) or '(ninguno)'}")
    print(f"  Log        : {ruta}")
    print("=" * 70)

    sys.excepthook = _excepthook
    atexit.register(_cerrar)
    return ruta


def _excepthook(exc_type, exc, tb) -> None:
    """Escribe cualquier error no controlado en el log antes de que muera todo."""
    # SystemExit (ej: argparse) y Ctrl+C se manejan normal, sin ruido de "ERROR".
    if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
        sys.__excepthook__(exc_type, exc, tb)
        return

    print("\n" + "=" * 70)
    print("✗ ERROR NO CONTROLADO — el proceso va a terminar")
    print("=" * 70)
    traceback.print_exception(exc_type, exc, tb)
    sys.__excepthook__(exc_type, exc, tb)


def _cerrar() -> None:
    """Cierra el archivo de log limpiamente al terminar el proceso."""
    global _archivo
    if _archivo:
        try:
            _archivo.flush()
            _archivo.close()
        except Exception:
            pass
        _archivo = None
