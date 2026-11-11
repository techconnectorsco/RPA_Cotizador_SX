from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import subprocess
import sys
import os

app = FastAPI(title="RPA Cotizador API")


class PeticionRPA(BaseModel):
    modo: str  # 'nueva', 'reintento', 'agregar'
    solicitud_id: str = None
    item_id: str = None
    texto: str = None


def ejecutar_en_background(args_lista):
    # En Windows, esto asegura que corra 100% despegado y sin abrir ventanas cmd
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(
        [sys.executable, "main.py"] + args_lista,
        creationflags=DETACHED_PROCESS,
        close_fds=True,
    )


@app.post("/api/disparar")
def disparar_rpa(req: PeticionRPA):
    if req.modo == "nueva":
        args = ["--solicitud-id", req.solicitud_id]
    elif req.modo == "reintento":
        args = ["--reintentar-item", req.item_id, "--nuevo-texto", req.texto]
    elif req.modo == "agregar":
        args = ["--agregar-busqueda", req.solicitud_id, "--texto", req.texto]
    else:
        return {"status": "error", "mensaje": "Modo no reconocido"}

    ejecutar_en_background(args)
    return {"status": "ok", "mensaje": f"RPA lanzado en background. Modo: {req.modo}"}
