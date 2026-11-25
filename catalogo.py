"""
catalogo.py  -  la BIBLIOTECA de PAC-RITMO

  * Metadatos (título, artista, álbum, género, año) y PORTADA del álbum leídos de las etiquetas ID3
    del mp3 (lector propio, sin librerías extra). Si faltan, se deduce del nombre del archivo.
  * La IA evalúa cada canción al escanearla: DIFICULTAD (0-10), CATEGORÍA y, solo en algunas
    canciones, un TEMA ESPECIAL (mapa y skins propios).
  * Caché del análisis en disco (la segunda vez es instantáneo) y escáner en segundo plano.
  * Biblioteca (JSON), orden por artista / dificultad y búsqueda.
"""
import colorsys
import hashlib
import io
import json
import os
import pickle
import re
import struct
import threading
import time
import unicodedata
import zlib

import numpy as np
import pygame

from sonido import analyze_song

BASE = os.path.dirname(os.path.abspath(__file__))
LIB_FILE = os.path.join(BASE, "biblioteca_ritmo.json")
SONG_DIR = os.path.join(BASE, "canciones")
CACHE_DIR = os.path.join(BASE, "cache")
ANALYSIS_VERSION = 3
DESCONOCIDO = "Artista desconocido"
CATEGORIAS = ["CHILL", "GROOVE", "ENERGÍA", "INTENSA", "FRENÉTICA"]
TEMAS = ("volcán", "océano", "galaxia")


# =================================================================== TEXTO
def norm(s):
    """Minúsculas y sin acentos (para ordenar y buscar)."""
    s = unicodedata.normalize("NFKD", str(s)).lower()
    return "".join(c for c in s if not unicodedata.combining(c))


_RUIDO = re.compile(
    r"\s*[\(\[\{【\（][^\)\]\}】\）]*(official|audio|video|lyric|visuali[sz]er|monstercat|release|\bhd\b|\b4k\b|\bpv\b|"
    r"original song|vocaloid|from album|remaster|free download|影絵|ＰＶ)[^\)\]\}】\）]*[\)\]\}】\）]", re.I)


def limpiar(s):
    """Quita el ruido típico de los nombres de YouTube: (Official Audio), [Monstercat Release], 【PV】..."""
    s = _RUIDO.sub("", s or "")
    s = re.sub(r"^\s*\[[^\]]{1,14}\]\s*", "", s)                      # [Electro] al principio
    s = re.sub(r"^\s*【[^】]*】\s*|\s*【[^】]*】\s*$", " ", s)             # 【東方】 al principio o al final
    s = re.sub(r"\s*[ＰP][ＶV]\s*$", "", s)                             # «PV» suelto al final
    return re.sub(r"\s{2,}", " ", s).strip(" -_")


# =================================================================== ETIQUETAS ID3 + PORTADA
def _syncsafe(b):
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _texto(b):
    if not b:
        return ""
    enc, body = b[0], b[1:]
    try:
        t = body.decode({0: "latin-1", 1: "utf-16", 2: "utf-16-be"}.get(enc, "utf-8"))
    except Exception:
        t = body.decode("latin-1", "ignore")
    return ", ".join(p.strip() for p in t.replace("\ufeff", "").split("\x00") if p.strip())


def _apic(d, v22):
    """Devuelve los bytes de la imagen dentro de un cuadro APIC (ID3v2.3/2.4) o PIC (ID3v2.2)."""
    enc, i = d[0], 1
    if v22:
        i += 3
    else:
        j = d.find(b"\x00", i)
        if j < 0:
            return None
        i = j + 1
    i += 1                                                              # tipo de imagen
    if enc in (1, 2):                                                   # descripción UTF-16
        while i + 1 < len(d) and not (d[i] == 0 and d[i + 1] == 0):
            i += 2
        i += 2
    else:
        j = d.find(b"\x00", i)
        i = (j if j >= 0 else len(d)) + 1
    img = d[i:]
    if img[:2] == b"\xff\xd8" or img[:4] == b"\x89PNG":
        return img
    for magic in (b"\xff\xd8\xff", b"\x89PNG"):                         # por si la descripción venía mal
        k = d.find(magic, max(0, i - 8), i + 128)
        if k >= 0:
            return d[k:]
    return None


