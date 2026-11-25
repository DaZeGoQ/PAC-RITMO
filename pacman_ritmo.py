"""
PAC-RITMO  -  Pac-Man inverso dirigido por TU música (.mp3)

Tú persigues a los fantasmas y ELLOS HUYEN de ti. La canción y la IA deciden cómo es el mapa, los
colores, la forma de los fantasmas y cómo actúan, siempre pegados al ritmo.

Requisitos:  pip install pygame numpy pyttsx3        (pyttsx3 es opcional: es la voz de la IA)
Archivos:    pacman_ritmo.py (este) + nucleo.py + sonido.py, en la misma carpeta.
Ejecutar:    python pacman_ritmo.py            (pantalla completa)
             python pacman_ritmo.py --ventana  (en ventana)

Subir canciones:  tecla U (explorador de archivos del juego), arrastrar .mp3 a la ventana, o copiarlos a
                  canciones/ (MI MÚSICA) o canciones/circuito/ (CIRCUITO). La pista especial de CIRCUITO solo
                  acepta Derezzed (Daft Punk): el archivo debe llamarse "...derezzed...mp3".
                  Los audios NO vienen incluidos: usa tus propias copias.

Controles:  Flechas / WASD mover | ENTER aceptar | ESC pausa / atrás | F11 pantalla completa
            , y . ajustan la sincronía | F2 música | F3 voz | F4 efectos suaves (menos destellos) | F1 depuración
"""
import os
import sys
import math
import random
import string
import threading
import zlib
import numpy as np
import pygame


from nucleo import *
from sonido import *


# ------------------------------------------------------------------ constantes
DESIGN_H, DESIGN_W = 740.0, 1300.0           # el HUD se diseña en estas unidades y se escala a la pantalla
CAM_D = 10.0
WALL_H = 0.55
PAR_RATE = 500.0                              # puntos/seg "de referencia" para el rango (x multiplicador)
ATTRACT = ("intro", "songs", "browser", "analyzing", "difficulty", "results")
PLAYING = ("countdown", "play", "pause", "tutorial")
GHOST_START = [(7, 3), (8, 3), (7, 6), (8, 6), (4, 3)]
KEYDIRS = {
    pygame.K_UP: (0, -1), pygame.K_w: (0, -1), pygame.K_DOWN: (0, 1), pygame.K_s: (0, 1),
    pygame.K_LEFT: (-1, 0), pygame.K_a: (-1, 0), pygame.K_RIGHT: (1, 0), pygame.K_d: (1, 0),
}
GRADE_COL = {"S": (255, 215, 80), "A": (110, 235, 140), "B": (100, 175, 255), "C": (255, 175, 70), "D": (255, 100, 110)}
PAL_GROOVE = make_palette(0.90, 0.52, 0.80, 0.92)
PAL_TRON = make_palette(0.53, 0.075, 0.90, 0.90, True)

DIFFS = [
    dict(name="FÁCIL", color=(90, 230, 140), ghosts=3, speed=1.25, aware=-2, error=2.0, mult=1.0,
         desc=["3 fantasmas", "más lentos y torpes", "puntos x1"]),
    dict(name="NORMAL", color=(90, 170, 255), ghosts=4, speed=1.0, aware=0, error=1.0, mult=1.5,
         desc=["4 fantasmas", "ritmo medio", "puntos x1.5"]),
    dict(name="DIFÍCIL", color=(255, 90, 110), ghosts=5, speed=0.85, aware=2, error=0.5, mult=2.0,
         desc=["5 fantasmas", "más rápidos y listos", "puntos x2"]),
]
TUT_CFG = dict(name="TUTORIAL", ghosts=2, speed=1.0, aware=0, error=1.0, mult=1.0)
for _d, _f in zip(DIFFS, (0.45, 0.75, 1.0)):      # cuántos efectos visuales: más en DIFÍCIL
    _d["fx"] = _f
TUT_CFG["fx"] = 0.3
# transparencia del mundo (1 = opaco): FÁCIL y NORMAL menos transparentes; DIFÍCIL cambia según cómo juegas
for _d, _o, _i, _fz, _ln, _t in zip(DIFFS, (0.85, 0.70, None), (0.8, 1.0, 1.3), ((6.0, 9.0), (5.0, 8.0), (3.5, 6.0)),
                                    (0.7, 1.0, 1.3), ("transparencia baja", "transparencia media", "transparencia dinámica")):
    _d.update(opa=_o, item=_i, fz=_fz, learn=_ln)         # item: frecuencia de ítems | fz: (congelar, ralentizar) en s
    _d["desc"].append(_t)
TUT_CFG.update(opa=0.85, item=1.0, fz=(5.0, 8.0), learn=1.0)
TUT_STEPS = [
    ("1 · MUÉVETE", "Usa las FLECHAS o WASD y come 6 puntos. Tu disco se mueve al compás de la música.",
     "Muévete con las flechas y come seis puntos."),
    ("2 · ATRÁPALOS", "Tú persigues a los fantasmas y ellos HUYEN de ti (A*). Si estás lejos, patrullan (BFS). Atrapa uno.",
     "Ahora persigue a un fantasma. Ellos huyen de ti."),
    ("3 · LA MÚSICA MANDA", "La canción y la IA deciden el mapa, los colores, el fondo y los fantasmas. En las partes muy movidas multiplican el escenario. Mira.",
     "Mira: la música cambia el mapa, los colores y a los fantasmas."),
    ("4 · 2D Y 3D", "En los cambios de sección y los drops, la IA pasa de 2D a 3D siguiendo el ritmo.",
     "Mira cómo cambio de dos D a tres D, al ritmo de la música."),
    ("5 · ¡LISTO!", "Recoge el ítem de HIELO: congela a los fantasmas (el RELOJ los ralentiza). La IA aprende de cómo juegas y se adapta.   ENTER para continuar.",
     "Muy bien. Recoge el ítem de hielo para congelar a los fantasmas. Cuando termines, pulsa enter."),
]


# ------------------------------------------------------------------ utilidades
class Entity:
    def __init__(self, x, y):
        self.x, self.y, self.vx, self.vy = x, y, float(x), float(y)
        self.dir = (1, 0)


class Ghost(Entity):
    def __init__(self, x, y, idx):
        super().__init__(x, y)
        self.idx, self.band = idx, idx % 3
        self.mode, self.path, self.goal, self.visited, self.fear, self.prev = "patrol", [], None, set(), 0.0, (x, y)


class Job:
    """Analiza una canción en un hilo para que la ventana no se congele."""

    def __init__(self, path):
        self.progress, self.song, self.error = 0.0, None, None
        threading.Thread(target=self._run, args=(path,), daemon=True).start()

    def _run(self, path):
        try:
            self.song = analyze_song(path, self._cb)
        except Exception as e:
            self.error = str(e)

    def _cb(self, p):
        self.progress = p


def title_of(path):
    return os.path.basename(path).rsplit(".", 1)[0]


def mmss(t):
    t = max(0, int(t))
    return f"{t // 60}:{t % 60:02d}"


def chaikin(pts, it=2, r=0.25):
    """Redondea un polígono (corte de esquinas de Chaikin): quita lo geométrico de los bordes."""
    for _ in range(it):
        out, n = [], len(pts)
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            out.append(((1 - r) * a[0] + r * b[0], (1 - r) * a[1] + r * b[1]))
            out.append((r * a[0] + (1 - r) * b[0], r * a[1] + (1 - r) * b[1]))
        pts = out
    return pts


def inset(pts, k):
    cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return [(cx + (x - cx) * k, cy + (y - cy) * k) for x, y in pts]


def convex_hull(points):
    pts = sorted(set((int(x), int(y)) for x, y in points))
    if len(pts) < 3:
        return pts
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for p_ in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p_) <= 0:
            lo.pop()
        lo.append(p_)
    for p_ in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p_) <= 0:
            up.pop()
        up.append(p_)
    return lo[:-1] + up[:-1]


def make_vignette(W, H):
    ys, xs = np.linspace(-1, 1, H)[None, :], np.linspace(-1, 1, W)[:, None]
    r = np.sqrt(0.8 * xs ** 2 + ys ** 2)
    v = np.clip(1.12 - 0.55 * r ** 2.2, 0.35, 1.0)
    return pygame.surfarray.make_surface((np.stack([v, v, v], -1) * 255).astype(np.uint8))


