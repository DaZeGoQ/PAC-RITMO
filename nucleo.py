"""
nucleo.py  -  lógica pura de PAC-RITMO (no depende de pygame)

  * Búsquedas: BFS (patrulla) y A* (huida).
  * Geometría de las formas (plano, cilindro, esfera, cubo) y matrices/cuaterniones de cámara.
  * Paletas de color: las decide la CANCIÓN y la IA (nada de arcoíris que gira solo).
  * Generador de mapas a partir de las características de la canción.
  * SceneAI: en cada compás decide, según la música, el MAPA, la FORMA (2D/3D), los COLORES y la
    FORMA y el COMPORTAMIENTO de los fantasmas.
  * Biblioteca de canciones (JSON): categorías MI MÚSICA y CIRCUITO.
"""
import math
import random
import heapq
import json
import os
import colorsys
from collections import deque

GW, GH = 16, 8
DIRS4 = [(1, 0), (-1, 0), (0, 1), (0, -1)]

PLANE, CYL, SPHERE, CUBE = range(4)
NAMES = ["PLANO 2D", "CILINDRO", "ESFERA", "CUBO"]
VNAMES = ["plano", "cilindro", "esfera", "cubo"]
WRAP_V = [True, False, False, False]          # ¿se puede cruzar arriba/abajo?


# =================================================================== BÚSQUEDAS
def neighbors(walls, c, wv=False, seam=True):
    """Celdas libres vecinas. En X siempre se envuelve (si seam); en Y solo si la forma lo permite."""
    x, y = c
    out = []
    for dx, dy in DIRS4:
        nx, ny = x + dx, y + dy
        if nx < 0 or nx >= GW:
            if not seam:
                continue
            nx %= GW
        if ny < 0 or ny >= GH:
            if not wv:
                continue
            ny %= GH
        if not walls[ny][nx]:
            out.append((nx, ny))
    return out


def wdist(a, b, wv=False):
    """Distancia Manhattan con envoltura (heurística admisible para A*)."""
    dx = abs(a[0] - b[0])
    dx = min(dx, GW - dx)
    dy = abs(a[1] - b[1])
    if wv:
        dy = min(dy, GH - dy)
    return dx + dy


def bfs_dist(walls, start, wv=False, seam=True):
    dist = {start: 0}
    q = deque([start])
    while q:
        c = q.popleft()
        for n in neighbors(walls, c, wv, seam):
            if n not in dist:
                dist[n] = dist[c] + 1
                q.append(n)
    return dist


def connected_strict(walls):
    """Conectado SIN usar el borde izq/der: lo está en cualquier forma."""
    floors = [(x, y) for y in range(GH) for x in range(GW) if not walls[y][x]]
    return bool(floors) and len(bfs_dist(walls, floors[0], False, seam=False)) == len(floors)


def bfs_patrullar(walls, inicio, wv, visited, avoid=()):
    """BÚSQUEDA CIEGA (BFS): ruta a la celda NO visitada más cercana; si ya visitó todo,
    olvida y empieza otra ronda (nunca se atasca)."""
    for _ in range(2):
        prev = {inicio: None}
        q = deque([inicio])
        while q:
            c = q.popleft()
            if c != inicio and c not in visited and c not in avoid:
                path = []
                while c != inicio:
                    path.append(c)
                    c = prev[c]
                return path[::-1]
            ns = neighbors(walls, c, wv)
            random.shuffle(ns)
            for n in ns:
                if n not in prev:
                    prev[n] = c
                    q.append(n)
        visited.clear()
        visited.add(inicio)
    return []


def astar(walls, start, goal, wv, extra=None):
    if start == goal:
        return []
    heap = [(wdist(start, goal, wv), 0, start)]
    best = {start: 0}
    prev = {}
    done = set()
    while heap:
        _, gc, c = heapq.heappop(heap)
        if c in done:
            continue
        if c == goal:
            path = []
            while c != start:
                path.append(c)
                c = prev[c]
            return path[::-1]
        done.add(c)
        for n in neighbors(walls, c, wv):
            cost = gc + 1 + (extra(n) if extra else 0)
            if cost < best.get(n, 1e9):
                best[n] = cost
                prev[n] = c
                heapq.heappush(heap, (cost + wdist(n, goal, wv), cost, n))
    return []


