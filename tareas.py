"""Recordatorios (mañana/noche) y botones de Telegram."""
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
    r = S.tg("sendMessage", **datos)
    return {"enviado": bool(r.get("ok"))}


def _marcar(ws, tarea_id):
    """Devuelve (estado_nuevo, descripcion, ya_estaba) o None si no existe."""
    ids = ws.col_values(1)
    if tarea_id not in ids:
        return None
    n = ids.index(tarea_id) + 1
    fila = ws.row_values(n) + [""] * 9
    tipo, estado = fila[3].strip(), fila[6].strip().upper()
    nuevo = "ENTREGADO" if tipo in ("guía", "guia", "entrega") else "ESTUDIADO"
    if estado == nuevo:
        return nuevo, fila[2], True
    ws.update(range_name=f"G{n}", values=[[nuevo]])
    return nuevo, fila[2], False


def procesar_botones():
    sh = S.abrir_sheet()
    cfg = S.leer_config(sh)
    chat = str(cfg.get("telegram_chat_id", ""))
    r = S.tg("getUpdates", timeout=0, allowed_updates=["callback_query"])
    ups = r.get("result", []) if r.get("ok") else []
    if not ups:
        return 0
    ws = sh.worksheet("Tareas")
    mensajes, hechas = {}, 0

    for u in ups:
        cq = u.get("callback_query")
        if not cq:
            continue
        msg = cq.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != chat:
            S.tg("answerCallbackQuery", callback_query_id=cq["id"])  # ignora a cualquier otra persona
            continue
        data = cq.get("data", "")
        if not data.startswith("ok|"):
            S.tg("answerCallbackQuery", callback_query_id=cq["id"])
            continue
        res = _marcar(ws, data[3:])
        S.tg("answerCallbackQuery", callback_query_id=cq["id"],
             text="Hecho ✅" if res else "No encontré esa tarea")
        mid = msg.get("message_id")
        est = mensajes.setdefault(mid, {"texto": msg.get("text", ""),
                                        "kb": (msg.get("reply_markup") or {}).get("inline_keyboard", []),
                                        "extra": []})
        est["kb"] = [row for row in est["kb"] if not any(b.get("callback_data") == data for b in row)]
        if res and not res[2]:
            est["extra"].append(f"✅ {res[1]}")
            hechas += 1

    for mid, est in mensajes.items():
        texto = est["texto"] + ("\n\n" + "\n".join(est["extra"]) if est["extra"] else "")
        S.tg("editMessageText", chat_id=chat, message_id=mid, text=texto[:4000],
             reply_markup={"inline_keyboard": est["kb"]})

    S.tg("getUpdates", offset=max(u["update_id"] for u in ups) + 1, limit=1, timeout=0)  # confirma lo leído
    return hechas
