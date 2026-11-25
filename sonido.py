"""
sonido.py  -  audio de PAC-RITMO

  * analyze_song():  decodifica un .mp3 con pygame y calcula BPM, rejilla de beats, golpes separados
                     (kick/snare/hi-hat), espectro de 32 bandas, "movida" por compás, secciones, surges y drops.
  * Efectos y música de menú sintetizados (no hay archivos de audio incluidos).
  * Audio: reproduce el mp3 del usuario y da su posición exacta para sincronizar el juego.
  * VoiceAI: la voz de la IA (pyttsx3, en otro hilo).
"""
import bisect
import math
import queue
import random
import threading

import numpy as np
import pygame

SR = 44100


# =================================================================== CANCIÓN / ANÁLISIS
class Song:
    def __init__(self):
        self.path, self.title, self.builtin = None, "Tutorial", True
        self.duration, self.bpm = 1e9, 128.0
        self.spb, self.offset = 60.0 / 128.0, 0.0
        self.onsets, self.sections, self.drops = [], [(0.0, 1)], []
        self.feats = dict(bass=0.4, high=0.2, rate=2.0)
        self.bass = self.mid = self.high = self.rms = None
        self.frr = 1.0
        self.curve = []
        self.onset_t, self.onset_s = [], []
        self.kick_t, self.kick_s, self.snare_t, self.snare_s, self.hat_t, self.hat_s = [], [], [], [], [], []
        self.segments, self.surges = [], []      # estructura que detecta el análisis
        self.bar_busy, self.spec = None, None    # "movida" por compás y espectro de 32 bandas

    @staticmethod
    def _between(ts, ss, t0, t1):
        i, j = bisect.bisect_left(ts, t0), bisect.bisect_left(ts, t1)
        return max(ss[i:j], default=0.0)

    def onset_between(self, t0, t1):
        """Fuerza (0..1) del golpe más fuerte (cualquier banda) entre t0 y t1."""
        return self._between(self.onset_t, self.onset_s, t0, t1)

    def kick_between(self, t0, t1):
        return self._between(self.kick_t, self.kick_s, t0, t1)

    def snare_between(self, t0, t1):
        return self._between(self.snare_t, self.snare_s, t0, t1)

    def hat_between(self, t0, t1):
        return self._between(self.hat_t, self.hat_s, t0, t1)

    def _at(self, a, t):
        i = min(max(int(t * self.frr), 0), len(a) - 1)
        return float(a[i])

    def band_at(self, band, t):
        """Energía 0..1 de una banda (0 graves, 1 medios, 2 agudos) en el instante t."""
        if self.builtin:
            return (1 - (((t - self.offset) / self.spb) % 1.0)) ** 3 * (0.8 - 0.2 * band)
        return self._at((self.bass, self.mid, self.high)[band], t)

    def bass_at(self, t):
        return self.band_at(0, t)

    def energy_at(self, t):
        return 0.5 if self.builtin else self._at(self.rms, t)

    def busy_at(self, t):
        """Qué tan MOVIDA está la canción (0..1), interpolada entre compases."""
        if self.bar_busy is None:
            return 0.3
        x = (t - self.offset) / (4 * self.spb) - 0.5
        i = int(math.floor(x))
        n = len(self.bar_busy)
        a, b = self.bar_busy[min(max(i, 0), n - 1)], self.bar_busy[min(max(i + 1, 0), n - 1)]
        return float(a + (b - a) * (x - i))

    def spec_at(self, t):
        """32 bandas de espectro (0..1) para visualizadores."""
        if self.spec is None:
            pulse = (1 - (((t - self.offset) / self.spb) % 1.0)) ** 3
            return [min(1.0, max(0.0, pulse * (0.9 - j * 0.02) + 0.12 + 0.10 * math.sin(t * 3 + j))) for j in range(32)]
        return self.spec[min(max(int(t * self.frr), 0), len(self.spec) - 1)]

    def level_at(self, t):
        i = bisect.bisect_right([s[0] for s in self.sections], t) - 1
        return self.sections[max(0, i)][1]

    def drop_between(self, t0, t1):
        return any(t0 <= d < t1 for d in self.drops)

    def segment_idx(self, t):
        """Índice de la sección (por cambios reales de la música) en el instante t."""
        if not self.segments:
            return 0
        return max(0, bisect.bisect_right([sg["t0"] for sg in self.segments], t) - 1)

    def segment_at(self, t):
        """Rasgos de la sección actual: level, busy, bright (agudos), bassr (graves), energy."""
        if not self.segments:
            return dict(level=1, busy=0.3, bright=0.33, bassr=0.4, energy=0.5)
        return self.segments[self.segment_idx(t)]

    def in_surge(self, t):
        """¿Estamos en una parte MUY MOVIDA de la canción?"""
        return any(a <= t < b for a, b in self.surges)