def astar_huir(walls, fantasma, jugador, wv, danger=4, dp=None, heat=None, hw=0.0):
    """BÚSQUEDA HEURÍSTICA (A*) para HUIR del jugador:
    1) distancias reales por el laberinto (BFS) del jugador (dp) y del fantasma (dg);
    2) meta = celda lejos del jugador, a la que el fantasma llegue ANTES que él y con salidas;
    3) ruta A* hasta esa meta que evita pasar cerca del jugador.
    APRENDIZAJE: 'heat' es el mapa de calor (0..1) de por dónde suele ir el jugador; con hw>0 los
    fantasmas prefieren huir hacia donde TÚ casi nunca vas."""
    dp = dp or bfs_dist(walls, jugador, wv)
    dg = bfs_dist(walls, fantasma, wv)
    use_h = heat is not None and hw > 0
    mejor, mejor_s = None, -1e9
    for c, dgc in dg.items():
        dpc = dp.get(c, 40)
        s = dpc + 2.0 * min(len(neighbors(walls, c, wv)), 4) - 0.3 * dgc + (0 if dpc > dgc else -8)
        if use_h:
            s -= hw * 4.0 * heat[c[1]][c[0]]
        if s > mejor_s:
            mejor, mejor_s = c, s
    if mejor is None or mejor == fantasma:
        return []
    return astar(walls, fantasma, mejor, wv,
                 extra=lambda c: 3 * max(0, danger - dp.get(c, 99)) + (hw * 2.0 * heat[c[1]][c[0]] if use_h else 0.0))


