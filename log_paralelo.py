"""
log_paralelo.py — Logs ordenados cuando varios scrapers corren a la vez.

EL PROBLEMA QUE RESUELVE
────────────────────────
main.py corre los scrapers en hilos y usaba contextlib.redirect_stdout para que
cada uno acumulara sus mensajes y los imprimiera juntos al final. Pero
redirect_stdout cambia sys.stdout GLOBALMENTE, no por hilo. Con tres hilos:

    hilo A entra → guarda "consola",  sys.stdout = buffer_A
    hilo B entra → guarda buffer_A,   sys.stdout = buffer_B
    hilo C entra → guarda buffer_B,   sys.stdout = buffer_C
    hilo A sale  → sys.stdout = "consola"      (parece correcto)
    hilo B sale  → sys.stdout = buffer_A       ← buffer ya descartado
    hilo C sale  → sys.stdout = buffer_B       ← buffer ya descartado

Al terminar, sys.stdout apunta a un buffer muerto y TODO lo que se imprima
despues se pierde sin aviso. En el flujo viejo casi no se notaba, porque cada
solicitud lanzaba un proceso que moria enseguida. Con el worker, que vive horas,
el stdout roto se queda roto: el proceso sigue trabajando y guardando en la base,
pero ciego.

LA SOLUCION
───────────
Un unico sys.stdout que sabe desde que hilo le estan escribiendo y enruta cada
linea al buffer de ese hilo. Si el hilo no registro buffer (el hilo principal,
por ejemplo), escribe a la consola de siempre. No se reemplaza nada mas: se
envuelve el _Tee que instala logger.py, asi el archivo de log sigue recibiendo
todo igual.

USO
───
    from log_paralelo import capturar_salida_del_hilo

    with capturar_salida_del_hilo() as buffer:
        ...codigo que imprime...

    print(buffer.getvalue(), end="")   # ya fuera, en orden
"""

from __future__ import annotations

import io
import sys
import threading
from contextlib import contextmanager


class _SalidaPorHilo:
    """
    Reemplaza sys.stdout una sola vez y reparte cada escritura segun el hilo.

    Es deliberadamente a prueba de fallos: si algo sale mal al escribir en el
    buffer de un hilo, cae a la salida real en lugar de perder la linea. Un
    problema de logging nunca debe tumbar una busqueda.
    """

    def __init__(self, salida_real):
        self._real = salida_real
        self._local = threading.local()

    # ── API para los hilos ──
    def registrar(self, buffer) -> None:
        self._local.buffer = buffer

    def liberar(self) -> None:
        self._local.buffer = None

    def _destino(self):
        return getattr(self._local, "buffer", None) or self._real

    # ── Interfaz de archivo ──
    def write(self, data):
        try:
            return self._destino().write(data)
        except Exception:
            try:
                return self._real.write(data)
            except Exception:
                return 0

    def flush(self):
        try:
            self._destino().flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return bool(self._real.isatty())
        except Exception:
            return False

    def __getattr__(self, nombre):
        # Cualquier otro atributo (encoding, fileno, errors...) lo delega a la
        # salida real, para que esto se comporte como un stdout normal.
        return getattr(self._real, nombre)


_instalacion = threading.Lock()
_enrutador: _SalidaPorHilo | None = None


def _asegurar_enrutador() -> _SalidaPorHilo:
    """Instala el enrutador en sys.stdout la primera vez que se necesita."""
    global _enrutador
    with _instalacion:
        if _enrutador is None or sys.stdout is not _enrutador:
            # Envuelve lo que haya ahora: la consola, o el _Tee de logger.py si
            # ya se llamo a configurar_logging().
            _enrutador = _SalidaPorHilo(sys.stdout)
            sys.stdout = _enrutador
        return _enrutador


@contextmanager
def capturar_salida_del_hilo():
    """
    Dentro del bloque, todo lo que imprima ESTE hilo va a un buffer propio.
    Los demas hilos no se ven afectados, y al salir la salida de este hilo
    vuelve a la normalidad sin tocar la de nadie mas.
    """
    enrutador = _asegurar_enrutador()
    buffer = io.StringIO()
    anterior = getattr(enrutador._local, "buffer", None)
    enrutador.registrar(buffer)
    try:
        yield buffer
    finally:
        # Se restaura lo que tenia ESTE hilo, no un valor global. Es la
        # diferencia con redirect_stdout y lo que evita el buffer muerto.
        enrutador.registrar(anterior)
