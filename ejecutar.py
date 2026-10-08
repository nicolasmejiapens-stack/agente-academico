"""Punto de entrada para GitHub Actions. Uso: python ejecutar.py [scan|probar|manana|noche|botones]
Imprime lo mínimo: los registros de un repositorio público son visibles para cualquiera."""
import logging
import sys

import main
import tareas

logging.getLogger().setLevel(logging.WARNING)

modo = sys.argv[1] if len(sys.argv) > 1 else "scan"
try:
    if modo == "probar":
        print(main.probar_conexion())
    elif modo in ("manana", "noche"):
        print(tareas.enviar_resumen(modo))
    elif modo == "botones":
        print({"tareas_marcadas": tareas.procesar_botones()})
    else:
        r = main.escanear()
        print({"procesados": r["procesados"], "pendientes_por_limite": r["pendientes_por_limite"],
               "lecturas": r.get("lecturas", {}), "problemas": len(r["problemas"])})
except Exception as e:
    print("ERROR:", type(e).__name__)
    sys.exit(1)