class PlayerModel:
    """La IA APRENDE de cómo juegas (durante la partida y entre partidas):
       - mapa de calor: por dónde sueles moverte (los fantasmas huyen hacia donde NO vas);
       - nivel (skill 0..1): puntos por segundo, fantasmas atrapados seguidos y qué tan cerca los persigues;
       - con tu nivel adapta velocidad, astucia y errores de los fantasmas, y cuántos ítems de ayuda suelta."""

    def __init__(self, prior=0.5, games=0, strength=1.0):
        self.skill, self.last_skill = prior, prior
        self.games, self.strength = games, strength
        self.heat = [[0.0] * GW for _ in range(GH)]
        self.steps, self.catch_times, self.last_lesson = 0, [], -99.0

    def visit(self, x, y):
        self.heat[y][x] += 1.0
        self.steps += 1

    def on_catch(self, now):
        self.catch_times.append(now)

    def on_bar(self, now, bar_sec, bar_pts, near_dist):
        for row in self.heat:                                   # olvida poco a poco lo antiguo
            for x in range(GW):
                row[x] *= 0.94
        rate = min(1.0, (bar_pts / max(0.1, bar_sec)) / 150.0)
        self.catch_times = [t for t in self.catch_times if now - t < 20.0]
        catch = min(1.0, len(self.catch_times) / 3.0)
        pressure = 1.0 - min(1.0, near_dist / 8.0)
        target = 0.4 * rate + 0.4 * catch + 0.2 * pressure
        self.skill = max(0.0, min(1.0, self.skill + (target - self.skill) * 0.10 * self.strength))

    @property
    def dev(self):
        return (self.skill - 0.5) * 2.0 * self.strength

    @property
    def speed_mult(self):          # <1 = fantasmas más rápidos
        return 1.0 - 0.28 * self.dev

    @property
    def aware_add(self):
        return int(round(3 * self.dev))

    @property
    def error_mult(self):
        return 1.0 - 0.6 * self.dev

    @property
    def danger_add(self):
        return int(round(1.5 * self.dev))

    @property
    def item_mult(self):           # >1 = ítems más espaciados (juegas bien); <1 = más seguidos (apuros)
        return 1.0 + 0.6 * self.dev

    @property
    def density_add(self):
        return 0.05 * self.dev

    @property
    def heat_weight(self):         # cuánto confían los fantasmas en lo que aprendieron de ti
        return min(1.0, self.steps / 120.0) * (0.5 + 0.5 * self.strength)

    def heat_norm(self):
        mx = max(max(r) for r in self.heat)
        if mx <= 0 or self.heat_weight <= 0:
            return None
        return [[v / mx for v in r] for r in self.heat]

    def lesson(self, now):
        """Algo que la IA aprendió de ti (o None). Se muestra en pantalla y la voz lo dice."""
        if self.steps < 80 or now - self.last_lesson < 40:
            return None
        tot = sum(sum(r) for r in self.heat) + 1e-9
        left = sum(self.heat[y][x] for y in range(GH) for x in range(GW // 2)) / tot
        top = sum(self.heat[y][x] for y in range(GH // 2) for x in range(GW)) / tot
        out = []
        if left > 0.62:
            out.append(("sueles ir por la izquierda", "los fantasmas huirán a la derecha"))
        elif left < 0.38:
            out.append(("sueles ir por la derecha", "los fantasmas huirán a la izquierda"))
        if top > 0.62:
            out.append(("sueles ir por arriba", "los fantasmas huirán hacia abajo"))
        elif top < 0.38:
            out.append(("sueles ir por abajo", "los fantasmas huirán hacia arriba"))
        if self.skill > self.last_skill + 0.08:
            out.append(("estás mejorando", "subo la velocidad y la astucia de los fantasmas"))
        elif self.skill < self.last_skill - 0.08:
            out.append(("te veo en apuros", "los fantasmas se relajan y traigo más ítems"))
        if not out:
            return None
        self.last_lesson, self.last_skill = now, self.skill
        return random.choice(out)


# =================================================================== GEOMETRÍA
def _norm(v):
    l = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2) or 1.0
    return (v[0] / l, v[1] / l, v[2] / l)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def surf(shape, uu, vv):
    """Punto 3D y normal exterior de la celda (uu, vv). Convención: e_u x e_v = -normal."""
    a = 2 * math.pi * uu / GW
    if shape == PLANE:
        return ((uu / GW - 0.5) * 13.0, (vv / GH - 0.5) * 6.5, 0.0), (0.0, 0.0, -1.0)
    if shape == CYL:
        R = 2.6
        ca, sa = math.cos(a), math.sin(a)
        return (R * ca, (vv / GH - 0.5) * 6.0, R * sa), (ca, 0.0, sa)
    phi = math.pi * vv / GH
    d = (math.sin(phi) * math.cos(a), -math.cos(phi), math.sin(phi) * math.sin(a))
    if shape == SPHERE:
        return (3.0 * d[0], 3.0 * d[1], 3.0 * d[2]), d
    n = 6                                   # CUBO (superelipsoide)
    k = sum(abs(c) ** n for c in d) ** (1.0 / n) or 1.0
    p = tuple(3.0 * c / k for c in d)
    g = tuple(math.copysign(abs(c) ** (n - 1), c) for c in d)
    return p, _norm(g)


def blend_surf(sa, sb, m, uu, vv):
    pa, na = surf(sa, uu, vv)
    if m <= 0:
        return pa, na
    pb, nb = surf(sb, uu, vv)
    p = tuple(pa[i] + (pb[i] - pa[i]) * m for i in range(3))
    n = tuple(na[i] + (nb[i] - na[i]) * m for i in range(3))
    return p, _norm(n)


def frame_at(sa, sb, m, uu, vv):
    """Base ortonormal de la superficie en (uu, vv): e1=+u, e2=+v, e3=-normal. Como matriz de
    cámara deja +u a la derecha, +v hacia abajo y al jugador de frente."""
    e = 0.05
    p1, _ = blend_surf(sa, sb, m, uu + e, vv)
    p0, _ = blend_surf(sa, sb, m, uu - e, vv)
    q1, _ = blend_surf(sa, sb, m, uu, vv + e)
    q0, _ = blend_surf(sa, sb, m, uu, vv - e)
    e1 = _norm((p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]))
    ev = (q1[0] - q0[0], q1[1] - q0[1], q1[2] - q0[2])
    d = _dot(ev, e1)
    e2 = _norm((ev[0] - d * e1[0], ev[1] - d * e1[1], ev[2] - d * e1[2]))
    return (e1, e2, _cross(e1, e2))


IDENT = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def Rx(a):
    c, s = math.cos(a), math.sin(a)
    return ((1, 0, 0), (0, c, -s), (0, s, c))


def Ry(a):
    c, s = math.cos(a), math.sin(a)
    return ((c, 0, s), (0, 1, 0), (-s, 0, c))


def Rz(a):
    c, s = math.cos(a), math.sin(a)
    return ((c, -s, 0), (s, c, 0), (0, 0, 1))


def mmul(A, B):
    return tuple(tuple(sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def mvec(M, p):
    return (M[0][0] * p[0] + M[0][1] * p[1] + M[0][2] * p[2],
            M[1][0] * p[0] + M[1][1] * p[1] + M[1][2] * p[2],
            M[2][0] * p[0] + M[2][1] * p[1] + M[2][2] * p[2])


def quat_from_mat(m):
    t = m[0][0] + m[1][1] + m[2][2]
    if t > 0:
        s = math.sqrt(t + 1) * 2
        return (0.25 * s, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s)
    if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1 + m[0][0] - m[1][1] - m[2][2]) * 2
        return ((m[2][1] - m[1][2]) / s, 0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s)
    if m[1][1] > m[2][2]:
        s = math.sqrt(1 + m[1][1] - m[0][0] - m[2][2]) * 2
        return ((m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s)
    s = math.sqrt(1 + m[2][2] - m[0][0] - m[1][1]) * 2
    return ((m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s)


def mat_from_quat(q):
    w, x, y, z = q
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
            (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
            (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))


def qnlerp(a, b, t):
    if sum(x * y for x, y in zip(a, b)) < 0:
        b = tuple(-x for x in b)
    q = tuple(x + (y - x) * t for x, y in zip(a, b))
    l = math.sqrt(sum(x * x for x in q)) or 1.0
    return tuple(x / l for x in q)


def shade(c, k):
    return (max(0, min(255, int(c[0] * k))), max(0, min(255, int(c[1] * k))), max(0, min(255, int(c[2] * k))))


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# =================================================================== PALETAS (las decide la música)
def _hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return (int(r * 255), int(g * 255), int(b * 255))


def make_palette(h1, h2, sat, val, tron=False):
    """h1 = color del mundo, h2 = color de los fantasmas (y de los acentos)."""
    line = _hsv(h1, sat, val)
    return dict(bg=_hsv(h1, 0.85, 0.035), floor=_hsv(h1, 0.9, 0.10), line=line, wall=_hsv(h1, sat * 0.55, 1.0),
                line2=_hsv(h2, sat, 1.0), dot=_hsv(h1, 0.30, 1.0), ring=_hsv(h1, sat, 0.55), accent=line,
                player=(225, 252, 255) if tron else (255, 236, 90),
                ghosts=[_hsv(h2 + d, 0.85, 1.0) for d in (0, 0.05, -0.05, 0.1, -0.1)])


def lerp_pal(a, b, t):
    out = {}
    for k in a:
        out[k] = [mix(x, y, t) for x, y in zip(a[k], b[k])] if k == "ghosts" else mix(a[k], b[k], t)
    return out


# =================================================================== FANTASMAS (forma y comportamiento)
FAMILIES = {"clásicos": ["classic", "classic", "classic"], "geométricos": ["hex", "diamond", "tri"],
            "espinas": ["square", "star", "star"], "orbes": ["orb", "orb", "orb"]}
# El nivel de intensidad de la canción (0..3) decide cómo actúan: speed = beats por paso.
PROFILES = [dict(name="tranquilos", speed=1.5, aware=6, error=0.20, danger=2),
            dict(name="alertas", speed=1.1, aware=8, error=0.10, danger=3),
            dict(name="veloces", speed=0.85, aware=10, error=0.04, danger=4),
            dict(name="frenéticos", speed=0.70, aware=12, error=0.0, danger=6)]


def pick_family(feats, level, rng):
    lv = level / 3.0
    w = [1.6 - lv, 1.0 + lv * feats["bass"] * 2, 0.3 + lv * 2 * feats["high"], 1.0 + (1 - lv) * (1 - feats["bass"])]
    return rng.choices(list(FAMILIES), weights=w)[0]


# =================================================================== MAPAS
STYLES = ["pilares", "pasillos", "anillos", "diagonales", "caos", "circuito"]


def ensure_connected(walls, rng):
    free = [[0] * GW for _ in range(GH)]
    for _ in range(300):
        floors = [(x, y) for y in range(GH) for x in range(GW) if not walls[y][x]]
        comp, cid = {}, 0
        for f in floors:
            if f in comp:
                continue
            for c in bfs_dist(walls, f, False, seam=False):
                comp[c] = cid
            cid += 1
        if cid <= 1:
            break
        cand = []
        for y in range(GH):
            for x in range(GW):
                if walls[y][x]:
                    ids = {comp[n] for n in neighbors(free, (x, y), False, False) if n in comp}
                    if len(ids) >= 2:
                        cand.append((x, y))
        if not cand:
            cand = [(x, y) for y in range(GH) for x in range(GW) if walls[y][x]]
        x, y = rng.choice(cand)
        walls[y][x] = 0
    return walls


def gen_map(style, density, rng):
    """Mitad izquierda 8x8 según el estilo + espejo = 16x8, siempre conectado."""
    h = [[0] * 8 for _ in range(GH)]
    d = max(0.05, min(0.5, density))
    if style == "pilares":
        for y in range(1, GH - 1, 2):
            for x in range(1, 8, 2):
                if rng.random() < d * 2.6:
                    h[y][x] = 1
                    if x + 1 < 8 and rng.random() < 0.5:
                        h[y][x + 1] = 1
    elif style == "pasillos":
        for y in range(1, GH - 1, 2):
            x = rng.randrange(0, 3)
            while x < 8:
                ln = rng.randint(2, 4)
                if rng.random() < d * 3.0:
                    for k in range(ln):
                        if x + k < 8:
                            h[y][x + k] = 1
                x += ln + rng.randint(1, 2)
    elif style == "anillos":
        m = rng.choice([1, 2])
        for x in range(m, 8):
            h[m][x] = h[GH - 1 - m][x] = 1
        for y in range(m, GH - m):
            h[y][m] = 1
        ring = [(x, y) for y in range(GH) for x in range(8) if h[y][x]]
        for x, y in rng.sample(ring, min(len(ring), int(3 + (0.5 - d) * 8))):
            h[y][x] = 0
    elif style == "diagonales":
        k, o = rng.choice([3, 4]), rng.randrange(4)
        for y in range(GH):
            for x in range(8):
                if (x + y + o) % k == 0 and rng.random() < 0.45 + d:
                    h[y][x] = 1
    elif style == "circuito":                      # pistas de circuito impreso
        for y in rng.sample(range(1, GH - 1), 3):
            a, b = rng.randrange(0, 3), rng.randrange(5, 8)
            for x in range(a, b + 1):
                h[y][x] = 1
            for _ in range(2):
                h[y][rng.randrange(a, b + 1)] = 0
        for x in rng.sample(range(1, 8), 2):
            a, b = rng.randrange(0, 3), rng.randrange(4, GH)
            for y in range(a, b):
                h[y][x] = 1
    else:
        for y in range(GH):
            for x in range(8):
                if rng.random() < d:
                    h[y][x] = 1
    return ensure_connected([row + row[::-1] for row in h], rng)


def pick_style(feats, intensity, rng, tron=False):
    w = [1 + 3 * feats["bass"], 1 + 1.5 * feats["bass"] + 0.5 * intensity, 1.2 + (1 - feats["high"]) * 1.5,
         1 + 3 * feats["high"], 0.2 + 1.8 * intensity, 4.0 if tron else 0.4]
    return rng.choices(STYLES, weights=w)[0]


# =================================================================== IA DE ESCENA
BG_STYLES = ["túnel", "horizonte", "ecualizador", "estrellas", "ondas"]
FX_MODES = ["eco", "mosaico", "espejo", "órbita"]


def seg_feats(seg, song):
    """Rasgos de ESTA sección (no de toda la canción), en la misma escala que song.feats."""
    return dict(bass=min(1.0, seg["bassr"] * 2.0), high=min(1.0, seg["bright"] * 2.0), rate=song.feats["rate"])


def pick_bg(seg, surge, rng, tron=False):
    """Fondo animado según la sección: graves -> túnel, agudos -> estrellas, calma -> ondas..."""
    lv = seg["level"] / 3.0
    w = [1.0 + 2.0 * seg["bassr"] + 0.5 * lv,
         1.2 + 0.8 * (1 - seg["bright"]) + (0.8 if seg["level"] == 2 else 0.0),
         0.5 + 2.5 * (1.0 if surge else lv) + lv,
         0.7 + 2.2 * seg["bright"],
         0.2 + 1.8 * (1 - lv)]
    if tron:
        w[1] *= 1.8                                   # la rejilla de horizonte pega con Tron
    return rng.choices(BG_STYLES, weights=w)[0]


def pick_fx(seg, rng):
    """Cómo se MULTIPLICA la escena hacia el fondo en las partes muy movidas."""
    return rng.choices(FX_MODES, weights=[1 + seg["bassr"], 0.8 + seg["bright"], 0.9 + 0.5 * seg["bassr"], 1.0])[0]


class SceneAI:
    """Escucha la canción y, en cada compás (4 beats), decide:
         MAPA, FORMA (2D <-> 3D), COLORES, FONDO, EFECTO DE MULTIPLICAR y FANTASMAS (forma + conducta).
    - Secciones y drops (según el análisis) -> cambios grandes.
    - PARTES MUY MOVIDAS (surges) -> el mapa y la forma cambian sin parar, los colores derivan y la
      escena se multiplica en el fondo; al calmarse todo vuelve a la normalidad."""

    def __init__(self, song, shapes, tron, special, seed):
        self.song, self.shapes, self.tron, self.special = song, shapes, tron, special
        self.rng = random.Random(seed + 99)
        self.hue_off = (seed % 997) / 997.0
        self.dir = 1 if seed % 2 else -1
        self.cur_level, self.cur_seg = song.level_at(0.0), song.segment_idx(0.0)
        self.last_shape = self.last_event = self.last_map = 0
        self.surge_on, self.fx_on, self.fx_until, self.fx_mode, self.walk = False, False, 0, "eco", 0.0
        self.decision, self.reason, self.age = "Escuchando...", "analizo el ritmo en vivo", 99.0

    def tick(self, dt):
        self.age += dt

    def _set(self, d, r):
        self.decision, self.reason, self.age = d, r, 0.0

    def palette_for(self, level, boost=0.0):
        f = self.song.feats
        if self.tron:                                   # Circuito: cian del mundo, naranja de los programas
            return make_palette(0.53 + 0.012 * level + 0.05 * boost, 0.075 + 0.01 * level,
                                0.92 - 0.09 * level, 0.85 + 0.05 * level, True)
        base = (0.92 + 0.55 * f["high"] - 0.15 * f["bass"] + self.hue_off * 0.6) % 1.0
        h1 = base + (0, 0.07, 0.16, 0.30)[level] * self.dir + boost
        return make_palette(h1, h1 + (0.33 if level < 2 else 0.5), 0.55 + 0.12 * level, 0.80 + 0.07 * level)

    def initial(self):
        lv = self.cur_level
        sf = seg_feats(self.song.segment_at(0.0), self.song)
        return (pick_style(sf, lv / 3.0, self.rng, self.tron), self.palette_for(lv),
                pick_family(sf, lv, self.rng))

    def initial_bg(self):
        return pick_bg(self.song.segment_at(0.0), False, self.rng, self.tron)

    def on_bar(self, beat, t, shape_now, perf):
        s, rng, acts = self.song, self.rng, []
        bar = 4 * s.spb
        seg, si, lvl = s.segment_at(t), s.segment_idx(t), s.level_at(t)
        sf = seg_feats(seg, s)
        surge = s.in_surge(t)
        drop = s.drop_between(t, t + bar)
        sect = si != self.cur_seg
        went_up = lvl > self.cur_level
        self.cur_seg, self.cur_level = si, lvl
        event = "¡drop!" if drop else ("cambia la sección" if sect else None)

        # ---- PARTES MUY MOVIDAS: entrar / salir
        if surge and not self.surge_on:
            self.surge_on = True
            acts += [("note", "Parte muy movida: el escenario cambiará sin parar."),
                     ("bg", pick_bg(seg, True, rng, self.tron), "parte muy movida")]
            self._set("¡Parte muy movida!", "el mapa y la forma cambian sin parar")
        elif not surge and self.surge_on:
            self.surge_on = False
            acts.append(("bg", pick_bg(seg, False, rng, self.tron), "se calma"))
            self._set("Se calma la música", "todo vuelve a la normalidad")
        if drop and not surge:
            self.fx_until = beat + 8                    # el drop multiplica la escena durante 2 compases
        want_fx = surge or beat < self.fx_until
        if want_fx != self.fx_on:
            self.fx_on = want_fx
            if want_fx:
                self.fx_mode = pick_fx(seg, rng)
            acts.append(("fx", want_fx, self.fx_mode, "parte muy movida" if surge else ("¡drop!" if want_fx else "se calma")))

        # ---- MAPA y COLORES
        if surge:
            if beat - self.last_map >= 4:               # un mapa nuevo en cada compás
                style = pick_style(sf, 1.0, rng, self.tron)
                self.walk = (self.walk + 0.07) % 1.0
                acts += [("map", style, "parte muy movida"),
                         ("palette", self.palette_for(max(lvl, 2), self.walk), "parte muy movida")]
                if beat - self.last_event >= 16:
                    acts.append(("ghosts", pick_family(sf, 3, rng), PROFILES[3]["name"], "parte muy movida"))
                    self.last_event = beat
                self.last_map = beat
        elif event and beat - self.last_event >= 6:
            style = pick_style(sf, lvl / 3.0, rng, self.tron)
            fam = pick_family(sf, lvl, rng)
            acts += [("map", style, event), ("palette", self.palette_for(lvl, 0.12 if drop else 0.0), event),
                     ("ghosts", fam, PROFILES[lvl]["name"], event), ("bg", pick_bg(seg, False, rng, self.tron), event)]
            self.last_event = self.last_map = beat
            self._set(f"Mapa {style} · fantasmas {fam}", f"{event} · son {PROFILES[lvl]['name']}")

        # ---- FORMA (2D <-> 3D)
        gap = 8 if (surge or self.special) else 12
        if beat - self.last_shape >= gap and len(self.shapes) > 1:
            go3d, why = None, ""
            if surge:
                go3d, why = (shape_now == PLANE) or rng.random() < 0.5, "parte muy movida"
            elif drop or (event and went_up):
                go3d, why = True, "sube la energía"
            elif event and not went_up:
                go3d, why = False, "baja la energía"
            elif shape_now != PLANE and perf < 0.2:
                go3d, why = False, "te veo perdido: vuelvo a 2D"
            elif beat - self.last_shape >= 32 and rng.random() < 0.6:
                go3d, why = (shape_now == PLANE), "cambio rítmico"
            target = None
            if go3d and shape_now == PLANE:
                target = rng.choices([CYL, SPHERE, CUBE], weights=[1, 1 + 3 * sf["high"], 1 + 3 * sf["bass"]])[0]
            elif go3d and shape_now != PLANE and beat - self.last_shape >= (8 if surge else 24):
                target = rng.choice([x for x in self.shapes if x not in (PLANE, shape_now)])
            elif go3d is False and shape_now != PLANE:
                target = PLANE
            if target is not None:
                acts.append(("shape", target, why))
                self.last_shape = beat
                if not surge:
                    self._set("Forma: " + NAMES[target], why)
        return acts


# =================================================================== BIBLIOTECA
BASE = os.path.dirname(os.path.abspath(__file__))
LIB_FILE = os.path.join(BASE, "biblioteca_ritmo.json")
SONG_DIR = os.path.join(BASE, "canciones")
CIR_DIR = os.path.join(SONG_DIR, "circuito")


def _entry(p):
    return dict(path=p, best=0, grade="")


def load_lib():
    lib = dict(mi_musica=[], circuito=[], derezzed=None, latency=0.0)
    try:
        with open(LIB_FILE, encoding="utf-8") as f:
            lib.update(json.load(f))
    except Exception:
        pass
    os.makedirs(CIR_DIR, exist_ok=True)
    known = {e["path"] for e in lib["mi_musica"] + lib["circuito"]}
    for folder, key in ((SONG_DIR, "mi_musica"), (CIR_DIR, "circuito")):
        for fn in sorted(os.listdir(folder)):
            p = os.path.join(folder, fn)
            if not fn.lower().endswith(".mp3"):
                continue
            if "derezzed" in fn.lower():
                if key == "circuito" and not lib["derezzed"]:
                    lib["derezzed"] = _entry(p)
            elif p not in known:
                lib[key].append(_entry(p))
    for key in ("mi_musica", "circuito"):
        lib[key] = [e for e in lib[key] if os.path.exists(e["path"])]
    if lib["derezzed"] and not os.path.exists(lib["derezzed"]["path"]):
        lib["derezzed"] = None
    return lib


def save_lib(lib):
    try:
        with open(LIB_FILE, "w", encoding="utf-8") as f:
            json.dump(lib, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def add_song(lib, tab, path):
    """tab 0 = MI MÚSICA, tab 1 = CIRCUITO. Devuelve un mensaje para la interfaz."""
    if not path.lower().endswith(".mp3"):
        return "Solo se aceptan archivos .mp3"
    if "derezzed" in os.path.basename(path).lower():
        if tab != 1:
            return "Derezzed solo se usa en CIRCUITO (pista especial)"
        lib["derezzed"] = _entry(path)
        save_lib(lib)
        return "Derezzed cargada en la pista especial"
    key = "mi_musica" if tab == 0 else "circuito"
    if any(e["path"] == path for e in lib[key]):
        return "Esa canción ya está en la lista"
    lib[key].append(_entry(path))
    save_lib(lib)
    return "Canción añadida"
