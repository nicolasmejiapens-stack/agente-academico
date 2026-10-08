import hmac
import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

import logica as L
import servicios as S

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gestor")
app = Flask(__name__)
LOCK = threading.Lock()

MAX_POR_CORRIDA = 15
LIMITE_BYTES = 15 * 1024 * 1024
FINALES = {"PROCESADO", "SIN_PROCESAR", "REQUIERE_OCR", "ERROR_MANUAL", "OTRO", "DUPLICADO_REVISAR"}


def autorizado():
    dado = request.headers.get("X-Cron-Token") or request.args.get("token", "")
    return hmac.compare_digest(dado, os.environ.get("CRON_TOKEN", "x" * 32))


def config_valida(materia, carpeta):
    return (materia and not materia.startswith("Escribe")
            and len(carpeta) >= 20 and " " not in carpeta and not carpeta.startswith("ID de"))


def procesar_archivo(sh, cfg, svc, f, materia, idx, materias_con_programa, hoy, inicio):
    """Devuelve (tipo, estado)."""
    nombre = f["name"]
    pista = bool(L.NOMBRE_PROGRAMA.search(nombre))
    if int(f.get("size") or 0) > LIMITE_BYTES and not pista:
        return "lectura", "SIN_PROCESAR"

    datos = S.descargar(svc, f)
    muestra = S.extraer_texto(f, datos, max_paginas=3)
    if pista:
        tipo = "programa"
    elif not muestra.strip():
        return "lectura", "SIN_PROCESAR"
    else:
        tipo = S.clasificar(cfg, nombre, muestra[:1500])

    if tipo != "programa":
        return tipo, ("SIN_PROCESAR" if tipo == "lectura" else "OTRO")

    if materia in materias_con_programa:
        S.telegram(cfg, f"⚠️ {materia}: encontré otro archivo que parece programa ({nombre}). "
                        "Ya había uno procesado, así que no lo mezclé. Revísalo.")
        return "programa", "DUPLICADO_REVISAR"

    texto = S.extraer_texto(f, datos)
    if len(texto.strip()) < 200:
        S.telegram(cfg, f"⚠️ {materia}: el programa '{nombre}' parece escaneado (sin texto). "
                        "Aún no lo puedo leer; falta agregar OCR.")
        return "programa", "REQUIERE_OCR"

    prog = S.extraer_programa(cfg, materia, inicio, texto)
    res = L.construir_filas(materia, idx, prog, inicio, hoy)
    if not res["n_semanas"] and not res["evals"]:
        raise S.ErrorIA("El programa no produjo semanas ni evaluaciones")
    S.agregar(sh, "Cronograma_Semestral", res["cron"])
    S.agregar(sh, "Control_Lecturas", res["lect"])
    S.agregar(sh, "Tareas", res["tareas"])
    S.telegram(cfg, L.mensaje_programa(materia, nombre, res))
    materias_con_programa.add(materia)
    return "programa", "PROCESADO"


def escanear():
    sh = S.abrir_sheet()
    cfg = S.leer_config(sh)
    svc = S.drive()
    vistos_ws = S.hoja_vistos(sh)
    zona = cfg.get("zona_horaria", "America/Bogota")
    hoy = datetime.now(ZoneInfo(zona)).date().isoformat()

    inicio = L.fecha_iso(cfg.get("fecha_inicio_semestre"))
    if not inicio:
        S.telegram(cfg, "⚠️ En Config, 'fecha_inicio_semestre' no es una fecha válida. Escríbela así: 2026-08-03")
        return {"ok": False, "procesados": 0, "pendientes_por_limite": 0, "problemas": ["fecha_inicio_semestre inválida"]}

    vistos, con_programa = {}, set()
    for n, fila in enumerate(vistos_ws.get_all_values()[1:], start=2):
        fila += [""] * (7 - len(fila))
        if fila[0]:
            vistos[fila[0]] = (n, fila[4], int(fila[5]) if fila[5].isdigit() else 0)
            if fila[3] == "programa" and fila[4] == "PROCESADO":
                con_programa.add(fila[1])

    resumen = {"ok": True, "procesados": 0, "pendientes_por_limite": 0, "problemas": []}
    for idx in range(1, 5):
        materia, carpeta = cfg.get(f"materia_{idx}", ""), cfg.get(f"carpeta_{idx}", "")
        if not config_valida(materia, carpeta):
            continue
        try:
            archivos = S.listar_archivos(svc, carpeta)
        except Exception as e:
            log.error("No pude leer la carpeta %s: %s", idx, type(e).__name__)
            resumen["problemas"].append(f"No pude leer la carpeta de {materia} (¿la compartiste con el robot?)")
            continue

        for f in archivos:
            if not S.soportado(f):
                continue
            previo = vistos.get(f["id"])
            if previo and previo[1] in FINALES:
                continue
            if resumen["procesados"] >= MAX_POR_CORRIDA:
                resumen["pendientes_por_limite"] += 1
                continue

            intentos = previo[2] if previo else 0
            tipo = ""
            try:
                tipo, estado = procesar_archivo(sh, cfg, svc, f, materia, idx, con_programa, hoy, inicio)
                intentos = 0
            except Exception as e:
                intentos += 1
                estado = "ERROR_MANUAL" if intentos >= 3 else "PENDIENTE_IA"
                log.error("Fallo procesando un archivo de la materia %s: %s", idx, type(e).__name__)
                if estado == "ERROR_MANUAL":
                    motivo = str(e) if isinstance(e, S.ErrorIA) else type(e).__name__
                    S.telegram(cfg, f"❌ No pude procesar '{f['name']}' ({materia}) tras 3 intentos: {motivo}")
                resumen["problemas"].append(f"{f['name']}: {estado}")

            fila = [f["id"], materia, f["name"], tipo, estado, intentos, hoy]
            if previo:
                vistos_ws.update(range_name=f"A{previo[0]}:G{previo[0]}", values=[fila])
            else:
                vistos_ws.append_row(fila, value_input_option="USER_ENTERED")
            vistos[f["id"]] = (previo[0] if previo else 0, estado, intentos)
            resumen["procesados"] += 1
    return resumen


@app.route("/")
def salud():
    return "ok"


def probar_conexion():
    sh = S.abrir_sheet()
    cfg = S.leer_config(sh)
    materias = [cfg.get(f"materia_{i}", "") for i in range(1, 5)]
    enviado = S.telegram(cfg, "✅ Conexión funcionando.\nMaterias en Config:\n" + "\n".join(f"• {m}" for m in materias))
    return {"sheet": "ok", "telegram": "ok" if enviado else "FALLÓ (revisa token y chat_id)"}


@app.route("/probar")
def probar():
    if not autorizado():
        return "No autorizado", 401
    try:
        return jsonify(probar_conexion())
    except Exception as e:
        log.exception("Fallo en /probar")
        return jsonify(error=type(e).__name__), 500


@app.route("/scan", methods=["GET", "POST"])
def scan():
    if not autorizado():
        return "No autorizado", 401
    if not LOCK.acquire(blocking=False):
        return jsonify(ok=False, msg="Ya hay un escaneo en curso"), 409
    try:
        return jsonify(escanear())
    except Exception as e:
        log.exception("Fallo en /scan")
        return jsonify(ok=False, error=type(e).__name__), 500
    finally:
        LOCK.release()