_IDS = {"TIT2": "title", "TT2": "title", "TPE1": "artist", "TP1": "artist", "TALB": "album", "TAL": "album",
        "TCON": "genre", "TCO": "genre", "TYER": "year", "TYE": "year", "TDRC": "year"}


def leer_tags(path, con_portada=True):
    """Lee título, artista, álbum, género, año y portada (bytes) del mp3. Nunca lanza excepciones."""
    t = dict(title="", artist="", album="", genre="", year="", cover=None)
    try:
        with open(path, "rb") as f:
            h = f.read(10)
            if h[:3] == b"ID3" and len(h) == 10:
                ver, flags, size = h[3], h[5], _syncsafe(h[6:10])
                data = f.read(min(size, 16 * 1024 * 1024))
                if flags & 0x80:                                         # des-sincronización global
                    data = data.replace(b"\xff\x00", b"\xff")
                pos = 0
                if flags & 0x40 and ver >= 3 and len(data) >= 4:         # cabecera extendida
                    pos = (_syncsafe(data[:4]) if ver == 4 else struct.unpack(">I", data[:4])[0] + 4)
                v22 = ver == 2
                hdr = 6 if v22 else 10
                while pos + hdr <= len(data):
                    if v22:
                        fid, fs, fl = data[pos:pos + 3], int.from_bytes(data[pos + 3:pos + 6], "big"), b"\x00\x00"
                    else:
                        fid = data[pos:pos + 4]
                        fs = _syncsafe(data[pos + 4:pos + 8]) if ver == 4 else struct.unpack(">I", data[pos + 4:pos + 8])[0]
                        fl = data[pos + 8:pos + 10]
                    if not fid.strip(b"\x00") or fs <= 0 or fs > len(data):
                        break
                    body = data[pos + hdr:pos + hdr + fs]
                    pos += hdr + fs
                    if ver == 4 and fl[1] & 0x01:                        # indicador de longitud de datos
                        body = body[4:]
                    if ver == 4 and fl[1] & 0x02:
                        body = body.replace(b"\xff\x00", b"\xff")
                    if ver >= 3 and (fl[1] & 0x0C):                      # comprimido / cifrado: se ignora
                        continue
                    fid = fid.decode("latin-1")
                    if fid in _IDS and not t[_IDS[fid]]:
                        t[_IDS[fid]] = _texto(body)
                    elif fid in ("APIC", "PIC") and con_portada and t["cover"] is None:
                        t["cover"] = _apic(body, v22)
            f.seek(0, 2)
            if f.tell() >= 128:                                          # ID3v1 al final del archivo
                f.seek(-128, 2)
                v1 = f.read(128)
                if v1[:3] == b"TAG":
                    dec = lambda b: b.split(b"\x00")[0].decode("latin-1", "ignore").strip()
                    for k, sl in (("title", slice(3, 33)), ("artist", slice(33, 63)), ("album", slice(63, 93)), ("year", slice(93, 97))):
                        if not t[k]:
                            t[k] = dec(v1[sl])
    except Exception:
        pass
    t["year"] = re.sub(r"\D", "", t["year"])[:4]
    if re.fullmatch(r"\(?\d+\)?", t["genre"] or ""):
        t["genre"] = ""
    return completar(t, path)


def completar(t, path):
    """Rellena lo que falte con el nombre del archivo («Artista - Título») y limpia los textos."""
    base = os.path.splitext(os.path.basename(path))[0]
    partes = [p.strip() for p in re.split(r"\s+[-–—]\s+", base) if p.strip()]
    if len(partes) >= 2:
        # «Título - Artista - Álbum» (típico de bandas sonoras) frente a «Artista - Título [- extra]»
        ost = len(partes) >= 3 and re.search(r"soundtrack|\bost\b|original|motion picture|score", partes[2], re.I)
        a, ti = (partes[1], partes[0]) if ost else (partes[0], partes[1])
        if not t["artist"]:
            t["artist"] = a
        if not t["title"]:
            t["title"] = ti
        if len(partes) >= 3 and not t["album"]:
            t["album"] = partes[2]
    t["title"] = limpiar(t["title"] or base) or base
    t["artist"] = limpiar(t["artist"]) or DESCONOCIDO
    t["album"] = limpiar(t["album"])
    return t


def desconocido(e):
    return e.get("artist", DESCONOCIDO) == DESCONOCIDO


