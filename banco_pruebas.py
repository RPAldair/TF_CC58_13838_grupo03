"""
banco_pruebas.py
================
Ejecuta el simulador sin interfaz gráfica y devuelve métricas por equipo.

Uso básico:
    python banco_pruebas.py
    python banco_pruebas.py --semilla 42 --ticks 5000
    python banco_pruebas.py --semillas 1 2 3 4 5

Requisitos:
    - Poner equipo3.py, simulador.py y equipo_base.py en la misma carpeta.
    - Copiar equipo3.py como equipoC.py antes de correr (ver instrucciones abajo).
    - pygame instalado (pip install pygame).

Cómo funciona:
    1. Fija SDL_VIDEODRIVER=dummy para que pygame no abra ventana.
    2. Reemplaza cargar_equipos_dinamicos para controlar qué clase juega cada letra.
    3. Envuelve enviar_mensaje_unidad para contar mensajes por performativa y equipo.
    4. Avanza tick a tick hasta simulacion_terminada o el tope de ticks.
    5. Imprime una tabla con hielo, batería, Ratio, unidades vivas y gasto de comunicación.

Criterio de aceptación (Tarea 0):
    Dos corridas con la misma semilla producen la misma tabla de resultados.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from collections import defaultdict
from typing import Dict

# --- Paso 1: deshabilitar ventana de pygame antes de importar el simulador ---
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

# Asegurar que el directorio del script está en el path para importar los módulos.
_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import simulador as _sim_mod  # noqa: E402  (importamos después de setear SDL)
from equipo_base import EquipoBase  # noqa: E402

# ---------------------------------------------------------------------------
# Parámetros por defecto
# ---------------------------------------------------------------------------
TICKS_MAXIMO = 5_000       # Una partida normal dura ~35 s = ~292 ticks reales.
                           # Ponemos margen amplio por si tarda más con 100 unidades.
TICK_DT = _sim_mod.TICK_DT # Reutilizamos la constante del motor (0.12 s por tick).


# ---------------------------------------------------------------------------
# Sustitución de cargar_equipos_dinamicos
# ---------------------------------------------------------------------------
def _cargar_equipos(mapa_letra_clase: Dict[str, type]) -> Dict[str, EquipoBase]:
    """Devuelve instancias de equipo según el mapa letra -> clase proporcionado.

    Las letras no presentes en el mapa usan EquipoBase (comportamiento nulo).
    """
    equipos: Dict[str, EquipoBase] = {}
    for letra in ("A", "B", "C", "D"):
        cls = mapa_letra_clase.get(letra, EquipoBase)
        inst = cls()
        if cls is EquipoBase:
            inst.nombre = f"Equipo {letra} (Base)"
        equipos[letra] = inst
    return equipos


# ---------------------------------------------------------------------------
# Contador de mensajes por equipo y performativa
# ---------------------------------------------------------------------------
class ContadorMensajes:
    """Intercepta enviar_mensaje_unidad para acumular estadísticas.

    Solo cuenta los envíos que el motor confirma como exitosos (retorna True).
    Separa los mensajes de gobernanza (con la clave 'gobernanza' en content)
    del tráfico de tareas (FIPA-ACL puro).
    """

    def __init__(self):
        # {equipo: {performativa: conteo}}
        self.tareas: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.gobernanza: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # Total de intentos (enviados) vs recibidos (exitosos)
        self.intentos: Dict[str, int] = defaultdict(int)
        self.exitosos: Dict[str, int] = defaultdict(int)
        self._original = None  # método original del motor

    def instalar(self, sim: _sim_mod.SimuladorMarte) -> None:
        """Reemplaza enviar_mensaje_unidad en la instancia del simulador."""
        self._original = sim.enviar_mensaje_unidad
        # Guardamos referencia al contador para el closure.
        contador = self

        def _enviar_envuelto(emisor, dest_id, asunto, datos):
            equipo = emisor.equipo
            contador.intentos[equipo] += 1
            resultado = contador._original(emisor, dest_id, asunto, datos)
            if resultado:
                contador.exitosos[equipo] += 1
                # Extraemos la performativa del sobre FIPA-ACL.
                performativa = "DESCONOCIDA"
                if isinstance(datos, dict):
                    performativa = datos.get("performative", "DESCONOCIDA")
                    content = datos.get("content", {})
                    # Separamos tráfico de gobernanza del de tareas.
                    if isinstance(content, dict) and "gobernanza" in content:
                        contador.gobernanza[equipo][performativa] += 1
                        return resultado
                contador.tareas[equipo][performativa] += 1
            return resultado

        sim.enviar_mensaje_unidad = _enviar_envuelto

    def total_tareas(self, equipo: str) -> int:
        return sum(self.tareas[equipo].values())

    def total_gobernanza(self, equipo: str) -> int:
        return sum(self.gobernanza[equipo].values())


# ---------------------------------------------------------------------------
# Ejecución de una partida
# ---------------------------------------------------------------------------
def correr_partida(
    semilla: int,
    mapa_letra_clase: Dict[str, type],
    ticks_maximo: int = TICKS_MAXIMO,
) -> Dict:
    """Corre una partida completa headless y retorna el diccionario de resultados."""
    random.seed(semilla)

    # Reemplazamos la función de carga dinámica antes de instanciar el simulador.
    _original_cargar = _sim_mod.cargar_equipos_dinamicos
    _sim_mod.cargar_equipos_dinamicos = lambda: _cargar_equipos(mapa_letra_clase)
    try:
        sim = _sim_mod.SimuladorMarte()
    finally:
        _sim_mod.cargar_equipos_dinamicos = _original_cargar

    # Instalamos el contador de mensajes.
    contador = ContadorMensajes()
    contador.instalar(sim)

    # Avanzamos tick a tick hasta que la simulación termine o alcancemos el tope.
    tick = 0
    while not sim.simulacion_terminada and tick < ticks_maximo:
        sim.actualizar(TICK_DT)
        tick += 1

    # Recopilamos métricas por equipo.
    resultados = {
        "semilla": semilla,
        "ticks": tick,
        "terminada": sim.simulacion_terminada,
        "equipos": {},
    }
    for letra in ("A", "B", "C", "D"):
        rec = sim.stats_equipo[letra]["recursos_en_base"]
        bat = sim.stats_equipo[letra]["bateria_usada"]
        ratio = rec / bat if bat > 0.0001 else 0.0
        vivas = sum(1 for u in sim.unidades if u.equipo == letra and u.estado != "muerto")
        total_msg = contador.total_tareas(letra) + contador.total_gobernanza(letra)
        # Cada mensaje exitoso consume 0.5 % al emisor; el bat_usada ya lo incluye,
        # pero calculamos la fracción solo de comunicación para el informe.
        bat_mensajes = total_msg * 0.5
        pct_comunicacion = (bat_mensajes / bat * 100) if bat > 0 else 0.0
        resultados["equipos"][letra] = {
            "nombre": sim.equipos[letra].nombre,
            "recursos": int(rec),
            "bateria_usada": round(bat, 1),
            "ratio": round(ratio, 4),
            "unidades_vivas": vivas,
            "mensajes_tareas": contador.total_tareas(letra),
            "mensajes_gobernanza": contador.total_gobernanza(letra),
            "mensajes_total": total_msg,
            "intentos_envio": contador.intentos[letra],
            "pct_bat_comunicacion": round(pct_comunicacion, 1),
            "detalle_tareas": dict(contador.tareas[letra]),
            "detalle_gobernanza": dict(contador.gobernanza[letra]),
        }
    return resultados


# ---------------------------------------------------------------------------
# Presentación de resultados
# ---------------------------------------------------------------------------
def imprimir_resultados(res: Dict) -> None:
    sem = res["semilla"]
    ticks = res["ticks"]
    terminada = "SÍ" if res["terminada"] else f"NO (tope {ticks} ticks)"
    print(f"\n{'='*70}")
    print(f"  Semilla {sem} | Ticks: {ticks} | Simulación terminada: {terminada}")
    print(f"{'='*70}")
    cabecera = (
        f"{'Letra':<6} {'Nombre':<24} {'Hielo':>6} {'Bat.':>9} "
        f"{'Ratio':>7} {'Vivas':>6} {'Msg':>6} {'%Bat':>6}"
    )
    print(cabecera)
    print("-" * 70)
    ranking = sorted(
        res["equipos"].items(), key=lambda kv: kv[1]["ratio"], reverse=True
    )
    for letra, d in ranking:
        print(
            f"{letra:<6} {d['nombre']:<24} {d['recursos']:>6} "
            f"{d['bateria_usada']:>9.1f} {d['ratio']:>7.4f} "
            f"{d['unidades_vivas']:>6} {d['mensajes_total']:>6} "
            f"{d['pct_bat_comunicacion']:>5.1f}%"
        )
    print()
    # Desglose de performativas por equipo.
    for letra, d in res["equipos"].items():
        det_t = d["detalle_tareas"]
        det_g = d["detalle_gobernanza"]
        if not det_t and not det_g:
            continue
        print(f"  [{letra}] Tareas → " + ", ".join(f"{k}: {v}" for k, v in sorted(det_t.items())))
        if det_g:
            print(f"  [{letra}] Gobernanza → " + ", ".join(f"{k}: {v}" for k, v in sorted(det_g.items())))
    print()


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Banco de pruebas headless del simulador.")
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--semilla", type=int, default=1, help="Semilla única (default: 1).")
    grupo.add_argument("--semillas", type=int, nargs="+", metavar="N",
                       help="Lista de semillas para correr varias partidas.")
    parser.add_argument("--ticks", type=int, default=TICKS_MAXIMO,
                        help=f"Tope de ticks por partida (default: {TICKS_MAXIMO}).")
    args = parser.parse_args()

    # Cargamos equipo3.py como la clase que juega en la letra C.
    # El simulador entregado usa 50 unidades; para 100 editar _generar_unidades
    # en una copia local del simulador (ver guía sección 2).
    import importlib
    try:
        mod = importlib.import_module("equipo3")
        # Buscamos la primera subclase de EquipoBase que no sea la propia base.
        clase_equipo3 = next(
            obj for nombre in dir(mod)
            for obj in [getattr(mod, nombre)]
            if isinstance(obj, type) and issubclass(obj, EquipoBase) and obj is not EquipoBase
        )
        print(f"[INFO] Usando clase '{clase_equipo3.__name__}' de equipo3.py como letra C.")
    except (ImportError, StopIteration) as exc:
        print(f"[AVISO] No se pudo cargar equipo3.py ({exc}). Usando EquipoBase para C.")
        clase_equipo3 = EquipoBase

    mapa = {"C": clase_equipo3}

    semillas = args.semillas if args.semillas else [args.semilla]
    for sem in semillas:
        res = correr_partida(sem, mapa, args.ticks)
        imprimir_resultados(res)


if __name__ == "__main__":
    main()
