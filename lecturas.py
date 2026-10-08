"""Etapa 3: asociar cada PDF a su lectura del programa, calcular páginas, recortar, resumir y enviar."""
import io
import logging
import os
import re
import tempfile
import time

from pypdf import PdfReader, PdfWriter

import logica as L
import servicios as S

log = logging.getLogger("gestor")
MAX_TG = 49 * 1024 * 1024
ESPERA = ("REVISAR_OFFSET", "REVISAR_PAGINAS")
CANDIDATOS = ("SIN_PROCESAR", "PENDIENTE_IA") + ESPERA


class TiempoAgotado(Exception):
    pass


class Documento:
    """PDF con extracción de texto digital y, si la página es una imagen, OCR con Tesseract."""

    def __init__(self, datos, deadline):
        self.datos, self.deadline = datos, deadline
        self.lector = PdfReader(io.BytesIO(datos))
        if self.lector.is_encrypted and not self.lector.decrypt(""):
            raise ValueError("PDF protegido con contraseña")
        self.n = len(self.lector.pages)
        self._cache, self._ruta, self.offset = {}, None, "sin_calcular"

    def digital(self, p):
        if p not in self._cache:
            try:
                self._cache[p] = self.lector.pages[p - 1].extract_text() or ""
            except Exception:
                self._cache[p] = ""
        return self._cache[p]

    def vacia(self, p):
        return len(self.digital(p).strip()) < 40

    def texto(self, p, ocr=True):
        t = self.digital(p)
        return t if (not self.vacia(p) or not ocr) else self._ocr(p)

    def _ocr(self, p):
        if time.monotonic() > self.deadline:
            raise TiempoAgotado()
        from pdf2image import convert_from_path
        import pytesseract
        if self._ruta is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            tmp.write(self.datos)
            tmp.close()
            self._ruta = tmp.name
        imgs = convert_from_path(self._ruta, dpi=200, first_page=p, last_page=p)
        return pytesseract.image_to_string(imgs[0], lang="spa+eng") if imgs else ""

    def cerrar(self):
        if self._ruta and os.path.exists(self._ruta):
            os.remove(self._ruta)

    def recortar(self, a, b):
        w = PdfWriter()
        for p in range(a, b + 1):
            w.add_page(self.lector.pages[p - 1])
        buf = io.BytesIO()
        w.write(buf)
        return buf.getvalue()