# =================================================================== JUEGO
class Game:
    def __init__(self, fullscreen=True, size=None):
        pygame.init()
        self.audio, self.voice = Audio(), VoiceAI()
        pygame.display.set_caption("PAC-RITMO")
        self.clock = pygame.time.Clock()
        self.fullscreen = fullscreen
        self.quality = 2                  # 2 = máxima; baja sola si el equipo va justo
        self.apply_display(fullscreen, size)
        self.lib = load_lib()
        self.latency = float(self.lib.get("latency", 0.0))
        self.cache, self.job, self.pick, self.cur_song = {}, None, None, None
        self.tab, self.sel, self.scroll, self.sel_diff = 0, [0, 0], [0, 0], 1
        self.msg, self.caption, self.banner = ("", 0.0), ("", 0.0), ("", 0.0)
        self.final = {}
        self.t = 0.0
        self.debug = self.show_paths = False
        self.quality, self.soft, self.fps_acc, self.fps_n, self.up = 2, False, 0.0, 0, 0
        self.tut_step, self.tut_t = 0, 0.0
        self.br_path, self.br_items, self.br_sel, self.br_scroll = "", [], 0, 0
        self.reset_world(DIFFS[1], builtin_song(), False)
        self.enter_attract("intro")
        self.voice.say("Bienvenido a Pac Ritmo. Yo soy la inteligencia que controla el escenario.")

    # ---------- pantalla
    def set_render_scale(self):
        """El mundo se dibuja a una resolución interna (tope según la calidad) y se escala a la pantalla;
        el HUD se dibuja encima a resolución nativa. Así 1440p/4K no hunden los FPS."""
        self.RH = int(min(self.H, (600, 810, 1080)[self.quality]))
        self.RW = int(self.W * self.RH / self.H)
        self.rK = self.K * self.RH / self.H
        self.RW2, self.RH2 = self.RW // 2, self.RH // 2
        self.RCX, self.RCY = self.RW // 2, int(self.RH * 0.5 + 8 * self.rK)
        self.focal0 = 700.0 * self.rK
        self.focal = self.focal0
        self.w1, self.w2 = max(1, int(self.rK)), max(2, int(2 * self.rK))
        mk = lambda w, h: pygame.Surface((max(1, w), max(1, h)))
        self.scene, self.trail, self.canvas, self.bgFull, self.glow = (mk(self.RW, self.RH) for _ in range(5))
        self.glow_small = mk(self.RW // 6, self.RH // 6)
        self.backing = mk(self.RW, self.RH)
        self.backing.set_colorkey((0, 0, 0))
        self.bgA, self.bgB = mk(self.RW2, self.RH2), mk(self.RW2, self.RH2)
        self.vig = make_vignette(self.RW2, self.RH2)
        self.fb = None

    def apply_display(self, full, size=None):
        if full:
            try:
                size = pygame.display.get_desktop_sizes()[0]
            except Exception:
                size = (1920, 1080)
            if size[0] < 640:
                size = (1920, 1080)
            self.screen = pygame.display.set_mode(size, pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode(size or (1366, 768))
        self.fullscreen = full
        self.W, self.H = self.screen.get_size()
        self.K = min(self.W / DESIGN_W, self.H / DESIGN_H)
        self.DW, self.DH = self.W / self.K, self.H / self.K
        self.CX, self.CY = self.W // 2, int(self.H * 0.5 + 8 * self.K)
        names = "segoeui,helveticaneue,arial,dejavusans"
        mk = lambda n, b=False: pygame.font.SysFont(names, max(8, int(n * self.K)), bold=b)
        self.f_xs, self.f_s, self.f_m, self.f_l, self.f_xl, self.f_t = mk(13, True), mk(16), mk(20, True), mk(30, True), mk(46, True), mk(84, True)
        self.set_render_scale()

    # ---------- mundo
    def reset_world(self, cfg, song, tron, special=False, tutorial=False):
        self.cfg, self.song, self.tron, self.special, self.tutorial = cfg, song, tron, special, tutorial
        self.shapes = [PLANE, CYL, SPHERE, CUBE]
        pr = {} if (tutorial or song.builtin) else self.lib.get("profile", {})
        self.model = PlayerModel(pr.get("skill", 0.5), pr.get("games", 0), cfg.get("learn", 1.0))   # lo que la IA sabe de ti
        self.items, self.freeze_until, self.slow_until = [], 0.0, 0.0
        self.item_timer = 14.0 * cfg.get("item", 1.0)
        seed = zlib.crc32((song.path or "tutorial").encode())
        self.rng = random.Random(seed)
        self.scene_ai = SceneAI(song, self.shapes, tron, special, seed)
        lvl0 = song.level_at(0.0) / 3.0
        if tutorial or song.builtin:
            style, pal, fam = "pasillos", (PAL_TRON if tron else PAL_GROOVE), "clásicos"
            walls = gen_map("pasillos", 0.14, random.Random(7))
        else:
            style, pal, fam = self.scene_ai.initial()
            walls = gen_map(style, 0.10 + 0.20 * lvl0, self.rng)
        self.map_style, self.walls = style, walls
        self.pal = self.pal_target = pal
        self.gfamily = fam
        self.hw = [[float(walls[y][x]) for x in range(GW)] for y in range(GH)]
        self.changing, self.flash = {}, {}
        self.dots = [[0 if walls[y][x] else 1 for x in range(GW)] for y in range(GH)]
        self.player = Entity(1, 3)
        self.relocate(self.player)
        self.dots[self.player.y][self.player.x] = 0
        self.prev_player, self.want = (self.player.x, self.player.y), (1, 0)
        n = 2 if tutorial else cfg["ghosts"]
        self.ghosts = [Ghost(*GHOST_START[i], i) for i in range(n)]
        for g in self.ghosts:
            self.relocate(g)
        self.ghosts_on = not tutorial
        self.sub = step_sub(song.spb)
        self.step_t = song.spb / self.sub
        self.song_time = 0.0
        bt = -song.offset / song.spb
        self.beat_total, self.beat_i, self.step_i = bt, math.floor(bt), math.floor(bt * self.sub)
        self.beat_tick = self.step_tick = self.bar_tick = False
        self.ghost_acc, self.flips_bar, self.bar_pts, self.perf = 0.0, 0, 0.0, 0.5
        self.score, self.combo, self.combo_deadline = 0, 0, 0.0
        self.stats = dict(dots=0, caught=0, maxcombo=0, rounds=0, maps=0, shapes=0, items=0)
        self.sa = self.sb = PLANE
        self.m, self.trans_t, self.transitioning, self.trans_dur = 0.0, 0.0, False, 1.0
        self.cam_q, self.M, self.pan = None, IDENT, (0.0, 0.0)
        self.elev, self.shake, self.yaw_kick, self.roll_kick, self.roll = 0.0, 0.0, 0.0, 0.0, 0.0
        self.pulse, self.wave_t = 0.0, 9.0
        self.rings, self.onset_i, self.player_pt = [], 0, (self.CX, self.CY)
        self.cur_profile_name = PROFILES[1]["name"]
        self.reset_fx()

    def enter_attract(self, state):
        self.state = state
        self.sa = self.sb = CUBE
        self.m, self.transitioning, self.cam_q = 0.0, False, None
        self.ghosts_on = True
        for g in self.ghosts:
            g.path, g.goal = [], None
        self.audio.play_music("menu")

    @property
    def cur_shape(self):
        return self.sb if self.m > 0.5 else self.sa

    @property
    def wv(self):
        return WRAP_V[self.cur_shape]

    def target(self, x, y, dx, dy):
        nx, ny = (x + dx) % GW, y + dy
        if ny < 0 or ny >= GH:
            if not self.wv:
                return None
            ny %= GH
        return nx, ny

    def can_go(self, e, d):
        t = self.target(e.x, e.y, *d)
        return t is not None and not self.walls[t[1]][t[0]]

    def relocate(self, e):
        if not self.walls[e.y][e.x]:
            return
        _, x, y = min((wdist((x, y), (e.x, e.y), True), x, y)
                      for y in range(GH) for x in range(GW) if not self.walls[y][x])
        e.x, e.y, e.vx, e.vy = x, y, float(x), float(y)

    def free_cell(self, far_from=None, mind=5):
        busy = {(e.x, e.y) for e in self.ghosts + [self.player]} | {(i["x"], i["y"]) for i in self.items}
        for _ in range(80):
            x, y = random.randrange(GW), random.randrange(GH)
            if self.walls[y][x] or (x, y) in busy:
                continue
            if far_from and wdist((x, y), far_from, self.wv) < mind:
                continue
            return x, y
        return None

    def say(self, banner, voice=None, prio=1):
        if banner:
            self.banner = (banner, 1.8)
        if voice:
            self.caption = (voice, 3.5)
            self.voice.say(voice, prio)

    def add_score(self, pts):
        self.bar_pts += pts
        self.score += int(pts * self.cfg["mult"])

    # ---------- cambios decididos por la música y la IA
    def set_family(self, fam):
        self.gfamily = fam

    def load_walls(self, new):
        for y in range(GH):
            for x in range(GW):
                if new[y][x]:
                    self.dots[y][x] = 0
                elif self.walls[y][x]:
                    self.dots[y][x] = 1
        self.walls = new
        self.changing = {}
        for y in range(GH):
            for x in range(GW):
                if abs(self.hw[y][x] - float(new[y][x])) > 0.01:
                    self.changing[(x, y)] = wdist((x, y), (self.player.x, self.player.y), True) * 0.045
        for e in self.ghosts + [self.player]:
            self.relocate(e)
        for g in self.ghosts:
            g.path, g.goal = [], None
            g.visited.clear()
        self.items = [i for i in self.items if not self.walls[i["y"]][i["x"]]]
        self.shake, self.wave_t = 12.0, 0.0
        self.audio.sfx("mutate")

    def apply_map(self, style):
        lvl = self.song.level_at(self.song_time + 4 * self.song.spb) / 3.0 if not self.song.builtin else 0.3
        self.map_style = style
        self.stats["maps"] += 1
        self.load_walls(gen_map(style, 0.10 + 0.20 * lvl + self.model.density_add, self.rng))

    def start_transition(self, target, why=""):
        if self.transitioning or target == self.sa:
            return
        self.sb, self.trans_t, self.transitioning = target, 0.0, True
        self.trans_dur = max(0.6, min(2.0, 2 * self.song.spb))
        self.stats["shapes"] += 1
        self.shake = 12.0
        self.yaw_kick = random.choice([-1, 1]) * random.uniform(1.0, 2.2)
        self.roll_kick = random.uniform(-0.5, 0.5)
        self.audio.sfx("whoosh")
        if target == PLANE:
            self.say(">> " + NAMES[target], "Cambio a dos D. Vista plana.", 2)
        else:
            self.say(">> " + NAMES[target], f"Cambio a tres D. Forma: {VNAMES[target]}.", 2)

    def mutate_cell(self):
        """El mapa respira con el ritmo: un golpe fuerte de la música voltea una casilla."""
        busy = {(e.x, e.y) for e in self.ghosts + [self.player]} | {(i["x"], i["y"]) for i in self.items}
        for _ in range(40):
            x, y = random.randrange(GW), random.randrange(GH)
            if (x, y) in busy:
                continue
            self.walls[y][x] ^= 1
            if self.walls[y][x] and not connected_strict(self.walls):
                self.walls[y][x] = 0
                continue
            self.dots[y][x] = 0 if self.walls[y][x] else 1
            self.flash[(x, y)] = 1.0
            return

    def profile(self):
        base = PROFILES[1] if self.song.builtin else PROFILES[self.song.level_at(self.song_time)]
        c, m = self.cfg, self.model
        speed = max(1.18 / self.sub, base["speed"] * c["speed"] * m.speed_mult)    # nunca más rápidos que tú
        return dict(name=base["name"], speed=speed, aware=base["aware"] + c["aware"] + m.aware_add,
                    error=max(0.0, min(0.5, base["error"] * c["error"] * m.error_mult)),
                    danger=max(1, base["danger"] + m.danger_add))

    def move_player(self):
        p = self.player
        self.prev_player = (p.x, p.y)
        for d in (self.want, p.dir):
            if self.can_go(p, d):
                p.x, p.y = self.target(p.x, p.y, *d)
                p.dir = d
                break
        self.model.visit(p.x, p.y)                                          # la IA aprende por dónde vas
        for it in list(self.items):
            if (it["x"], it["y"]) == (p.x, p.y):
                self.items.remove(it)
                self.pickup_item(it)
        if self.dots[p.y][p.x]:
            self.dots[p.y][p.x] = 0
            self.stats["dots"] += 1
            self.add_score(10)
            self.audio.sfx("dot%d" % (self.stats["dots"] % 2))
            if not any(any(r) for r in self.dots):
                self.dots = [[0 if self.walls[y][x] else 1 for x in range(GW)] for y in range(GH)]
                self.stats["rounds"] += 1
                self.add_score(300)
                self.say("¡RONDA!  +300")

    def step_ghost_to(self, g, nxt):
        if nxt not in neighbors(self.walls, (g.x, g.y), self.wv):
            g.path = []
            return
        dx, dy = nxt[0] - g.x, nxt[1] - g.y
        dx = -1 if dx > 1 else (1 if dx < -1 else dx)
        dy = -1 if dy > 1 else (1 if dy < -1 else dy)
        g.dir = (dx, dy)
        g.x, g.y = nxt

    def move_ghosts(self, prof):
        """Los fantasmas HUYEN de ti (A*) mientras estés a su alcance; si estás lejos, patrullan (BFS)."""
        if not self.ghosts_on:
            return
        wv = self.wv
        pl = (self.player.x, self.player.y)
        dp = bfs_dist(self.walls, pl, wv)
        heat, hw = self.model.heat_norm(), self.model.heat_weight
        for g in self.ghosts:
            pos = (g.x, g.y)
            g.prev = pos
            if dp.get(pos, 99) <= prof["aware"]:                         # A*: huir
                g.mode = "flee"
                g.path = astar_huir(self.walls, pos, pl, wv, prof["danger"], dp, heat, hw)
                g.goal = g.path[-1] if g.path else None
            else:                                                        # BFS: patrullar
                if g.mode == "flee":
                    g.mode, g.path = "patrol", []
                g.visited.add(pos)
                g.visited.update(neighbors(self.walls, pos, wv))
                if not g.path:
                    claimed = {o.goal for o in self.ghosts if o is not g and o.goal}
                    g.path = bfs_patrullar(self.walls, pos, wv, g.visited, claimed)
                    g.goal = g.path[-1] if g.path else None
            if g.path:
                if random.random() < prof["error"]:
                    opts = neighbors(self.walls, pos, wv)
                    if opts:
                        g.path = []
                        self.step_ghost_to(g, random.choice(opts))
                else:
                    self.step_ghost_to(g, g.path.pop(0))

    def check_catch(self):
        p = self.player
        for g in self.ghosts:
            same = (g.x, g.y) == (p.x, p.y)
            swap = (g.x, g.y) == self.prev_player and g.prev == (p.x, p.y)
            if same or swap:
                self.combo = min(5, self.combo + 1) if self.song_time < self.combo_deadline else 1
                self.combo_deadline = self.song_time + 6.0
                self.stats["caught"] += 1
                self.model.on_catch(self.song_time)
                self.stats["maxcombo"] = max(self.stats["maxcombo"], self.combo)
                self.add_score(500 * self.combo)
                self.audio.sfx(f"catch{self.combo}")
                c = self.free_cell((p.x, p.y), 7)
                if c:
                    g.x, g.y, g.vx, g.vy, g.prev = c[0], c[1], float(c[0]), float(c[1]), c
                g.path, g.goal, g.mode = [], None, "patrol"
                g.visited.clear()
                self.say(f"¡ATRAPADO!  x{self.combo}", "Combo máximo." if self.combo == 5 else None, 0)

    # ---------- lógica de partida / tutorial
    def update_play(self, dt):
        now = self.song_time
        for it in self.items:
            it["ttl"] -= dt
        self.items = [i for i in self.items if i["ttl"] > 0]
        if self.state == "play":
            self.spawn_items(dt)
        if self.step_tick:
            self.move_player()
            self.check_catch()
            prof = self.profile()
            if prof["name"] != self.cur_profile_name:
                self.cur_profile_name = prof["name"]
            if now < self.freeze_until:                                  # CONGELADOS: no se mueven
                self.ghost_acc = 0.0
            else:
                self.ghost_acc += 1.0 / self.sub
                spd = prof["speed"] * (2.2 if now < self.slow_until else 1.0)   # LENTOS: la mitad de rápidos
                if self.ghost_acc >= spd:                                # los fantasmas se mueven en el ritmo
                    self.ghost_acc -= spd
                    self.move_ghosts(prof)
                    self.check_catch()
            if self.state == "play" and not self.song.builtin:
                self.rhythm_mutation()
        if self.state != "play":
            return
        self.scene_ai.tick(dt)
        if self.bar_tick:
            self.on_bar(now)
        if now >= self.song.duration - 0.2 or (now > 5 and self.audio.song_ended()):
            self.finish_song()

    def spawn_items(self, dt):
        """La IA suelta ítems de ayuda: más seguido si te ve en apuros, menos si juegas muy bien."""
        self.item_timer -= dt
        if self.item_timer > 0 or len(self.items) >= 2:
            return
        m = self.model
        kind = random.choices(["freeze", "slow"], weights=[0.35 + 0.45 * (1 - m.skill), 0.65 - 0.25 * (1 - m.skill)])[0]
        c = self.free_cell((self.player.x, self.player.y), 4)
        if c:
            self.items.append(dict(kind=kind, x=c[0], y=c[1], ttl=14.0))
            self.audio.sfx("item")
        self.item_timer = random.uniform(16.0, 24.0) * m.item_mult * self.cfg.get("item", 1.0)

    def pickup_item(self, it):
        fz, sl = self.cfg.get("fz", (5.0, 8.0))
        self.stats["items"] += 1
        self.add_score(100)
        if it["kind"] == "freeze":
            self.freeze_until = self.song_time + fz
            self.audio.sfx("freeze")
            self.say("¡CONGELADOS!", "Fantasmas congelados.", 1)
        else:
            self.slow_until = self.song_time + sl
            self.audio.sfx("slow")
            self.say("¡LENTOS!", "Fantasmas más lentos.", 1)

    def rhythm_mutation(self):
        """El mapa respira con el ritmo: bombo y caja voltean casillas; cuanto más movida la parte, más."""
        song = self.song
        cap = int(round(song.busy_at(self.song_time) * 5)) + (2 if song.in_surge(self.song_time) else 0)
        if self.flips_bar >= cap:
            return
        ts = song.offset + self.step_i * self.step_t
        hit = max(song.kick_between(ts - self.step_t / 2, ts + self.step_t / 2),
                  0.9 * song.snare_between(ts - self.step_t / 2, ts + self.step_t / 2))
        if hit >= (0.55 if song.in_surge(self.song_time) else 0.75):
            self.mutate_cell()
            self.flips_bar += 1

    def on_bar(self, now):
        self.flips_bar = 0
        rate = self.bar_pts / (4 * self.song.spb)
        self.perf = 0.7 * self.perf + 0.3 * min(1.0, rate / 150.0)
        near = min((wdist((self.player.x, self.player.y), (g.x, g.y), self.wv) for g in self.ghosts), default=8)
        self.model.on_bar(now, 4 * self.song.spb, self.bar_pts, near)
        self.bar_pts = 0.0
        lesson = self.model.lesson(now)
        if lesson:
            self.scene_ai._set("Aprendí: " + lesson[0], lesson[1])
            self.say("", f"Aprendí que {lesson[0]}: {lesson[1]}.", 0)
        shape_now = self.sb if self.transitioning else self.sa
        parts, banner, fxban = [], "", ""
        for act in self.scene_ai.on_bar(self.beat_i, now, shape_now, self.perf):
            k = act[0]
            if k == "map":
                self.apply_map(act[1])
                banner = "MAPA: " + act[1].upper()
                if act[2] != "parte muy movida":
                    parts.append(("¡Drop! " if act[2] == "¡drop!" else "") + f"Nuevo patrón: {act[1]}.")
            elif k == "palette":
                self.pal_target = act[1]
            elif k == "ghosts":
                self.set_family(act[1])
                if act[3] != "parte muy movida":
                    parts.append(f"Fantasmas {act[2]}.")
            elif k == "shape":
                self.start_transition(act[1], act[2])
            elif k == "bg":
                self.set_bg(act[1])
            elif k == "fx":
                self.mult_target = 1.0 if act[1] else 0.0
                if act[1]:
                    self.fx_mode = act[2]
                    fxban = "EFECTO: " + act[2].upper()
            elif k == "note":
                self.say("¡PARTE MUY MOVIDA!", act[1], 1)
        if parts:
            self.say(banner, " ".join(parts), 1)
        elif banner or fxban:
            self.banner = (banner or fxban, 1.2)

    def finish_song(self):
        par = self.song.duration * PAR_RATE * self.cfg["mult"]
        r = self.score / max(1.0, par)
        grade = "S" if r >= 1.0 else "A" if r >= 0.75 else "B" if r >= 0.5 else "C" if r >= 0.3 else "D"
        entry = self.pick[1] if self.pick else None
        newbest = bool(entry) and self.score > entry.get("best", 0)
        if newbest:
            entry["best"], entry["grade"] = self.score, grade
        self.lib["latency"] = self.latency
        pr = self.lib.setdefault("profile", dict(skill=0.5, games=0))      # la IA te recuerda para la próxima partida
        pr["skill"] = round(self.model.skill if pr.get("games", 0) == 0 else 0.6 * pr["skill"] + 0.4 * self.model.skill, 3)
        pr["games"] = pr.get("games", 0) + 1
        save_lib(self.lib)
        self.final = dict(self.stats, score=self.score, grade=grade, title=self.song.title, diff=self.cfg["name"],
                          new=newbest, tron=self.tron, family=self.gfamily, style=self.map_style,
                          skill=int(self.model.skill * 100), adj=int(round((1 / max(0.1, self.model.speed_mult) - 1) * 100)))
        self.audio.stop_song()
        self.audio.sfx("end")
        self.voice.say(f"Canción terminada. Puntuación final: {self.score}. Rango {grade}.", 2)
        self.reset_world(DIFFS[1], builtin_song(), self.tron)
        self.enter_attract("results")

    # ---------- tutorial
    def start_tutorial(self):
        self.reset_world(TUT_CFG, builtin_song(), False, tutorial=True)
        self.state, self.tut_step, self.tut_t = "tutorial", 0, 0.0
        self.audio.play_music("game")
        self.voice.say(TUT_STEPS[0][2], 2)

    def tut_next(self):
        self.tut_step += 1
        s, self.tut_t = self.tut_step, 0.0
        self.audio.sfx("select")
        self.voice.say(TUT_STEPS[s][2], 2)
        if s == 1:
            self.ghosts_on = True
        elif s == 2:                          # demostración: la música cambia mapa, colores y fantasmas
            self.apply_map("circuito")
            self.pal_target = PAL_TRON
            self.set_family("geométricos")
            self.set_bg("ecualizador")
            self.fx_mode, self.mult_target = "mosaico", 1.0
        elif s == 3:
            self.start_transition(CUBE, "demo")
        elif s == 4:
            self.start_transition(PLANE, "demo")
            c = self.free_cell((self.player.x, self.player.y), 3)
            if c:
                self.items.append(dict(kind="freeze", x=c[0], y=c[1], ttl=90.0))

    def update_tutorial(self, dt):
        s, self.tut_t = self.tut_step, self.tut_t + dt
        if s == 2 and self.tut_t > 4.5:
            self.mult_target = 0.0
        if s == 0 and self.stats["dots"] >= 6:
            self.tut_next()
        elif s == 1 and self.stats["caught"] >= 1:
            self.tut_next()
        elif (s == 2 and self.tut_t > 6) or (s == 3 and self.tut_t > 6.5 and not self.transitioning):
            self.tut_next()

    # ---------- flujo de pantallas
    def default_pal(self):
        return PAL_TRON if self.tab == 1 else PAL_GROOVE

    def goto_songs(self):
        self.audio.stop_song()
        self.reset_world(DIFFS[1], builtin_song(), self.tab == 1)
        self.enter_attract("songs")

    def tab_items(self, tab):
        if tab == 0:
            return [dict(title=title_of(e["path"]), entry=e, special=False) for e in self.lib["mi_musica"]]
        return ([dict(title="DEREZZED  —  Daft Punk", entry=self.lib["derezzed"], special=True)]
                + [dict(title=title_of(e["path"]), entry=e, special=False) for e in self.lib["circuito"]])

    def open_item(self):
        items = self.tab_items(self.tab)
        if not items:
            self.msg = ("No hay canciones: pulsa U o arrastra tus .mp3 a la ventana", 4.0)
            return
        it = items[min(self.sel[self.tab], len(items) - 1)]
        if it["entry"] is None:
            self.msg = ("Falta Derezzed: pulsa U y elige el archivo (su nombre debe incluir 'derezzed')", 5.0)
            return
        self.pick = (self.tab, it["entry"], it["special"])
        path = it["entry"]["path"]
        if path in self.cache:
            self.goto_difficulty(self.cache[path])
        elif not self.audio.ok:
            self.msg = ("El audio no está disponible en este equipo", 4.0)
        else:
            self.job, self.state = Job(path), "analyzing"

    def goto_difficulty(self, song):
        self.cur_song = song
        self.state = "difficulty"

    def start_play(self):
        tab, entry, special = self.pick
        self.reset_world(DIFFS[self.sel_diff], self.cur_song, tab == 1, special=special)
        self.state, self.cd_t, self.cd_last = "countdown", 3.99, 4
        self.audio.play_music(None)
        self.audio.sfx("select")

    def begin_song(self):
        if not self.audio.play_song(self.song.path):
            self.msg = ("No se pudo reproducir el mp3", 4.0)
            self.goto_songs()
            return
        self.state = "play"
        self.song_time = self.latency
        bt = (self.song_time - self.song.offset) / self.song.spb
        self.beat_total, self.beat_i, self.step_i = bt, math.floor(bt), math.floor(bt * self.sub)
        self.rings, self.onset_i = [], 0
        self.audio.sfx("go")
        self.say("¡YA!", "Comienza la canción.", 2)

    # ---------- explorador de archivos (funciona en pantalla completa)
    def br_open(self):
        start = os.path.expanduser("~/Music")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        self.br_load(start)
        self.state = "browser"

    def br_load(self, path):
        self.br_path, items = path, []
        if path == "":
            items = [(f"{d}:\\", True) for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
        else:
            items.append(("..", True))
            try:
                names = sorted(os.listdir(path), key=str.lower)
            except Exception:
                names = []
            items += [(n, True) for n in names if not n.startswith(".") and os.path.isdir(os.path.join(path, n))]
            items += [(n, False) for n in names if n.lower().endswith(".mp3") and os.path.isfile(os.path.join(path, n))]
        self.br_items, self.br_sel, self.br_scroll = items, 0, 0

    def br_up(self):
        if self.br_path == "":
            return
        parent = os.path.dirname(self.br_path)
        if parent == self.br_path:
            parent = "" if os.name == "nt" else parent
        self.br_load(parent)

    def br_enter(self):
        if not self.br_items:
            return
        name, isdir = self.br_items[self.br_sel]
        if isdir:
            if name == "..":
                self.br_up()
            else:
                self.br_load(name if self.br_path == "" else os.path.join(self.br_path, name))
        else:
            self.msg = (add_song(self.lib, self.tab, os.path.join(self.br_path, name)), 3.0)

    def br_add_all(self):
        n = sum(1 for name, d in self.br_items if not d and add_song(self.lib, self.tab, os.path.join(self.br_path, name)) == "Canción añadida")
        self.msg = (f"{n} canciones añadidas", 3.0)

    def on_drop(self, path):
        if self.state in ("songs", "browser"):
            self.msg = (add_song(self.lib, self.tab, path), 4.0)

    # ---------- actualización
    def update(self, dt):
        st = self.state
        if st == "pause":
            return
        if st == "analyzing":
            j = self.job
            if j.error:
                self.msg, self.state = ("No se pudo analizar: " + j.error, 5.0), "songs"
            elif j.song:
                self.cache[j.song.path] = j.song
                self.goto_difficulty(j.song)
            self.update_common(dt)
            return
        self.update_common(dt)
        if st in ("intro", "songs", "browser", "difficulty", "results"):
            if self.step_tick:
                self.ghost_acc += 1.0 / self.sub
                if self.ghost_acc >= 1.0:
                    self.ghost_acc -= 1.0
                    self.move_ghosts(dict(speed=1, aware=7, error=0.1, danger=3))
        elif st == "countdown":
            self.cd_t -= dt
            n = int(self.cd_t) + 1
            if n != self.cd_last and self.cd_t > 0:
                self.cd_last = n
                self.audio.sfx("beep")
            if self.cd_t <= 0:
                self.begin_song()
        elif st in ("play", "tutorial"):
            self.update_play(dt)
            if st == "tutorial":
                self.update_tutorial(dt)
        self.audio.set_volume(dt, self.voice.speaking)

    def update_common(self, dt):
        self.t += dt
        song = self.song
        if self.state == "play" and not song.builtin:
            pos = self.audio.song_pos()
            self.song_time = pos + self.latency if pos >= 0 else self.song_time + dt
        else:
            self.song_time += dt
        bt = (self.song_time - song.offset) / song.spb
        pb, ps = self.beat_i, self.step_i
        self.beat_total, self.beat_i, self.step_i = bt, math.floor(bt), math.floor(bt * self.sub)
        self.beat_tick, self.step_tick = self.beat_i > pb, self.step_i > ps
        self.bar_tick = self.beat_tick and self.beat_i % 4 == 0
        self.pulse = (1 - (bt % 1.0)) ** 3
        # anillos de fondo: uno por cada golpe fuerte de la canción
        if song.builtin:
            if self.beat_tick:
                self.rings.append((self.song_time, 0.8))
        else:
            while self.onset_i < len(song.onsets) and song.onsets[self.onset_i][0] <= self.song_time:
                if song.onsets[self.onset_i][1] > 0.45 and self.state == "play":
                    self.rings.append(song.onsets[self.onset_i])
                self.onset_i += 1
        self.rings = [r for r in self.rings if self.song_time - r[0] < 1.4][-14:]
        # los colores los decide la IA y se funden suavemente
        if self.state == "songs":
            self.pal_target = self.default_pal()
        self.pal = lerp_pal(self.pal, self.pal_target, min(1.0, dt * 2.0))
        k = min(1.0, dt * 18)
        for e in [self.player] + self.ghosts:
            if abs(e.x - e.vx) > GW / 2 or abs(e.y - e.vy) > GH / 2:
                e.vx, e.vy = float(e.x), float(e.y)
            else:
                e.vx += (e.x - e.vx) * k
                e.vy += (e.y - e.vy) * k
        for g in self.ghosts:
            g.fear += ((1.0 if g.mode == "flee" else 0.0) - g.fear) * min(1.0, dt * 6)
        for c in list(self.flash):
            self.flash[c] -= dt
            if self.flash[c] <= 0:
                del self.flash[c]
        for c in list(self.changing):
            self.changing[c] -= dt
            if self.changing[c] <= 0:
                del self.changing[c]
                self.flash[c] = 0.9
        for y in range(GH):
            for x in range(GW):
                if (x, y) not in self.changing:
                    tgt, h = float(self.walls[y][x]), self.hw[y][x]
                    self.hw[y][x] = tgt if abs(tgt - h) < 0.01 else h + (tgt - h) * min(1.0, dt * 7)
        self.wave_t += dt
        for name in ("banner", "caption", "msg"):
            txt, left = getattr(self, name)
            setattr(self, name, (txt, max(0.0, left - dt)))
        if self.transitioning:
            self.trans_t += dt
            s = min(1.0, self.trans_t / self.trans_dur)
            self.m = s * s * (3 - 2 * s)
            if s >= 1.0:
                self.sa, self.m, self.transitioning = self.sb, 0.0, False
        if self.transitioning:
            pw = (1 - self.m if self.sa == PLANE else 0) + (self.m if self.sb == PLANE and self.sa != PLANE else 0)
        else:
            pw = 1.0 if self.sa == PLANE else 0.0
        self.elev = 1.0 - pw
        self.shake *= math.exp(-dt * 5)
        self.update_fx(dt)
        self.focal = self.focal0 * (1 + (0.025 + 0.04 * self.fx_level) * self.pulse)
        self.update_camera(dt)

    def update_camera(self, dt):
        if self.state in ATTRACT:
            self.M = mmul(Rx(0.5), Ry(self.t * 0.25))
            self.pan = (-2.6 if self.state == "intro" else 0.0, 0.0)
            return
        pl = self.player
        qt = quat_from_mat(frame_at(self.sa, self.sb, self.m, pl.vx + 0.5, pl.vy + 0.5))
        self.cam_q = qt if self.cam_q is None else qnlerp(self.cam_q, qt, min(1.0, dt * 7))
        self.yaw_kick *= math.exp(-dt * 2.2)
        self.roll_kick *= math.exp(-dt * 2.0)
        self.roll = self.roll_kick + self.elev * 0.10 * math.sin(self.t * 0.5) + self.fx_level * 0.05 * math.sin(self.beat_total * math.pi * 0.5)
        self.M = mmul(Rz(self.roll), mmul(Rx(0.35 * self.elev), mmul(Ry(self.yaw_kick * self.elev), mat_from_quat(self.cam_q))))
        pp, _ = blend_surf(self.sa, self.sb, self.m, pl.vx + 0.5, pl.vy + 0.5)
        q = mvec(self.M, pp)
        self.pan = (q[0] * self.elev * 0.8, q[1] * self.elev * 0.8)

    # ---------- proyección
    def project(self, q):
        d = max(0.6, CAM_D + q[2])
        s = self.focal / d
        return (self.RCX + (q[0] - self.pan[0]) * s, self.RCY + (q[1] - self.pan[1]) * s), s

    def place(self, vx, vy, lift=0.0):
        p, n = blend_surf(self.sa, self.sb, self.m, vx + 0.5, vy + 0.5)
        q, nq = mvec(self.M, p), mvec(self.M, n)
        front = -nq[0] * q[0] - nq[1] * q[1] + nq[2] * (-CAM_D - q[2]) > 0
        pt, s = self.project((q[0] + nq[0] * lift, q[1] + nq[1] * lift, q[2] + nq[2] * lift))
        return pt, s, front, q

    # ---------- dibujo del mundo
    # ======================= FONDOS, MULTIPLICAR LA ESCENA Y EFECTOS =======================
    def reset_fx(self):
        self.bg_style = "túnel" if (self.song.builtin or self.tutorial) else self.scene_ai.initial_bg()
        self.bg_prev, self.bg_mix = self.bg_style, 1.0
        self.fx_mode, self.mult_target, self.mult_amt, self.mult_vel = "eco", 0.0, 0.0, 0.0
        self.fb = None
        self.stars = [[random.uniform(0, 6.283), random.uniform(0, 1), random.uniform(0.4, 1.6)] for _ in range(140)]
        self.flash_fx, self.fx_level, self.surge = 0.0, 0.0, False
        self.diff_fx = self.cfg.get("fx", 0.7)
        self.opacity, self.panel_alpha = 0.7, 190

    def set_bg(self, style):
        if style != self.bg_style:
            self.bg_prev, self.bg_style, self.bg_mix = self.bg_style, style, 0.0

    def adapt_quality(self, dt):
        """Si el equipo va justo, baja solos los efectos más caros (y los sube si sobra potencia)."""
        self.fps_acc += dt
        self.fps_n += 1
        if self.fps_n >= 90:
            avg = self.fps_acc / self.fps_n
            self.fps_acc, self.fps_n = 0.0, 0
            if avg > 1 / 38 and self.quality > 0:
                self.quality -= 1
                self.up = 0
                self.set_render_scale()
            elif avg < 1 / 52 and self.quality < 2:
                self.up += 1
                if self.up >= 2:
                    self.quality, self.up = self.quality + 1, 0
                    self.set_render_scale()
            else:
                self.up = 0

    def update_fx(self, dt):
        song = self.song
        if self.state in PLAYING and not song.builtin:
            self.surge = song.in_surge(self.song_time)
            fx = self.diff_fx * (0.5 + 0.5 * song.busy_at(self.song_time)) + (0.2 if self.surge else 0.0)
        elif self.state in PLAYING:
            self.surge, fx = False, self.diff_fx * 0.7
        else:
            self.surge, fx = False, 0.35
        if self.soft:
            fx *= 0.35
        self.fx_level += (min(1.2, fx) - self.fx_level) * min(1.0, dt * 2.0)
        if self.state == "play" and self.step_tick and not song.builtin and not self.soft:
            ts = song.offset + self.step_i * self.step_t
            hit = max(song.kick_between(ts - self.step_t / 2, ts + self.step_t / 2),
                      0.8 * song.snare_between(ts - self.step_t / 2, ts + self.step_t / 2))
            if hit > 0.75:
                self.flash_fx = min(1.0, self.flash_fx + 0.6 * self.fx_level)
        # transparencia del mundo: FÁCIL/NORMAL fijas (poco transparentes); DIFÍCIL sube y baja según cómo juegues
        base = self.cfg.get("opa")
        if base is None:
            base = 0.82 - 0.55 * self.model.skill + 0.18 * math.sin(self.beat_total * math.pi / 24) - (0.10 if self.surge else 0.0)
        base = max(0.15, min(0.92, base))
        if self.soft:
            base = max(base, 0.75)
        if self.state not in PLAYING:
            base = 0.7
        self.opacity += (base - self.opacity) * min(1.0, dt * 1.5)
        self.panel_alpha = int(120 + 130 * self.opacity) if self.state in PLAYING else 190
        self.flash_fx *= math.exp(-dt * 7)
        if self.bg_mix < 1.0:
            self.bg_mix = min(1.0, self.bg_mix + dt / 1.2)
        if self.state == "songs" and self.bg_mix >= 1.0:
            want = "horizonte" if self.tab == 1 else "túnel"
            if want != self.bg_style:
                self.set_bg(want)
        # "multiplicar": muelle amortiguado -> entra y sale suave, sin saltos
        acc = (self.mult_target - self.mult_amt) * 28.0 - self.mult_vel * 9.0
        self.mult_vel += acc * dt
        self.mult_amt = max(0.0, min(1.0, self.mult_amt + self.mult_vel * dt))
        en = 0.5 if song.builtin else song.energy_at(self.song_time)
        bass = song.band_at(0, self.song_time)
        for st_ in self.stars:
            st_[1] += st_[2] * dt * (0.12 + 0.9 * en + 1.4 * bass)
            if st_[1] > 1.25:
                st_[0], st_[1] = random.uniform(0, 6.283), 0.02

    def bg_draw(self, style, s, k):
        """Fondo animado al ritmo de la canción (espectro, graves, beats) en la superficie s."""
        if k <= 0.01:
            return
        w, h = s.get_size()
        cx, cy, m = w // 2, h // 2, min(w, h)
        pal, song, t, bt = self.pal, self.song, self.song_time, self.beat_total
        bass, mid, high = song.band_at(0, t), song.band_at(1, t), song.band_at(2, t)
        lw = max(1, int(self.rK * 1.2))
        cos, sin = math.cos, math.sin
        if style == "túnel":
            n, sides = 11, 6
            for i in range(n):
                ph = (bt * 0.22 + i / n) % 1.0
                r = (0.03 + ph ** 1.7 * 0.95) * m * 1.1
                rot = bt * 0.04 * (1 + mid) + i * 0.15
                pts = chaikin([(cx + r * cos(rot + 2 * math.pi * j / sides), cy + r * sin(rot + 2 * math.pi * j / sides)) for j in range(sides)], 2)
                c = pal["line"] if i % 2 == 0 else pal["line2"]
                pygame.draw.polygon(s, shade(c, ((1 - ph) * 0.55 + 0.35 * bass * (1 - ph)) * k), pts, lw * (1 + int(ph * 2)))
        elif style == "horizonte":
            hy = int(h * 0.50)
            sr = int(m * (0.16 + 0.04 * bass))
            pygame.draw.circle(s, shade(pal["line2"], 0.55 * k), (cx, hy - int(sr * 0.2)), sr)
            for i in range(-14, 15):
                pygame.draw.line(s, shade(pal["line"], 0.45 * k), (cx + i * w * 0.012, hy), (cx + i * w * 0.16, h), lw)
            for j in range(12):
                z = (bt * 0.5 + j / 12) % 1.0
                y = hy + int(z ** 2.2 * (h - hy))
                pygame.draw.line(s, shade(pal["line"], (0.2 + 0.6 * z) * k * (0.7 + 0.5 * bass)), (0, y), (w, y), lw)
            pygame.draw.line(s, shade(pal["line2"], 0.8 * k), (0, hy), (w, hy), lw)
        elif style == "ecualizador":
            spec = song.spec_at(t)
            r0, n = m * (0.16 + 0.05 * bass), 64
            for j in range(n):
                v = float(spec[j if j < 32 else 63 - j])
                a = 2 * math.pi * j / n + bt * 0.05
                r1 = r0 + m * (0.05 + 0.42 * v)
                c = pal["line"] if j % 2 == 0 else pal["line2"]
                pygame.draw.line(s, shade(c, (0.35 + 0.65 * v) * k), (cx + r0 * cos(a), cy + r0 * sin(a)),
                                 (cx + r1 * cos(a), cy + r1 * sin(a)), max(2, lw * 2))
            pygame.draw.circle(s, shade(pal["line"], 0.6 * k), (cx, cy), int(r0), lw)
        elif style == "estrellas":
            for a, r, sp in self.stars:
                r1 = max(0.0, r - 0.03 * (0.5 + sp + bass * 2))
                f = min(1.0, r * 1.5)
                pygame.draw.line(s, shade(pal["line"] if sp < 1.0 else pal["line2"], (0.3 + 0.7 * f) * k),
                                 (cx + cos(a) * r1 * w * 0.75, cy + sin(a) * r1 * h * 0.75),
                                 (cx + cos(a) * r * w * 0.75, cy + sin(a) * r * h * 0.75), max(1, int(lw * (0.6 + f * 2))))
        else:                                                                   # ondas
            for layer in range(3):
                amp = m * (0.06 + 0.18 * (bass, mid, high)[layer])
                fq = (0.012, 0.02, 0.032)[layer]
                pts = [(x, cy + (layer - 1) * m * 0.22 + amp * sin(x * fq + bt * (1.5 + layer) + layer * 2)) for x in range(0, w + 8, 8)]
                pygame.draw.lines(s, shade(pal["line2"] if layer == 1 else pal["line"], (0.45 + 0.3 * (layer == 1)) * k), False, pts, lw * 2)
        for t0, sv in self.rings:                                               # un anillo por golpe fuerte
            age = t - t0
            pygame.draw.circle(s, shade(pal["ring"], 0.9 * k * max(0.0, 1 - age / 1.4) * sv), (cx, cy), int((50 + age * 520) * self.rK * 0.5), lw)

    def render_bg(self):
        A = self.bgA
        A.fill((0, 0, 0))
        gain = 0.8 if self.state in PLAYING else 0.7
        if self.bg_mix < 1.0:
            self.bg_draw(self.bg_prev, A, (1 - self.bg_mix) * gain)
            self.bgB.fill((0, 0, 0))
            self.bg_draw(self.bg_style, self.bgB, self.bg_mix * gain)
            A.blit(self.bgB, (0, 0), special_flags=pygame.BLEND_RGB_ADD)
        else:
            self.bg_draw(self.bg_style, A, gain)
        if self.quality >= 1:
            A.blit(self.vig, (0, 0), special_flags=pygame.BLEND_RGB_MULT)
        up = pygame.transform.smoothscale if self.quality >= 1 else pygame.transform.scale
        return up(A, (self.RW, self.RH), self.bgFull)

    def draw_multiply(self, out, sc):
        """La propia partida se MULTIPLICA y pasa a ser parte del fondo; al calmarse vuelve a una sola."""
        a = self.mult_amt
        if a < 0.01:
            self.fb = None
            return
        a = a * a * (3 - 2 * a)
        W, H, pal, bt = self.RW, self.RH, self.pal, self.beat_total
        mode = self.fx_mode if self.quality >= 1 or self.fx_mode != "eco" else "mosaico"
        tint = mix((255, 255, 255), pal["line2"], 0.45)
        if mode == "eco":                                   # túnel de copias que giran y se alejan
            if self.fb is None:
                self.fb = pygame.Surface((self.RW2, self.RH2))
                self.fb.fill((0, 0, 0))
            tmp = pygame.transform.rotozoom(self.fb, a * 1.4 * math.sin(bt * 0.5), 1.0 + (0.03 + 0.035 * self.pulse) * a)
            lay = pygame.Surface((self.RW2, self.RH2))
            lay.blit(tmp, ((self.RW2 - tmp.get_width()) // 2, (self.RH2 - tmp.get_height()) // 2))
            f = 0.66 * a
            lay.fill((int(tint[0] * f), int(tint[1] * f), int(tint[2] * f)), special_flags=pygame.BLEND_RGB_MULT)
            out.blit(pygame.transform.scale(lay, (W, H)), (0, 0), special_flags=pygame.BLEND_RGB_ADD)
            return
        if mode == "mosaico":                               # 8 copias que salen del centro a su casilla
            small = pygame.transform.smoothscale(sc, (W // 3, H // 3))
            cols = [pal["line"], pal["line2"], pal["wall"]]
            i = 0
            for gy in range(3):
                for gx in range(3):
                    if gx == 1 and gy == 1:
                        continue
                    px = W // 3 + (gx * (W // 3) - W // 3) * a + math.sin(bt * math.pi + i) * 6 * a
                    py = H // 3 + (gy * (H // 3) - H // 3) * a
                    c = mix((255, 255, 255), cols[i % 3], 0.55)
                    cp = small.copy()
                    cp.fill((int(c[0] * 0.75 * a), int(c[1] * 0.75 * a), int(c[2] * 0.75 * a)), special_flags=pygame.BLEND_RGB_MULT)
                    out.blit(cp, (px, py), special_flags=pygame.BLEND_RGB_ADD)
                    i += 1
        elif mode == "espejo":                              # caleidoscopio: 4 reflejos del centro
            cw, ch = W // 2, H // 2
            ox, oy = int(W // 4 + math.sin(bt * 0.25) * W * 0.03 * a), int(H // 4 + math.cos(bt * 0.2) * H * 0.03 * a)
            c = sc.subsurface((ox, oy, cw, ch))
            for img, pos in ((c, (0, 0)), (pygame.transform.flip(c, True, False), (cw, 0)),
                             (pygame.transform.flip(c, False, True), (0, ch)), (pygame.transform.flip(c, True, True), (cw, ch))):
                t_ = img.copy()
                t_.fill((int(tint[0] * 0.7 * a), int(tint[1] * 0.7 * a), int(tint[2] * 0.7 * a)), special_flags=pygame.BLEND_RGB_MULT)
                out.blit(t_, pos, special_flags=pygame.BLEND_RGB_ADD)
        else:                                               # órbita: 6 copias girando alrededor
            small = pygame.transform.smoothscale(sc, (W // 3, H // 3))
            for i in range(6):
                ang = bt * 0.35 + i * math.pi / 3
                rad = a * min(W, H) * 0.34
                img = pygame.transform.rotozoom(small, math.degrees(ang) * 0.5 * a, (0.5 + 0.25 * math.sin(bt * 0.5 + i)) * (0.5 + 0.5 * a))
                img.fill((int(tint[0] * 0.8 * a), int(tint[1] * 0.8 * a), int(tint[2] * 0.8 * a)), special_flags=pygame.BLEND_RGB_MULT)
                out.blit(img, (W / 2 + math.cos(ang) * rad * 1.55 - img.get_width() / 2, H / 2 + math.sin(ang) * rad - img.get_height() / 2),
                         special_flags=pygame.BLEND_RGB_ADD)

    def post_fx(self, out):
        """Efectos de pantalla: más y más fuertes en DIFÍCIL y en las partes movidas (F4 los suaviza)."""
        f, W, H, pal = self.fx_level, self.RW, self.RH, self.pal
        if self.soft:
            return
        if f > 0.45:                                        # bandas de color que barren la pantalla
            hgt = max(2, int(H * 0.10))
            for i in range(3):
                y = int(((self.beat_total * 0.18 + i / 3) % 1.0) * H)
                band = pygame.Surface((W, hgt))
                band.fill(shade(pal["line2"] if i % 2 else pal["line"], 0.10 * f))
                out.blit(band, (0, y), special_flags=pygame.BLEND_RGB_ADD)
        if self.flash_fx > 0.02:                            # destello de color en los golpes fuertes
            out.fill(shade(mix(pal["line2"], (255, 255, 255), 0.3), 0.18 * self.flash_fx), special_flags=pygame.BLEND_RGB_ADD)
        if f > 0.7 and self.quality >= 2:                   # ondulación horizontal
            tmp = out.copy()
            amp = int(self.rK * 7 * (f - 0.6) * (0.4 + self.pulse))
            n = 36
            sh = H // n
            for i in range(n):
                out.blit(tmp, (int(math.sin(self.beat_total * 2.0 + i * 0.5) * amp), i * sh), (0, i * sh, W, sh))
        if f > 0.85 and self.quality >= 2 and self.pulse > 0.3:      # aberración cromática en el golpe
            d = max(1, int(self.rK * (3 + 5 * self.pulse) * (f - 0.7) * 2))
            r, b = out.copy(), out.copy()
            r.fill((255, 0, 0), special_flags=pygame.BLEND_RGB_MULT)
            b.fill((0, 0, 255), special_flags=pygame.BLEND_RGB_MULT)
            out.fill((0, 255, 0), special_flags=pygame.BLEND_RGB_MULT)
            out.blit(r, (-d, 0), special_flags=pygame.BLEND_RGB_ADD)
            out.blit(b, (d, 0), special_flags=pygame.BLEND_RGB_ADD)

    def draw_world(self, sc):
        pal, M, song = self.pal, self.M, self.song
        bass = song.bass_at(self.song_time)
        en = 0.5 if song.builtin else song.energy_at(self.song_time)
        V = [[(mvec(M, p), mvec(M, n)) for p, n in (blend_surf(self.sa, self.sb, self.m, ui, vi) for ui in range(GW + 1))]
             for vi in range(GH + 1)]
        hh = WALL_H * self.elev * (1 + 0.6 * bass)
        show_dots = self.state in PLAYING
        bk, bkon = self.backing, self.opacity > 0.05                       # capa opaca detrás del tablero
        if bkon:
            bk.fill((0, 0, 0))
            bcol = tuple(max(3, c) for c in shade(pal["floor"], 0.35))
        items = []
        for y in range(GH):
            for x in range(GW):
                cs = [V[y][x], V[y][x + 1], V[y + 1][x + 1], V[y + 1][x]]
                z = sum(c[0][2] for c in cs) / 4
                cen = [sum(c[0][i] for c in cs) / 4 for i in range(3)]
                nav = [sum(c[1][i] for c in cs) for i in range(3)]
                front = -nav[0] * cen[0] - nav[1] * cen[1] + nav[2] * (-CAM_D - cen[2]) > 0
                base = [self.project(c[0])[0] for c in cs]
                hw, rt = self.hw[y][x], None
                if hw > 0.04:
                    sk, hgt = 0.55 + 0.45 * hw, hh * hw
                    rt = chaikin([self.project((cen[0] + (c[0][0] - cen[0]) * sk + c[1][0] * hgt,
                                                cen[1] + (c[0][1] - cen[1]) * sk + c[1][1] * hgt,
                                                cen[2] + (c[0][2] - cen[2]) * sk + c[1][2] * hgt))[0] for c in cs], 2, 0.15)
                rb = chaikin(inset(base, 0.95), 2, 0.15)                      # casillas redondeadas
                items.append((z, 0, (x, y, rb, rt, front, hw)))
                if show_dots and self.dots[y][x] and front:
                    pt, s = self.project((cen[0] + nav[0] * 0.02, cen[1] + nav[1] * 0.02, cen[2] + nav[2] * 0.02))
                    items.append((z - 0.01, 1, (pt, max(1, int(0.045 * s * (1 + 0.6 * self.pulse))))))
        items.sort(key=lambda i: -i[0])
        for z, kind, d in items:
            if kind == 1:
                pygame.draw.circle(sc, pal["dot"], (int(d[0][0]), int(d[0][1])), d[1])
                continue
            x, y, rb, rt, front, hw = d
            if front:
                fl = self.flash.get((x, y), 0)
                fill = shade(pal["floor"], 0.8 + 0.8 * bass + 0.5 * en)
                if fl:
                    fill = mix(fill, (255, 255, 255), min(1, fl))
                pygame.draw.polygon(sc, fill, rb)
                if bkon:
                    pygame.draw.polygon(bk, bcol, rb)
                pygame.draw.polygon(sc, shade(pal["line"], 0.28 + 0.30 * self.pulse + 0.30 * en), rb, self.w1)
                if rt:
                    kb = 0.4 + 0.6 * hw
                    hull = convex_hull(rb + rt)                         # cuerpo del muro: blando, sin aristas
                    if len(hull) >= 3:
                        pygame.draw.polygon(sc, shade(pal["wall"], 0.10 * kb), hull)
                        if bkon:
                            pygame.draw.polygon(bk, bcol, hull)
                        pygame.draw.polygon(sc, shade(pal["line"], 0.45 * kb), hull, self.w1)
                    pygame.draw.polygon(sc, shade(pal["wall"], 0.16 * kb), rt)
                    if bkon:
                        pygame.draw.polygon(bk, bcol, rt)
                    pygame.draw.polygon(sc, shade(pal["wall"], (0.7 + 0.3 * self.pulse) * kb), rt, self.w2)
            else:
                pygame.draw.polygon(sc, shade(pal["line"], 0.15), rt if rt else rb, self.w1)
        if self.wave_t < 1.0 and self.state in PLAYING:
            k = 1 - self.wave_t
            pygame.draw.circle(sc, shade(pal["line2"], k), (int(self.player_pt[0]), int(self.player_pt[1])),
                               int(self.wave_t * 700 * self.rK), max(1, int(5 * k * self.rK)))
        if self.show_paths:
            for g in self.ghosts:
                pts = [self.place(g.x, g.y, 0.08)] + [self.place(c[0], c[1], 0.08) for c in g.path]
                for p0, p1 in zip(pts, pts[1:]):
                    if p0[2] and p1[2] and abs(p0[0][0] - p1[0][0]) < 300 * self.rK:
                        pygame.draw.line(sc, shade(pal["ghosts"][g.idx % 5], 0.8), p0[0], p1[0], self.w2)

    def ghost_shape(self, tr, kind, cx, cy, r, col, front):
        lw = self.w2
        fill = shade(col, 0.5)
        if kind == "classic":
            pygame.draw.circle(tr, col, (cx, cy - int(r * 0.1)), r)
            pygame.draw.rect(tr, col, (cx - r, cy - int(r * 0.1), 2 * r, int(r * 0.95)), border_radius=int(r * 0.45))
        elif kind == "orb":
            pygame.draw.circle(tr, fill, (cx, cy), r)
            pygame.draw.circle(tr, col, (cx, cy), r, lw)
            pygame.draw.circle(tr, col, (cx, cy), int(r * 0.4))
        elif kind == "square":
            pygame.draw.rect(tr, fill, (cx - r, cy - r, 2 * r, 2 * r), border_radius=int(r * 0.55))
            pygame.draw.rect(tr, col, (cx - r, cy - r, 2 * r, 2 * r), lw, border_radius=int(r * 0.55))
        else:
            if kind == "hex":
                pts = [(cx + r * math.cos(k * math.pi / 3), cy + r * math.sin(k * math.pi / 3)) for k in range(6)]
            elif kind == "diamond":
                pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
            elif kind == "tri":
                pts = [(cx, cy - r), (cx + r, cy + r * 0.8), (cx - r, cy + r * 0.8)]
            else:                                                           # estrella
                pts = [(cx + (r if k % 2 == 0 else r * 0.45) * math.cos(k * math.pi / 4 - math.pi / 2),
                        cy + (r if k % 2 == 0 else r * 0.45) * math.sin(k * math.pi / 4 - math.pi / 2)) for k in range(8)]
            pts = chaikin(pts, 2, 0.2)                                      # esquinas redondeadas
            pygame.draw.polygon(tr, fill, pts)
            pygame.draw.polygon(tr, col, pts, lw)
        if front and kind in ("classic", "square", "hex", "star", "diamond"):
            for dx in (-r // 3, r // 3):
                pygame.draw.circle(tr, (255, 255, 255), (cx + dx, cy - r // 4), max(2, r // 5))

    def draw_entity(self, tr, e, pac=False):
        pal = self.pal
        pt, s, front, q = self.place(e.vx, e.vy, 0.3 * self.elev)
        cx, cy = int(pt[0]), int(pt[1])
        if pac:
            col = shade(pal["player"], 1.0 if front else 0.22)
            r = max(4, int(0.30 * s))
            self.player_pt = (cx, cy)
            p0 = self.place(e.vx, e.vy)[0]
            p1 = self.place(e.vx + e.dir[0] * 0.3, e.vy + e.dir[1] * 0.3)[0]
            ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
            if self.tron:                                                   # disco de identidad
                pygame.draw.circle(tr, shade(col, 0.45), (cx, cy), r)
                pygame.draw.circle(tr, shade(pal["line"], 1.0 if front else 0.22), (cx, cy), r, self.w2 + 1)
                pygame.draw.circle(tr, col, (cx, cy), int(r * 0.5), self.w2)
                pygame.draw.line(tr, col, (cx, cy), (cx + math.cos(ang) * r, cy + math.sin(ang) * r), self.w2 + 1)
            else:
                pygame.draw.circle(tr, col, (cx, cy), r)
                m = 0.12 + 0.45 * abs(math.sin(self.beat_total * math.pi * 2))
                pts = [(cx, cy)] + [(cx + math.cos(ang - m + 2 * m * i / 8) * r * 1.2,
                                     cy + math.sin(ang - m + 2 * m * i / 8) * r * 1.2) for i in range(9)]
                pygame.draw.polygon(tr, (0, 0, 0), pts)
            return
        kind = FAMILIES[self.gfamily][e.band]
        frozen, slowed = self.song_time < self.freeze_until, self.song_time < self.slow_until
        base = mix(pal["ghosts"][e.idx % 5], (140, 180, 255), 0.5 * e.fear)
        if frozen:
            base = mix(base, (190, 240, 255), 0.8)
        elif slowed:
            base = mix(base, (190, 150, 255), 0.45)
        col = shade(base, 1.0 if front else 0.22)
        size = 1.0 + (0.0 if frozen else 0.45 * self.song.band_at(e.band, self.song_time))   # late con su banda
        r = max(4, int(0.30 * s * size))
        self.ghost_shape(tr, kind, cx, cy, r, col, front)
        if frozen and front:                                                   # hielo: aro y cristales
            pygame.draw.circle(tr, (230, 250, 255), (cx, cy), int(r * 1.45), self.w2)
            for k in range(6):
                a = k * math.pi / 3 + 0.3
                pygame.draw.line(tr, (255, 255, 255), (cx + math.cos(a) * r * 1.2, cy + math.sin(a) * r * 1.2),
                                 (cx + math.cos(a) * r * 1.7, cy + math.sin(a) * r * 1.7), self.w2)
        elif slowed and front:                                                 # reloj: onda lenta
            pygame.draw.circle(tr, (190, 150, 255), (cx, cy), int(r * (1.25 + 0.3 * ((self.beat_total * 0.5) % 1.0))), self.w1)

    def draw_items(self, tr):
        for it in self.items:
            if it["ttl"] < 3 and int(self.t * 6) % 2:
                continue
            pt, s, front, _ = self.place(it["x"], it["y"], 0.3 * self.elev)
            if not front:
                continue
            cx, cy = int(pt[0]), int(pt[1])
            r, lw = max(6, int(0.34 * s * (1 + 0.12 * self.pulse))), self.w2
            if it["kind"] == "freeze":                                          # copo de nieve
                col = (200, 240, 255)
                pygame.draw.circle(tr, shade(col, 0.35), (cx, cy), r)
                pygame.draw.circle(tr, col, (cx, cy), r, lw)
                for k in range(3):
                    a = k * math.pi / 3 + self.t * 0.5
                    dx, dy = math.cos(a) * r * 0.85, math.sin(a) * r * 0.85
                    pygame.draw.line(tr, (255, 255, 255), (cx - dx, cy - dy), (cx + dx, cy + dy), lw)
                    for sg in (-1, 1):
                        ex, ey, pa = cx + sg * dx * 0.6, cy + sg * dy * 0.6, a + math.pi / 2
                        pygame.draw.line(tr, col, (ex - math.cos(pa) * r * 0.22, ey - math.sin(pa) * r * 0.22),
                                         (ex + math.cos(pa) * r * 0.22, ey + math.sin(pa) * r * 0.22), max(1, lw // 2))
            else:                                                                # reloj
                col = (190, 150, 255)
                pygame.draw.circle(tr, shade(col, 0.35), (cx, cy), r)
                pygame.draw.circle(tr, col, (cx, cy), r, lw)
                a = self.t * 2.0
                pygame.draw.line(tr, (255, 255, 255), (cx, cy), (cx + math.cos(a - math.pi / 2) * r * 0.75, cy + math.sin(a - math.pi / 2) * r * 0.75), lw)
                pygame.draw.line(tr, col, (cx, cy), (cx + math.cos(a * 0.12 - math.pi / 2) * r * 0.5, cy + math.sin(a * 0.12 - math.pi / 2) * r * 0.5), lw + 1)

    # ---------- interfaz (se dibuja en unidades de diseño y se escala a la pantalla)
    def sx(self, v):
        return int(round(v * self.K))

    def R(self, r):
        return pygame.Rect(self.sx(r[0]), self.sx(r[1]), max(1, self.sx(r[2])), max(1, self.sx(r[3])))

    def txt(self, font, s, color, pos, anchor="topleft"):
        img = font.render(s, True, color)
        self.screen.blit(img, img.get_rect(**{anchor: (self.sx(pos[0]), self.sx(pos[1]))}))

    def tw(self, font, s):
        return font.size(s)[0] / self.K

    def fit(self, font, s, w):
        if self.tw(font, s) <= w:
            return s
        while len(s) > 3 and self.tw(font, s + "…") > w:
            s = s[:-1]
        return s + "…"

    def wrap(self, font, text, width):
        lines, cur = [], ""
        for wd in text.split():
            t = (cur + " " + wd).strip()
            if self.tw(font, t) <= width:
                cur = t
            else:
                lines.append(cur)
                cur = wd
        return lines + ([cur] if cur else [])

    def drect(self, color, rect, width=0, radius=0):
        pygame.draw.rect(self.screen, color, self.R(rect), max(1, self.sx(width)) if width else 0, border_radius=self.sx(radius))

    def dcirc(self, color, c, r, width=0):
        pygame.draw.circle(self.screen, color, (self.sx(c[0]), self.sx(c[1])), max(1, self.sx(r)), max(1, self.sx(width)) if width else 0)

    def dline(self, color, a, b, w=1):
        pygame.draw.line(self.screen, color, (self.sx(a[0]), self.sx(a[1])), (self.sx(b[0]), self.sx(b[1])), max(1, self.sx(w)))

    def dpoly(self, color, pts, width=0):
        pygame.draw.polygon(self.screen, color, [(self.sx(x), self.sx(y)) for x, y in pts], max(1, self.sx(width)) if width else 0)

    def panel(self, rect, accent, title=None, alpha=None):
        if alpha is None:
            alpha = self.panel_alpha
        x, y, w, h = rect
        s = pygame.Surface((max(1, self.sx(w)), max(1, self.sx(h))), pygame.SRCALPHA)
        rad = self.sx(14)
        pygame.draw.rect(s, (10, 12, 32, alpha), (0, 0, s.get_width(), s.get_height()), border_radius=rad)
        pygame.draw.rect(s, (*accent, 150), (0, 0, s.get_width(), s.get_height()), max(1, self.sx(2)), border_radius=rad)
        pygame.draw.rect(s, (*accent, 255), (self.sx(16), 0, self.sx(44), max(2, self.sx(3))), border_radius=2)
        self.screen.blit(s, (self.sx(x), self.sx(y)))
        if title:
            self.txt(self.f_xs, title, accent, (x + 18, y + 12))

    def btn(self, rect, key, label, color, primary=True):
        x, y, w, h = rect
        s = pygame.Surface((max(1, self.sx(w)), max(1, self.sx(h))), pygame.SRCALPHA)
        pygame.draw.rect(s, (*color, 70 if primary else 25), (0, 0, s.get_width(), s.get_height()), border_radius=self.sx(10))
        pygame.draw.rect(s, (*color, 230 if primary else 120), (0, 0, s.get_width(), s.get_height()), max(1, self.sx(2)), border_radius=self.sx(10))
        self.screen.blit(s, (self.sx(x), self.sx(y)))
        self.txt(self.f_xs, key, color, (x + 16, y + h / 2), "midleft")
        self.txt(self.f_m, label, (255, 255, 255), (x + 92, y + h / 2), "midleft")

    def card(self, rect, color, selected, active=True):
        x, y, w, h = rect
        s = pygame.Surface((max(1, self.sx(w)), max(1, self.sx(h))), pygame.SRCALPHA)
        pygame.draw.rect(s, (*color, 80 if selected else 18), (0, 0, s.get_width(), s.get_height()), border_radius=self.sx(14))
        pygame.draw.rect(s, (*color, 255 if selected else 90), (0, 0, s.get_width(), s.get_height()),
                         max(1, self.sx(3 if selected and active else 2)), border_radius=self.sx(14))
        self.screen.blit(s, (self.sx(x), self.sx(y)))

    def draw_title(self, x, y):
        a, b = self.pal["line"], self.pal["line2"]
        for i, ch in enumerate("PAC-RITMO"):
            img = self.f_t.render(ch, True, mix(a, b, (math.sin(self.t * 1.0 + i * 0.5) + 1) / 2))
            self.screen.blit(img, (self.sx(x), self.sx(y)))
            x += img.get_width() / self.K + 1

    def draw_hud(self):
        pal = self.pal
        acc = pal["accent"]
        DW, DH = self.DW, self.DH
        # --- puntos y combo
        self.panel((20, 16, 340, 116), acc, "P U N T O S")
        self.txt(self.f_xl, f"{self.score:,}", (255, 255, 255), (36, 38))
        self.txt(self.f_xs, f"DIFICULTAD {self.cfg['name']}  ·  PUNTOS x{self.cfg['mult']:g}", (150, 160, 205), (38, 100))
        if self.combo > 1 and self.song_time < self.combo_deadline:
            self.txt(self.f_xs, "COMBO", (255, 190, 60), (270, 14))
            self.txt(self.f_xl, f"x{self.combo}", (255, 190, 60), (270, 30))
        # --- minimapa
        px = DW - 224
        self.panel((px, 16, 204, 116), acc, f"M A P A · {self.map_style.upper()}")
        c, ox, oy = 9, px + 30, 44
        for y in range(GH):
            for x in range(GW):
                if self.walls[y][x]:
                    self.drect(pal["wall"], (ox + x * c, oy + y * c, c - 1, c - 1), 0, 2)
                elif self.dots[y][x]:
                    self.dcirc(shade(pal["dot"], 0.7), (ox + x * c + c / 2, oy + y * c + c / 2), 1)
        for it in self.items:
            self.dcirc((190, 240, 255) if it["kind"] == "freeze" else (190, 150, 255), (ox + it["x"] * c + c / 2, oy + it["y"] * c + c / 2), 4, 1)
        for g in self.ghosts:
            self.dcirc(pal["ghosts"][g.idx % 5], (ox + g.x * c + c / 2, oy + g.y * c + c / 2), 3)
        self.dcirc(pal["player"], (ox + self.player.x * c + c / 2, oy + self.player.y * c + c / 2), 4)
        # --- centro: canción o tutorial
        cx = DW / 2
        if self.state == "tutorial":
            title, text, _ = TUT_STEPS[self.tut_step]
            self.panel((cx - 340, 12, 680, 112), (120, 220, 255), "T U T O R I A L")
            self.txt(self.f_m, title, (255, 255, 255), (cx - 322, 32))
            for i, ln in enumerate(self.wrap(self.f_s, text, 640)[:3]):
                self.txt(self.f_s, ln, (205, 215, 245), (cx - 322, 58 + i * 20))
            for i in range(len(TUT_STEPS)):
                self.dcirc((120, 220, 255) if i <= self.tut_step else (60, 64, 100), (cx + 255 + i * 14, 40), 4)
            self.txt(self.f_xs, "ESC: saltar tutorial", (130, 140, 190), (cx + 332, 100), "topright")
        else:
            self.panel((cx - 200, 16, 400, 96), acc, "C A N C I Ó N")
            self.txt(self.f_m, self.fit(self.f_m, self.song.title, 360), (255, 255, 255), (cx - 182, 40))
            frac = max(0.0, min(1.0, self.song_time / max(1, self.song.duration)))
            self.drect((50, 52, 80), (cx - 182, 74, 364, 6), 0, 3)
            self.drect(acc, (cx - 182, 74, 364 * frac, 6), 0, 3)
            self.txt(self.f_xs, f"{mmss(self.song_time)} / {mmss(self.song.duration)}", (150, 160, 205), (cx - 182, 87))
            self.txt(self.f_xs, f"{self.song.bpm:.0f} BPM", (150, 160, 205), (cx + 182, 87), "topright")
        # --- efectos activos de los ítems
        fz, sl = self.cfg.get("fz", (5.0, 8.0))
        now = self.song_time
        eff = []
        if now < self.freeze_until:
            eff.append(("CONGELADOS", (190, 240, 255), (self.freeze_until - now) / fz))
        if now < self.slow_until:
            eff.append(("LENTOS", (190, 150, 255), (self.slow_until - now) / sl))
        for i, (nm, col, fr_) in enumerate(eff):
            x, y = cx - 215 + i * 230, (130 if self.state == "tutorial" else 120)
            self.panel((x, y, 200, 28), col, None, 220)
            self.txt(self.f_xs, nm, col, (x + 14, y + 8))
            self.drect((50, 52, 80), (x + 112, y + 11, 78, 6), 0, 3)
            self.drect(col, (x + 112, y + 11, 78 * max(0.0, min(1.0, fr_)), 6), 0, 3)
        # --- IA
        ai = self.scene_ai
        self.panel((20, DH - 134, 440, 118), acc, "I A   D E   E S C E N A")
        lvl = self.song.level_at(self.song_time) if not self.song.builtin else 1
        for i in range(4):
            self.drect(acc if i <= lvl else (45, 48, 75), (36 + i * 26, DH - 104, 22, 8), 0, 3)
        self.txt(self.f_xs, f"FONDO {self.bg_style.upper()} · FX {self.fx_mode.upper()} {int(self.mult_amt * 100)}%", (130, 140, 190), (146, DH - 107))
        for i, col in enumerate((pal["line"], pal["wall"], pal["line2"])):
            self.drect(col, (382 + i * 24, DH - 126, 20, 9), 0, 3)
        fade = 1.0 if ai.age < 8 else 0.55
        self.txt(self.f_m, self.fit(self.f_m, ai.decision, 410), shade((255, 255, 255), fade), (36, DH - 88))
        self.txt(self.f_s, self.fit(self.f_s, ai.reason, 410), shade((170, 180, 220), fade), (36, DH - 62))
        self.txt(self.f_xs, f"FANTASMAS {self.gfamily.upper()} · {self.cur_profile_name.upper()}", (130, 140, 190), (36, DH - 36))
        for i, g in enumerate(self.ghosts):
            col = pal["ghosts"][g.idx % 5]
            self.dcirc(col, (300 + i * 50, DH - 29), 5)
            frz = now < self.freeze_until
            self.txt(self.f_xs, "ICE" if frz else ("A*" if g.mode == "flee" else "BFS"),
                     (190, 240, 255) if frz else ((255, 150, 200) if g.mode == "flee" else (130, 200, 255)), (308 + i * 50, DH - 36))
        # --- escenario
        dim = "2D" if (self.sa == PLANE and not self.transitioning) else "3D"
        self.panel((DW - 330, DH - 118, 310, 102), acc, "E S C E N A R I O")
        self.txt(self.f_m, f"{NAMES[self.cur_shape]}  ·  {dim}", (255, 255, 255), (DW - 312, DH - 92))
        self.txt(self.f_xs, f"ATRAPADOS {self.stats['caught']}   PUNTOS {self.stats['dots']}   RONDAS {self.stats['rounds']}", (150, 160, 205), (DW - 312, DH - 60))
        md = self.model
        adj = int(round((1.0 / max(0.1, md.speed_mult) - 1.0) * 100))
        self.txt(self.f_xs, f"TU NIVEL {int(md.skill * 100)}%   ·   FANTASMAS {adj:+d}% VELOC.", (150, 160, 205), (DW - 312, DH - 42))
        self.drect((50, 52, 80), (DW - 312, DH - 27, 270, 5), 0, 2)
        self.drect(acc, (DW - 312, DH - 27, 270 * md.skill, 5), 0, 2)
        # --- banner y subtítulo de la voz
        txt, lt = self.banner
        if lt > 0:
            img = self.f_l.render(txt, True, (255, 255, 255))
            img.set_alpha(int(255 * min(1.0, lt * 1.5)))
            self.screen.blit(img, img.get_rect(midtop=(self.sx(cx), self.sx(DH - 138))))
        cap, ct = self.caption
        if ct > 0:
            w = min(self.tw(self.f_s, cap) + 56, DW - 960)
            self.panel((cx - w / 2, DH - 92, w, 40), pal["line2"], None, int(190 * min(1, ct)))
            self.dcirc(pal["line2"], (cx - w / 2 + 20, DH - 72), 5)
            self.txt(self.f_s, self.fit(self.f_s, cap, w - 50), (225, 240, 255), (cx - w / 2 + 34, DH - 82))

    def draw_intro(self):
        acc = self.pal["accent"]
        ph = self.DH - 160
        self.panel((60, 80, 640, ph), acc, None, 205)
        self.draw_title(86, 100)
        self.txt(self.f_m, "¡Tú persigues, ellos huyen... al ritmo de TU música!", (255, 255, 255), (94, 214))
        lines = ["Sube tus canciones .mp3: la música y la IA deciden el mapa, los colores, la forma de los fantasmas y cómo actúan.",
                 "Los fantasmas HUYEN de ti con A* y patrullan con BFS; tú los sigues mientras el mapa cambia con la canción.",
                 "La IA alterna 2D y 3D en los cambios de sección y los drops, y te lo avisa con su voz.",
                 "Categorías: MI MÚSICA y CIRCUITO (estética Tron, con la pista especial Derezzed)."]
        y = 262
        for ln in lines:
            self.dcirc(acc, (102, y + 9), 4)
            for sub in self.wrap(self.f_s, ln, 560):
                self.txt(self.f_s, sub, (205, 215, 245), (118, y))
                y += 22
            y += 10
        self.btn((94, 80 + ph - 130, 560, 50), "ENTER", "Jugar tutorial interactivo", acc, True)
        self.btn((94, 80 + ph - 70, 560, 42), "S", "Saltar a las canciones", acc, False)

    def draw_songs(self):
        acc = self.pal["accent"]
        DW, DH = self.DW, self.DH
        self.panel((60, 40, DW - 120, DH - 80), acc, None, 215)
        for i, (nm, pa) in enumerate((("MI MÚSICA", PAL_GROOVE), ("CIRCUITO", PAL_TRON))):
            x, y, w, h = 90 + i * 210, 62, 190, 40
            self.card((x, y, w, h), pa["accent"], i == self.tab)
            self.txt(self.f_m, nm, (255, 255, 255) if i == self.tab else shade(pa["accent"], 0.7), (x + w / 2, y + h / 2), "center")
        self.txt(self.f_xs, "← → categoría", (110, 120, 170), (520, 76))
        items = self.tab_items(self.tab)
        sel = min(self.sel[self.tab], max(0, len(items) - 1))
        vis = max(4, int((DH - 80 - 124 - 60) // 60))
        sc = self.scroll[self.tab]
        sc = sel if sel < sc else (sel - vis + 1 if sel >= sc + vis else sc)
        self.scroll[self.tab] = sc
        dx, dw = DW - 390, 300
        lw = dx - 90 - 20
        if not items:
            self.txt(self.f_m, "Aún no hay canciones aquí", (205, 215, 245), (90 + lw / 2, 300), "center")
            self.txt(self.f_s, "Pulsa U para buscar tus .mp3 o arrástralos a la ventana", (150, 160, 205), (90 + lw / 2, 332), "center")
        for r, it in enumerate(items[sc:sc + vis]):
            i = sc + r
            x, y, w, h = 90, 124 + r * 60, lw, 52
            self.card((x, y, w, h), self.pal["line2"] if it["special"] else acc, i == sel)
            self.txt(self.f_xs, f"{i + 1:02d}", (110, 120, 170), (x + 16, y + h / 2), "midleft")
            tcol = (255, 215, 120) if it["special"] else (255, 255, 255)
            if it["special"]:
                self.dpoly(tcol, [(x + 58 + 8 * math.cos(math.pi * (-0.5 + 0.4 * k)), y + h / 2 + 8 * math.sin(math.pi * (-0.5 + 0.4 * k))) for k in range(5)])
                self.txt(self.f_m, self.fit(self.f_m, it["title"], w - 220), tcol, (x + 78, y + 7))
                self.txt(self.f_xs, "PISTA ESPECIAL", self.pal["line2"], (x + 78, y + 32))
            else:
                self.txt(self.f_m, self.fit(self.f_m, it["title"], w - 170), tcol, (x + 58, y + h / 2), "midleft")
            e = it["entry"]
            if e is None:
                self.txt(self.f_xs, "FALTA EL ARCHIVO", (255, 150, 60), (x + w - 16, y + h / 2), "midright")
            elif e.get("best"):
                self.txt(self.f_m, e["grade"], GRADE_COL.get(e["grade"], (255, 255, 255)), (x + w - 16, y + 6), "topright")
                self.txt(self.f_xs, f"{e['best']:,}", (150, 160, 205), (x + w - 16, y + 32), "topright")
        self.panel((dx, 124, dw, DH - 80 - 124 - 40), acc, "D E T A L L E", 150)
        if items:
            it = items[sel]
            for i, ln in enumerate(self.wrap(self.f_m, it["title"], dw - 36)[:3]):
                self.txt(self.f_m, ln, (255, 255, 255), (dx + 18, 150 + i * 24))
            song = self.cache.get(it["entry"]["path"]) if it["entry"] else None
            y = 240
            if song:
                for k, v in (("BPM", f"{song.bpm:.0f}"), ("DURACIÓN", mmss(song.duration)), ("SECCIONES", str(len(song.sections))), ("DROPS", str(len(song.drops)))):
                    self.txt(self.f_xs, k, (130, 140, 190), (dx + 18, y))
                    self.txt(self.f_m, v, (255, 255, 255), (dx + dw - 18, y - 3), "topright")
                    y += 30
            elif it["entry"]:
                self.txt(self.f_s, "Se analiza al elegirla.", (150, 160, 205), (dx + 18, y))
            note = ("Solo suena Derezzed (Daft Punk). La música y la IA deciden mapa, colores y fantasmas, y alternan el plano 2D con las formas 3D."
                    if it["special"] else ("Estética Tron: la música y la IA arman el circuito." if self.tab == 1 else "La música y la IA arman el mapa."))
            for i, ln in enumerate(self.wrap(self.f_s, note, dw - 36)):
                self.txt(self.f_s, ln, (205, 215, 245), (dx + 18, 370 + i * 20))
        self.txt(self.f_xs, "ENTER jugar    U buscar .mp3    SUPR quitar    F11 pantalla completa    ESC atrás", (130, 140, 190), (90, DH - 70))
        txt, lt = self.msg
        if lt > 0:
            self.txt(self.f_s, self.fit(self.f_s, txt, DW - 200), (255, 200, 120), (90, DH - 96))

    def draw_browser(self):
        acc = self.pal["accent"]
        DW, DH = self.DW, self.DH
        w, h = 800, DH - 100
        x0 = (DW - w) / 2
        self.panel((x0, 50, w, h), acc, "E X P L O R A D O R", 225)
        self.txt(self.f_s, self.fit(self.f_s, self.br_path or "Unidades", w - 60), (170, 180, 220), (x0 + 24, 76))
        vis = int((h - 130) // 34)
        sel = self.br_sel
        self.br_scroll = sel if sel < self.br_scroll else (sel - vis + 1 if sel >= self.br_scroll + vis else self.br_scroll)
        for r, (name, isdir) in enumerate(self.br_items[self.br_scroll:self.br_scroll + vis]):
            i = self.br_scroll + r
            y = 108 + r * 34
            if i == sel:
                self.card((x0 + 16, y - 2, w - 32, 32), acc, True)
            col = (255, 215, 120) if isdir else (255, 255, 255)
            self.txt(self.f_m, ("[ ] " if isdir else "♪ ") + self.fit(self.f_m, name, w - 100), col, (x0 + 30, y + 14), "midleft")
        self.txt(self.f_xs, "ENTER abrir / añadir    ← subir carpeta    A añadir todas las de la carpeta    ESC cerrar", (130, 140, 190), (x0 + 24, 50 + h - 52))
        txt, lt = self.msg
        if lt > 0:
            self.txt(self.f_s, self.fit(self.f_s, txt, w - 48), (255, 200, 120), (x0 + 24, 50 + h - 30))

    def draw_analyzing(self):
        acc = self.pal["accent"]
        cx = self.DW / 2
        self.panel((cx - 330, 250, 660, 200), acc, None, 220)
        self.txt(self.f_l, "ANALIZANDO EL RITMO", (255, 255, 255), (cx, 276), "midtop")
        self.txt(self.f_s, self.fit(self.f_s, title_of(self.pick[1]["path"]), 600), (170, 180, 220), (cx, 322), "midtop")
        p = self.job.progress if self.job else 0
        self.drect((50, 52, 80), (cx - 280, 370, 560, 12), 0, 6)
        self.drect(acc, (cx - 280, 370, 560 * p, 12), 0, 6)
        self.txt(self.f_xs, f"{int(p * 100)}%   ·   buscando tempo, beats, golpes y secciones", (130, 140, 190), (cx, 396), "midtop")

    def draw_difficulty(self):
        acc, song = self.pal["accent"], self.cur_song
        DW, DH = self.DW, self.DH
        self.panel((80, 50, DW - 160, DH - 100), acc, None, 215)
        self.txt(self.f_xs, "C I R C U I T O" if self.pick[0] == 1 else "M I   M Ú S I C A", acc, (110, 72))
        self.txt(self.f_l, self.fit(self.f_l, song.title, DW - 260), (255, 255, 255), (110, 92))
        self.txt(self.f_s, f"{song.bpm:.0f} BPM   ·   {mmss(song.duration)}   ·   {len(song.sections)} secciones   ·   {len(song.drops)} drops", (170, 180, 220), (110, 134))
        x0, y0, wd, ht = 110, 172, DW - 220, 120
        self.drect((14, 16, 40), (x0, y0, wd, ht), 0, 8)
        for a_, b_ in song.surges:                                  # zonas rojas = partes muy movidas
            self.drect((78, 24, 30), (x0 + wd * a_ / song.duration, y0, max(2, wd * (b_ - a_) / song.duration), ht), 0, 0)
        pts = [(x0 + wd * i / (len(song.curve) - 1), y0 + ht - 4 - (ht - 12) * v) for i, v in enumerate(song.curve)]
        self.dpoly(shade(acc, 0.35), [(x0, y0 + ht)] + pts + [(x0 + wd, y0 + ht)])
        for a, b in zip(pts, pts[1:]):
            self.dline(acc, a, b, 2)
        for t, lv in song.sections[1:]:
            xx = x0 + wd * t / song.duration
            self.dline((90, 96, 150), (xx, y0), (xx, y0 + ht))
        for t in song.drops:
            xx = x0 + wd * t / song.duration
            self.dpoly(self.pal["line2"], [(xx, y0 + 4), (xx - 6, y0 - 6), (xx + 6, y0 - 6)])
        self.txt(self.f_xs, "MOVIDA DE LA CANCIÓN  ·  zona roja = parte muy movida (el mapa cambia sin parar)  ·  ▼ = drop", (110, 120, 170), (x0, y0 + ht + 8))
        self.txt(self.f_xs, "D I F I C U L T A D", acc, (110, 340))
        cw = (wd - 40) / 3
        for i, d in enumerate(DIFFS):
            x, y = x0 + i * (cw + 20), 368
            self.card((x, y, cw, 180), d["color"], i == self.sel_diff)
            self.txt(self.f_l, d["name"], d["color"], (x + cw / 2, y + 16), "midtop")
            for j, ln in enumerate(d["desc"]):
                self.txt(self.f_s, ln, (215, 222, 250), (x + cw / 2, y + 70 + j * 28), "midtop")
        self.btn((x0, DH - 160, wd, 52), "ENTER", "¡A JUGAR!", acc, True)
        self.txt(self.f_xs, "← →  cambiar dificultad      ESC  volver", (130, 140, 190), (DW / 2, DH - 90), "midtop")

    def draw_results(self):
        f = self.final
        acc = (PAL_TRON if f.get("tron") else PAL_GROOVE)["accent"]
        DW, DH = self.DW, self.DH
        w = 780
        x0 = (DW - w) / 2
        self.panel((x0, 50, w, DH - 100), acc, None, 220)
        self.txt(self.f_xs, "C A N C I Ó N   T E R M I N A D A", acc, (DW / 2, 72), "midtop")
        self.txt(self.f_m, self.fit(self.f_m, f["title"], w - 80), (255, 255, 255), (DW / 2, 94), "midtop")
        self.txt(self.f_t, f["grade"], GRADE_COL[f["grade"]], (DW / 2 - 200, 126), "midtop")
        self.txt(self.f_xs, "PUNTUACIÓN", (130, 140, 190), (DW / 2 + 10, 150))
        self.txt(self.f_xl, f"{f['score']:,}", (255, 255, 255), (DW / 2 + 10, 168))
        self.txt(self.f_s, f"{f['diff']}" + ("   ¡NUEVO RÉCORD!" if f["new"] else ""), (255, 215, 80) if f["new"] else (170, 180, 220), (DW / 2 + 10, 226))
        rows = [("Fantasmas atrapados", f["caught"]), ("Combo máximo", f"x{f['maxcombo']}"), ("Puntos comidos", f["dots"]),
                ("Rondas completas", f["rounds"]), ("Mapas nuevos que armó la IA", f["maps"]),
                ("Cambios 2D ↔ 3D", f["shapes"]), ("Último estilo de mapa", f["style"]), ("Última familia de fantasmas", f["family"]),
                ("Ítems recogidos", f["items"]), ("Nivel que aprendió la IA de ti", f"{f['skill']}%  ·  fantasmas {f['adj']:+d}% veloc.")]
        for i, (k, v) in enumerate(rows):
            y = 282 + i * 31
            self.txt(self.f_s, k, (205, 215, 245), (x0 + 50, y))
            self.txt(self.f_m, str(v), (255, 255, 255), (x0 + w - 50, y - 2), "topright")
            self.dline((50, 54, 90), (x0 + 50, y + 26), (x0 + w - 50, y + 26))
        self.btn((x0 + 50, DH - 130, w - 100, 46), "ENTER", "Elegir otra canción", acc, True)

    def draw_countdown(self):
        n = int(self.cd_t) + 1
        label = "¡YA!" if n <= 0 else str(min(n, 3))
        img = pygame.transform.rotozoom(self.f_t.render(label, True, (255, 255, 255)), 0, 1.6 * (1 + 0.35 * (self.cd_t % 1.0)))
        self.screen.blit(img, img.get_rect(center=(self.CX, self.CY)))
        self.txt(self.f_m, "¡PREPÁRATE!  la música empieza ya", (200, 210, 250), (self.DW / 2, self.DH / 2 + 110), "midtop")

    def draw_pause(self):
        ov = pygame.Surface((self.W, self.H), pygame.SRCALPHA)
        ov.fill((4, 4, 16, 170))
        self.screen.blit(ov, (0, 0))
        cx, cy = self.DW / 2, self.DH / 2
        self.panel((cx - 300, cy - 150, 600, 300), self.pal["accent"], None, 235)
        self.txt(self.f_xl, "PAUSA", (255, 255, 255), (cx, cy - 126), "midtop")
        self.txt(self.f_m, "ESC  continuar", (205, 215, 245), (cx, cy - 50), "midtop")
        self.txt(self.f_m, "Q  abandonar canción", (205, 215, 245), (cx, cy - 18), "midtop")
        self.txt(self.f_s, f"Sincronía audio/imagen:  ,  y  .   →  {self.latency * 1000:+.0f} ms", (170, 180, 220), (cx, cy + 36), "midtop")
        self.txt(self.f_xs, "F11 pantalla completa  ·  F2 música  ·  F3 voz de la IA  ·  F4 efectos suaves", (130, 140, 190), (cx, cy + 92), "midtop")

    def draw(self):
        sc = self.scene
        sc.fill((0, 0, 0))
        self.draw_world(sc)
        self.trail.fill((186, 186, 186), special_flags=pygame.BLEND_RGB_MULT)      # estela que se desvanece
        self.draw_items(self.trail)
        for g in self.ghosts:
            self.draw_entity(self.trail, g)
        if self.state in PLAYING:
            self.draw_entity(self.trail, self.player, pac=True)
        sc.blit(self.trail, (0, 0), special_flags=pygame.BLEND_RGB_ADD)
        pygame.transform.smoothscale(sc, self.glow_small.get_size(), self.glow_small)
        pygame.transform.smoothscale(self.glow_small, (self.RW, self.RH), self.glow)
        ox = oy = 0
        if self.shake > 0.5:
            ox, oy = int(random.uniform(-1, 1) * self.shake * self.rK), int(random.uniform(-1, 1) * self.shake * self.rK)
        direct = (self.RW == self.W and self.RH == self.H)
        out = self.screen if direct else self.canvas
        out.blit(self.render_bg(), (0, 0))                                  # 1) fondo animado
        self.draw_multiply(out, sc)                                         # 2) copias de la escena en el fondo
        if self.opacity > 0.05:                                             # tablero más o menos opaco
            self.backing.set_alpha(int(self.opacity * 235))
            out.blit(self.backing, (ox, oy))
        out.blit(sc, (ox, oy), special_flags=pygame.BLEND_RGB_ADD)          # 3) la partida
        out.blit(self.glow, (ox, oy), special_flags=pygame.BLEND_RGB_ADD)
        if self.fx_mode == "eco" and self.mult_amt > 0.01 and self.quality >= 1:
            self.fb = pygame.transform.smoothscale(out, (self.RW2, self.RH2))   # realimentación del eco
        self.post_fx(out)                                                   # 4) efectos de pantalla
        if not direct:
            (pygame.transform.smoothscale if self.quality >= 1 else pygame.transform.scale)(out, (self.W, self.H), self.screen)
        st = self.state
        if st in PLAYING:
            self.draw_hud()
        {"intro": self.draw_intro, "songs": self.draw_songs, "browser": self.draw_browser, "analyzing": self.draw_analyzing,
         "difficulty": self.draw_difficulty, "results": self.draw_results, "countdown": self.draw_countdown,
         "pause": self.draw_pause}.get(st, lambda: None)()

    def on_key(self, key):
        st = self.state
        if key == pygame.K_F1:
            self.debug = not self.debug
        elif key == pygame.K_F2:
            self.audio.muted = not self.audio.muted
        elif key == pygame.K_F3:
            self.voice.muted = not self.voice.muted
        elif key == pygame.K_F4:
            self.soft = not self.soft
            self.banner = ("EFECTOS SUAVES " + ("ACTIVADOS" if self.soft else "DESACTIVADOS"), 1.8)
        elif key == pygame.K_F11:
            self.apply_display(not self.fullscreen)
            return
        if st == "intro":
            if key == pygame.K_RETURN:
                self.audio.sfx("select")
                self.start_tutorial()
            elif key == pygame.K_s:
                self.audio.sfx("select")
                self.goto_songs()
            elif key == pygame.K_ESCAPE:
                self.quit()
        elif st == "songs":
            items = self.tab_items(self.tab)
            if key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_a, pygame.K_d):
                self.tab = 1 - self.tab
                self.audio.sfx("move")
            elif key in (pygame.K_UP, pygame.K_w) and items:
                self.sel[self.tab] = (self.sel[self.tab] - 1) % len(items)
                self.audio.sfx("move")
            elif key in (pygame.K_DOWN, pygame.K_s) and items:
                self.sel[self.tab] = (self.sel[self.tab] + 1) % len(items)
                self.audio.sfx("move")
            elif key == pygame.K_RETURN:
                self.open_item()
            elif key == pygame.K_u:
                self.br_open()
            elif key in (pygame.K_DELETE, pygame.K_BACKSPACE) and items:
                it = items[min(self.sel[self.tab], len(items) - 1)]
                if it["special"]:
                    self.lib["derezzed"] = None
                elif it["entry"] in self.lib["mi_musica"]:
                    self.lib["mi_musica"].remove(it["entry"])
                elif it["entry"] in self.lib["circuito"]:
                    self.lib["circuito"].remove(it["entry"])
                save_lib(self.lib)
                self.sel[self.tab] = 0
            elif key == pygame.K_ESCAPE:
                self.enter_attract("intro")
        elif st == "browser":
            n = len(self.br_items)
            if key in (pygame.K_UP, pygame.K_w) and n:
                self.br_sel = (self.br_sel - 1) % n
            elif key in (pygame.K_DOWN, pygame.K_s) and n:
                self.br_sel = (self.br_sel + 1) % n
            elif key == pygame.K_RETURN:
                self.br_enter()
            elif key in (pygame.K_LEFT, pygame.K_BACKSPACE):
                self.br_up()
            elif key == pygame.K_a:
                self.br_add_all()
            elif key == pygame.K_ESCAPE:
                self.state = "songs"
        elif st == "analyzing":
            if key == pygame.K_ESCAPE:
                self.state = "songs"
        elif st == "difficulty":
            if key in (pygame.K_LEFT, pygame.K_a):
                self.sel_diff = (self.sel_diff - 1) % 3
                self.audio.sfx("move")
            elif key in (pygame.K_RIGHT, pygame.K_d):
                self.sel_diff = (self.sel_diff + 1) % 3
                self.audio.sfx("move")
            elif key == pygame.K_RETURN:
                self.start_play()
            elif key == pygame.K_ESCAPE:
                self.state = "songs"
        elif st == "tutorial":
            if key in KEYDIRS:
                self.want = KEYDIRS[key]
            elif key == pygame.K_ESCAPE or (key == pygame.K_RETURN and self.tut_step == len(TUT_STEPS) - 1):
                self.goto_songs()
        elif st == "countdown":
            if key == pygame.K_ESCAPE:
                self.goto_songs()
        elif st == "play":
            if key in KEYDIRS:
                self.want = KEYDIRS[key]
            elif key == pygame.K_ESCAPE:
                self.state = "pause"
                self.audio.pause()
            elif key in (pygame.K_COMMA, pygame.K_PERIOD):
                self.latency += 0.01 if key == pygame.K_PERIOD else -0.01
            elif self.debug:
                if pygame.K_1 <= key <= pygame.K_4:
                    self.start_transition(key - pygame.K_1, "debug")
                elif key == pygame.K_m:
                    self.apply_map(random.choice(STYLES))
                elif key == pygame.K_p:
                    self.show_paths = not self.show_paths
        elif st == "pause":
            if key == pygame.K_ESCAPE:
                self.state = "play"
                self.audio.unpause()
            elif key in (pygame.K_COMMA, pygame.K_PERIOD):
                self.latency += 0.01 if key == pygame.K_PERIOD else -0.01
            elif key == pygame.K_q:
                self.audio.unpause()
                self.goto_songs()
        elif st == "results":
            if key in (pygame.K_RETURN, pygame.K_ESCAPE):
                self.audio.sfx("select")
                self.goto_songs()
            elif key == pygame.K_t:
                self.start_tutorial()

    def quit(self):
        self.lib["latency"] = self.latency
        save_lib(self.lib)
        pygame.quit()
        sys.exit()

    def run(self):
        while True:
            dt = min(self.clock.tick(60) / 1000, 0.05)
            self.adapt_quality(dt)
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    self.quit()
                elif e.type == pygame.KEYDOWN:
                    self.on_key(e.key)
                elif e.type == pygame.DROPFILE:
                    self.on_drop(e.file)
            self.update(dt)
            self.draw()
            pygame.display.flip()


if __name__ == "__main__":
    Game(fullscreen="--ventana" not in sys.argv).run()
