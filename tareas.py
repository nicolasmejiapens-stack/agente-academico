"""Recordatorios (mañana/noche) y botones de Telegram."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import logica as L
import servicios as S


def _hoy(cfg):
    return datetime.now(ZoneInfo(cfg.get("zona_horaria", "America/Bogota"))).date().isoformat()


def leer_tareas(ws):
    tareas = []
    for n, f in enumerate(ws.get_all_values(value_render_option="UNFORMATTED_VALUE")[1:], start=2):
        f = list(f) + [""] * (9 - len(f))
        if not str(f[0]).strip():
            continue
        tareas.append({"id": str(f[0]).strip(), "materia": str(f[1]), "desc": str(f[2]), "tipo": str(f[3]).strip(),
                       "fecha": L.fecha_iso(f[4]), "estado": str(f[6]).strip().upper(), "fila": n})
    return tareas


def enviar_resumen(modo):
    sh = S.abrir_sheet()
    cfg = S.leer_config(sh)
    texto, botones = L.armar_resumen(leer_tareas(sh.worksheet("Tareas")), _hoy(cfg), modo)
    if not texto:
        return {"enviado": False}
    datos = {"chat_id": cfg.get("telegram_chat_id", ""), "text": texto, "disable_web_page_preview": True}
    if botones:
        datos["reply_markup"] = {"inline_keyboard": botones}
    return {"enviado": bool(S.tg("sendMessage", **datos).get("ok"))}


def _marcar(ws, tarea_id):
    """Marca una tarea. Devuelve (estado_nuevo, descripcion, ya_estaba, tipo) o None si no existe."""
    ids = ws.col_values(1)
    if tarea_id not in ids:
        return None
    n = ids.index(tarea_id) + 1
    fila = ws.row_values(n) + [""] * 9
    tipo, estado = fila[3].strip(), fila[6].strip().upper()
    nuevo = "ENTREGADO" if tipo in ("guía", "guia", "entrega") else "ESTUDIADO"
    if estado == nuevo:
        return nuevo, fila[2], True, tipo
    ws.update(range_name=f"G{n}", values=[[nuevo]])
    return nuevo, fila[2], False, tipo


def _marcar_lectura(sh, prog_id, estado):
    """Cambia el estado en Control_Lecturas. Devuelve (titulo, ya_estaba) o None."""
    ws = sh.worksheet("Control_Lecturas")
    ids = ws.col_values(1)
    if prog_id not in ids:
        return None
    n = ids.index(prog_id) + 1
    fila = ws.row_values(n) + [""] * 15
    ya = fila[11].strip().upper() == estado
    if not ya:
        ws.update(range_name=f"L{n}", values=[[estado]])
    return fila[3], ya


def _enviar_original(sh, cfg, prog_id):
    ws = sh.worksheet("Control_Lecturas")
    ids = ws.col_values(1)
    if prog_id not in ids:
        return False
    fila = ws.row_values(ids.index(prog_id) + 1) + [""] * 15
    m = re.search(r"/d/([A-Za-z0-9_-]+)", fila[13])
    if not m:
        return False
    svc = S.drive()
    meta = svc.files().get(fileId=m.group(1), fields="name,size,mimeType", supportsAllDrives=True).execute()
    chat = cfg.get("telegram_chat_id", "")
    if int(meta.get("size") or 0) > S_MAX:
        S.tg("sendMessage", chat_id=chat, text=f"El PDF es muy pesado para Telegram. Ábrelo en Drive:\n{fila[13]}")
        return True
    datos = S.descargar(svc, {"id": m.group(1), "mimeType": meta["mimeType"]})
    return bool(S.tg_documento(chat, meta["name"], datos, "📖 PDF completo"))


S_MAX = 49 * 1024 * 1024


def _accion(sh, cfg, data):
    """Ejecuta un botón. Devuelve (aviso, linea_extra, quitar_boton)."""
    tipo, _, cid = data.partition("|")
    if tipo == "ok":
        res = _marcar(sh.worksheet("Tareas"), cid)
        if not res:
            return "No encontré esa tarea", None, True
        if res[3] == "lectura":
            _marcar_lectura(sh, "PROG" + cid[1:], "ESTUDIADO")
        return "Hecho ✅", (None if res[2] else f"✅ {res[1]}"), True
    if tipo == "est":
        r = _marcar_lectura(sh, cid, "ESTUDIADO")
        if not r:
            return "No encontré la lectura", None, True
        _marcar(sh.worksheet("Tareas"), "T" + cid[4:])  # sincroniza la tarea, si existe
        return "Estudiado ✅", (None if r[1] else f"✅ Estudiado: {r[0]}"), True
    if tipo == "gu":
        r = _marcar_lectura(sh, cid, "ENTREGADO")
        if not r:
            return "No encontré la lectura", None, True
        return "Guía registrada 📝", (None if r[1] else f"📝 Guía realizada: {r[0]}"), True
    if tipo == "pdf":
        ok = _enviar_original(sh, cfg, cid)
        return ("Enviando el PDF 📖" if ok else "No pude enviar el PDF"), None, False
    return "", None, False


def procesar_botones():
    sh = S.abrir_sheet()
    cfg = S.leer_config(sh)
    chat = str(cfg.get("telegram_chat_id", ""))
    r = S.tg("getUpdates", timeout=0, allowed_updates=["callback_query"])
    ups = r.get("result", []) if r.get("ok") else []
    if not ups:
        return 0
    mensajes, hechas = {}, 0

    for u in ups:
        cq = u.get("callback_query")
        if not cq:
            continue
        msg = cq.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != chat:  # ignora a cualquier otra persona
            S.tg("answerCallbackQuery", callback_query_id=cq["id"])
            continue
        try:
            aviso, extra, quitar = _accion(sh, cfg, cq.get("data", ""))
        except Exception as e:
            S.log.error("Fallo en un botón: %s", type(e).__name__)
            aviso, extra, quitar = "Ocurrió un error, intenta de nuevo", None, False
        S.tg("answerCallbackQuery", callback_query_id=cq["id"], text=aviso)
        if not quitar:
            continue
        mid = msg.get("message_id")
        media = bool(msg.get("document") or msg.get("caption") is not None)
        est = mensajes.setdefault(mid, {
            "texto": (msg.get("caption") if media else msg.get("text")) or "", "media": media,
            "kb": (msg.get("reply_markup") or {}).get("inline_keyboard", []), "extra": []})
        est["kb"] = [row for row in est["kb"] if not any(b.get("callback_data") == cq.get("data") for b in row)]
        if extra:
            est["extra"].append(extra)
            hechas += 1

    for mid, est in mensajes.items():
        texto = est["texto"] + ("\n\n" + "\n".join(est["extra"]) if est["extra"] else "")
        kb = {"inline_keyboard": est["kb"]}
        if est["media"]:
            S.tg("editMessageCaption", chat_id=chat, message_id=mid, caption=texto[:1000], reply_markup=kb)
        else:
            S.tg("editMessageText", chat_id=chat, message_id=mid, text=texto[:4000], reply_markup=kb)

    S.tg("getUpdates", offset=max(u["update_id"] for u in ups) + 1, limit=1, timeout=0)  # confirma lo leído
    return hechas
