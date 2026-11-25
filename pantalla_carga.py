# -*- coding: utf-8 -*-
"""
Pantalla de carga con música de fondo.

Requiere:  pip install pygame      (no necesita numpy ni archivos de audio)

Qué hace
  - Fondo con zoom suave, "latido" al compás de la música y barrido de brillo.
  - Notas musicales y chispas flotando; ecualizador animado.
  - Barra de progreso con degradado, rayas animadas y un robot que se come los
    puntos mientras persigue a una criatura.
  - Música chiptune generada por código (loop de 8 compases) sincronizada con
    los efectos visuales. Si pones "musica_carga.mp3/.ogg/.wav" junto a este archivo,
    se usa esa en lugar de la generada.
  - Las tareas de carga se ejecutan en un hilo aparte, así la animación NUNCA se congela.

Personajes propios (opcional): guarda "personajes.png" (PNG con transparencia: el personaje
de la izquierda es el cazador y los demás son los fantasmas) en .vscode/imagenes/ o junto a
este archivo. En la barra, el cazador se come a los fantasmas conforme avanza la carga.
(También sirven "cazador.png" y "presa.png" sueltos.) Sin ellos se usan los personajes
dibujados por código.
Opcional: "cazador_cerrado.png" (el cazador con la boca cerrada) para que abra y cierre la
boca; si no existe, el cazador "mastica" estirando y comprimiendo el sprite.
Al acercarse el cazador, cada fantasma se asusta (azul, parpadea y trata de huir); al
alcanzarlo se lo traga, con onda expansiva, puntos, notas y un sonido.

Teclas: M = silenciar / activar música, ESC = salir.

Uso desde el juego:
    from loading_screen import run_loading

    tareas = [
        ("Generando música...",   crear_musica),
        ("Cargando sprites...",   cargar_sprites),
        ("Construyendo laberinto...", generar_laberinto),
    ]
    run_loading(screen, tareas)

Notas
  * Las funciones de las tareas corren en un hilo secundario: cargar archivos,
    generar sonidos o calcular datos está bien. Evita crear la ventana o llamar
    a .convert() / .convert_alpha() dentro de ellas (hazlo antes o después).
  * La música usa el mismo formato de mixer que sonido.py (44100 Hz, 16 bits,
    estéreo), así que no interfiere con el audio de tu juego.
"""
import array
import math
import os
import random
import sys
import threading
import time

import pygame

# ---------------------------------------------------------------- Configuración
CARPETA = os.path.dirname(os.path.abspath(__file__))
# Se usa la primera imagen que exista:
IMAGENES = [
    os.path.join(CARPETA, ".vscode", "imagenes", "logo.jpg"),
    os.path.join(CARPETA, "portada.png"),
    os.path.join(CARPETA, "logo.jpg"),
]
# Música opcional propia (si existe, reemplaza a la generada). Cualquiera de estas:
MUSICAS = [os.path.join(CARPETA, "musica_carga" + ext) for ext in (".mp3", ".ogg", ".wav")]

FPS = 60
BPM = 124                  # compás de la música generada (los efectos visuales lo siguen)
VOLUMEN = 0.5
DURACION_MINIMA = 4.5      # segundos mínimos en pantalla aunque la carga sea instantánea
FADE_IN = 0.9
FADE_OUT = 0.7
FORMATO_MIXER = (44100, -16, 2, 512)   # igual que sonido.py

CIAN = (0, 230, 240)
MAGENTA = (255, 70, 200)
AMARILLO = (255, 220, 70)
VERDE = (120, 255, 140)
VIOLETA = (170, 120, 255)
BLANCO = (240, 240, 250)
PALETA = [CIAN, MAGENTA, AMARILLO, VERDE, VIOLETA]

# Escribe aquí tus consejos (se muestran en orden aleatorio). Revisa que coincidan
# con las reglas reales de tu juego.
CONSEJOS = [
    "Ahora tú eres el cazador: los fantasmas huyen de ti.",
    "La música crea el camino: sigue el ritmo.",
    "En este laberinto la presa eres tú... pero al revés.",
    "Escucha la canción: el compás te dice cuándo avanzar.",
    "Moverte con el ritmo es la mejor forma de alcanzarlos.",
    "No los persigas a ciegas: anticipa por dónde van a escapar.",
    "Los fantasmas aprenden de tus movimientos. Cambia de ruta.",
    "Algunos ítems congelan o ralentizan el tiempo. Úsalos a tu favor.",
    "Cierra los pasillos para acorralarlos en una esquina.",
    "Puedes jugar con tu propia música.",
    "Cada partida mejora tu calificación: apunta a la S.",
    "Pulsa M para silenciar o activar la música.",
]


# ---------------------------------------------------------------- Utilidades
def color_arcoiris(k):
    """0..1  ->  cian -> magenta -> amarillo."""
    k = max(0.0, min(1.0, k))
    a, b, p = (CIAN, MAGENTA, k / 0.5) if k < 0.5 else (MAGENTA, AMARILLO, (k - 0.5) / 0.5)
    return tuple(int(a[i] + (b[i] - a[i]) * p) for i in range(3))


def mezclar(a, b, p):
    return tuple(int(a[i] + (b[i] - a[i]) * p) for i in range(3))


def crear_resplandor(r, color):
    """Disco de luz (para mezclar con BLEND_ADD)."""
    s = pygame.Surface((2 * r, 2 * r))
    for i in range(r, 0, -2):
        k = (1 - i / r) ** 2
        pygame.draw.circle(s, tuple(int(c * k) for c in color), (r, r), i)
    return s


