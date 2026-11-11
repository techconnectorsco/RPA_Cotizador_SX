from fastapi import FastAPI
from pydantic import BaseModel
import subprocess
import sys
import os
from pathlib import Path

app = FastAPI(title="RPA Cotizador API")
BASE_DIR = Path(__file__).resolve().parent


class PeticionRPA(BaseModel):
    modo: str
    solicitud_id: str | None = None
    item_id: str | None = None
    texto: str | None = None


def ejecutar_en_background(args_lista: list[str]):
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(
        [sys.executable, "main.py"] + args_lista,
        cwd=str(BASE_DIR),
        env=os.environ.copy(),  # <--- Hereda exactamente el bloque env de PM2
        creationflags=DETACHED_PROCESS,
        close_fds=True,
    )


@app.post("/api/disparar")
def disparar_rpa(req: PeticionRPA):
    if req.modo == "nueva" and req.solicitud_id:
        args = ["--solicitud-id", req.solicitud_id]
    elif req.modo == "reintento" and req.item_id and req.texto:
        args = ["--reintentar-item", req.item_id, "--nuevo-texto", req.texto]
    elif req.modo == "agregar" and req.solicitud_id and req.texto:
        args = ["--agregar-busqueda", req.solicitud_id, "--texto", req.texto]
    else:
        return {"status": "error", "mensaje": "Parámetros inválidos"}

    ejecutar_en_background(args)
    return {"status": "ok", "carpeta": str(BASE_DIR), "modo": req.modo}
