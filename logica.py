"""Lógica pura (sin conexiones externas): fechas, páginas y construcción de filas."""
import re
import uuid
from collections import Counter
from datetime import date, timedelta

NOMBRE_PROGRAMA = re.compile(
    r"syllabus|s[ií]labo|programa\s+(de\s+)?(la\s+)?(materia|asignatura|curso|acad[eé]mico)"
    r"|plan\s+de\s+(curso|asignatura)|microcurr[ií]culo",
    re.I,
)


def limpio(v):
    """Texto seguro para Sheets: evita que un texto que empiece con = + - @ se lea como fórmula."""
    s = "" if v is None else str(v).strip()
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def fecha_iso(v):
    """Acepta ISO, dd/mm/aaaa o el número de serie que entrega Google Sheets."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    s = str(v).strip()
    if isinstance(v, (int, float)) or re.fullmatch(r"\d{5}(\.\d+)?", s):
        try:
            n = int(float(s))
        except Exception:
            return None
        return (date(1899, 12, 30) + timedelta(days=n)).isoformat() if 20000 < n < 80000 else None
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except Exception:
        pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except Exception:
            return None
    return None


def entero(v):
    try:
        return int(float(str(v).strip()))
    except Exception:
        return None


def porcentaje(v):
    try:
        return float(str(v).replace("%", "").replace(",", ".").strip())
    except Exception:
        return None


def inicio_semana(inicio_semestre, semana):
    d, n = fecha_iso(inicio_semestre), entero(semana)
    if not d or not n or n < 1:
        return None
    return date.fromisoformat(d) + timedelta(days=7 * (n - 1))


def paginas(txt):
    nums = re.findall(r"\d+", str(txt or ""))
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    if len(nums) == 1:
        return int(nums[0]), int(nums[0])
    return "", ""


def construir_filas(materia, idx, programa, inicio_semestre, hoy):
    suf = uuid.uuid4().hex[:4]
    cron, lect, tareas, evals, avisos = [], [], [], [], []
    n_sem = 0

    for s in programa.get("semanas") or []:
        n = entero(s.get("semana"))
        if not n:
            continue
        n_sem += 1
        ini = inicio_semana(inicio_semestre, n)
        cron.append([f"CR{idx}-S{n}-{suf}", materia, n, limpio(s.get("tema")), "clase",
                     ini.isoformat() if ini else "", "", "PENDIENTE"])
        for k, l in enumerate(s.get("lecturas") or [], 1):
            titulo, autor = limpio(l.get("titulo")), limpio(l.get("autor"))
            if not titulo and not autor:
                continue
            p_ini, p_fin = paginas(l.get("paginas_impresas"))
            obligatoria = l.get("obligatoria") is not False  # obligatoria por defecto
            lect.append([f"PROG{idx}-S{n}-L{k}-{suf}", materia, n, titulo, autor,
                         "SI" if obligatoria else "NO", p_ini, p_fin, "", "", "",
                         "PENDIENTE", "", "", hoy])
            if obligatoria:
                fin = ini + timedelta(days=6) if ini else None
                desc = "Leer: " + (titulo or autor) + (f" ({autor})" if titulo and autor else "")
                tareas.append([f"T{idx}-S{n}-L{k}-{suf}", materia, limpio(desc), "lectura",
                               fin.isoformat() if fin else "", "programa", "PENDIENTE", "", hoy])

    total = 0.0
    for j, e in enumerate(programa.get("evaluaciones") or [], 1):
        nombre = limpio(e.get("nombre")) or "Evaluación"
        tipo = e.get("tipo") if e.get("tipo") in ("parcial", "quiz", "entrega") else "entrega"
        n = entero(e.get("semana"))
        f = fecha_iso(e.get("fecha")) if e.get("fecha") else None
        aprox = False
        if not f and n:
            ini = inicio_semana(inicio_semestre, n)
            if ini:
                f, aprox = ini.isoformat(), True
        pct = porcentaje(e.get("porcentaje"))
        if pct is not None:
            total += pct
        etiqueta = nombre + (" (fecha aproximada: inicio de la semana)" if aprox else "")
        cron.append([f"CR{idx}-E{j}-{suf}", materia, n or "", etiqueta, tipo, f or "",
                     f"{pct:g}%" if pct is not None else "", "PENDIENTE"])
        tareas.append([f"T{idx}-E{j}-{suf}", materia, etiqueta,
                       "parcial" if tipo in ("parcial", "quiz") else "entrega",
                       f or "", "programa", "PENDIENTE", "", hoy])
        evals.append({"nombre": nombre, "tipo": tipo, "fecha": f, "aprox": aprox, "pct": pct})
        if not f:
            avisos.append(f"'{nombre}' no tiene fecha en el programa")

    if evals and abs(total - 100) > 0.5:
        avisos.append(f"Los porcentajes suman {total:g}%, no 100%. Revisa el programa")
    if n_sem and n_sem != 16:
        avisos.append(f"Se detectaron {n_sem} semanas (esperaba 16)")

    return {"cron": cron, "lect": lect, "tareas": tareas, "evals": evals,
            "n_semanas": n_sem, "n_lecturas": len(lect), "avisos": avisos}


def mensaje_programa(materia, nombre_archivo, res):
    t = [f"📘 Programa procesado: {materia}", f"Archivo: {nombre_archivo}",
         f"Semanas: {res['n_semanas']} · Lecturas: {res['n_lecturas']} · Evaluaciones: {len(res['evals'])}"]
    if res["evals"]:
        t.append("\n📅 Evaluaciones:")
        for e in res["evals"]:
            fecha = (e["fecha"] or "sin fecha") + (" (aprox.)" if e["aprox"] else "")
            pct = f" · {e['pct']:g}%" if e["pct"] is not None else ""
            t.append(f"• {e['nombre']} ({e['tipo']}) — {fecha}{pct}")
    if res["avisos"]:
        t.append("\n⚠️ Revisar:")
        t += [f"• {a}" for a in res["avisos"]]
    return "\n".join(t)


# ---------- Recordatorios ----------
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _cuando(d):
    if d < 0:
        return f"venció hace {-d} d"
    return {0: "vence hoy", 1: "vence mañana"}.get(d, f"en {d} días")


def _linea(t):
    return f"• [{t['materia']}] {t['desc']} — {_cuando(t['dias'])}"


def _boton(t):
    return [{"text": "✅ " + t["desc"][:30], "callback_data": "ok|" + t["id"]}]


def armar_resumen(tareas, hoy, modo="manana", max_botones=10):
    """Devuelve (texto, botones). Texto vacío = no enviar nada."""
    h = date.fromisoformat(hoy)
    pend = []
    for t in tareas:
        if t["estado"] != "PENDIENTE" or t["id"].startswith("EJEMPLO"):
            continue
        d = (date.fromisoformat(t["fecha"]) - h).days if t["fecha"] else None
        pend.append({**t, "dias": d})
    atras = sorted([t for t in pend if t["dias"] is not None and t["dias"] < 0], key=lambda t: -t["dias"])
    de_hoy = [t for t in pend if t["dias"] == 0]
    semana = sorted([t for t in pend if t["dias"] is not None and 1 <= t["dias"] <= 7], key=lambda t: t["dias"])
    fecha_txt = f"{DIAS[h.weekday()]} {h.day} {MESES[h.month - 1]}"

    if modo == "noche":
        lista = de_hoy + atras[:5]
        if not lista:
            return "", []
        texto = f"🌙 Aún pendiente ({fecha_txt}):\n" + "\n".join(_linea(t) for t in lista)
        return texto[:3900], [_boton(t) for t in lista[:max_botones]]

    partes = [f"☀️ Buenos días — {fecha_txt}"]
    alertas = [t for t in pend if t["tipo"] in ("parcial", "entrega") and t["dias"] in (7, 3, 1, 0)]
    if alertas:
        partes.append("\n🚨 Alertas:\n" + "\n".join(
            f"• {t['desc']} ({t['materia']}): " + ("es HOY" if t["dias"] == 0 else f"faltan {t['dias']} días")
            for t in alertas))
    if atras:
        extra = f" (mostrando 5 de {len(atras)})" if len(atras) > 5 else ""
        partes.append(f"\n🔴 Atrasadas{extra}:\n" + "\n".join(_linea(t) for t in atras[:5]))
    if de_hoy:
        partes.append("\n📌 Para hoy:\n" + "\n".join(_linea(t) for t in de_hoy))
    if semana:
        extra = f"\n… y {len(semana) - 10} más" if len(semana) > 10 else ""
        partes.append("\n📅 Próximos 7 días:\n" + "\n".join(_linea(t) for t in semana[:10]) + extra)
    parciales = sorted([t for t in pend if t["tipo"] == "parcial" and t["dias"] is not None and t["dias"] >= 0],
                       key=lambda t: t["dias"])
    if parciales:
        p = parciales[0]
        partes.append(f"\n🎯 Próximo parcial: {p['desc']} ({p['materia']}) — {_cuando(p['dias'])}")
    if not (atras or de_hoy or semana):
        partes.append("\nSin pendientes para hoy ni esta semana 🎉")
    lista = atras[:5] + de_hoy + semana
    return "\n".join(partes)[:3900], [_boton(t) for t in lista[:max_botones]]


# ---------- Páginas y texto (lecturas) ----------
def numeros_de_pagina(texto):
    """Números impresos que parecen número de página (primeras y últimas líneas)."""
    lineas = [x.strip() for x in (texto or "").splitlines() if x.strip()]
    nums = set()
    for x in lineas[:3] + lineas[-3:]:
        m = re.fullmatch(r"[\W_]*(\d{1,4})[\W_]*", x)
        if not m and len(x) <= 80:
            m = re.fullmatch(r"(\d{1,4})\s+[^\d]{3,}", x) or re.fullmatch(r"[^\d]{3,}\s+(\d{1,4})", x)
        if m:
            nums.add(int(m.group(1)))
    return {n for n in nums if 1 <= n <= 2000}


def decidir_offset(observaciones, min_votos=3):
    """observaciones: [(pagina_pdf, {numeros})]. Devuelve el desfase (pág. PDF - pág. impresa) o None."""
    c = Counter()
    for p, nums in observaciones:
        for n in nums:
            if -10 <= p - n <= 120:
                c[p - n] += 1
    mc = c.most_common(2)
    if not mc:
        return None
    off, votos = mc[0]
    segundo = mc[1][1] if len(mc) > 1 else 0
    return off if votos >= min_votos and votos >= 2 * segundo else None


def partir(texto, limite=3800):
    """Parte un texto largo en trozos que caben en un mensaje, cortando en saltos de línea."""
    partes, actual = [], ""
    for linea in (texto or "").split("\n"):
        while len(linea) > limite:
            if actual:
                partes.append(actual)
                actual = ""
            partes.append(linea[:limite])
            linea = linea[limite:]
        if len(actual) + len(linea) + 1 > limite:
            if actual:
                partes.append(actual)
            actual = linea
        else:
            actual = (actual + "\n" + linea) if actual else linea
    if actual:
        partes.append(actual)
    return partes


_EN = {"the", "and", "of", "to", "in", "is", "that", "for", "with", "was", "were", "which", "this", "by",
       "are", "from", "their", "have", "has", "his", "her", "they", "been", "not", "but", "these", "than"}
_ES = {"el", "la", "los", "las", "de", "y", "en", "que", "un", "una", "es", "por", "con", "para", "se", "del",
       "al", "como", "su", "más", "sus", "lo", "fue", "son", "esta", "este", "entre", "sobre"}


def idioma(texto):
    """'en', 'es', 'mixto' o 'desconocido', contando palabras muy comunes (sin usar IA)."""
    palabras = re.findall(r"[a-záéíóúñü]+", (texto or "")[:8000].lower())
    en = sum(1 for w in palabras if w in _EN)
    es = sum(1 for w in palabras if w in _ES)
    if en + es < 20:
        return "desconocido"
    if en > es * 1.5:
        return "en"
    return "es" if es > en * 1.5 else "mixto"
