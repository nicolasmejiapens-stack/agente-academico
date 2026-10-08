"""Lógica pura (sin conexiones externas): fechas, páginas y construcción de filas."""
import re
import uuid
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
    try:
        return date.fromisoformat(str(v).strip()[:10]).isoformat()
    except Exception:
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
