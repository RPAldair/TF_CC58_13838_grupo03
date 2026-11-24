from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from itertools import combinations
from time import perf_counter

from equipo_base import (
    EquipoBase, Percepcion, UnidadVista, UMBRAL_BATERIA_BAJA,
    distancia_chebyshev as distancia,
)

PASOS = tuple((dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy)
ROVER, EXPLORADOR, RECARGA = "rover", "dron_explorador", "dron_recarga"
# Estos tiempos siguen el ritmo del motor: el rover actúa cada dos ticks.
SEGUNDOS_POR_TICK = 0.12
INTERVALO_PULSO = 0.85
COSTO_PULSO = 3.0
COSTO_PASO_NORMAL = 1.0
COSTO_PASO_TORMENTA = 5.0
AHORRO, EXPANSION, SUPERVIVENCIA, REPLIEGUE = (
    "ahorro", "expansion", "supervivencia", "repliegue"
)
# Pesos de hielo, desplazamiento, carroña y riesgo, seguidos del umbral de
# hielo para reportar y el periodo mínimo entre reportes de una misma celda.
POLITICAS = {
    AHORRO: (4.0, 3.5, 2.0, 4.5, 3, 48),
    EXPANSION: (5.0, 2.5, 1.5, 3.0, 2, 32),
    SUPERVIVENCIA: (3.5, 3.0, 4.0, 5.0, 3, 42),
    REPLIEGUE: (3.0, 4.5, 3.0, 5.5, 4, 64),
}
ONTOLOGIA = "marte.grupo3"
ASUNTO = "marte.acl"
ESPERAR = ("esperar",)


# Sesión 2: el estado indica qué está haciendo cada robot y cuándo debe cambiar.
class Estado(Enum):
    OCIOSO = auto()
    EXPLORANDO = auto()
    RECOLECTANDO = auto()
    RETORNANDO = auto()
    CONSUMIENDO_CARRONA = auto()
    ESPERANDO_RECARGA = auto()
    PATRULLANDO = auto()
    RETORNANDO_BASE = auto()
    EN_ESPERA = auto()
    EN_RESCATE = auto()


@dataclass(frozen=True)
class Tarea:
    tipo: str
    pos: tuple
    valor: float
    tick: int
    identificador: str = ""
    conversacion: str = ""

    @property
    def clave(self):
        return self.tipo, self.identificador or self.pos

    def datos(self):
        return {
            "tipo": self.tipo, "pos": self.pos, "valor": self.valor,
            "tick": self.tick, "identificador": self.identificador,
            "conversacion": self.conversacion,
        }

    @classmethod
    def desde(cls, datos):
        return cls(datos["tipo"], tuple(datos["pos"]), datos["valor"],
                   datos["tick"], datos.get("identificador", ""),
                   datos.get("conversacion", ""))


@dataclass
class Subasta:
    tarea: Tarea
    conversacion: str
    candidatos: tuple
    vence: int
    pujas: dict = field(default_factory=dict)


@dataclass
class Agente:
    indice: int
    estado: Estado
    # Sesión 1: cada robot recuerda lo que vio o lo que le avisaron por mensaje.
    hielo: dict = field(default_factory=dict)
    restos: dict = field(default_factory=dict)
    tormentas: deque = field(default_factory=lambda: deque(maxlen=6))
    reportados: dict = field(default_factory=dict)
    reservas: dict = field(default_factory=dict)
    rescates: dict = field(default_factory=dict)
    objetivo: Tarea | None = None
    coordinador: str | None = None
    subasta: Subasta | None = None
    oferta: tuple | None = None
    solicitud: str = ""
    vigencia: int = 0
    patrulla: int = 0
    reintento: int = 0
    intentos: int = 0
    ultima_limpieza: int = -100
    siguiente_reporte: int = 0
    politica: str = AHORRO
    ronda: int = -1
    eleccion: tuple | None = None
    difusion: deque = field(default_factory=deque)
    trayectoria: deque = field(default_factory=lambda: deque(maxlen=4))


class Equipo3(EquipoBase):
    """Implementación del grupo 3 para el torneo."""

    nombre = "Equipo 3"

    def inicializar(self, letra, base_pos, grid_size=(60, 45)):
        super().inicializar(letra, base_pos, grid_size)
        self.agentes = {}
        self.roles = {ROVER: [], EXPLORADOR: [], RECARGA: []}
        self.rol_por_id = {}
        self.tick = None
        self.cache_riesgo = {}
        self.segundos_turno = 0.0
        self.latencias = deque(maxlen=4096)
        self.envios_api = 0
        self.recepciones = 0
        self.resultado_eleccion = None

    def decidir_unidad(self, unidad: UnidadVista, percepcion: Percepcion, api) -> tuple:
        inicio = perf_counter()
        if percepcion.tick != self.tick:
            if self.tick is not None:
                self.latencias.append(self.segundos_turno * 1000)
            self.tick = percepcion.tick
            self.segundos_turno = 0.0
            self.cache_riesgo.clear()
        try:
            return self._decidir(unidad, percepcion, api)
        finally:
            self.segundos_turno += perf_counter() - inicio

    def perfil_rendimiento(self):
        """Devuelve latencias en ms para la ventana de los últimos 4096 turnos."""
        muestras = sorted([*self.latencias, self.segundos_turno * 1000])
        return {
            "turnos": len(muestras),
            "media_ms": sum(muestras) / len(muestras),
            "p95_ms": muestras[(95 * len(muestras) - 1) // 100],
            "p99_ms": muestras[(99 * len(muestras) - 1) // 100],
            "max_ms": muestras[-1],
            "intentos_envio": self.envios_api,
            "mensajes_recibidos": self.recepciones,
        }

    def _decidir(self, u, p, api):
        if u.estado == "muerto":
            return ESPERAR
        if u.id not in self.agentes:
            ids = self.roles[u.tipo]
            inicial = {ROVER: Estado.OCIOSO, EXPLORADOR: Estado.PATRULLANDO,
                       RECARGA: Estado.EN_ESPERA}
            self.agentes[u.id] = Agente(len(ids), inicial[u.tipo])
            ids.append(u.id)
            self.rol_por_id[u.id] = u.tipo

        m = self.agentes[u.id]
        riesgo = self._campo_riesgo(p)
        self._observar(u, p, m)
        self._mensajes(u, p, m, riesgo, api)
        if u.tipo == ROVER:
            if u.estado == "consumiendo":
                m.estado = Estado.CONSUMIENDO_CARRONA
                return ESPERAR
            if u.bateria <= UMBRAL_BATERIA_BAJA or u.estado == "esperando_recarga":
                m.estado = Estado.ESPERANDO_RECARGA
                m.objetivo = None
                self._pedir_auxilio(u, p, m, api)
                if u.estado == "esperando_recarga":
                    return ESPERAR
                return ("solicitar_recarga",)
            if m.estado == Estado.ESPERANDO_RECARGA:
                m.intentos = 0
                m.reintento = 0
                m.solicitud = ""
        elif u.bateria <= UMBRAL_BATERIA_BAJA:
            self._cancelar_rescate(u, m, api)
            return self._volver_dron(u, m, p, riesgo)

        self._gobernar(u, p, m, api)
        self._cerrar_subasta(u, p, m, riesgo, api)
        if u.tipo == ROVER:
            return self._rover(u, p, m, riesgo, api)
        if u.tipo == RECARGA:
            return self._recargador(u, p, m, riesgo, api)
        return self._explorador(u, p, m, riesgo, api)

    def _campo_riesgo(self, p):
        """Precalcula cada tormenta visible una vez por tick y su unión local."""
        firma = tuple(sorted(tuple(t) for t in p.tormentas_visible))
        clave = ("campo", firma)
        campo = self.cache_riesgo.get(clave)
        if campo is None:
            campo = {}
            w, h = p.grid_size
            for t in firma:
                celdas = self.cache_riesgo.get(("tormenta", t))
                if celdas is None:
                    x, y, radio = t
                    celdas = {}
                    for nx in range(max(0, int(x - radio)), min(w, int(x + radio) + 1)):
                        for ny in range(max(0, int(y - radio)), min(h, int(y + radio) + 1)):
                            cuadrado = (nx - x) ** 2 + (ny - y) ** 2
                            if cuadrado <= radio * radio:
                                celdas[nx, ny] = 1 + (radio - cuadrado ** 0.5) / max(1, radio)
                    self.cache_riesgo["tormenta", t] = celdas
                for celda, nivel in celdas.items():
                    campo[celda] = campo.get(celda, 0) + nivel
            self.cache_riesgo[clave] = campo
        return campo

    def _ruta(self, origen, destino, riesgo, tipo, alcance=0):
        """Estima el gasto del trayecto directo con las tormentas que se ven ahora."""
        distancia_total = distancia(origen, destino)
        pasos = max(0, distancia_total - alcance)
        if not pasos or not riesgo:
            return pasos * COSTO_PASO_NORMAL, 0.0

        dx, dy = destino[0] - origen[0], destino[1] - origen[1]
        anterior_en_tormenta = origen in riesgo
        pasos_en_tormenta = 0
        pasos_expuestos = 0
        for i in range(1, pasos + 1):
            x = origen[0] + round(dx * i / distancia_total)
            y = origen[1] + round(dy * i / distancia_total)
            siguiente_en_tormenta = (x, y) in riesgo
            # El motor cobra 5 si salimos de una celda con tormenta o entramos a una.
            pasos_en_tormenta += anterior_en_tormenta or siguiente_en_tormenta
            pasos_expuestos += anterior_en_tormenta
            anterior_en_tormenta = siguiente_en_tormenta

        tiempo_por_paso = SEGUNDOS_POR_TICK * (2 if tipo == ROVER else 1)
        pulsos_esperados = pasos_expuestos * tiempo_por_paso / INTERVALO_PULSO
        gasto_movimiento = (
            (pasos - pasos_en_tormenta) * COSTO_PASO_NORMAL
            + pasos_en_tormenta * COSTO_PASO_TORMENTA
        )
        # Cada pulso quita 3 puntos. Si el empuje nos desvía, reservamos
        # un paso extra por pulso para retomar el rumbo dentro de la tormenta.
        gasto_pulsos = pulsos_esperados * COSTO_PULSO
        gasto_desvio = pulsos_esperados * COSTO_PASO_TORMENTA
        return gasto_movimiento + gasto_pulsos + gasto_desvio, pasos_en_tormenta / pasos

    def _mover(self, u, p, m, destino, riesgo):
        """Gradiente local con penalización por exposición y visitas recientes."""
        if u.pos == destino:
            return ESPERAR
        if not m.trayectoria or m.trayectoria[-1] != u.pos:
            m.trayectoria.append(u.pos)
        x, y = u.pos
        w, h = p.grid_size
        # Buscamos una celda que nos acerque al destino y evitamos las más riesgosas.
        peso_riesgo = POLITICAS[m.politica][3] + 1
        mejor = float("inf")
        accion = ESPERAR
        for dx, dy in PASOS:
            vecina = x + dx, y + dy
            if 0 <= vecina[0] < w and 0 <= vecina[1] < h:
                costo = (distancia(vecina, destino)
                         + 0.08 * (abs(vecina[0] - destino[0]) + abs(vecina[1] - destino[1]))
                         + peso_riesgo * riesgo.get(vecina, 0)
                         + 1.8 * m.trayectoria.count(vecina))
                if costo < mejor:
                    mejor = costo
                    accion = "mover", dx, dy
        return accion

    def _observar(self, u, p, m):
        # Guardamos el recurso por coordenada y anotamos el tick en que lo vimos.
        visibles = set()
        for x, y, cantidad in p.hielo_visible:
            pos = x, y
            if cantidad > 0:
                m.hielo[pos] = Tarea("hielo", pos, cantidad, p.tick)
                visibles.add(pos)
            else:
                m.hielo.pop(pos, None)
        # Si volvemos a una celda y está vacía, borramos la veta de la memoria.
        if u.pos not in visibles:
            m.hielo.pop(u.pos, None)
        for resto in p.restos_visible:
            pos = tuple(resto["pos"])
            tipo = resto.get("tipo", "")
            combustible = resto.get("combustible", 20 if tipo == ROVER else 10)
            m.restos[pos] = Tarea("restos", pos, combustible, p.tick, resto["id"])
        if p.tormentas_visible:
            m.tormentas.append((p.tick, tuple(p.tormentas_visible)))
        # Limpiamos los datos antiguos cada cierto tiempo para dejar de usarlos.
        if p.tick - m.ultima_limpieza >= 18:
            m.ultima_limpieza = p.tick
            m.hielo = {pos: t for pos, t in m.hielo.items() if p.tick - t.tick <= 90}
            m.restos = {pos: t for pos, t in m.restos.items() if p.tick - t.tick <= 60}
            m.reportados = {k: t for k, t in m.reportados.items() if p.tick - t <= 100}
            m.reservas = {k: t for k, t in m.reservas.items() if t > p.tick}
            m.rescates = {k: t for k, t in m.rescates.items() if p.tick - t.tick <= 64}

    def _valor(self, u, p, m, tarea, riesgo):
        hielo, costo, restos, peligro = POLITICAS[m.politica][:4]
        # Comparamos cuánto ganamos con el gasto y el riesgo de ir y volver.
        alcance = {"base": 2, "restos": 1, "rescate": 1}.get(tarea.tipo, 0)
        ida, exposicion = self._ruta(u.pos, tarea.pos, riesgo, u.tipo, alcance)
        vuelta, riesgo_vuelta = self._ruta(tarea.pos, p.base_pos, riesgo, u.tipo, 2)
        antiguedad = max(0, p.tick - tarea.tick)
        if tarea.tipo == "rescate":
            if u.bateria - ida - vuelta <= 27 or not u.carga_recarga:
                return float("-inf")
            return (100 - tarea.valor) / 20 - ida / 12 - 3 * exposicion + u.bateria / 100

        espacio = max(0, u.capacidad - u.carga)
        ganancia = min(espacio, tarea.valor) / 5 if tarea.tipo == "hielo" else 0
        combustible = 0.0
        if tarea.tipo == "restos":
            falta = 100 - u.bateria
            if u.bateria >= 82 and self._aliado_para_transferir(u, p):
                falta = 100
            combustible = min(falta, tarea.valor) / 20
        if tarea.tipo == "base":
            ganancia = u.carga / 5
            vuelta = riesgo_vuelta = 0
        excedente = 0 if tarea.tipo == "base" else 4
        if u.bateria - ida - vuelta <= UMBRAL_BATERIA_BAJA + excedente:
            return float("-inf")
        # Una observación antigua vale menos porque el recurso pudo agotarse.
        frescura = max(0.15, 1 - antiguedad / 100)
        return (frescura * (hielo * ganancia + restos * combustible)
                - costo * (ida + vuelta) / 40
                - peligro * max(exposicion, riesgo_vuelta))

    def _emitir(self, u, api, receptor, acto, conversacion, contenido):
        self.envios_api += 1
        sobre = {
            "performative": acto, "sender": u.id, "conversation_id": conversacion,
            "ontology": ONTOLOGIA, "content": contenido,
        }
        if receptor is None:
            return api.enviar_a_recargador_cercano(ASUNTO, sobre)
        return api.enviar_mensaje(receptor, ASUNTO, sobre)

    def _pedir_auxilio(self, u, p, m, api):
        if p.tick < m.reintento or u.bateria <= 2:
            return
        conversacion = f"{u.id}:auxilio:{p.tick}"
        tarea = Tarea("rescate", u.pos, u.bateria, p.tick, u.id, conversacion)
        drones = self.roles[RECARGA]
        receptor = None
        if m.intentos and drones:
            receptor = drones[(m.indice + m.intentos - 1) % len(drones)]
        self._emitir(u, api, receptor, "REQUEST", conversacion, tarea.datos())
        m.solicitud = conversacion
        m.intentos += 1
        m.reintento = p.tick + 24

    def _mensajes(self, u, p, m, riesgo, api):
        if m.oferta and p.tick > m.oferta[1]:
            m.oferta = None
        for entrada in p.mensajes:
            mensaje = entrada.get("datos", {})
            if not isinstance(mensaje, dict) or mensaje.get("ontology") != ONTOLOGIA:
                continue
            if not isinstance(mensaje.get("content"), dict):
                continue
            self.recepciones += 1
            acto = mensaje.get("performative")
            emisor = mensaje.get("sender")
            conversacion = mensaje.get("conversation_id")
            datos = mensaje["content"]
            if "gobernanza" in datos:
                self._mensaje_gobierno(u, p, m, api, acto, emisor, conversacion, datos)
            elif acto == "REQUEST" and u.tipo == RECARGA:
                tarea = Tarea.desde(datos)
                m.rescates[tarea.clave] = tarea
            elif acto == "CFP" and m.oferta is None and m.subasta is None:
                tarea = Tarea.desde(datos)
                if self._disponible(u, p, m, tarea):
                    valor = self._valor(u, p, m, tarea, riesgo)
                    if valor > float("-inf") and p.tick < datos["vence"]:
                        m.oferta = conversacion, datos["vence"] + 4
                        self._emitir(u, api, emisor, "PROPOSE", conversacion, {"valor": valor})
            elif acto == "PROPOSE" and m.subasta and conversacion == m.subasta.conversacion:
                if emisor in m.subasta.candidatos:
                    m.subasta.pujas[emisor] = datos["valor"]
            elif acto in ("ACCEPT_PROPOSAL", "REJECT_PROPOSAL") and m.oferta:
                if conversacion == m.oferta[0]:
                    m.oferta = None
                    if acto == "ACCEPT_PROPOSAL":
                        tarea = Tarea.desde(datos)
                        if self._disponible(u, p, m, tarea):
                            self._asignar(u, p, m, tarea, api, emisor)
                        elif tarea.tipo == "rescate":
                            self._emitir(u, api, tarea.identificador, "FAILURE",
                                         tarea.conversacion, {})
            elif u.tipo == ROVER and conversacion == m.solicitud:
                if acto == "AGREE":
                    m.reintento = max(m.reintento, p.tick + datos.get("espera", 24))
                elif acto == "FAILURE":
                    m.reintento = min(m.reintento, p.tick + 2)
            elif acto == "INFORM":
                self._incorporar_informe(p, m, datos)

    def _incorporar_informe(self, p, m, datos):
        tarea = Tarea.desde(datos)
        if tarea.tipo == "rescate":
            m.rescates.pop(tarea.clave, None)
            return
        mapa = m.hielo if tarea.tipo == "hielo" else m.restos
        anterior = mapa.get(tarea.pos)
        if anterior and anterior.tick >= tarea.tick:
            return
        # Si un aliado confirma que se agotó, retiramos el dato de la memoria.
        if tarea.valor <= 0:
            mapa.pop(tarea.pos, None)
        elif p.tick - tarea.tick <= 60:
            mapa[tarea.pos] = tarea

    def _disponible(self, u, p, m, tarea):
        if u.bateria <= 30 or u.estado != "activo" or m.objetivo:
            return False
        if tarea.tipo == "rescate":
            return (u.tipo == RECARGA and p.base_aliada_cerca and u.carga_recarga > 0
                    and self._rescate_final_posible(u, p, tarea))
        return (not p.fase_final and u.tipo == ROVER and u.carga < u.capacidad
                and m.estado != Estado.RETORNANDO)

    def _rescate_final_posible(self, u, p, tarea):
        return (not p.fase_final or (p.tiempo_final_restante > 5
                                    and distancia(u.pos, tarea.pos) <= 8))

    def _asignar(self, u, p, m, tarea, api, coordinador=None):
        m.objetivo = tarea
        m.coordinador = coordinador
        if tarea.tipo != "rescate":
            self._incorporar_informe(p, m, tarea.datos())
        m.vigencia = p.tick + (64 if tarea.tipo == "rescate" else 18)
        if tarea.tipo == "rescate":
            m.estado = Estado.EN_RESCATE
            m.rescates.pop(tarea.clave, None)
            self._emitir(u, api, tarea.identificador, "AGREE", tarea.conversacion,
                         {"espera": max(24, distancia(u.pos, tarea.pos) * 2 + 8)})

    def _abrir_subasta(self, u, p, m, tarea, riesgo, api, candidatos):
        conversacion = f"{u.id}:{p.tick}:{tarea.tipo}"
        m.subasta = Subasta(tarea, conversacion, candidatos, p.tick + 4)
        if self._disponible(u, p, m, tarea):
            valor = self._valor(u, p, m, tarea, riesgo)
            if valor > float("-inf"):
                m.subasta.pujas[u.id] = valor
        datos = {**tarea.datos(), "vence": m.subasta.vence}
        for candidato in candidatos:
            self._emitir(u, api, candidato, "CFP", conversacion, datos)

    def _cerrar_subasta(self, u, p, m, riesgo, api):
        subasta = m.subasta
        if not subasta or p.tick < subasta.vence:
            return
        m.subasta = None
        tarea = subasta.tarea
        if p.fase_final and tarea.tipo != "rescate":
            return
        pujas = subasta.pujas
        if u.id in pujas and not self._disponible(u, p, m, tarea):
            pujas.pop(u.id)
        ganador = min(pujas, key=lambda uid: (-pujas[uid], uid)) if pujas else None
        for perdedor in subasta.candidatos:
            acto = "ACCEPT_PROPOSAL" if perdedor == ganador else "REJECT_PROPOSAL"
            self._emitir(u, api, perdedor, acto, subasta.conversacion, tarea.datos())
        if ganador == u.id:
            self._asignar(u, p, m, tarea, api)
        elif ganador:
            m.reservas[tarea.clave] = p.tick + (42 if tarea.tipo == "rescate" else 18)
        elif tarea.tipo == "rescate":
            self._emitir(u, api, tarea.identificador, "FAILURE", tarea.conversacion, {})
            m.rescates.pop(tarea.clave, None)
        elif u.tipo != ROVER:
            sobre = {
                "performative": "INFORM", "sender": u.id,
                "conversation_id": subasta.conversacion, "ontology": ONTOLOGIA,
                "content": tarea.datos(),
            }
            self.envios_api += 1
            api.enviar_a_rover_ocioso_cercano(ASUNTO, sobre, pos_ref=tarea.pos)

    def _rol(self, aliado):
        return aliado.get("tipo") or self.rol_por_id.get(aliado["id"])

    def _candidatos(self, u, p, tarea, rol, limite=2):
        posibles = []
        for a in p.aliados_visible:
            if (a["id"] == u.id or self._rol(a) != rol
                    or a.get("estado") != "activo" or a.get("bateria", 0) <= 30):
                continue
            pos = tuple(a["pos"])
            if rol == RECARGA and distancia(pos, p.base_pos) > 2:
                continue
            if rol == ROVER and a.get("carga", 0) >= a.get("capacidad", 5):
                continue
            posibles.append((distancia(pos, tarea.pos), -a["bateria"], a["id"]))
        return tuple(x[2] for x in sorted(posibles)[:limite])

    def _reportar(self, u, p, m, riesgo, api):
        if p.fase_final or m.subasta or p.tick < m.siguiente_reporte or u.bateria < 40:
            return
        minimo, espera = POLITICAS[m.politica][4:]
        hallazgos = [
            m.hielo[x, y] for x, y, cantidad in p.hielo_visible if cantidad >= minimo
        ]
        hallazgos.extend(m.restos[tuple(r["pos"])] for r in p.restos_visible)
        disponibles = [
            t for t in hallazgos
            if p.tick - m.reportados.get(t.clave, -1000) >= espera
            and p.tick >= m.reservas.get(t.clave, 0)
            and distancia(t.pos, p.base_pos) <= 38
        ]
        if disponibles:
            tarea = max(disponibles, key=lambda t: (
                (t.valor / 5 if t.tipo == "hielo" else t.valor / 20)
                - distancia(t.pos, p.base_pos) / 45,
                t.tipo == "restos" and m.politica == SUPERVIVENCIA,
            ))
            m.reportados[tarea.clave] = p.tick
            m.siguiente_reporte = p.tick + max(7, espera // 4)
            vecinos = self._candidatos(u, p, tarea, ROVER)
            self._abrir_subasta(u, p, m, tarea, riesgo, api, vecinos)

    def _aliado_para_transferir(self, u, p):
        return min(
            (a for a in p.aliados_visible if a["id"] != u.id
             and self._rol(a) == ROVER and a.get("estado") != "muerto"
             and a.get("bateria", 100) < 55 and distancia(u.pos, tuple(a["pos"])) <= 1),
            key=lambda a: (a["bateria"], a["id"]), default=None,
        )

    def _otro_ocupa(self, u, p, tarea):
        if tarea.tipo == "hielo" and tarea.valor > 2:
            return False
        alcance = int(tarea.tipo == "restos")
        llegada = distancia(u.pos, tarea.pos)
        return any(
            a["id"] != u.id and self._rol(a) == ROVER
            and a.get("estado") in ("activo", "consumiendo") and a.get("bateria", 0) > 25
            and (tarea.tipo == "restos" or a.get("carga", 0) < a.get("capacidad", 5))
            and (distancia(tuple(a["pos"]), tarea.pos), a["id"]) < (llegada, u.id)
            and distancia(tuple(a["pos"]), tarea.pos) <= alcance + 1
            for a in p.aliados_visible
        )

    def _objetivo_rover(self, u, p, m, riesgo):
        # Acotamos la evaluación de ruta a los candidatos que mas convengan
        opciones = (
            t for mapa in (m.hielo, m.restos) for t in mapa.values()
            if p.tick - t.tick <= (60 if t.tipo == "restos" else 90)
            and p.tick >= m.reservas.get(t.clave, 0)
            and distancia(u.pos, t.pos) <= 36
        )
        opciones = sorted(opciones, key=lambda t: (
            distancia(u.pos, t.pos) - (t.valor if t.tipo == "hielo" else t.valor / 5),
            -t.tick, t.pos,
        ))[:10]
        mejor = None
        valor = 0.0
        if u.carga:
            base = Tarea("base", p.base_pos, u.carga, p.tick)
            valor = max(0.0, self._valor(u, p, m, base, riesgo) - 0.35)
        for tarea in opciones:
            if self._otro_ocupa(u, p, tarea):
                continue
            utilidad = self._valor(u, p, m, tarea, riesgo)
            if utilidad > valor and utilidad > float("-inf"):
                mejor, valor = tarea, utilidad
        return mejor

    def _rover(self, u, p, m, riesgo, api):
        if p.fase_final or u.carga >= u.capacidad or m.estado == Estado.RETORNANDO:
            m.objetivo = None
            return self._volver_rover(u, p, m, riesgo)

        tarea = m.objetivo
        if tarea:
            mapa = m.hielo if tarea.tipo == "hielo" else m.restos
            actual = mapa.get(tarea.pos)
            alcance = int(tarea.tipo == "restos")
            cerca = distancia(u.pos, tarea.pos) <= alcance
            agotada = cerca and (actual is None or actual.tick != p.tick)
            peligrosa = not cerca and self._valor(u, p, m, tarea, riesgo) == float("-inf")
            if (agotada or actual is None or peligrosa or p.tick > m.vigencia
                    or self._otro_ocupa(u, p, tarea)):
                if agotada:
                    mapa.pop(tarea.pos, None)
                    if m.coordinador and u.bateria > 30:
                        aviso = Tarea(tarea.tipo, tarea.pos, 0, p.tick, tarea.identificador)
                        self._emitir(u, api, m.coordinador, "INFORM",
                                     f"{u.id}:agotada:{p.tick}", aviso.datos())
                m.objetivo = tarea = None
            else:
                tarea = actual
        if tarea is None and m.oferta is None and m.subasta is None:
            tarea = self._objetivo_rover(u, p, m, riesgo)
            if tarea:
                vecinos = self._candidatos(u, p, tarea, ROVER)
                competidores = tuple(
                    uid for uid in vecinos if uid < u.id
                )
                # Si ya está en posición, extraer resulta más barato que negociar.
                if competidores and distancia(u.pos, tarea.pos) in (2, 3, 4, 5):
                    self._abrir_subasta(u, p, m, tarea, riesgo, api, competidores)
                    m.reservas[tarea.clave] = p.tick + 6
                    tarea = None
                else:
                    self._asignar(u, p, m, tarea, api)
        if tarea:
            cerca = distancia(u.pos, tarea.pos) <= int(tarea.tipo == "restos")
            if tarea.tipo == "hielo":
                m.estado = Estado.RECOLECTANDO
                if cerca and tarea.tick == p.tick:
                    m.vigencia = max(m.vigencia, p.tick + 2)
                    return ("extraer",)
            elif cerca and tarea.tick == p.tick:
                m.estado = Estado.CONSUMIENDO_CARRONA
                m.objetivo = None
                aliado = self._aliado_para_transferir(u, p) if u.bateria >= 82 else None
                modo = ("transferir", aliado["id"]) if aliado else "propia"
                return ("consumir_restos", tarea.identificador, modo)
            return self._mover(u, p, m, tarea.pos, riesgo)
        if m.oferta or m.subasta:
            m.estado = Estado.OCIOSO
            return ESPERAR
        if u.carga:
            return self._volver_rover(u, p, m, riesgo)

        ida, _ = self._ruta(u.pos, p.base_pos, riesgo, u.tipo, 2)
        reserva = 46 if m.politica == REPLIEGUE else 42
        if u.bateria <= reserva + ida and not p.base_aliada_cerca:
            # Volver antes de agotar la batería facilita un rescate cercano.
            return self._volver_rover(u, p, m, riesgo)
        return self._avanzar_busqueda(u, p, m, riesgo)

    def _avanzar_busqueda(self, u, p, m, riesgo):
        m.estado = Estado.EXPLORANDO
        w, h = p.grid_size
        # Espiral discreta: primero se visita el cuadrante aliado y después
        # se avanza hacia sectores más lejanos mediante saltos deterministas.
        bx, by = p.base_pos
        alcance = (0.42, 0.63, 0.90)[(m.patrulla // 4) % 3]
        i = m.indice + 13 * m.patrulla
        x = round((w - 1) * alcance * (1 + i % 11) / 11)
        y = round((h - 1) * alcance * (1 + (i * 7) % 13) / 13)
        destino = (abs(bx - x), abs(by - y))
        if distancia(u.pos, destino) <= 1:
            m.patrulla += 1
        return self._mover(u, p, m, destino, riesgo)

    def _volver_rover(self, u, p, m, riesgo):
        m.estado = Estado.RETORNANDO
        if p.base_aliada_cerca:
            m.estado = Estado.OCIOSO
            return ("depositar",) if u.carga else ESPERAR
        return self._mover(u, p, m, p.base_pos, riesgo)

    def _volver_dron(self, u, m, p, riesgo):
        m.estado = Estado.RETORNANDO_BASE
        if p.base_aliada_cerca:
            if u.bateria < 99 or (u.tipo == RECARGA and not u.carga_recarga):
                return ("recargar_en_base",)
            m.estado = Estado.EN_ESPERA if u.tipo == RECARGA else Estado.PATRULLANDO
            return ESPERAR
        return self._mover(u, p, m, p.base_pos, riesgo)

    def _explorador(self, u, p, m, riesgo, api):
        vuelta, _ = self._ruta(u.pos, p.base_pos, riesgo, u.tipo, 2)
        if p.fase_final or m.estado == Estado.RETORNANDO_BASE or u.bateria <= 31 + vuelta:
            return self._volver_dron(u, m, p, riesgo)

        self._reportar(u, p, m, riesgo, api)
        m.estado = Estado.PATRULLANDO
        w, h = p.grid_size
        # Dividimos el mapa en 30 sectores para que los drones no patrullen
        # todos la misma zona. Cada uno recorre cuatro puntos de su sector.
        col, fila = m.indice % 6, (m.indice // 6) % 5
        wx, wy = ((0.22, 0.25), (0.77, 0.25), (0.77, 0.75), (0.22, 0.75))[m.patrulla % 4]
        x, y = int((col + wx) * w / 6), int((fila + wy) * h / 5)
        bx, by = p.base_pos
        destino = abs(bx - x), abs(by - y)
        if distancia(u.pos, destino) <= 1:
            m.patrulla += 1
        return self._mover(u, p, m, destino, riesgo)

    def _cancelar_rescate(self, u, m, api):
        tarea = m.objetivo
        if tarea and tarea.tipo == "rescate":
            m.objetivo = None
            if u.bateria > 2:
                self._emitir(u, api, tarea.identificador, "FAILURE",
                             tarea.conversacion, {})

    def _recargador(self, u, p, m, riesgo, api):
        if not u.carga_recarga:
            self._cancelar_rescate(u, m, api)
            return self._volver_dron(u, m, p, riesgo)
        if m.objetivo:
            tarea = m.objetivo
            rover = next((a for a in p.aliados_visible if a["id"] == tarea.identificador), None)
            destino = tuple(rover["pos"]) if rover else tarea.pos
            if (p.tick > m.vigencia or (rover and rover["estado"] == "muerto")
                    or (rover and rover["estado"] != "esperando_recarga"
                        and rover["bateria"] > tarea.valor + 5)):
                self._cancelar_rescate(u, m, api)
                return self._volver_dron(u, m, p, riesgo)
            if distancia(u.pos, destino) <= 1:
                m.objetivo = None
                m.estado = Estado.RETORNANDO_BASE
                return ("recargar_aliado", tarea.identificador)
            m.estado = Estado.EN_RESCATE
            self._reportar(u, p, m, riesgo, api)
            return self._mover(u, p, m, destino, riesgo)
        if not p.base_aliada_cerca:
            return self._volver_dron(u, m, p, riesgo)
        if u.bateria < 92 or m.estado == Estado.RETORNANDO_BASE:
            return self._volver_dron(u, m, p, riesgo)

        m.estado = Estado.EN_ESPERA
        pendientes = [
            t for t in m.rescates.values()
            if p.tick - t.tick <= 64 and p.tick >= m.reservas.get(t.clave, 0)
            and self._rescate_final_posible(u, p, t)
        ]
        if pendientes and m.subasta is None and m.oferta is None:
            tarea = min(pendientes, key=lambda t: (
                t.valor, distancia(u.pos, t.pos), t.tick, t.identificador,
            ))
            vecinos = self._candidatos(u, p, tarea, RECARGA)
            self._abrir_subasta(u, p, m, tarea, riesgo, api, vecinos)
        return ESPERAR

    def _preferencias(self, u, p):
        if p.fase_final:
            return (REPLIEGUE, SUPERVIVENCIA, AHORRO, EXPANSION)
        if u.bateria < 45 or len(p.restos_visible) >= 2:
            return (SUPERVIVENCIA, AHORRO, REPLIEGUE, EXPANSION)
        if u.en_tormenta or p.tormentas_visible:
            return (AHORRO, SUPERVIVENCIA, REPLIEGUE, EXPANSION)
        if sum(cantidad >= 4 for _, _, cantidad in p.hielo_visible) >= 2:
            return (EXPANSION, AHORRO, SUPERVIVENCIA, REPLIEGUE)
        if p.tick >= 360 and not p.hielo_visible:
            return (REPLIEGUE, AHORRO, SUPERVIVENCIA, EXPANSION)
        return (AHORRO, EXPANSION, SUPERVIVENCIA, REPLIEGUE)

    def _mensaje_gobierno(self, u, p, m, api, acto, emisor, conversacion, datos):
        tipo = datos["gobernanza"]
        ronda = datos["ronda"]
        if acto == "CFP" and tipo == "voto" and u.bateria > 35:
            self._emitir(u, api, emisor, "PROPOSE", conversacion, {
                "gobernanza": "voto", "ronda": ronda, "orden": self._preferencias(u, p),
            })
        elif acto == "PROPOSE" and m.eleccion and ronda == m.eleccion[0]:
            _, _, elegibles, votos = m.eleccion
            if emisor in elegibles:
                votos[emisor] = tuple(datos["orden"])
        elif acto == "INFORM" and ronda > m.ronda:
            m.ronda = ronda
            m.politica = datos["politica"]
            if tipo == "relevo":
                m.difusion = deque((uid, ronda, m.politica) for uid in datos["destinos"])

    def _gobernar(self, u, p, m, api):
        if p.fase_final:
            m.politica = REPLIEGUE
            return
        recargadores = self.roles[RECARGA]
        ronda, tramo = divmod(p.tick, 240)
        if m.difusion and u.bateria > 35:
            for _ in range(min(3, len(m.difusion))):
                uid, anterior, politica = m.difusion.popleft()
                self._emitir(u, api, uid, "INFORM", f"votacion:{anterior}", {
                    "gobernanza": "politica", "ronda": anterior, "politica": politica,
                })
        if not recargadores or u.id != recargadores[ronda % len(recargadores)]:
            return
        if 12 <= tramo <= 18 and m.eleccion is None and m.ronda < ronda:
            panel = []
            for rol, n in ((ROVER, 4), (EXPLORADOR, 2), (RECARGA, 1)):
                lista = self.roles[rol]
                if lista:
                    inicio = (ronda * n) % len(lista)
                    panel.extend(
                        lista[(inicio + i) % len(lista)] for i in range(min(n, len(lista)))
                    )
            elegibles = tuple(uid for uid in panel if uid != u.id)
            m.eleccion = ronda, p.tick + 9, elegibles, {u.id: self._preferencias(u, p)}
            for uid in elegibles:
                self._emitir(u, api, uid, "CFP", f"votacion:{ronda}", {
                    "gobernanza": "voto", "ronda": ronda,
                })
        if m.eleccion and p.tick >= m.eleccion[1]:
            self._resolver_eleccion(u, m, api)

    def _resolver_eleccion(self, u, m, api):
        ronda, _, _, votos = m.eleccion
        m.eleccion = None
        orden = (AHORRO, SUPERVIVENCIA, EXPANSION, REPLIEGUE)
        puntos = dict.fromkeys(orden, 0)
        matriz = {}
        for a, b in combinations(orden, 2):
            favor = sum(voto.index(a) < voto.index(b) for voto in votos.values())
            contra = len(votos) - favor
            matriz[a, b], matriz[b, a] = favor, contra
            diferencia = int(favor > contra) - int(contra > favor)
            puntos[a] += diferencia
            puntos[b] -= diferencia
        politica = max(orden, key=puntos.get)
        m.ronda, m.politica = ronda, politica
        self.resultado_eleccion = {
            "ronda": ronda, "politica": politica, "votos": len(votos),
            "copeland": puntos, "comparaciones": matriz,
        }
        relevos = self.roles[RECARGA]
        destinatarios = self.roles[ROVER] + self.roles[EXPLORADOR]
        for i, uid in enumerate(relevos):
            grupo = destinatarios[i::len(relevos)]
            if uid == u.id:
                m.difusion.extend((receptor, ronda, politica) for receptor in grupo)
            else:
                self._emitir(u, api, uid, "INFORM", f"votacion:{ronda}", {
                    "gobernanza": "relevo", "ronda": ronda,
                    "politica": politica, "destinos": grupo,
                })