# =================================================================== PORTADAS
def decodificar_portada(data, lado=512):
    """Bytes de imagen -> Surface cuadrada (recorte central). None si no se puede leer."""
    if not data:
        return None
    try:
        img = pygame.image.load(io.BytesIO(data))
        w, h = img.get_size()
        m = min(w, h)
        img = img.subsurface(((w - m) // 2, (h - m) // 2, m, m)).copy()
        return pygame.transform.smoothscale(img, (lado, lado)) if m > lado else img
    except Exception:
        return None


def redondear(surf, lado, radio):
    """Copia de la imagen a lado x lado con esquinas redondeadas (requiere pantalla creada)."""
    s = pygame.transform.smoothscale(surf, (lado, lado)).convert_alpha()
    mask = pygame.Surface((lado, lado), pygame.SRCALPHA)
    pygame.draw.rect(mask, (255, 255, 255, 255), (0, 0, lado, lado), border_radius=radio)
    s.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    return s


def color_dominante(surf):
    """Color vivo representativo de la portada (para el acento de la interfaz)."""
    r, g, b = pygame.transform.average_color(pygame.transform.smoothscale(surf, (8, 8)))[:3]
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    r, g, b = colorsys.hsv_to_rgb(h, max(s, 0.45), max(v, 0.82))
    return int(r * 255), int(g * 255), int(b * 255)


def portada_generada(titulo, artista, lado=512):
    """Portada de relleno cuando el mp3 no trae imagen: degradado y figuras según el nombre."""
    h = zlib.crc32((artista + "|" + titulo).encode("utf-8"))
    hue = (h % 360) / 360.0
    s = pygame.Surface((lado, lado))
    for y in range(lado):
        k = y / lado
        c = colorsys.hsv_to_rgb((hue + 0.12 * k) % 1.0, 0.65, 0.30 + 0.55 * (1 - k))
        pygame.draw.line(s, (int(c[0] * 255), int(c[1] * 255), int(c[2] * 255)), (0, y), (lado, y))
    over = pygame.Surface((lado, lado), pygame.SRCALPHA)
    for i in range(4):
        r = int(lado * (0.12 + ((h >> (i * 5)) & 15) / 70))
        cx, cy = ((h >> (i * 4 + 3)) % lado), ((h >> (i * 4 + 9)) % lado)
        c = colorsys.hsv_to_rgb((hue + 0.1 * (i + 1)) % 1.0, 0.5, 1.0)
        pygame.draw.circle(over, (int(c[0] * 255), int(c[1] * 255), int(c[2] * 255), 55), (cx, cy), r)
    for i in range(0, lado, max(8, lado // 24)):
        pygame.draw.line(over, (255, 255, 255, 14), (0, i), (lado, i - lado // 3), 2)
    s.blit(over, (0, 0))
    try:
        f = pygame.font.SysFont("segoeui,arial,dejavusans", int(lado * 0.46), bold=True)
        letra = (titulo.strip()[:1] or "♪").upper()
        img = f.render(letra, True, (255, 255, 255))
        img.set_alpha(150)
        s.blit(img, img.get_rect(center=(lado // 2, int(lado * 0.52))))
    except Exception:
        pass
    return s


# =================================================================== IA: DIFICULTAD, CATEGORÍA Y TEMA ESPECIAL
def evaluar(song):
    """La IA puntúa la dificultad (0-10) a partir del análisis y asigna una categoría.
    Pesos: tempo 25 %, movida media 35 %, partes muy movidas 20 %, densidad de golpes 12 %, variedad 8 %."""
    busy = np.asarray(song.bar_busy, float) if len(song.bar_busy) else np.array([0.3])
    tempo = min(1.0, max(0.0, (song.bpm - 70) / 110.0))
    mb = float(busy.mean())
    surge = sum(b - a for a, b in song.surges) / max(1.0, song.duration)
    rate = len(song.onsets) / max(1.0, song.duration)
    var = float(busy.std())
    d = 10 * (0.25 * tempo + 0.35 * mb + 0.20 * min(1.0, surge / 0.35) + 0.12 * min(1.0, rate / 5.0) + 0.08 * min(1.0, var * 2.5))
    d = round(max(0.5, min(10.0, d)), 1)
    i = 0 if d < 2.5 else 1 if d < 4.5 else 2 if d < 6.5 else 3 if d < 8.0 else 4
    return dict(diff=d, cat=CATEGORIAS[i], stars=i + 1, surges=len(song.surges), busy=round(mb, 2))


def elegir_tema(song, path):
    """La IA decide si esta canción merece un TEMA ESPECIAL (mapa y skins propios): solo cuando su
    carácter encaja claramente con uno y, además, no en todas (filtro estable por canción)."""
    f = song.feats
    busy = np.asarray(song.bar_busy, float) if len(song.bar_busy) else np.array([0.3])
    mb, var = float(busy.mean()), float(busy.std())
    tempo = min(1.0, max(0.0, (song.bpm - 70) / 110.0))
    afin = {
        "volcán": 0.5 * f["bass"] + 0.5 * mb + (0.12 if song.bpm >= 110 else 0.0),
        "océano": 0.6 * (1 - mb) + 0.25 * (1.0 if song.bpm <= 105 else 0.0) + 0.15 * (1 - f["high"]),
        "galaxia": 0.45 * f["high"] + 0.30 * tempo + 0.25 * min(1.0, var * 3),
    }
    mejor = max(afin, key=afin.get)
    pasa = zlib.crc32(os.path.basename(path).encode("utf-8")) % 100 < 55
    return mejor if (afin[mejor] >= 0.62 and pasa) else ""


def aplicar_meta(e, song):
    """Guarda en la ficha de la canción lo que la IA descubrió al escanearla."""
    ev = evaluar(song)
    e.update(bpm=round(song.bpm, 1), dur=round(song.duration, 1), diff=ev["diff"], cat=ev["cat"], stars=ev["stars"],
             surges=ev["surges"], tema=elegir_tema(song, e["path"]), curva=[round(float(v), 2) for v in song.curve[::5]],
             scan_ver=ANALYSIS_VERSION)
    e.pop("scan_error", None)


# =================================================================== CACHÉ DEL ANÁLISIS
def _clave(path):
    st = os.stat(path)
    return hashlib.sha1(f"{path}|{st.st_size}|{int(st.st_mtime)}|{ANALYSIS_VERSION}".encode("utf-8")).hexdigest()[:24]


def cargar_cache(path):
    try:
        with open(os.path.join(CACHE_DIR, _clave(path) + ".pkl"), "rb") as f:
            return pickle.load(f)                                       # solo archivos que escribió el propio juego
    except Exception:
        return None


def guardar_cache(path, song):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = os.path.join(CACHE_DIR, _clave(path) + ".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(song, f, protocol=4)
        os.replace(tmp, os.path.join(CACHE_DIR, _clave(path) + ".pkl"))
    except Exception:
        pass


def analizar_con_cache(path, progreso=None):
    song = cargar_cache(path)
    if song is None:
        song = analyze_song(path, progreso)
        guardar_cache(path, song)
    return song


# =================================================================== BIBLIOTECA
def _ficha(path):
    t = leer_tags(path, con_portada=False)
    return dict(path=path, title=t["title"], artist=t["artist"], album=t["album"], genre=t["genre"], year=t["year"],
                best=0, grade="")


def cargar_biblioteca():
    lib = dict(canciones=[], latency=0.0, profile=dict(skill=0.5, games=0))
    raw = {}
    try:
        with open(LIB_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        pass
    vistos = set()

    def meter(e):
        if isinstance(e, dict) and e.get("path") and e["path"] not in vistos:
            vistos.add(e["path"])
            lib["canciones"].append(e)

    for valor in raw.values():                                           # migra cualquier lista/ficha de versiones antiguas
        if isinstance(valor, list):
            for e in valor:
                meter(e)
        else:
            meter(valor)
    lib["latency"] = float(raw.get("latency", 0.0))
    lib["profile"] = raw.get("profile", lib["profile"])
    os.makedirs(SONG_DIR, exist_ok=True)
    for raiz, _, archivos in os.walk(SONG_DIR):
        for fn in sorted(archivos):
            if fn.lower().endswith(".mp3"):
                p = os.path.join(raiz, fn)
                if p not in vistos:
                    meter(_ficha(p))
    lib["canciones"] = [e for e in lib["canciones"] if os.path.exists(e["path"])]
    for e in lib["canciones"]:
        if "artist" not in e:                                            # ficha antigua: añade las etiquetas
            e.update({k: v for k, v in _ficha(e["path"]).items() if k not in ("path", "best", "grade")})
        if e.get("scan_ver") != ANALYSIS_VERSION:
            for k in ("bpm", "dur", "diff", "cat", "stars", "surges", "tema", "curva"):
                e.pop(k, None)
    return lib


def guardar_biblioteca(lib):
    try:
        tmp = LIB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lib, f, ensure_ascii=False, indent=1)
        os.replace(tmp, LIB_FILE)
    except Exception:
        pass


def anadir_cancion(lib, path):
    """Añade un mp3. Devuelve un mensaje para la interfaz."""
    if not path.lower().endswith(".mp3"):
        return "Solo se aceptan archivos .mp3"
    if any(e["path"] == path for e in lib["canciones"]):
        return "Esa canción ya está en la biblioteca"
    lib["canciones"].append(_ficha(path))
    guardar_biblioteca(lib)
    return "Canción añadida: la IA la está escaneando"


def escaneada(e):
    return e.get("scan_ver") == ANALYSIS_VERSION and "diff" in e


# =================================================================== ESCÁNER EN SEGUNDO PLANO
class Escaner(threading.Thread):
    """Analiza una por una las canciones sin escanear mientras no estés jugando."""

    def __init__(self, lib):
        super().__init__(daemon=True)
        self.lib, self.cola, self.lock = lib, [], threading.Lock()
        self.pausa, self.version, self.hechas, self.total, self.actual = False, 0, 0, 0, ""
        self.enabled = True

    def pendientes(self):
        with self.lock:
            for e in self.lib["canciones"]:
                if not escaneada(e) and not e.get("scan_error") and e not in self.cola:
                    self.cola.append(e)
                    self.total += 1

    def prioridad(self, e):
        with self.lock:
            if e in self.cola:
                self.cola.remove(e)
                self.cola.insert(0, e)

    def run(self):
        sin_guardar = 0
        while True:
            if self.pausa or not self.enabled:
                time.sleep(0.25)
                continue
            self.pendientes()
            with self.lock:
                e = self.cola.pop(0) if self.cola else None
            if e is None:
                self.actual = ""
                if sin_guardar:
                    guardar_biblioteca(self.lib)
                    sin_guardar = 0
                self.hechas = self.total = 0
                time.sleep(0.4)
                continue
            self.actual = e.get("title", "")
            try:
                aplicar_meta(e, analizar_con_cache(e["path"]))
            except Exception as ex:
                e["scan_error"] = str(ex)[:80]
            self.hechas += 1
            self.version += 1
            sin_guardar += 1
            if sin_guardar >= 4:
                guardar_biblioteca(self.lib)
                sin_guardar = 0


# =================================================================== VISTA: ORDEN, AGRUPACIÓN Y BÚSQUEDA
ORDENES = ("ARTISTA", "DIFICULTAD")


def construir_vista(canciones, orden="ARTISTA", consulta="", categoria=None):
    """Devuelve (filas, canciones_en_orden). Una fila es ('hdr', texto, n) o ('song', ficha)."""
    q = norm(consulta).strip()
    lista = []
    for e in canciones:
        if categoria and e.get("cat") != categoria:
            continue
        if q and q not in norm(" ".join((e.get("artist", ""), e.get("title", ""), e.get("album", ""),
                                         e.get("cat", ""), e.get("genre", ""), e.get("tema", "")))):
            continue
        lista.append(e)
    if orden == "DIFICULTAD":
        lista.sort(key=lambda e: (e.get("diff", 99.0), norm(e.get("artist", "")), norm(e.get("title", ""))))
        grupo = lambda e: e.get("cat") or "SIN ESCANEAR"
    else:
        lista.sort(key=lambda e: (1 if desconocido(e) else 0, norm(e.get("artist", "")), norm(e.get("album", "")), norm(e.get("title", ""))))
        grupo = lambda e: e.get("artist", DESCONOCIDO)
    cuenta = {}
    for e in lista:
        cuenta[norm(grupo(e))] = cuenta.get(norm(grupo(e)), 0) + 1
    filas, ultimo = [], None
    for e in lista:
        g = grupo(e)
        if norm(g) != ultimo:
            filas.append(("hdr", g, cuenta[norm(g)]))
            ultimo = norm(g)
        filas.append(("song", e))
    return filas, lista