def detectar_offset(doc, pi):
    """Desfase entre página impresa y página del visor. None si no se puede determinar."""
    ventana = list(range(max(1, pi - 5), min(doc.n, pi + 45) + 1))
    obs = [(p, L.numeros_de_pagina(doc.digital(p))) for p in ventana]
    off = L.decidir_offset(obs, 3)
    if off is not None:
        return off
    vacias = [p for p in ventana if doc.vacia(p)]
    if not vacias:
        return None
    for p in vacias[::max(1, len(vacias) // 8)][:8]:
        obs.append((p, L.numeros_de_pagina(doc.texto(p))))
    return L.decidir_offset(obs, 2)


def _leer_filas(sh):
    filas = []
    valores = sh.worksheet("Control_Lecturas").get_all_values(value_render_option="UNFORMATTED_VALUE")
    for n, r in enumerate(valores[1:], start=2):
        r = list(r) + [""] * (15 - len(r))
        if not str(r[0]).strip():
            continue
        filas.append({"n": n, "id": str(r[0]).strip(), "materia": str(r[1]), "semana": r[2], "titulo": str(r[3]),
                      "autor": str(r[4]), "obl": str(r[5]).strip().upper() != "NO", "pi": L.entero(r[6]),
                      "pf": L.entero(r[7]), "off": L.entero(r[8]), "estado": str(r[11]).strip() or "PENDIENTE",
                      "link_f": str(r[12]).strip(), "link_o": str(r[13]).strip()})
    return filas


def _es_pdf(nombre):
    return nombre.lower().endswith(".pdf")


def _ficha(nombre, datos):
    n = nombre.lower()
    mime = "application/pdf" if n.endswith(".pdf") else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if n.endswith(".docx")
        else "application/vnd.google-apps.document")
    return {"name": nombre, "mimeType": mime}


def _enviar_lectura(cfg, r, a, b, recortado, doc_bytes, nombre_doc, resumen, hoy):
    chat = cfg.get("telegram_chat_id", "")
    pag = ""
    if r["pi"] is not None and r["pf"] is not None:
        pag = f"\nPáginas impresas {r['pi']}–{r['pf']}" + (f" (visor PDF {a}–{b})" if recortado else "")
    cab = (f"📖 {r['materia']} · Semana {r['semana']}\n{r['autor']} — {r['titulo']}{pag}\n"
           f"{'Obligatoria' if r['obl'] else '📎 Sugerida'}\n\n")
    for parte in L.partir(cab + resumen):
        S.tg("sendMessage", chat_id=chat, text=parte, disable_web_page_preview=True)
    kb = [[{"text": "✅ Estudiado", "callback_data": "est|" + r["id"]},
           {"text": "📝 Guía realizada", "callback_data": "gu|" + r["id"]}]]
    if recortado:
        kb.append([{"text": "📖 PDF completo", "callback_data": "pdf|" + r["id"]}])
    file_id = None
    if doc_bytes and len(doc_bytes) <= MAX_TG:
        cap = f"📄 {'Fragmento' if recortado else 'Lectura'}: {r['titulo']}"[:200]
        file_id = S.tg_documento(chat, nombre_doc, doc_bytes, cap, {"inline_keyboard": kb})
    if not file_id:
        extra = "\n(No pude adjuntar el PDF; está en tu carpeta de Drive.)" if doc_bytes else ""
        S.tg("sendMessage", chat_id=chat, text="Marca tu avance:" + extra, reply_markup={"inline_keyboard": kb})
    return f"tg:{file_id}" if file_id else "(sin archivo adjunto)"


def _procesar_fila(cfg, doc, datos_originales, nombre, r, hoy, ws_cl, link_o):
    """Devuelve 'ok', 'offset' o 'paginas'. Envía todo a Telegram si sale bien."""
    pi, pf = r["pi"], r["pf"]
    off, recortado = None, False
    if doc is None:  # Word / Google Doc: se resume completo
        texto = S.extraer_texto(_ficha(nombre, datos_originales), datos_originales)
        a = b = ""
        doc_bytes, nombre_doc = None, nombre
    else:
        if pi is None and pf is None:
            if doc.n > 150:
                return "paginas"
            a, b = 1, doc.n
        elif pi is None or pf is None or pf < pi:
            return "paginas"
        elif doc.n <= (pf - pi + 1) + 6:  # el PDF ya es el fragmento
            a, b = 1, doc.n
        else:
            off = r["off"]
            if off is None:
                if doc.offset == "sin_calcular":
                    doc.offset = detectar_offset(doc, pi)
                off = doc.offset
            if off is None:
                return "offset"
            a, b = pi + off, pf + off
            if a < 1 or b > doc.n:
                return "offset"
            recortado = True
        rango = range(a, b + 1)
        if sum(1 for p in rango if doc.vacia(p)) > 45:
            return "paginas"  # demasiadas páginas escaneadas para un solo OCR
        texto = "\n\n".join(doc.texto(p) for p in rango)
        doc_bytes = doc.recortar(a, b) if recortado else datos_originales
        nombre_doc = (f"S{r['semana']}_{r['titulo'][:40]}_pp{a}-{b}.pdf" if recortado else nombre)

    if len(texto.strip()) < 300:
        raise ValueError("No pude extraer texto suficiente de la lectura")
    resumen = S.resumir(cfg, r["materia"], r["semana"], r["titulo"], r["autor"], texto, profundo=r["obl"])
    link_f = _enviar_lectura(cfg, r, a, b, recortado, doc_bytes, nombre_doc, resumen, hoy)
    ws_cl.update(range_name=f"I{r['n']}:O{r['n']}",
                 values=[[off if off is not None else "", a, b, r["estado"], link_f, link_o, hoy]])
    return "ok"


def _procesar_archivo(sh, cfg, svc, id_arch, materia, nombre, estado, hoy, deadline):
    """Devuelve el estado nuevo del archivo en Archivos_Vistos, o None si no hay nada que hacer aún."""
    filas = _leer_filas(sh)
    link_o = f"https://drive.google.com/file/d/{id_arch}/view"
    reclamadas = [r for r in filas if id_arch in r["link_o"]]

    if estado in ESPERA:  # esperando que completes datos en el Sheet
        pend = [r for r in reclamadas if not r["link_f"]]
        listo = (any(r["off"] is not None for r in pend) if estado == "REVISAR_OFFSET"
                 else any(r["pi"] is not None and r["pf"] is not None for r in pend))
        if not listo:
            return None

    es_pdf = _es_pdf(nombre)
    datos = S.descargar(svc, {"id": id_arch, **_ficha(nombre, b"")})
    doc = Documento(datos, deadline) if es_pdf else None
    try:
        if not reclamadas:
            libres = [r for r in filas if r["materia"] == materia and r["id"].startswith("PROG") and not r["link_o"]]
            muestra = ("\n".join(doc.digital(p) for p in (1, 2) if p <= doc.n) if doc
                       else S.extraer_texto(_ficha(nombre, datos), datos)[:1500])
            ids = S.asociar_lectura(cfg, nombre, muestra, libres) if libres else []
            if not ids:
                S.telegram(cfg, f"📎 {materia}: no pude asociar '{nombre}' a ninguna lectura del programa. "
                                "Si es una lectura, revisa que el programa esté procesado; luego cambia su estado a "
                                "SIN_PROCESAR en la pestaña Archivos_Vistos para reintentar.")
                return "SIN_ASOCIAR"
            ws_cl = sh.worksheet("Control_Lecturas")
            for r in filas:
                if r["id"] in ids:
                    ws_cl.update(range_name=f"N{r['n']}", values=[[link_o]])
                    r["link_o"] = link_o
            reclamadas = [r for r in filas if r["id"] in ids]

        ws_cl = sh.worksheet("Control_Lecturas")
        pendientes = [r for r in reclamadas if not r["link_f"]]
        faltan = []
        for r in pendientes:
            res = _procesar_fila(cfg, doc, datos, nombre, r, hoy, ws_cl, link_o)
            if res != "ok":
                faltan.append((r, res))
    finally:
        if doc:
            doc.cerrar()

    if not faltan:
        return "PROCESADO"
    nuevo = "REVISAR_OFFSET" if any(x[1] == "offset" for x in faltan) else "REVISAR_PAGINAS"
    if estado not in ESPERA:
        lista = "\n".join(f"• Semana {r['semana']}: {r['titulo']}" for r, _ in faltan)
        if nuevo == "REVISAR_OFFSET":
            msg = ("No pude calcular el desfase entre la página impresa y la del visor del PDF. "
                   "En la pestaña Control_Lecturas, llena la columna Offset de estas lecturas "
                   "(página del visor menos página impresa; por ejemplo, si la 45 impresa es la 57 del visor, escribe 12)")
        else:
            msg = ("Faltan las páginas de estas lecturas, o el rango es demasiado largo para leerlo con OCR. "
                   "Llena Pag_impresa_inicio y Pag_impresa_fin en Control_Lecturas")
        S.telegram(cfg, f"🔧 {materia} — '{nombre}': {msg}:\n{lista}")
    return nuevo


def procesar_lecturas(sh, cfg, svc, hoy, deadline, max_archivos=4):
    ws_v = S.hoja_vistos(sh)
    hechos, resultado = 0, {"procesadas": 0, "pendientes": 0}
    cands = []
    for n, f in enumerate(ws_v.get_all_values()[1:], start=2):
        f = list(f) + [""] * (7 - len(f))
        if f[3] == "lectura" and f[4] in CANDIDATOS:
            cands.append((n, f))
    for n, f in cands:
        if hechos >= max_archivos or time.monotonic() > deadline - 180:
            resultado["pendientes"] += 1
            continue
        id_arch, materia, nombre, estado = f[0], f[1], f[2], f[4]
        intentos = int(f[5]) if str(f[5]).isdigit() else 0
        try:
            nuevo = _procesar_archivo(sh, cfg, svc, id_arch, materia, nombre, estado, hoy, deadline)
            intentos = 0
        except TiempoAgotado:
            resultado["pendientes"] += 1
            break
        except Exception as e:
            intentos += 1
            nuevo = "ERROR_MANUAL" if intentos >= 3 else "PENDIENTE_IA"
            log.error("Fallo con una lectura de la materia: %s", type(e).__name__)
            if nuevo == "ERROR_MANUAL":
                motivo = str(e) if isinstance(e, (S.ErrorIA, ValueError)) else type(e).__name__
                S.telegram(cfg, f"❌ No pude procesar la lectura '{nombre}' ({materia}) tras 3 intentos: {motivo}")
        if nuevo is None:
            continue
        ws_v.update(range_name=f"E{n}:G{n}", values=[[nuevo, intentos, hoy]])
        hechos += 1
        if nuevo == "PROCESADO":
            resultado["procesadas"] += 1
    return resultado