def crear_nota(color, tipo, tam):
    """Nota musical con halo. tipo 0 = negra, 1 = corchea."""
    w, h = tam, int(tam * 1.45)
    base = pygame.Surface((w, h), pygame.SRCALPHA)
    cab = pygame.Rect(0, 0, int(w * 0.52), int(w * 0.36))
    cab.center = (int(w * 0.36), int(h * 0.80))
    pygame.draw.ellipse(base, color, cab)
    sx = cab.right - 1
    pygame.draw.line(base, color, (sx, cab.centery), (sx, int(h * 0.12)), max(2, int(w * 0.1)))
    if tipo == 1:
        pygame.draw.polygon(base, color, [(sx, int(h * 0.12)), (int(w * 0.98), int(h * 0.34)),
                                          (int(w * 0.86), int(h * 0.52)), (sx, int(h * 0.30))])
    pad = int(tam * 0.5)
    fin = pygame.Surface((w + 2 * pad, h + 2 * pad), pygame.SRCALPHA)
    halo = pygame.transform.smoothscale(base, (int(w * 1.7), int(h * 1.7)))
    halo.set_alpha(70)
    fin.blit(halo, halo.get_rect(center=fin.get_rect().center))
    fin.blit(base, (pad, pad))
    return fin


def dibujar_robot(surf, x, y, t, r):
    """El cazador (robot cian). Mira a la derecha y mastica."""
    cuerpo = pygame.Rect(0, 0, r * 2, r * 2)
    cuerpo.center = (x, y)
    pygame.draw.rect(surf, CIAN, cuerpo, border_radius=int(r * 0.6))
    for dx in (-r // 2, r // 2):
        pygame.draw.line(surf, CIAN, (x + dx, y - r), (x + dx, y - r - int(r * 0.35)), max(2, r // 7))
        pygame.draw.circle(surf, AMARILLO, (x + dx, y - r - int(r * 0.42)), max(2, r // 6))
    for dx in (-r * 0.3, r * 0.35):
        pygame.draw.circle(surf, BLANCO, (int(x + dx), int(y - r * 0.12)), int(r * 0.27))
        pygame.draw.circle(surf, (8, 8, 24), (int(x + dx + r * 0.09), int(y - r * 0.12)), int(r * 0.12))
    abre = max(2, int(r * 0.12 + abs(math.sin(t * 9)) * r * 0.25))
    pygame.draw.line(surf, (8, 8, 24), (x - int(r * 0.4), y + int(r * 0.48)),
                     (x + int(r * 0.4), y + int(r * 0.48)), abre)


def dibujar_presa(surf, x, y, t, r, color=MAGENTA):
    """La criatura asustada que huye."""
    pygame.draw.circle(surf, color, (x, y), r)
    for i, dx in enumerate((-0.55, 0, 0.55)):
        sube = math.sin(t * 14 + i * 2) * r * 0.18
        pygame.draw.circle(surf, color, (int(x + dx * r), int(y + r * 0.9 + sube)), max(2, int(r * 0.27)))
    for dx in (-0.36, 0.36):
        pygame.draw.circle(surf, BLANCO, (int(x + dx * r), int(y - r * 0.18)), max(2, int(r * 0.27)))
        pygame.draw.circle(surf, (8, 8, 24), (int(x + dx * r - r * 0.09), int(y - r * 0.18)), max(1, int(r * 0.13)))


# ---------------------------------------------------------------- Música generada
def _midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


# (nota raíz, intervalos del acorde) por compás: Am F C G | Am F G E
PROGRESION = [(57, (0, 3, 7)), (53, (0, 4, 7)), (60, (0, 4, 7)), (55, (0, 4, 7)),
              (57, (0, 3, 7)), (53, (0, 4, 7)), (55, (0, 4, 7)), (52, (0, 4, 7))]
# melodía: (tiempo dentro del compás, duración, nota MIDI)
MELODIA = [
    [(0, 1.5, 76), (1.5, .5, 74), (2, 2, 72)],
    [(0, 1.5, 72), (1.5, .5, 74), (2, 1, 76), (3, 1, 74)],
    [(0, 1, 76), (1, 1, 79), (2, 1.5, 76), (3.5, .5, 74)],
    [(0, 2, 74), (2, 1, 72), (3, 1, 71)],
    [(0, .5, 81), (.5, .5, 79), (1, 1, 76), (2, 1, 79), (3, 1, 76)],
    [(0, 1, 77), (1, .5, 76), (1.5, .5, 72), (2, 2, 77)],
    [(0, 1, 79), (1, 1, 76), (2, 1, 74), (3, 1, 71)],
    [(0, 1, 68), (1, 1, 71), (2, 2, 76)],
]


def sintetizar_musica(freq_salida=44100, canales=2, bpm=BPM):
    """Devuelve bytes PCM de 16 bits con un loop chiptune de 8 compases (sin numpy)."""
    sr = 22050 if freq_salida % 22050 == 0 else freq_salida   # se sintetiza barato y se repite
    rep = freq_salida // sr
    beat = 60.0 / bpm
    N = int(sr * beat * 4 * len(PROGRESION))
    izq = [0.0] * N
    der = [0.0] * N
    rnd = random.Random(7)

    def voz(t_beats, dur_beats, freq, vol, forma, pan=0.0, duty=0.5, caida=3.0):
        s = int(t_beats * beat * sr)
        n = int(dur_beats * beat * sr)
        gl, gr = vol * (1 - max(pan, 0)), vol * (1 + min(pan, 0))
        inc = freq / sr
        fac = math.exp(-caida / max(n, 1))
        env = 1.0
        for i in range(n):
            p = (i * inc) % 1.0
            w = (1.0 if p < duty else -1.0) if forma == "c" else 4.0 * abs(p - 0.5) - 1.0
            v = w * env * min(1.0, i / 100.0) * min(1.0, (n - i) / 160.0)
            env *= fac
            j = (s + i) % N
            izq[j] += v * gl
            der[j] += v * gr

    def bombo(t_beats):
        s, n, fase = int(t_beats * beat * sr), int(0.22 * sr), 0.0
        for i in range(n):
            fase += (45 + 110 * math.exp(-i / (0.025 * sr))) / sr
            v = math.sin(2 * math.pi * fase) * math.exp(-i / (0.07 * sr)) * 0.5
            j = (s + i) % N
            izq[j] += v
            der[j] += v

    def caja(t_beats):
        s, n = int(t_beats * beat * sr), int(0.16 * sr)
        for i in range(n):
            e = math.exp(-i / (0.045 * sr))
            v = (rnd.uniform(-1, 1) * 0.55 + math.sin(2 * math.pi * 190 * i / sr) * 0.35) * e * 0.28
            j = (s + i) % N
            izq[j] += v
            der[j] += v

    def hat(t_beats, vol):
        s, n, prev = int(t_beats * beat * sr), int(0.045 * sr), 0.0
        for i in range(n):
            r = rnd.uniform(-1, 1)
            v = (r - prev) * math.exp(-i / (0.011 * sr)) * vol
            prev = r
            j = (s + i) % N
            izq[j] += v * 0.8
            der[j] += v * 1.2

    for c, (raiz, ac) in enumerate(PROGRESION):
        base = c * 4
        tonos = [raiz + 12 + ac[0], raiz + 12 + ac[1], raiz + 12 + ac[2], raiz + 24]
        for k, idx in enumerate((0, 1, 2, 3, 2, 1, 2, 3)):                       # arpegio
            voz(base + k * 0.5, 0.5, _midi(tonos[idx]), 0.10, "c", -0.35, 0.25, 3.5)
        for k, off in enumerate((0, 0, 12, 0, 0, 0, 12, 7)):                     # bajo
            voz(base + k * 0.5, 0.46, _midi(raiz - 12 + off), 0.30, "t", 0.0, caida=1.2)
        for tb, dur, nota in MELODIA[c]:                                         # melodía
            voz(base + tb, dur * 0.95, _midi(nota), 0.13, "c", 0.3, 0.5, 1.6)
        bombo(base)
        bombo(base + 2)
        if c % 2:
            bombo(base + 3.5)
        caja(base + 1)
        caja(base + 3)
        for k in range(8):
            hat(base + k * 0.5, 0.10 if k % 2 == 0 else 0.20)

    pico = max(max(abs(x) for x in izq), max(abs(x) for x in der), 1e-9)
    g = 0.85 * 32767 / pico
    li = array.array("h", [int(x * g) for x in izq])
    ld = array.array("h", [int(x * g) for x in der])
    if canales == 1:
        mono = array.array("h", [(a + b) // 2 for a, b in zip(li, ld)])
        out = array.array("h", [0]) * (N * rep)
        for r in range(rep):
            out[r::rep] = mono
    else:
        out = array.array("h", [0]) * (2 * N * rep)
        for r in range(rep):
            out[2 * r::2 * rep] = li
            out[2 * r + 1::2 * rep] = ld
    return out.tobytes()


class Musica:
    """Música de fondo: archivo propio si existe; si no, la generada por código."""

    def __init__(self):
        self.disponible = False
        self.listo = False
        self.sonando = False
        self.silencio = False
        self.sonido = None
        self._sfx = {}
        self.archivo = next((m for m in MUSICAS if os.path.exists(m)), None)
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(*FORMATO_MIXER, allowedchanges=0)
            self.disponible = True
        except pygame.error as e:
            print("[carga] Sin audio:", e)
            return
        if self.archivo:
            self.listo = True
        else:
            threading.Thread(target=self._sintetizar, daemon=True).start()

    def _sintetizar(self):
        try:
            freq, fmt, canales = pygame.mixer.get_init()
            if fmt != -16:
                raise RuntimeError("formato de mixer no soportado")
            self.sonido = pygame.mixer.Sound(buffer=sintetizar_musica(freq, canales))
            self.listo = True
        except Exception as e:  # la carga sigue sin música
            print("[carga] No se pudo generar la música:", e)
            self.disponible = False

    def _volumen(self):
        v = 0.0 if self.silencio else VOLUMEN
        if self.archivo:
            pygame.mixer.music.set_volume(v)
        elif self.sonido:
            self.sonido.set_volume(v)

    def reproducir(self):
        if not (self.disponible and self.listo) or self.sonando:
            return
        try:
            if self.archivo:
                pygame.mixer.music.load(self.archivo)
                pygame.mixer.music.play(-1, fade_ms=600)
            else:
                self.sonido.play(-1, fade_ms=600)
            self._volumen()
            self.sonando = True
        except pygame.error as e:
            print("[carga] No se pudo reproducir:", e)
            self.disponible = False

    def efecto_comer(self, i):
        """Sonido corto ascendente al comerse un fantasma (cada uno suena más agudo)."""
        if not self.disponible or self.silencio or not pygame.mixer.get_init():
            return
        try:
            snd = self._sfx.get(i)
            if snd is None:
                freq, fmt, canales = pygame.mixer.get_init()
                if fmt != -16:
                    return
                n = int(0.3 * freq)
                f0 = 200 * 1.12 ** i
                buf = array.array("h")
                fase = 0.0
                for k in range(n):
                    p = k / n
                    fase += f0 * (1 + 3.5 * p ** 0.7) / freq
                    v = (1.0 if fase % 1.0 < 0.5 else -1.0) * (1 - p) ** 1.5 * min(1.0, k / 80) * 9000
                    buf.extend([int(v)] * canales)
                snd = self._sfx[i] = pygame.mixer.Sound(buffer=buf.tobytes())
                snd.set_volume(0.5)
            snd.play()
        except pygame.error:
            pass

    def alternar_silencio(self):
        self.silencio = not self.silencio
        if self.sonando:
            self._volumen()

    def desvanecer(self, ms):
        if not self.sonando:
            return
        if self.archivo:
            pygame.mixer.music.fadeout(ms)
        else:
            self.sonido.fadeout(ms)


# ---------------------------------------------------------------- Carga en segundo plano
class Cargador(threading.Thread):
    def __init__(self, tareas):
        super().__init__(daemon=True)
        self.tareas = tareas
        self.hechas = 0
        self.actual = tareas[0][0]
        self.error = None
        self.terminado = False

    def run(self):
        try:
            for i, (texto, fn) in enumerate(self.tareas):
                self.actual = texto
                if fn:
                    fn()
                self.hechas = i + 1
        except BaseException as e:  # se reenvía al hilo principal
            self.error = e
        finally:
            self.terminado = True


def _tareas_demo():
    nombres = ("Afinando instrumentos", "Cargando sprites", "Construyendo el laberinto",
               "Sincronizando el ritmo", "Preparando niveles")
    return [(n, (lambda d=random.uniform(0.7, 1.3): time.sleep(d))) for n in nombres]


class Nota:
    """Partícula: nota musical que flota (o confeti que sale disparado)."""
    __slots__ = ("x", "y", "vx", "vy", "surf", "vida", "edad", "amp", "fr", "ph", "drag")

    def __init__(self, x, y, vx, vy, surf, vida, amp=0.0, fr=2.0, drag=0.0):
        self.x, self.y, self.vx, self.vy = x, y, vx, vy
        self.surf, self.vida, self.edad = surf, vida, 0.0
        self.amp, self.fr, self.drag = amp, fr, drag
        self.ph = random.uniform(0, 6.28)

    def actualizar(self, dt):
        self.edad += dt
        if self.drag:
            k = max(0.0, 1 - self.drag * dt)
            self.vx *= k
            self.vy *= k
        self.x += self.vx * dt
        self.y -= self.vy * dt

    def dibujar(self, screen):
        a = min(1.0, self.edad / 0.4, (self.vida - self.edad) / 1.2)
        if a <= 0:
            return
        self.surf.set_alpha(int(255 * a))
        x = self.x + self.amp * math.sin(self.edad * self.fr + self.ph)
        screen.blit(self.surf, self.surf.get_rect(center=(int(x), int(self.y))))


# ---------------------------------------------------------------- Pantalla
class PantallaCarga:
    def __init__(self, screen, tareas=None, continuar_musica=False):
        self.screen = screen
        self.W, self.H = screen.get_size()
        self.u = self.H / 705.0                       # factor de escala según la resolución
        self.tareas = tareas or _tareas_demo()
        self.continuar_musica = continuar_musica
        u, W, H = self.u, self.W, self.H

        fam = "consolas,dejavusansmono,couriernew"
        self.f_estado = pygame.font.SysFont(fam, max(12, int(22 * u)))
        self.f_pct = pygame.font.SysFont(fam, max(16, int(34 * u)), bold=True)
        self.f_tip = pygame.font.SysFont(fam, max(11, int(18 * u)))
        self.f_mini = pygame.font.SysFont(fam, max(10, int(14 * u)))

        self.fondo = self._crear_fondo()
        self.vineta = self._crear_vineta()
        self.sombra = self._crear_sombra()
        self.brillo = self._crear_brillo()
        self.glow = crear_resplandor(int(70 * u), CIAN)
        self.negro = pygame.Surface((W, H))
        self.negro.fill((0, 0, 0))

        # barra de progreso
        self.bx = int(W * 0.09)
        self.bw = W - 2 * self.bx
        self.bh = max(14, int(26 * u))
        self.by = H - int(125 * u)
        self.degradado = pygame.Surface((self.bw, self.bh), pygame.SRCALPHA)
        for px in range(self.bw):
            pygame.draw.line(self.degradado, (*color_arcoiris(px / self.bw), 255), (px, 0), (px, self.bh))

        # personajes propios para la barra (personajes.png, o cazador.png / presa.png)
        self.spr_cazador, self.fantasmas = self._cargar_personajes(int(self.bh * 0.72))
        self.spr_cerrado = (self._cargar_sprite("cazador_cerrado", self.spr_cazador.get_height())
                            if self.spr_cazador else None)     # opcional: cuadro con la boca cerrada
        # versiones "asustadas" de los fantasmas (azul y blanco, para parpadear antes de ser comidos)
        self.fant_azul = [self._asustado(s, (40, 60, 235)) for s in self.fantasmas]
        self.fant_blanco = [self._asustado(s, (235, 235, 255)) for s in self.fantasmas]
        self.comidos = set()
        self.tragando = []       # fantasmas siendo tragados: [indice, x, y, edad]
        self.anillos = []        # ondas expansivas: [edad]
        self.t_comio = 99.0      # segundos desde el último fantasma comido
        self.musica = None
        self.popups = []
        self.consejos = random.sample(CONSEJOS, len(CONSEJOS))

        # partículas
        tam = max(18, int(30 * u))
        self.notas = [crear_nota(c, k, tam) for c in PALETA for k in (0, 1)]
        self.particulas = []
        self.estrellas = [[random.uniform(0, W), random.uniform(0, H), random.uniform(15, 60) * u,
                           random.choice((1, 1, 2, 2, 3)), random.uniform(0, 6.28), random.choice(PALETA)]
                          for _ in range(70)]

        # ecualizador
        self.n_barras = 56
        self.alt = [0.0] * self.n_barras
        self.eq_alto = int(H * 0.26)
        self.eq = pygame.Surface((W, self.eq_alto), pygame.SRCALPHA)

    # ---- recursos
    def _cargar_personajes(self, r):
        """
        Devuelve (cazador, [fantasmas]).
        1) personajes.png: una sola imagen con transparencia; el personaje que está más a la
           izquierda es el cazador y los demás (de izquierda a derecha) son los fantasmas.
        2) Si no existe, cazador.png y presa.png sueltos.
        3) Si tampoco, se usan los personajes dibujados por código (cazador=robot).
        """
        alto = int(r * 2.9)
        for ruta in (os.path.join(CARPETA, ".vscode", "imagenes", "personajes.png"),
                     os.path.join(CARPETA, "personajes.png")):
            if not os.path.exists(ruta):
                continue
            try:
                img = pygame.image.load(ruta).convert_alpha()
                rects = [c.get_bounding_rects()[0]
                         for c in pygame.mask.from_surface(img, 40).connected_components(300)]
                rects.sort(key=lambda q: q.x)
                if len(rects) < 2:
                    print(f"[carga] {ruta}: se esperaban al menos 2 personajes separados.")
                    continue
                esc = alto / rects[0].height

                def preparar(rc, voltear):
                    s = pygame.transform.smoothscale(
                        img.subsurface(rc).copy(), (max(1, int(rc.w * esc)), max(1, int(rc.h * esc))))
                    return pygame.transform.flip(s, True, False) if voltear else s
                # los fantasmas se voltean para que miren hacia donde huyen (derecha)
                return preparar(rects[0], False), [preparar(q, True) for q in rects[1:]]
            except pygame.error as e:
                print(f"[carga] No se pudo leer {ruta}: {e}")
        presa = self._cargar_sprite("presa", int(alto * 0.85))
        return self._cargar_sprite("cazador", alto), ([presa] if presa else [])

    @staticmethod
    def _asustado(spr, color):
        """Silueta de color sólido con el detalle original apenas visible."""
        sil = pygame.mask.from_surface(spr, 40).to_surface(setcolor=(*color, 255), unsetcolor=(0, 0, 0, 0))
        detalle = spr.copy()
        detalle.set_alpha(90)
        sil.blit(detalle, (0, 0))
        return sil

    def _comer(self, x, y, i):
        """Se come un fantasma: lo traga, onda expansiva, puntos, notas y sonido."""
        self.tragando.append([i, x, y, 0.0])
        self.anillos.append([0.0])
        self.t_comio = 0.0
        if self.musica:
            self.musica.efecto_comer(i)
        self.popups.append([x, self.by - 16 * self.u, 0.0, str(200 * 2 ** i)])
        for _ in range(10):
            ang, vel = random.uniform(0, 6.28), random.uniform(80, 260) * self.u
            self.particulas.append(Nota(x, y, math.cos(ang) * vel, math.sin(ang) * vel,
                                        random.choice(self.notas), random.uniform(0.8, 1.4),
                                        amp=3 * self.u, fr=6, drag=2.0))

    def _dibujar_popups(self, dt):
        for p in self.popups:
            p[2] += dt
        self.popups = [p for p in self.popups if p[2] < 1.0]
        for x, y, e, txt in self.popups:
            self._texto(self.f_estado, txt, CIAN, (int(x), int(y - e * 10 * self.u)), "center",
                        alpha=int(255 * (1 - e)))

    def _cargar_sprite(self, nombre, alto):
        """Imagen PNG propia (con transparencia) en .vscode/imagenes/ o junto a este archivo."""
        for ruta in (os.path.join(CARPETA, ".vscode", "imagenes", nombre + ".png"),
                     os.path.join(CARPETA, nombre + ".png")):
            if os.path.exists(ruta):
                try:
                    img = pygame.image.load(ruta).convert_alpha()
                    rects = [c.get_bounding_rects()[0]
                             for c in pygame.mask.from_surface(img, 40).connected_components(20)]
                    if rects:                                   # recorta los márgenes transparentes
                        img = img.subsurface(rects[0].unionall(rects[1:])).copy()
                    esc = alto / img.get_height()
                    return pygame.transform.smoothscale(img, (max(1, int(img.get_width() * esc)), alto))
                except pygame.error as e:
                    print(f"[carga] No se pudo leer {ruta}: {e}")
        return None

    def _crear_fondo(self):
        W, H = self.W, self.H
        img = None
        for ruta in IMAGENES:
            if os.path.exists(ruta):
                try:
                    img = pygame.image.load(ruta).convert()
                    break
                except pygame.error as e:
                    print(f"[carga] No se pudo leer {ruta}: {e}")
        fondo = pygame.Surface((W, H))
        if img is None:
            print("[carga] No se encontró la imagen de fondo; se usa un degradado. Busqué en:")
            for r in IMAGENES:
                print("   ", r)
            for y in range(H):
                k = y / H
                pygame.draw.line(fondo, (int(10 + 40 * k), 8, int(40 + 60 * k)), (0, y), (W, y))
            return fondo
        iw, ih = img.get_size()
        # relleno borroso y oscuro (para cuando la ventana no tiene la misma proporción)
        chico = pygame.transform.smoothscale(img, (max(2, W // 24), max(2, H // 24)))
        fondo.blit(pygame.transform.smoothscale(chico, (W, H)), (0, 0))
        velo = pygame.Surface((W, H))
        velo.set_alpha(110)
        fondo.blit(velo, (0, 0))
        # imagen completa, sin recortar, con los bordes difuminados
        esc = min(W / iw, H / ih)
        tam = (int(iw * esc), int(ih * esc))
        cont = pygame.transform.smoothscale(img, tam).convert_alpha()
        borde = max(2, int(min(tam) * 0.08))
        mascara = pygame.Surface(tam, pygame.SRCALPHA)
        mascara.fill((255, 255, 255, 255))
        for i in range(borde):
            a = int(255 * i / borde)
            if tam[0] < W - 2:
                pygame.draw.line(mascara, (255, 255, 255, a), (i, 0), (i, tam[1]))
                pygame.draw.line(mascara, (255, 255, 255, a), (tam[0] - 1 - i, 0), (tam[0] - 1 - i, tam[1]))
            if tam[1] < H - 2:
                pygame.draw.line(mascara, (255, 255, 255, a), (0, i), (tam[0], i))
                pygame.draw.line(mascara, (255, 255, 255, a), (0, tam[1] - 1 - i), (tam[0], tam[1] - 1 - i))
        cont.blit(mascara, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        fondo.blit(cont, ((W - tam[0]) // 2, (H - tam[1]) // 2))
        return fondo

    def _crear_vineta(self):
        w, h = 80, 60
        s = pygame.Surface((w, h), pygame.SRCALPHA)
        for y in range(h):
            for x in range(w):
                d = math.hypot((x / (w - 1) - 0.5) * 2, (y / (h - 1) - 0.5) * 2) / 1.4142
                s.set_at((x, y), (4, 2, 16, int(min(255, 360 * max(0.0, d - 0.42) ** 1.5))))
        return pygame.transform.smoothscale(s, (self.W, self.H))

    def _crear_sombra(self):
        alto = int(300 * self.u)
        s = pygame.Surface((self.W, alto), pygame.SRCALPHA)
        for y in range(alto):
            pygame.draw.line(s, (5, 3, 20, int(225 * (y / alto) ** 1.4)), (0, y), (self.W, y))
        return s

    def _crear_brillo(self):
        ancho = max(40, int(160 * self.u))
        b = pygame.Surface((ancho, int(self.H * 1.7)))
        for x in range(ancho):
            v = int(70 * math.sin(math.pi * x / ancho) ** 2)
            pygame.draw.line(b, (v, v, v), (x, 0), (x, b.get_height()))
        return pygame.transform.rotate(b, -18)

    # ---- utilidades de dibujo
    def _texto(self, fuente, txt, color, pos, ancla="topleft", alpha=255):
        img = fuente.render(txt, True, color)
        sombra = fuente.render(txt, True, (0, 0, 0))
        if alpha < 255:
            img.set_alpha(alpha)
            sombra.set_alpha(alpha)
        r = img.get_rect(**{ancla: pos})
        off = max(1, int(2 * self.u))
        self.screen.blit(sombra, r.move(off, off))
        self.screen.blit(img, r)

    def _lanzar_notas(self, n):
        for _ in range(n):
            surf = random.choice(self.notas)
            self.particulas.append(Nota(random.uniform(self.W * 0.05, self.W * 0.95), self.H * 0.88,
                                        0, random.uniform(70, 140) * self.u, surf,
                                        random.uniform(4.5, 6.5), amp=random.uniform(15, 40) * self.u,
                                        fr=random.uniform(1.5, 3.0)))

    def _confeti(self):
        for _ in range(44):
            ang = random.uniform(0, math.pi)
            vel = random.uniform(180, 520) * self.u
            self.particulas.append(Nota(self.W / 2, self.H * 0.55, math.cos(ang) * vel * random.choice((-1, 1)),
                                        math.sin(ang) * vel, random.choice(self.notas),
                                        random.uniform(1.8, 2.8), amp=6 * self.u, fr=6, drag=1.4))

    def _dibujar_fondo(self, t, pulso):
        W, H = self.W, self.H
        z = 1.03 + 0.02 * math.sin(t * 0.6) + 0.016 * pulso
        cw, ch = int(W / z), int(H / z)
        cx = W / 2 + 0.012 * W * math.sin(t * 0.4)
        cy = H / 2 + 0.012 * H * math.cos(t * 0.33)
        rect = pygame.Rect(int(cx - cw / 2), int(cy - ch / 2), cw, ch).clamp(self.fondo.get_rect())
        self.screen.blit(pygame.transform.smoothscale(self.fondo.subsurface(rect), (W, H)), (0, 0))
        # tinte de color que cambia lentamente y late con el compás
        c = pygame.Color(0)
        c.hsva = ((t * 25) % 360, 100, 100, 100)
        k = 0.08 + 0.20 * pulso
        self.screen.fill((int(c.r * k), int(c.g * k), int(c.b * k)), special_flags=pygame.BLEND_ADD)
        # barrido de brillo cada ~5.5 s
        fase = (t % 5.5) / 1.8
        if fase < 1:
            x = int(-self.brillo.get_width() + fase * (W + 2 * self.brillo.get_width()))
            self.screen.blit(self.brillo, (x, -int(H * 0.35)), special_flags=pygame.BLEND_ADD)

    def _dibujar_estrellas(self, t, dt):
        for e in self.estrellas:
            e[1] -= e[2] * dt
            if e[1] < -5:
                e[0], e[1] = random.uniform(0, self.W), self.H + 5
            br = 0.4 + 0.6 * abs(math.sin(t * 2 + e[4]))
            col = tuple(int(c * br) for c in e[5])
            pygame.draw.circle(self.screen, col, (int(e[0]), int(e[1])), max(1, int(e[3] * self.u)))

    def _dibujar_eq(self, t, pulso):
        n, W = self.n_barras, self.W
        paso = W / n
        bw = max(2, int(paso * 0.62))
        self.eq.fill((0, 0, 0, 0))
        for i in range(n):
            v = (0.5 + 0.5 * math.sin(t * 2.6 + i * 0.55)) * (0.5 + 0.5 * math.sin(t * 1.3 - i * 0.31 + 1.0))
            obj = 0.12 + 0.55 * v + 0.38 * pulso * (0.4 + 0.6 * ((i * 37) % 11) / 10)
            self.alt[i] += (obj - self.alt[i]) * 0.35
            h = max(2, int(self.alt[i] * self.eq_alto))
            pygame.draw.rect(self.eq, (*color_arcoiris(i / (n - 1)), 120),
                             (int(i * paso + (paso - bw) / 2), self.eq_alto - h, bw, h), border_radius=bw // 2)
        self.screen.blit(self.eq, (0, self.H - self.eq_alto))

    def _dibujar_barra(self, prog, t, pulso, dt=0.0):
        scr, bx, by, bw, bh, u = self.screen, self.bx, self.by, self.bw, self.bh, self.u
        for tr in self.tragando:
            tr[3] += dt
        for an in self.anillos:
            an[0] += dt
        self.tragando = [tr for tr in self.tragando if tr[3] < 0.35]
        self.anillos = [an for an in self.anillos if an[0] < 0.5]
        self.t_comio += dt
        pista = pygame.Rect(bx, by, bw, bh)
        pygame.draw.rect(scr, (16, 14, 44), pista, border_radius=bh // 2)
        relleno = int(bw * prog)
        if relleno > 4:
            capa = self.degradado.subsurface((0, 0, relleno, bh)).copy()
            rayas = pygame.Surface((relleno, bh), pygame.SRCALPHA)
            despl = (t * 45) % 36
            for sx in range(-bh - 36, relleno, 36):
                x0 = sx + despl
                pygame.draw.polygon(rayas, (255, 255, 255, 42),
                                    [(x0, bh), (x0 + bh, 0), (x0 + bh + 14, 0), (x0 + 14, bh)])
            capa.blit(rayas, (0, 0))
            mascara = pygame.Surface((relleno, bh), pygame.SRCALPHA)
            pygame.draw.rect(mascara, (255, 255, 255, 255), (0, 0, relleno, bh), border_radius=bh // 2)
            capa.blit(mascara, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
            scr.blit(capa, (bx, by))
        r = int(bh * 0.72)
        cx = bx + max(r, min(bw - r, relleno))
        cy = by + bh // 2
        # puntos por comer (desaparecen al pasar el robot)
        paso = max(18, int(32 * u))
        for px in range(bx + paso, bx + bw - paso, paso):
            if px > cx + r + 4:
                pygame.draw.circle(scr, AMARILLO, (px, cy), max(2, int(3 * u)))
        gp = bx + bw - int(14 * u)
        if gp > cx + r:
            pygame.draw.circle(scr, AMARILLO, (gp, cy), int(6 * u + 2 * math.sin(t * 6) * u))
        if self.fantasmas:
            # fantasmas repartidos por la barra: el cazador se los come al llegar a ellos
            n = len(self.fantasmas)
            for i, spr in enumerate(self.fantasmas):
                if i in self.comidos:
                    continue
                gx0 = bx + int(bw * (i + 1) / (n + 1))
                # miedo: 0 = cazador lejos, 1 = encima. Al acercarse el fantasma se asusta,
                # se pone azul, parpadea, tiembla y trata de huir hacia la derecha.
                miedo = max(0.0, min(1.0, (5 * r - (gx0 - cx)) / (5 * r)))
                gx = gx0 + int(1.4 * r * miedo)
                gy = cy + int(math.sin(t * (9 + 14 * miedo) + i * 1.7) * (4 + 4 * miedo) * u)
                if cx + r * 1.1 >= gx:
                    self.comidos.add(i)
                    self._comer(max(gx, cx + int(r * 1.2)), gy, i)   # el trago empieza frente a la boca
                    continue
                if miedo > 0.05:
                    parpadeo = miedo > 0.7 and int(t * 10) % 2 == 0
                    spr = (self.fant_blanco if parpadeo else self.fant_azul)[i]
                scr.blit(spr, spr.get_rect(center=(gx, gy)))
        elif prog < 0.93:
            dibujar_presa(scr, int(cx + r * 4.2), int(cy + math.sin(t * 8) * 3 * u), t, int(r * 0.78))
        # fantasmas siendo tragados: giran y se encogen hacia la boca
        ancho_c = self.spr_cazador.get_width() if self.spr_cazador else 2 * r
        boca = (cx + int(ancho_c * 0.22), cy)
        for i, x0, y0, edad in self.tragando:
            p = min(1.0, edad / 0.35)
            e = p * p
            base = self.fant_blanco[i] if p > 0.5 else self.fant_azul[i]
            img = pygame.transform.rotozoom(base, 540 * p, max(0.05, 1 - 0.9 * p))
            scr.blit(img, img.get_rect(center=(int(x0 + (boca[0] - x0) * e), int(y0 + (boca[1] - y0) * e))))
        # onda expansiva al comer
        for (edad,) in self.anillos:
            e = edad / 0.5
            rad = int(r * (0.8 + 3.2 * e))
            col = tuple(int(c * (1 - e)) for c in AMARILLO)
            anillo = pygame.Surface((2 * rad + 8, 2 * rad + 8))
            pygame.draw.circle(anillo, col, (rad + 4, rad + 4), rad, max(2, int(5 * u * (1 - e))))
            scr.blit(anillo, anillo.get_rect(center=boca), special_flags=pygame.BLEND_ADD)
        scr.blit(self.glow, self.glow.get_rect(center=(cx, cy)), special_flags=pygame.BLEND_ADD)
        if self.spr_cazador:
            trago = max(0.0, 1 - self.t_comio / 0.28)           # se "infla" al tragar
            if self.spr_cerrado:                                  # boca abierta / cerrada
                base, k = (self.spr_cerrado if int(t * 8) % 2 else self.spr_cazador), 0.0
            else:                                                 # sin cuadro cerrado: estira y comprime
                base, k = self.spr_cazador, 0.5 + 0.5 * math.sin(t * 10)
            w, h = base.get_size()
            s = pygame.transform.smoothscale(base, (int(w * (1 + 0.06 * k + 0.22 * trago)),
                                                    int(h * (1 - 0.08 * k + 0.22 * trago))))
            scr.blit(s, s.get_rect(center=(cx, cy)))
        else:
            dibujar_robot(scr, cx, cy, t, r)
        pygame.draw.rect(scr, mezclar(CIAN, BLANCO, 0.6 * pulso), pista, 2, border_radius=bh // 2)

    # ---- bucle principal
    def _salir(self):
        pygame.quit()
        sys.exit()

    def run(self):
        scr, W, H, u = self.screen, self.W, self.H, self.u
        reloj = pygame.time.Clock()
        cargador = Cargador(self.tareas)
        cargador.start()
        musica = self.musica = Musica()
        total = max(1, len(self.tareas))
        inicio = time.perf_counter()
        mostrado, hechas_prev, t_tarea = 0.0, 0, inicio
        ultimo_beat, flash = -1, 0.0
        t_listo = t_salida = None
        estado_prev, t_estado = "", inicio
        ms_salida = FADE_OUT

        while True:
            dt = reloj.tick(FPS) / 1000.0
            ahora = time.perf_counter()
            t = ahora - inicio
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    self._salir()
                if e.type == pygame.KEYDOWN:
                    if e.key == pygame.K_ESCAPE:
                        self._salir()
                    elif e.key == pygame.K_m:
                        musica.alternar_silencio()

            if cargador.error is not None:
                musica.desvanecer(200)
                raise cargador.error

            # --- progreso (avanza suave y "se arrastra" mientras corre cada tarea)
            if cargador.hechas != hechas_prev:
                hechas_prev, t_tarea = cargador.hechas, ahora
            creep = 0.0 if cargador.terminado else 0.8 * (1 - math.exp(-(ahora - t_tarea) / 1.6))
            objetivo = 1.0 if cargador.terminado else min(0.99, (cargador.hechas + creep) / total)
            mostrado += (objetivo - mostrado) * min(1.0, dt * 5)
            if t_listo is None and cargador.terminado and mostrado > 0.995 and t >= DURACION_MINIMA:
                t_listo, flash = ahora, 1.0
                self._confeti()
            if t_listo is not None:
                mostrado = 1.0

            # --- compás: los efectos siguen el ritmo; la música arranca justo en un tiempo
            tb = t * BPM / 60.0
            idx = int(tb)
            pulso = (1 - (tb - idx)) ** 3
            if idx != ultimo_beat:
                ultimo_beat = idx
                self._lanzar_notas(2 if idx % 2 == 0 else 1)
                if not musica.sonando:
                    musica.reproducir()
            flash = max(0.0, flash - dt * 2.2)

            # --- dibujo
            self._dibujar_fondo(t, pulso)
            self._dibujar_estrellas(t, dt)
            self.particulas = [p for p in self.particulas if p.edad < p.vida]
            for p in self.particulas:
                p.actualizar(dt)
                p.dibujar(scr)
            scr.blit(self.vineta, (0, 0))
            scr.blit(self.sombra, (0, H - self.sombra.get_height()))
            self._dibujar_eq(t, pulso)
            self._dibujar_barra(mostrado, t, pulso, dt)
            self._dibujar_popups(dt)

            estado = "¡LISTO!" if t_listo else cargador.actual
            if estado != estado_prev:
                estado_prev, t_estado = estado, ahora
            a = min(1.0, (ahora - t_estado) / 0.25)
            txt = estado if t_listo else estado.rstrip(".") + "." * (int(t * 3) % 4)
            self._texto(self.f_estado, txt, AMARILLO if t_listo else BLANCO,
                        (self.bx, self.by - int(46 * u) + int((1 - a) * 10 * u)), alpha=int(255 * a))
            self._texto(self.f_pct, f"{int(mostrado * 100)}%", mezclar(AMARILLO, BLANCO, 0.5 * pulso),
                        (self.bx + self.bw, self.by - int(54 * u)), "topright")
            per = 3.4
            fase = t % per
            self._texto(self.f_tip, self.consejos[int(t / per) % len(self.consejos)], (180, 180, 220),
                        (W // 2, H - int(44 * u)), "center",
                        alpha=int(255 * max(0.0, min(1.0, fase / 0.5, (per - fase) / 0.5))))
            if musica.disponible:
                self._texto(self.f_mini, "[M] Música: " + ("OFF" if musica.silencio else "ON"),
                            (140, 140, 190), (W - int(18 * u), int(14 * u)), "topright", alpha=200)

            # --- fundidos
            alfa = 0
            if t < FADE_IN:
                alfa = int(255 * (1 - t / FADE_IN))
            if flash > 0:
                v = int(150 * flash)
                scr.fill((v, v, v), special_flags=pygame.BLEND_ADD)
            if t_listo is not None and t_salida is None and ahora - t_listo > 0.9:
                t_salida = ahora
                if not self.continuar_musica:
                    musica.desvanecer(int(ms_salida * 1000))
            if t_salida is not None:
                p = (ahora - t_salida) / ms_salida
                if p >= 1:
                    return
                alfa = max(alfa, int(255 * p))
            if alfa > 0:
                self.negro.set_alpha(alfa)
                scr.blit(self.negro, (0, 0))

            pygame.display.flip()


def run_loading(screen, tareas=None, continuar_musica=False):
    """
    Muestra la pantalla de carga hasta que terminen todas las tareas.

    tareas           lista de (texto, funcion). Si es None se simula una carga de prueba.
    continuar_musica False: la música se apaga con fundido al terminar.
                     True : sigue sonando (útil si el menú no tiene su propia música).
    """
    PantallaCarga(screen, tareas, continuar_musica).run()


def main():
    pygame.init()
    screen = pygame.display.set_mode((1366, 768))
    pygame.display.set_caption("Cargando...")
    run_loading(screen)          # sin tareas: carga simulada de prueba
    print("Carga terminada: inicia el juego aquí.")
    pygame.quit()


if __name__ == "__main__":
    main()