def builtin_song(bpm=128.0):
    s = Song()
    s.bpm, s.spb = bpm, 60.0 / bpm
    return s


def step_sub(spb):
    """Pasos del jugador por beat: el que deje cada paso más cerca de 0.24 s."""
    return min((1, 2, 4), key=lambda k: abs(spb / k - 0.24))


def _smooth(a, k):
    k = max(1, int(k))
    return np.convolve(a, np.ones(k) / k, mode="same")


def _smooth3(a):
    if len(a) < 3:
        return np.asarray(a, dtype=float)
    p = np.pad(a, 1, mode="edge")
    return 0.25 * p[:-2] + 0.5 * p[1:-1] + 0.25 * p[2:]


def _norm01(a):
    return np.clip(a / (np.percentile(a, 98) + 1e-9), 0, 1)


def analyze_song(path, progress=None, max_seconds=600):
    """Analiza el .mp3 con pygame + numpy y devuelve un Song con:
       tempo y rejilla de beats · golpes separados (kick / snare / hi-hat) · espectro de 32 bandas ·
       'movida' por compás · secciones por cambios reales de la música · surges (partes muy movidas) · drops."""
    pr = progress or (lambda p: None)
    pr(0.02)
    snd = pygame.mixer.Sound(path)
    freq = pygame.mixer.get_init()[0]
    arr = pygame.sndarray.array(snd)
    del snd
    x = arr.astype(np.float32)
    del arr
    if x.ndim == 2:
        x = x.mean(axis=1)
    x = x[: int(max_seconds * freq)]
    d = max(1, freq // 22050)
    x = x[: len(x) // d * d].reshape(-1, d).mean(axis=1) / 32768.0
    sr = freq // d
    dur = len(x) / sr
    if dur < 8:
        raise ValueError("La canción es muy corta (mínimo 8 s)")
    pr(0.08)

    hop, nfft = 512, 1024
    T = (len(x) - nfft) // hop + 1
    win = np.hanning(nfft).astype(np.float32)
    fr = np.fft.rfftfreq(nfft, 1.0 / sr)
    mb, mm, mh, mall = (fr >= 20) & (fr < 150), (fr >= 150) & (fr < 2000), (fr >= 2000) & (fr < 8000), fr < 8000
    ed = np.geomspace(40, 10000, 33) / (sr / nfft)
    edges = [max(1, int(round(ed[0])))]
    for v in ed[1:]:
        edges.append(min(nfft // 2, max(edges[-1] + 1, int(round(v)))))
    spec = np.zeros((T, 32), np.float32)
    bass, mid, high, rms, flux, fluxb, fluxm, fluxh = (np.zeros(T, np.float32) for _ in range(8))
    prev = None
    CH = 1200
    for s in range(0, T, CH):
        e = min(T, s + CH)
        idx = np.arange(s, e)[:, None] * hop + np.arange(nfft)[None, :]
        seg = x[idx] * win
        mag = np.abs(np.fft.rfft(seg, axis=1))
        lm = np.log1p(mag * 30)
        bass[s:e] = np.sqrt((mag[:, mb] ** 2).mean(axis=1))
        mid[s:e] = np.sqrt((mag[:, mm] ** 2).mean(axis=1))
        high[s:e] = np.sqrt((mag[:, mh] ** 2).mean(axis=1))
        rms[s:e] = np.sqrt((seg ** 2).mean(axis=1))
        for b in range(32):
            e0 = edges[b]
            spec[s:e, b] = mag[:, e0:max(edges[b + 1], e0 + 1)].mean(axis=1)
        dif = np.diff(lm, axis=0, prepend=(lm[:1] if prev is None else prev[None, :]))
        pos = np.maximum(dif, 0)
        flux[s:e], fluxb[s:e] = pos[:, mall].sum(axis=1), pos[:, mb].sum(axis=1)
        fluxm[s:e], fluxh[s:e] = pos[:, mm].sum(axis=1), pos[:, mh].sum(axis=1)
        prev = lm[-1]
        pr(0.08 + 0.52 * e / T)
    frr = sr / hop
    tshift = nfft / (2.0 * sr)
    spec = np.clip(np.log1p(spec * 30) / (np.percentile(np.log1p(spec * 30), 98, axis=0) + 1e-9)[None, :], 0, 1)
    spec = np.stack([_smooth(spec[:, b], 2) for b in range(32)], 1).astype(np.float32)

    # ---- fuerza de golpes por banda
    def hp(a):
        a = np.maximum(a - _smooth(a, frr), 0)
        return a / (np.percentile(a, 99) + 1e-9)
    env_all, env_b, env_m, env_h = hp(flux), hp(fluxb), hp(fluxm), hp(fluxh)
    env = 0.5 * env_all + 1.0 * env_b                 # el bombo (graves) manda en la rejilla de beats
    env_s = _smooth(env, 3) * 3
    pr(0.64)

    # ---- tempo: candidatos de la autocorrelación (y sus múltiplos) evaluados con precisión x cobertura
    n = len(env)
    f = np.fft.rfft(env - env.mean(), 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    lmin, lmax = int(frr * 60 / 190), int(frr * 60 / 65)
    lags = np.arange(lmin, lmax)
    bp = 60 * frr / lags
    prior = np.exp(-0.5 * (np.log2(bp / 115) / 0.7) ** 2)
    sc = ac[lmin:lmax] * prior
    cands = set()
    for i in np.argsort(-sc)[:40]:
        if all(abs(bp[i] - c) > 4 for c in cands) and len(cands) < 6:
            cands.add(round(float(bp[i]), 1))
    for c in list(cands):
        for m in (2.0, 0.5, 1.5, 2.0 / 3.0):
            if 65 <= c * m <= 190:
                cands.add(round(c * m, 1))
    et = _smooth(0.3 * env_all + env_b, 3)
    tot = float(et.sum()) + 1e-9

    def comb(bpm, nph):
        spbf = frr * 60 / bpm
        top_ = (0.0, 0.0)
        for ph in np.linspace(0, spbf, nph, endpoint=False):
            idx = (ph + np.arange(int((n - 2 - ph) / spbf)) * spbf).astype(int) + 1
            v = np.maximum(et[idx], np.maximum(et[idx - 1], et[np.minimum(idx + 1, n - 1)]))
            score = float(v.mean()) * min(1.0, float(v.sum()) / tot)      # precisión x cobertura
            if score > top_[0]:
                top_ = (score, ph)
        return top_
    best = (-1.0, 120.0)
    for c in cands:
        pri = float(np.exp(-0.5 * (np.log2(c / 115) / 0.7) ** 2)) ** 0.5
        for bpm in c * (1 + np.linspace(-0.02, 0.02, 9)):
            v = comb(bpm, 12)[0] * pri
            if v > best[0]:
                best = (v, float(bpm))
    top2 = (-1.0, best[1], 0.0)
    for bpm in best[1] * (1 + np.linspace(-0.015, 0.015, 31)):
        v, ph = comb(bpm, 24)
        if v > top2[0]:
            top2 = (v, float(bpm), ph)
    _, bpm, ph = top2
    spb = 60.0 / bpm
    offset = (ph / frr + tshift) % spb
    pr(0.74)

    # ---- golpes (picos) por banda
    def pick_peaks(e, rel):
        es = _smooth(e, 3) * 3
        thr = _smooth(es, frr * 1.5) + 0.25 * es.std()
        idx = np.where((es[1:-1] > es[:-2]) & (es[1:-1] >= es[2:]) & (es[1:-1] > thr[1:-1]) & (es[1:-1] > rel * es.max()))[0] + 1
        top = np.percentile(es[idx], 95) + 1e-9 if len(idx) else 1.0
        return [(float(i / frr + tshift), float(min(1.0, es[i] / top))) for i in idx]
    onsets, kicks = pick_peaks(env, 0.15), pick_peaks(env_b, 0.20)
    snares, hats = pick_peaks(env_m, 0.20), pick_peaks(env_h, 0.25)

    # ---- características por COMPÁS: energía, brillo, graves, densidad de golpes, flujo
    bass, mid, high, rms = (_norm01(_smooth(a, 3)) for a in (bass, mid, high, rms))
    bar = 4 * spb
    nb = max(1, int(dur / bar))
    on_t, on_s = np.array([o[0] for o in onsets] or [0.0]), np.array([o[1] for o in onsets] or [0.0])
    env_f = _smooth(env, 3)
    en, br, bsr, fl, dn = (np.zeros(nb) for _ in range(5))
    for k in range(nb):
        i0 = min(T - 1, int(max(0.0, offset + k * bar) * frr))
        i1 = max(i0 + 1, min(T, int(max(0.0, offset + (k + 1) * bar) * frr) + 1))
        b_, m_, h_ = bass[i0:i1].mean(), mid[i0:i1].mean(), high[i0:i1].mean()
        tot = b_ + m_ + h_ + 1e-9
        en[k], bsr[k], br[k], fl[k] = rms[i0:i1].mean(), b_ / tot, h_ / tot, env_f[i0:i1].mean()
        a, b = np.searchsorted(on_t, [offset + k * bar, offset + (k + 1) * bar])
        dn[k] = on_s[a:b].sum() if len(onsets) else 0.0
    n01 = lambda a: np.clip(a / (np.percentile(a, 95) + 1e-9), 0, 1)
    en_n, fl_n, dn_n, br_n, bsr_n = n01(en), n01(fl), n01(dn), n01(br), n01(bsr)
    busy = _smooth3(0.45 * dn_n + 0.30 * fl_n + 0.25 * en_n)
    busy = busy / (busy.max() + 1e-9)                          # 1.0 = la parte más movida DE ESTA canción
    pr(0.82)

    # ---- niveles de intensidad (0..3) por "movida", sostenidos al menos 3 compases
    q = np.quantile(busy, [0.30, 0.55, 0.80])
    lv = np.digitize(busy, q)
    lv = np.array([int(np.median(lv[max(0, k - 2):k + 3])) for k in range(nb)])
    cur, lvs = int(lv[0]), lv.copy()
    for k in range(1, nb):
        new = int(lv[k])
        if new != cur and all(int(v) == new for v in lv[k:k + 3]):
            cur = new
        lvs[k] = cur

    # ---- secciones: cambios de nivel + novedad (cambio real de timbre/energía/densidad)
    bounds = {0} | {k for k in range(1, nb) if lvs[k] != lvs[k - 1]}
    F = np.stack([en_n, br_n, bsr_n, dn_n], 1)
    nov = np.zeros(nb)
    for k in range(4, nb - 3):
        nov[k] = np.linalg.norm(F[k:k + 4].mean(0) - F[k - 4:k].mean(0))
    if nb > 12 and nov.std() > 0:
        th = nov.mean() + 0.8 * nov.std()
        bounds |= {k for k in range(4, nb - 3) if nov[k] >= th and nov[k] >= nov[k - 1] and nov[k] > nov[k + 1]}
    kept = [0]
    for k in sorted(bounds)[1:]:
        if k - kept[-1] >= 4:
            kept.append(k)
    if len(kept) > 1 and nb - kept[-1] < 4:
        kept.pop()
    segments = []
    for i, k0 in enumerate(kept):
        k1 = kept[i + 1] if i + 1 < len(kept) else nb
        sl = slice(k0, k1)
        segments.append(dict(t0=0.0 if i == 0 else offset + k0 * bar, t1=dur if k1 >= nb else offset + k1 * bar,
                             level=int(round(float(lvs[sl].mean()))), busy=float(busy[sl].mean()),
                             bright=float(br[sl].mean()), bassr=float(bsr[sl].mean()), energy=float(en[sl].mean())))
    sections = [(sg["t0"], sg["level"]) for sg in segments]

    # ---- SURGES: tramos muy movidos (>= 2 compases seguidos, máx. 45 % de la canción)
    def runs(th):
        mask = busy >= th
        for k in range(1, nb - 1):
            if not mask[k] and mask[k - 1] and mask[k + 1]:
                mask[k] = True
        out, k = [], 0
        while k < nb:
            if mask[k]:
                j = k
                while j < nb and mask[j]:
                    j += 1
                if j - k >= 2:
                    out.append((k, j))
                k = j
            else:
                k += 1
        return out
    rs = runs(max(0.62, float(np.quantile(busy, 0.70))))
    rs.sort(key=lambda r: -float(busy[r[0]:r[1]].mean()))
    kept_r, used = [], 0
    for a_, b_ in rs:                                     # los más movidos primero, hasta el 45 % de la canción
        if used + (b_ - a_) <= 0.45 * nb:
            kept_r.append((a_, b_))
            used += b_ - a_
    rs = sorted(kept_r)
    surges = [(float(offset + a * bar), float(offset + b * bar)) for a, b in rs]

    # ---- drops: salto brusco de energía hacia una parte fuerte
    rng_e = float(en.max() - en.min()) + 1e-9
    drops = [float(offset + k * bar) for k in range(2, nb) if en[k] - en[k - 1] > 0.3 * rng_e and en[k] > 0.6 * en.max()]

    s = Song()
    s.path, s.builtin = path, False
    s.title = path.replace("\\", "/").split("/")[-1].rsplit(".", 1)[0]
    s.duration, s.bpm, s.spb, s.offset = dur, float(bpm), spb, float(offset)
    s.onsets, s.sections, s.drops = onsets, sections, drops
    s.onset_t, s.onset_s = [o[0] for o in onsets], [o[1] for o in onsets]
    s.kick_t, s.kick_s = [o[0] for o in kicks], [o[1] for o in kicks]
    s.snare_t, s.snare_s = [o[0] for o in snares], [o[1] for o in snares]
    s.hat_t, s.hat_s = [o[0] for o in hats], [o[1] for o in hats]
    s.segments, s.surges, s.bar_busy, s.spec = segments, surges, busy, spec
    tot = float(bass.mean() + mid.mean() + high.mean()) + 1e-9
    s.feats = {k: min(1.0, v) for k, v in dict(bass=float(bass.mean()) / tot * 2.0, high=float(high.mean()) / tot * 2.0,
                                                rate=len(onsets) / dur).items()}
    s.bass, s.mid, s.high, s.rms, s.frr = bass, mid, high, rms, frr
    s.curve = [float(v) for v in np.interp(np.linspace(0, nb - 1, 240), np.arange(nb), busy)]
    pr(1.0)
    return s


# =================================================================== SÍNTESIS (efectos + música de menú)
def _t(n):
    return np.arange(n) / SR


def tone(freq, dur, decay=8.0, harm=(1.0,), vol=1.0):
    t = _t(int(dur * SR))
    return vol * sum(h * np.sin(2 * np.pi * freq * (i + 1) * t) for i, h in enumerate(harm)) * np.exp(-decay * t)


def sweep(f0, f1, dur, decay=4.0, vol=1.0):
    n = int(dur * SR)
    return vol * np.sin(2 * np.pi * np.cumsum(np.linspace(f0, f1, n)) / SR) * np.exp(-decay * _t(n))


def noise(dur, decay=10.0, vol=1.0):
    n = int(dur * SR)
    return vol * (np.random.rand(n) * 2 - 1) * np.exp(-decay * _t(n))


def lowpass(x, k):
    return np.convolve(x, np.ones(k) / k, mode="same")


def cat(*a):
    return np.concatenate(a)


def to_sound(mono, vol=1.0):
    s = (np.tanh(mono) * 32767 * vol).astype(np.int16)
    return pygame.sndarray.make_sound(np.ascontiguousarray(np.column_stack((s, s))))


def _put(buf, x, start):
    n = len(buf)
    start %= n
    end = start + len(x)
    if end <= n:
        buf[start:end] += x
    else:
        buf[start:] += x[:n - start]
        buf[:end - n] += x[n - start:]


def build_loop(full, bpm=128.0):
    """Loop de 8 compases en La menor. full=True añade batería (se usa en el tutorial)."""
    spb = round(60.0 / bpm * SR)
    sp16 = spb // 4
    buf = np.zeros(8 * 4 * spb)
    roots = [110.0, 87.31, 130.81, 98.0]
    chords = [[440, 523.25, 659.25, 880], [349.23, 440, 523.25, 698.46],
              [523.25, 659.25, 783.99, 1046.5], [392, 493.88, 587.33, 783.99]]
    kick = sweep(150, 45, 0.22, 12) * 1.3
    clap = (lambda x: x - lowpass(x, 8))(noise(0.18, 22)) * 1.2
    hat = np.diff(noise(0.06, 70), prepend=0) * 0.8
    pat = [0, 1, 2, 3, 2, 1, 2, 3]
    for bar in range(8):
        c = bar // 2
        if not full and bar % 2 == 0:
            n = 8 * spb
            env = np.sin(np.pi * np.arange(n) / n) ** 1.5
            _put(buf, sum(np.sin(2 * np.pi * f * 0.5 * _t(n)) for f in chords[c][:3]) * env * 0.12, bar * 4 * spb)
        for st in range(16):
            pos = (bar * 16 + st) * sp16
            _put(buf, tone(chords[c][pat[st % 8]], 0.16, 14, (1, .35, .15)) * (0.16 if full else 0.11), pos)
            if full:
                if st % 4 == 0:
                    _put(buf, kick * 0.9, pos)
                if st in (4, 12):
                    _put(buf, clap * 0.5, pos)
                if st % 4 == 2:
                    _put(buf, hat * 0.35, pos)
                if st % 2 == 0:
                    _put(buf, tone(roots[c] * (1 if st % 4 == 0 else 2), 0.22, 7, (1, .5, .25)) * 0.33, pos)
            elif st % 4 == 2:
                _put(buf, hat * 0.18, pos)
    buf /= max(1.0, np.abs(buf).max() / 0.85)
    return to_sound(buf)


def build_sfx():
    S = {}
    S["dot0"] = to_sound(tone(740, 0.05, 32, (1, .3)), 0.35)
    S["dot1"] = to_sound(tone(988, 0.05, 32, (1, .3)), 0.35)
    for k, f in enumerate((523, 587, 659, 740, 831)):
        S[f"catch{k + 1}"] = to_sound(cat(tone(f, 0.07, 12, (1, .4)), tone(f * 1.5, 0.2, 9, (1, .4))), 0.6)
    sc = [330, 392, 494, 587, 659, 784, 988]
    parts = []
    for i in range(7):
        parts += [tone(random.choice(sc), 0.05, 22, (1, .5)) * (0.6 + 0.4 * (i % 2)), noise(0.02, 40) * 0.3]
    S["mutate"] = to_sound(cat(*parts), 0.45)
    t = _t(int(1.2 * SR))
    env = np.sin(np.pi * t / 1.2) ** 2
    S["whoosh"] = to_sound(lowpass(noise(1.2, 0.3), 30) * 5 * env + sweep(160, 1000, 1.2, 0.6) * 0.25 * env, 0.7)
    S["move"] = to_sound(tone(660, 0.04, 40), 0.4)
    S["select"] = to_sound(cat(tone(660, 0.07, 18), tone(990, 0.14, 12)), 0.6)
    S["beep"] = to_sound(tone(440, 0.15, 8), 0.6)
    S["go"] = to_sound(tone(880, 0.4, 5, (1, .4)), 0.7)
    S["end"] = to_sound(sweep(880, 110, 1.0, 2.5), 0.7)
    S["item"] = to_sound(cat(tone(1320, 0.05, 20), tone(1760, 0.09, 14)), 0.4)                      # aparece un ítem
    S["freeze"] = to_sound(cat(*[tone(f, 0.05, 14, (1, .6, .3)) for f in (2349, 1976, 1760, 1480, 1175, 988)])
                           + 0.0, 0.6)                                                               # cristales de hielo
    S["slow"] = to_sound(sweep(700, 90, 0.8, 2.2) + sweep(350, 45, 0.8, 2.2) * 0.6, 0.6)             # el tiempo se frena
    return S


class Audio:
    def __init__(self):
        self.ok, self.track, self.muted, self.vol, self.song_on = False, None, False, 0.5, False
        try:
            pygame.mixer.init(SR, -16, 2, 512, allowedchanges=0)
            pygame.mixer.set_num_channels(16)
            pygame.mixer.set_reserved(1)
            self.mch = pygame.mixer.Channel(0)
            self.music = {"menu": build_loop(False), "game": build_loop(True)}
            self.fx = build_sfx()
            self.ok = True
        except Exception as e:
            print("Audio desactivado:", e)

    def play_music(self, name):
        if not self.ok or (self.track == name and not self.song_on):
            return
        self.stop_song()
        self.track = name
        self.mch.stop()
        if name:
            self.mch.play(self.music[name], loops=-1)

    def play_song(self, path):
        if not self.ok:
            return False
        self.mch.stop()
        self.track = None
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
        except Exception as e:
            print("No se pudo reproducir:", e)
            return False
        self.song_on = True
        return True

    def stop_song(self):
        if self.ok and self.song_on:
            pygame.mixer.music.stop()
        self.song_on = False

    def song_pos(self):
        """Segundos reproducidos del mp3 (-1 si aún no suena)."""
        if not (self.ok and self.song_on):
            return -1.0
        p = pygame.mixer.music.get_pos()
        return p / 1000.0 if p >= 0 else -1.0

    def song_ended(self):
        return self.ok and self.song_on and not pygame.mixer.music.get_busy()

    def pause(self):
        if self.ok:
            pygame.mixer.pause()
            pygame.mixer.music.pause()

    def unpause(self):
        if self.ok:
            pygame.mixer.unpause()
            pygame.mixer.music.unpause()

    def sfx(self, name):
        if self.ok and name in self.fx:
            ch = pygame.mixer.find_channel()
            if ch:
                ch.play(self.fx[name])

    def set_volume(self, dt, ducked):
        if not self.ok:
            return
        target = 0.0 if self.muted else (0.35 if ducked else (0.9 if self.song_on else 0.55))
        self.vol += (target - self.vol) * min(1.0, dt * 5)
        self.mch.set_volume(self.vol)
        pygame.mixer.music.set_volume(self.vol)


class VoiceAI:
    """Voz de la IA con pyttsx3 en un hilo aparte (no congela el juego)."""

    def __init__(self):
        self.enabled, self.muted, self.speaking = False, False, False
        self.q = queue.Queue(maxsize=3)
        try:
            import pyttsx3
            self._pyttsx3 = pyttsx3
        except Exception:
            print("Sin pyttsx3: voz desactivada (pip install pyttsx3)")
            return
        self.enabled = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            eng = self._pyttsx3.init()
            keys = ("spanish", "español", "es-mx", "es_mx", "es-es", "es_es", "sabina", "paulina", "helena", "pablo")
            for v in eng.getProperty("voices"):
                meta = (str(v.name) + " " + str(getattr(v, "languages", "")) + " " + str(v.id)).lower()
                if any(k in meta for k in keys):
                    eng.setProperty("voice", v.id)
                    break
            eng.setProperty("rate", 185)
        except Exception as e:
            print("Voz desactivada:", e)
            self.enabled = False
            return
        while True:
            text = self.q.get()
            try:
                self.speaking = True
                eng.say(text)
                eng.runAndWait()
            except Exception:
                pass
            finally:
                self.speaking = False

    def say(self, text, prio=1):
        if not self.enabled or self.muted:
            return
        if prio == 0 and (self.speaking or not self.q.empty()):
            return
        try:
            self.q.put_nowait(text)
        except queue.Full:
            try:
                self.q.get_nowait()
                self.q.put_nowait(text)
            except Exception:
                pass
