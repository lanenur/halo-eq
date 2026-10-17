#!/usr/bin/env python3
"""Halo EQ: Linux PEQ app for TANCHJIM DSP dongles (KT Micro, USB 31b2), e.g. BUNNY DSP.

Device protocol from jeromeof/devicePEQ (ktmicroUsbHidHandler.js). Preset API from the
official TANCHJIM Android app (angelapi.tanchjim.com); browsing needs no login.
"""
import bisect
import cmath
import glob
import json
import math
import os
import re
import select
import struct
import sys
from datetime import datetime
from pathlib import Path

API = "https://angelapi.tanchjim.com/api/v1/eqManage/"
# USB product name -> preset list name on TANCHJIM's server (DeviceBaseInfo in the official app)
MODELS = {"TANCHJIM-DSP S": "DSP S", "TANCHJIM-ONE DSP": "ONE", "TANCHJIM TANYA DSP": "TANYA",
          "TANCHJIM-ZERO DSP": "ZERO", "TANCHJIM-ZERO ULTIMA DSP": "ZERO ULTIMA", "TANCHJIM-EA FORCE": "EA FORCE",
          "TANCHJIM-FOLA": "FOLA", "FISSION-Rational HiFi Edition": "FISSION", "TANCHJIM-STARGATE II": "STARGATE II",
          "TANCHJIM-FISSION  DSP": "FISSION", "TANCHJIM FISSION": "FISSION", "TANCHJIM-ORIGIN AT": "ORIGIN AT",
          "TANCHJIM-SPACE": "SPACE", "TANCHJIM-LUNA": "LUNA", "TANCHJIM-LUNA AT": "LUNA AT",
          "TANCHJIM BUNNY DSP": "BUNNY", "TANCHJIM-DSP M": "DSP M", "TANCHJIM-SPACE II": "SPACE II",
          "TANCHJIM-SPACE PRO": "SPACE PRO", "TANCHJIM-SPACE PRO AT": "SPACE PRO AT", "TANCHJIM-OLA II DSP": "OLA II",
          "TANCHJIM-RITA DSP": "RITA DSP", "TANCHJIM-PINTO DSP": "PINTO DSP", "BUNNY FOR GAMING": "BUNNY",
          "TANCHJIM-RITA AT DSP": "RITA AT DSP"}
# writes only where devicePEQ confirms this register layout; anything else is read-only
VERIFIED = ("TANCHJIM BUNNY DSP", "TANCHJIM-ONE DSP", "TANCHJIM FISSION", "TANCHJIM-FISSION  DSP")
BANDS = 5
TYPE_CODE = {"PEAKING": 0, "LOW_SHELF": 3, "HIGH_SHELF": 4, "LOW_PASS": 1}
CODE_TYPE = {v: k for k, v in TYPE_CODE.items()}
TYPE_LABEL = {"PEAKING": "PK", "LOW_SHELF": "LS", "HIGH_SHELF": "HS", "LOW_PASS": "LP"}
LP_BAND = 4  # the official app only ever puts a low-pass on the last band (DSPCommand.getTypeCQGain)
# iirStrFormat spellings on TANCHJIM's server
FORMAT = {"PEAKING": "PEAKING", "LOW_SHELF": "LOW_SHELF", "LOWSHELF": "LOW_SHELF", "HIGH_SHELF": "HIGH_SHELF",
          "HIGHSHELF": "HIGH_SHELF", "LOWPASS": "LOW_PASS", "LOW_PASS": "LOW_PASS"}
GAME_PLATFORMS = {"Phone": 1, "PC": 2, "Switch": 3}
FILES = "https://cosgz.tanchjim.com/"  # where AutoEQ measurement and target curves live
CACHE = Path.home() / ".cache/halo-eq/curves"
CURVE_LIST = Path.home() / ".cache/halo-eq/curve-list.json"  # so the IEM graph works when the server is down
# many community presets only have Chinese names; this keyless endpoint translates them to English
TRANSLATE = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=en&dt=t"
TCACHE = Path.home() / ".cache/halo-eq/translations.json"
SETTINGS = Path.home() / ".config/halo-eq/settings.json"
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
STORE = Path.home() / ".config/halo-eq/presets.json"
FAVS = Path.home() / ".config/halo-eq/favorites.json"
OLD_STORE = Path.home() / ".config/bunnyeq/presets.json"  # name before the rebrand
FS = 48000
FLAT = [["PEAKING", f, 0.0, 1.0] for f in (60, 250, 1000, 4000, 12000)]
GRID_F = [20 * 1000 ** (i / 300) for i in range(301)]  # graph x points, 20 Hz..20 kHz log-spaced

# --- device (HID report 0x4B: reg 0 0 0 cmd 0 d0 d1 d2 d3) ---
REPORT, READ, WRITE, COMMIT = 0x4B, 0x52, 0x57, 0x53
BAND_REG, PREAMP_REG, SLOT_REG, MIC_REG = 0x26, 0x66, 0x24, 0x65
SLOT_ON, SLOT_OFF = 0x03, 0x02


class Unsafe(Exception):
    pass


def packet(reg, cmd, data=b""):
    return bytes([REPORT, reg, 0, 0, 0, cmd, 0]) + data.ljust(4, b"\0")


def allowed(i, t):
    return t in TYPE_CODE and (t != "LOW_PASS" or i == LP_BAND)


def clamp(bands, preamp):
    """Round to what the device stores and keep every value in the safe range."""
    c = lambda v, lo, hi: max(lo, min(hi, v))  # noqa: E731
    return ([[t if allowed(i, t) else "PEAKING", round(c(f, 20, 20000)), round(c(g, -12, 12), 1), round(c(q, 0.1, 10), 3)]
             for i, (t, f, g, q) in enumerate(bands)], round(c(preamp, -12, 0)))


def sane(bands, preamp):
    return all(allowed(i, b[0]) for i, b in enumerate(bands)) and (bands, preamp) == clamp(bands, preamp)


class Device:
    def __init__(self):
        for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
            uevent = Path(d, "device/uevent").read_text()
            if "HID_ID=0003:000031B2:" in uevent:
                self.name = re.search(r"HID_NAME=(.*)", uevent).group(1)
                # HID_NAME is "<manufacturer> <product>"; endswith keeps SPACE from matching SPACE PRO
                self.readonly = None if self.name.endswith(VERIFIED) else \
                    f"{self.name} is not a confirmed model, so the app only reads from it"
                self.abbr = next((a for k, a in MODELS.items() if self.name.endswith(k)), None)
                self.path = "/dev/" + os.path.basename(d)
                self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
                return
        raise OSError("TANCHJIM DSP not plugged in")

    def send(self, reg, cmd, data=b""):
        os.write(self.fd, packet(reg, cmd, data))

    def ask(self, reg, cmd, data=b""):
        try:  # drop stale replies (band writes are not always ACKed)
            while os.read(self.fd, 64):
                pass
        except BlockingIOError:
            pass
        self.send(reg, cmd, data)
        while select.select([self.fd], [], [], 1.0)[0]:
            r = os.read(self.fd, 64)
            if r[0] == REPORT and r[1] == reg and r[5] == cmd:
                return r[1:]
        raise TimeoutError(f"device did not answer (reg 0x{reg:02x})")

    def read(self):
        bands = []
        for i in range(BANDS):
            gain, freq = struct.unpack("<hH", self.ask(BAND_REG + 2 * i, READ)[6:10])
            q, t = struct.unpack("<HB", self.ask(BAND_REG + 2 * i + 1, READ)[6:9])
            bands.append([CODE_TYPE.get(t, f"unknown type {t}"), freq, gain / 10, q / 1000])
        preamp = struct.unpack("b", self.ask(PREAMP_REG, READ)[6:7])[0]
        return bands, preamp

    def enabled(self):
        return self.ask(SLOT_REG, READ, bytes([SLOT_ON]))[6] != SLOT_OFF

    def set_enabled(self, on):
        self.ask(SLOT_REG, WRITE, bytes([SLOT_ON if on else SLOT_OFF]))

    def write(self, bands, preamp):
        if self.readonly:
            raise Unsafe(self.readonly)
        bands, preamp = clamp(bands, preamp)
        if not self.enabled():
            self.set_enabled(True)
        for i, (t, f, g, q) in enumerate(bands):
            self.send(BAND_REG + 2 * i, WRITE, struct.pack("<hH", round(g * 10), f))
            self.send(BAND_REG + 2 * i + 1, WRITE, struct.pack("<HB", round(q * 1000), TYPE_CODE[t]))
        self.send(PREAMP_REG, WRITE, struct.pack("b", preamp))
        got = self.read()
        if got != (bands, preamp):
            raise Unsafe(f"Device read back different values, so nothing was saved to its memory.\n"
                         f"The dongle goes silent with unsaved changes: unplug and replug it to restore the saved EQ.\n"
                         f"Sent: {bands} preamp {preamp}\nGot: {got[0]} preamp {got[1]}")
        self.save()

    def save(self):
        self.send(0, COMMIT)  # saves to the dongle's flash; callers only get here after a matching read-back
        os.close(self.fd)  # the dongle re-enumerates after saving, so this handle is dead

    def mic(self):
        return struct.unpack("b", self.ask(MIC_REG, READ)[6:7])[0] // 2  # stored as dB x 2, app shows whole dB

    def set_mic(self, db):
        if self.readonly:
            raise Unsafe(self.readonly)
        db = round(max(-15, min(15, db)))  # official app's slider range
        self.send(MIC_REG, WRITE, struct.pack("b", db * 2))
        if self.mic() != db:
            raise Unsafe("Device read back a different mic gain, so nothing was saved.\n"
                         "Unplug and replug the dongle to restore the saved settings.")
        self.save()


# --- filter math (RBJ cookbook biquads) ---
def biquad(t, f, g, q):
    A = 10 ** (g / 40)
    w = 2 * math.pi * f / FS
    c, a = math.cos(w), math.sin(w) / (2 * q)
    if t == "PEAKING":
        return (1 + a * A, -2 * c, 1 - a * A), (1 + a / A, -2 * c, 1 - a / A)
    if t == "LOW_PASS":  # gain does nothing here
        return ((1 - c) / 2, 1 - c, (1 - c) / 2), (1 + a, -2 * c, 1 - a)
    s = 2 * math.sqrt(A) * a
    if t == "LOW_SHELF":
        return ((A * ((A + 1) - (A - 1) * c + s), 2 * A * ((A - 1) - (A + 1) * c), A * ((A + 1) - (A - 1) * c - s)),
                ((A + 1) + (A - 1) * c + s, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - s))
    return ((A * ((A + 1) + (A - 1) * c + s), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - s)),
            ((A + 1) - (A - 1) * c + s, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - s))


GRID_Z = [cmath.exp(-2j * math.pi * f / FS) for f in GRID_F]


def response_grid(bands):
    """dB at every GRID_F point; coefficients computed once per band, not per point."""
    coeffs = [biquad(*b) for b in bands]
    out = []
    for z in GRID_Z:
        z2, db = z * z, 0.0
        for b, a in coeffs:
            db += 20 * math.log10(abs((b[0] + b[1] * z + b[2] * z2) / (a[0] + a[1] * z + a[2] * z2)))
        out.append(db)
    return out


def response(bands, preamp, f):
    z = cmath.exp(-2j * math.pi * f / FS)
    db = preamp
    for band in bands:
        b, a = biquad(*band)
        db += 20 * math.log10(abs((b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)))
    return db


# --- presets ---
def parse_code(code, types=""):
    """App share format: 'gain freq q' per band, space separated."""
    v = [float(s) for s in code.split()]
    types = [FORMAT.get(t.upper(), "PEAKING") for t in types.split()]
    bands = [[types[i] if i < len(types) and allowed(i, types[i]) else "PEAKING", v[3 * i + 1], v[3 * i], v[3 * i + 2]]
             for i in range(min(len(v) // 3, BANDS))]
    return bands + [list(b) for b in FLAT[len(bands):]]


def to_code(bands):
    return " ".join(f"{g:g} {f:g} {q:g}" for _, f, g, q in bands)


APO_TYPES = {"PK": "PEAKING", "PEQ": "PEAKING", "LS": "LOW_SHELF", "LSC": "LOW_SHELF", "LSQ": "LOW_SHELF",
             "HS": "HIGH_SHELF", "HSC": "HIGH_SHELF", "HSQ": "HIGH_SHELF"}
APO_FILTER = re.compile(r"Filter\s*\d*\s*:\s*ON\s+(\w+)\s+Fc\s+([\d.]+)\s*Hz(?:\s+Gain\s+([-\d.]+)\s*dB)?(?:\s+Q\s+([\d.]+))?", re.I)


def parse_apo(text):
    """Equalizer APO / AutoEQ ParametricEQ.txt -> (bands, preamp, dropped filter count)."""
    found = APO_FILTER.findall(text)
    filters = [[APO_TYPES[t.upper()], float(f), float(g or 0), float(q or 0.707)] for t, f, g, q in found
               if t.upper() in APO_TYPES]
    if not filters:
        raise ValueError("no PK/LS/HS filters found")
    # the dongle has 5 bands: keep the 5 strongest, in frequency order
    keep = sorted(sorted(filters, key=lambda b: -abs(b[2]))[:BANDS], key=lambda b: b[1])
    pre = re.search(r"Preamp:\s*([-\d.]+)", text, re.I)
    return keep + [list(b) for b in FLAT[len(keep):]], float(pre.group(1)) if pre else 0.0, len(found) - len(keep)


def to_apo(bands, preamp):
    kind = {"PEAKING": "PK", "LOW_SHELF": "LSC", "HIGH_SHELF": "HSC"}
    return f"Preamp: {preamp:g} dB\n" + "".join(
        f"Filter {i + 1}: ON LPQ Fc {f:g} Hz Q {q:g}\n" if t == "LOW_PASS" else
        f"Filter {i + 1}: ON {kind[t]} Fc {f:g} Hz Gain {g:g} dB Q {q:g}\n" for i, (t, f, g, q) in enumerate(bands))


def from_api(x):
    return {"name": x.get("eqNameEn") or x.get("eqName") or "?",
            "author": (x.get("member") or {}).get("name") or "",
            "detail": x.get("eqDetailEn") or x.get("eqDetail") or "",
            "likes": x.get("likeCnt") or 0, "downloads": x.get("downloadCnt") or 0,
            "bands": parse_code(x["eqData"], x.get("iirStrFormat") or ""), "preamp": x.get("preamp") or 0}


def parse_curve(text):
    """Measurement/target file ('freq dB' per line) -> dB at GRID_F, 1/12-octave smoothed, 0 dB at 1 kHz."""
    pts = sorted(tuple(map(float, ln.replace(",", " ").split()[:2])) for ln in text.splitlines()
                 if re.match(r"\s*[\d.]+[\s,]+-?[\d.]+", ln))
    fs = [f for f, _ in pts]
    out = []
    for f in GRID_F:
        lo, hi = bisect.bisect_left(fs, f / 2 ** (1 / 24)), bisect.bisect_right(fs, f * 2 ** (1 / 24))
        if hi > lo:
            out.append(sum(d for _, d in pts[lo:hi]) / (hi - lo))
        else:  # sparse file: nearest point on a log scale
            i = bisect.bisect_left(fs, f)
            j = min((k for k in (i - 1, i) if 0 <= k < len(fs)), key=lambda k: abs(math.log(fs[k] / f)))
            out.append(pts[j][1])
    ref = out[min(range(len(GRID_F)), key=lambda i: abs(GRID_F[i] - 1000))]
    return [d - ref for d in out]


def from_autoeq(d, name):
    kind = lambda t: "LOW_SHELF" if "low" in t.lower() else "HIGH_SHELF" if "high" in t.lower() else "PEAKING"  # noqa: E731
    bands = [[kind(f["type"]), f["fc"], f["gain"], f["q"]] for f in d["filters"]][:BANDS]
    return {"name": name, "detail": "Made by TANCHJIM's AutoEQ server. Check the curve, then Apply.",
            "bands": bands + [list(b) for b in FLAT[len(bands):]], "preamp": d.get("preamp") or 0}


def from_game(x):
    return {"name": x.get("gameNameEn") or x.get("gameNameZh") or "?",
            "detail": x.get("gameDetailEn") or x.get("gameDetailZh") or "",
            "bands": parse_code(x["gameEqData"], x.get("iirStrFormat") or ""), "preamp": 0}


def selftest():
    assert packet(0x26, WRITE, struct.pack("<hH", -30, 1000)) == bytes([0x4B, 0x26, 0, 0, 0, 0x57, 0, 0xE2, 0xFF, 0xE8, 0x03])
    assert abs(response([["PEAKING", 1000, 6.0, 1.0]], 0, 1000) - 6.0) < 0.01
    assert abs(response([["LOW_SHELF", 100, -4.0, 0.7]], 0, 20) + 4.0) < 0.3
    assert abs(response([["HIGH_SHELF", 8000, 3.0, 0.7]], 0, 20) - 0.0) < 0.1
    p = from_api({"eqData": "-4 30 0.3 -6 200 0.5", "iirStrFormat": "LOW_SHELF PEAKING", "eqName": "x"})
    assert p["bands"][0] == ["LOW_SHELF", 30.0, -4.0, 0.3] and len(p["bands"]) == BANDS
    assert parse_code(to_code(p["bands"]))[1] == p["bands"][1]
    assert clamp([["PEAKING", 5, 30.0, 0.0]], 4) == ([["PEAKING", 20, 12.0, 0.1]], 0)
    assert sane(*clamp(p["bands"], -3)) and not sane([["PEAKING", 0, 0.0, 0.0]], 0)
    assert not sane([["unknown type 7", 100, 0.0, 1.0]], 0)
    apo = "Preamp: -6.4 dB\nFilter 1: ON LSC Fc 105 Hz Gain 5.5 dB Q 0.70\nFilter 2: ON PK Fc 30 Hz Gain -1 dB Q 1\n" + \
        "".join(f"Filter {i}: ON PK Fc {i * 1000} Hz Gain {i} dB Q 1.5\n" for i in range(3, 9)) + "Filter 9: ON LP Fc 18000 Hz\n"
    bands, pre, dropped = parse_apo(apo)
    assert pre == -6.4 and dropped == 4 and bands[0] == ["LOW_SHELF", 105.0, 5.5, 0.7] and [b[2] for b in bands[1:]] == [5, 6, 7, 8]
    assert parse_apo(to_apo(bands, -6))[:2] == (bands, -6.0)
    g = from_game({"gameNameEn": "Valorant", "gameEqData": "-4 30 0.3 -6 200 0.5 -3 1400 0.7 -6.5 4300 0.6 0.6 8000 0.9",
                   "iirStrFormat": "PEAKING PEAKING PEAKING PEAKING LOWPASS"})
    assert g["bands"][4] == ["LOW_PASS", 8000.0, 0.6, 0.9] and sane(*clamp(g["bands"], 0))
    assert clamp([["LOW_PASS", 100, 0, 1]] * 2, 0)[0][0][0] == "PEAKING"  # LP only allowed on band 5
    assert not sane([["LOW_PASS", 100, 0.0, 1.0]] + [["PEAKING", 100, 0.0, 1.0]] * 4, 0)
    c = parse_curve("10 0\n1000 5\n2000 8\n20000 2\n")
    assert len(c) == len(GRID_F) and abs(c[150]) < 1e-9 and c[-1] == -3 and c[0] == -5  # 0 dB at 1 kHz
    a = from_autoeq({"preamp": -5.8, "filters": [{"type": "Peaking", "fc": 588, "gain": 3.1, "q": 2.58}]}, "x")
    assert a["bands"][0] == ["PEAKING", 588, 3.1, 2.58] and len(a["bands"]) == BANDS
    bs = [["PEAKING", 1000, 6.0, 1.0], ["LOW_PASS", 9000, 0, 0.8]]
    assert all(abs(a - response(bs, 0, f)) < 1e-9 for a, f in zip(response_grid(bs), GRID_F))
    assert abs(response([["LOW_PASS", 8000, 0, 0.707]], 0, 100)) < 0.01 and response([["LOW_PASS", 8000, 0, 0.707]], 0, 16000) < -10
    print("ok")


def probe():
    """Read-only report for owners of other TANCHJIM devices to send back."""
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent = Path(d, "device/uevent").read_text()
        if "31B2" in uevent or "TANCHJIM" in uevent.upper():
            print(os.path.basename(d), *re.findall(r"HID_(?:ID|NAME)=.*", uevent))
            print("  descriptor:", Path(d, "device/report_descriptor").read_bytes().hex(" "))
    try:
        dev = Device()
    except OSError as e:
        return print("KT Micro read test skipped:", e)
    print("model:", dev.name, "| preset list:", dev.abbr, "| writes allowed:", not dev.readonly)
    for reg in [SLOT_REG] + [BAND_REG + i for i in range(2 * BANDS)] + [PREAMP_REG, MIC_REG]:
        try:
            print(f"  reg 0x{reg:02x}:", dev.ask(reg, READ, bytes([SLOT_ON]) if reg == SLOT_REG else b"").hex(" "))
        except TimeoutError as e:
            print(f"  reg 0x{reg:02x}:", e)


def remove(lst, row):
    """Remove one row (None: all) without Qt selecting a neighbour, which would load it over the editor."""
    lst.blockSignals(True)
    lst.clear() if row is None else lst.takeItem(row)
    lst.blockSignals(False)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return [] if default is None else default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))


# --- UI ---
from urllib.parse import urlencode  # noqa: E402

from PySide6.QtCore import QPointF, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPolygonF  # noqa: E402
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest  # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QDoubleSpinBox, QGridLayout,  # noqa: E402
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPushButton, QSpinBox, QSplitter, QTabWidget, QVBoxLayout,
                               QWidget)

FMIN, FMAX = 20, 20000
COLORS = ["#e8585a", "#f0a030", "#4cb860", "#3c9be8", "#b070e0"]


class Graph(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.drag = win, None
        self.iem, self.dbmax = None, 15  # IEM measurement in view; ±24 dB fits a raw measurement, ±15 an EQ alone
        self.setMinimumSize(600, 280)

    def fx(self, f):
        return 40 + (self.width() - 50) * math.log(f / FMIN) / math.log(FMAX / FMIN)

    def fy(self, db):
        return 10 + (self.height() - 30) * (self.dbmax - db) / (2 * self.dbmax)

    def base(self, f):
        """The stock IEM at f, so the dots ride on the IEM-with-EQ curve (0 without a measurement)."""
        if not self.iem:
            return 0
        i = round((len(GRID_F) - 1) * math.log(f / GRID_F[0]) / math.log(GRID_F[-1] / GRID_F[0]))
        return self.iem[max(0, min(len(GRID_F) - 1, i))]

    def offset(self, i, f):
        """Everything under band i's dot except its own gain: the stock IEM plus the other bands at f."""
        return self.base(f) + response([b for j, b in enumerate(self.win.bands()) if j != i], 0, f)

    def handles(self):
        return [QPointF(self.fx(f), self.fy(g + self.offset(i, f))) for i, (_, f, g, _) in enumerate(self.win.bands())]

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.base())
        text = pal.text().color()
        grid = QColor(text)
        grid.setAlpha(45)  # theme "mid" vanishes on dark themes
        # like the TANCHJIM app: what you hear (IEM + EQ) over the stock IEM; the EQ alone until a measurement loads
        self.iem, target = self.win.iem_curves()
        self.dbmax = 24 if self.iem else 15
        for f, lbl in [(20, "20"), (50, "50"), (100, "100"), (200, "200"), (500, "500"), (1000, "1k"),
                       (2000, "2k"), (5000, "5k"), (10000, "10k"), (20000, "20k")]:
            p.setPen(grid)
            p.drawLine(QPointF(self.fx(f), self.fy(self.dbmax)), QPointF(self.fx(f), self.fy(-self.dbmax)))
            p.setPen(text)
            p.drawText(QPointF(min(self.fx(f) - 8, self.width() - 26), self.height() - 4), lbl)
        for db in range(-self.dbmax + 3, self.dbmax, 3):
            p.setPen(QPen(text if db == 0 else grid, 1))
            p.drawLine(QPointF(40, self.fy(db)), QPointF(self.width() - 10, self.fy(db)))
            p.setPen(text)
            p.drawText(QPointF(4, self.fy(db) + 4), f"{db:+d}")
        base = self.iem or [0.0] * len(GRID_F)
        soft = QColor(text)
        soft.setAlpha(110)
        legend = []
        if self.iem:
            p.setPen(QPen(soft, 1.5, Qt.DashLine))
            p.drawPolyline(self.line(self.iem))
            legend.append(f"- - stock {self.win.iembox.currentText()}")
        if target:  # AutoEQ tab only
            p.setPen(QPen(QColor("#f0b429"), 1.5, Qt.DashLine))
            p.drawPolyline(self.line(target))
            legend.append("- - target")
        if self.win.last:  # what the dongle holds now, under the curve being edited
            p.setPen(QPen(soft, 1.5, Qt.DotLine))
            p.drawPolyline(self.line([a + b for a, b in zip(base, response_grid(self.win.last[0]))]))
            legend.append("··· on the dongle")
        p.setPen(QPen(QColor("#4aa8f5"), 2.5))  # fixed: some themes' highlight is too dark to see on the graph
        p.drawPolyline(self.line([a + b for a, b in zip(base, response_grid(self.win.bands()))]))
        if self.iem:
            legend.append("—— with this EQ")
            p.setPen(text)
            p.drawText(QPointF(48, 22), "    ".join(legend))
        for i, h in enumerate(self.handles()):
            p.setPen(QPen(QColor("white"), 1.5))
            p.setBrush(QColor(COLORS[i]))
            p.drawEllipse(h, 9, 9)
            p.drawText(h + QPointF(-3.5, 4.5), str(i + 1))

    def line(self, dbs):  # preamp is only volume, so curves leave it out and the dots stay on the line
        # GRID_F is log-spaced, so its points are evenly spaced on screen
        m = self.dbmax
        step, ky, top = (self.width() - 50) / (len(GRID_F) - 1), (self.height() - 30) / (2 * m), 10 + (self.height() - 30) / 2
        return QPolygonF([QPointF(40 + i * step, top - max(-m, min(m, db)) * ky) for i, db in enumerate(dbs)])

    def nearest(self, pos):
        d = [(h - pos).manhattanLength() for h in self.handles()]
        return d.index(min(d)) if min(d) < 24 else None

    def mousePressEvent(self, e):
        self.drag = self.nearest(e.position())

    def mouseReleaseEvent(self, _):
        self.drag = None

    def mouseMoveEvent(self, e):
        if self.drag is None:
            return
        x = (e.position().x() - 40) / (self.width() - 50)
        db = self.dbmax - 2 * self.dbmax * (e.position().y() - 10) / (self.height() - 30)
        row = self.win.rows[self.drag]
        row[1].setValue(FMIN * (FMAX / FMIN) ** max(0.0, min(1.0, x)))
        row[2].setValue(db - self.offset(self.drag, row[1].value()))

    def wheelEvent(self, e):
        i = self.nearest(e.position())
        if i is not None:
            q = self.win.rows[i][3]
            q.setValue(q.value() * 1.1 ** (e.angleDelta().y() / 120))


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Halo EQ")
        self.dev, self.last, self.backed_up = None, None, False  # last = what the device holds, to skip no-op writes
        self.abbr, self.devname = "BUNNY", "TANCHJIM BUNNY DSP"
        self.net = QNetworkAccessManager(self)
        self.page, self.sort, self.search = 1, 1, ""

        self.tabs = QTabWidget()
        self.official = QListWidget()
        self.community = QListWidget()
        self.mine = QListWidget()
        self.games = QListWidget()
        self.favs = QListWidget()
        for lst in (self.official, self.community, self.mine, self.games, self.favs):
            lst.setWordWrap(True)
            lst.currentItemChanged.connect(lambda it, _: it and self.load(it.data(Qt.UserRole)))
            lst.itemDoubleClicked.connect(lambda it: (self.load(it.data(Qt.UserRole)), self.apply()))
        comm = QWidget()
        cl = QVBoxLayout(comm)
        top = QHBoxLayout()
        self.sortbox = QComboBox()
        self.sortbox.addItems(["Newest", "Most downloaded", "Most liked"])
        self.sortbox.setCurrentIndex(1)
        self.sortbox.currentIndexChanged.connect(lambda i: self.fetch_community(sort=i))
        self.searchbox = QLineEdit(placeholderText="Search…")
        self.searchbox.returnPressed.connect(lambda: self.fetch_community(search=self.searchbox.text().strip()))
        top.addWidget(self.sortbox)
        top.addWidget(self.searchbox)
        more = QPushButton("Load more")
        more.clicked.connect(lambda: self.fetch_community(page=self.page + 1))
        cl.addLayout(top)
        cl.addWidget(self.community)
        cl.addWidget(more)
        mine = QWidget()
        ml = QVBoxLayout(mine)
        ml.addWidget(self.mine)
        mb = QHBoxLayout()
        for label, fn in [("Import APO…", self.import_apo), ("Export APO…", self.export_apo), ("Delete", self.delete_mine)]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            mb.addWidget(b)
        ml.addLayout(mb)
        games = QWidget()
        gl = QVBoxLayout(games)
        self.platform = QComboBox()
        self.platform.addItems(GAME_PLATFORMS)
        self.platform.setCurrentText("PC")
        self.platform.currentTextChanged.connect(self.fetch_games)
        gl.addWidget(self.platform)
        gl.addWidget(self.games)
        auto = QWidget()
        al = QVBoxLayout(auto)
        hint = QLabel("Pick your IEM and the sound you want. TANCHJIM's server works out 5 EQ bands "
                      "that bend your IEM toward the yellow target. Check the solid curve, then Apply.")
        hint.setWordWrap(True)
        self.iembox, self.targetbox = QComboBox(), QComboBox()
        for box in (self.iembox, self.targetbox):
            box.currentIndexChanged.connect(self.fetch_curves)
        gen = QPushButton("Generate EQ")
        gen.clicked.connect(self.generate)
        for w in (hint, QLabel("Your IEM"), self.iembox, QLabel("Target sound"), self.targetbox, gen):
            al.addWidget(w)
        al.addStretch()
        self.curves = {}  # file id -> parsed curve
        self.autotab = auto
        self.minetab = mine
        self.tabs.addTab(self.official, "Official")
        self.tabs.addTab(comm, "Community")
        self.tabs.addTab(games, "Games")
        self.tabs.addTab(auto, "AutoEQ")
        self.tabs.currentChanged.connect(lambda _: self.graph.update())
        self.tabs.addTab(self.favs, "★")
        self.tabs.addTab(mine, "Mine")

        right = QWidget()
        rl = QVBoxLayout(right)
        self.title = QLabel("<b>Flat</b>")
        self.title.setWordWrap(True)
        self.star = QPushButton("☆ Favorite", toolTip="Add or remove this preset in the ★ tab")
        self.star.clicked.connect(self.toggle_fav)
        self.graph = Graph(self)
        self.graph.setToolTip("Drag a numbered dot to change frequency and gain; scroll on it to change width (Q)")
        head = QHBoxLayout()
        head.addWidget(self.title, 1)
        head.addWidget(self.star, 0, Qt.AlignTop)
        rl.addLayout(head)
        rl.addWidget(self.graph, 1)
        grid = QGridLayout()
        for c, h in enumerate(["", "Type", "Freq (Hz)", "Gain (dB)", "Q"]):
            grid.addWidget(QLabel(f"<b>{h}</b>"), 0, c)
        self.rows = []
        for i in range(BANDS):
            dot = QLabel(f"<span style='color:{COLORS[i]}'>●</span> {i + 1}")
            t = QComboBox()
            for key, label in TYPE_LABEL.items():
                if allowed(i, key):
                    t.addItem(label, key)
            f = QDoubleSpinBox(decimals=0, minimum=20, maximum=20000)
            g = QDoubleSpinBox(decimals=1, minimum=-12, maximum=12, singleStep=0.5)
            q = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=10, singleStep=0.05)
            for w in (t, f, g, q):
                (w.currentIndexChanged if w is t else w.valueChanged).connect(self.edited)
            for c, w in enumerate((dot, t, f, g, q)):
                grid.addWidget(w, i + 1, c)
            self.rows.append((t, f, g, q))
        self.preamp = QDoubleSpinBox(decimals=0, minimum=-12, maximum=0,
                                     toolTip="Lowers overall volume so boosted bands don't distort")
        self.preamp.valueChanged.connect(self.edited)  # marks unsaved; the graph ignores preamp
        grid.addWidget(QLabel("Preamp (dB)"), BANDS + 1, 0, 1, 2)
        grid.addWidget(self.preamp, BANDS + 1, 2)
        self.micgain = QSpinBox(minimum=-15, maximum=15, enabled=False)
        self.micbtn = QPushButton("Set mic gain", enabled=False, toolTip="Save the mic gain on the dongle (it reconnects)")
        self.micbtn.clicked.connect(self.set_mic)
        grid.addWidget(QLabel("Mic gain (dB)"), BANDS + 1, 3)
        grid.addWidget(self.micgain, BANDS + 1, 4)
        grid.addWidget(self.micbtn, BANDS + 2, 4)
        rl.addLayout(grid)

        buttons = QHBoxLayout()
        for label, fn, tip in [
                ("Load from dongle", self.read_device, "Show the EQ saved on the dongle (your edits are thrown away)"),
                ("Apply to dongle", self.apply, "Save this EQ on the dongle. It beeps and reconnects; that is normal."),
                ("Save as mine", self.save_mine, "Keep this EQ in the Mine tab"),
                ("Copy share code", self.copy_code, "Copy this EQ as text in the TANCHJIM app's format"),
                ("Paste share code", self.paste_code, "Load an EQ from a share code or Equalizer APO text")]:
            b = QPushButton(label, toolTip=tip)
            b.clicked.connect(fn)
            buttons.addWidget(b)
        self.applybtn = buttons.itemAt(1).widget()
        self.applybtn.setStyleSheet("font-weight: bold")
        rl.addLayout(buttons)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.tabs)
        tip = QLabel("Click a preset to try it in the editor. Double-click to put it on the dongle.")
        tip.setWordWrap(True)
        ll.addWidget(tip)
        self.settings = read_json(SETTINGS, {})
        self.trbox = QCheckBox("Translate preset names to English", checked=self.settings.get("translate", True),
                               toolTip="Sends public preset names to Google Translate. Off: show them as written.")
        self.trbox.toggled.connect(self.set_translate)
        ll.addWidget(self.trbox)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([400, 700])
        self.setCentralWidget(split)
        self.resize(1180, 680)

        self.current = None
        self.tr = read_json(TCACHE, {})
        self.set_bands(FLAT, 0)
        self.load_mine()
        self.fill(self.favs, read_json(FAVS), raw=True)
        self.connect_device(quiet=True)
        self.fetch_presets()
        self.fetch_games(self.platform.currentText())
        if CURVE_LIST.exists():
            self.fill_autoeq(read_json(CURVE_LIST, {}), save=False)
        # with a device name the server only returns targets, so ask without one to get the IEMs too
        self.post("getEqFileList", {"page": 1, "pageSize": 200}, self.fill_autoeq, device=False)

    # editor
    def edited(self):
        dirty = self.last is not None and clamp(self.bands(), self.preamp.value()) != self.last
        self.setWindowTitle("Halo EQ — unsaved changes" if dirty else "Halo EQ")
        self.applybtn.setText("Apply to dongle ●" if dirty else "Apply to dongle")
        self.graph.update()

    def bands(self):
        return [[t.currentData(), f.value(), g.value(), q.value()] for t, f, g, q in self.rows]

    def set_bands(self, bands, preamp):
        for (t, f, g, q), (bt, bf, bg, bq) in zip(self.rows, bands):
            t.setCurrentIndex(max(0, t.findData(bt)))
            f.setValue(bf)
            g.setValue(bg)
            q.setValue(bq)
        self.preamp.setValue(preamp)

    def load(self, p):
        self.current = p
        self.star.setText("★ Favorite" if self.fav_index(p) is not None else "☆ Favorite")
        self.set_bands(p["bands"], p["preamp"])
        self.show_title(p)

    def show_title(self, p):
        by = f" — by {p['author']}" if p.get("author") else ""
        self.title.setText(f"<b>{self.t(p['name'])}</b>{by}<br><small>{self.t(p.get('detail', ''))}</small>")

    # network
    def post(self, endpoint, body, done, device=True):
        if device:
            body = {"deviceName": self.abbr, "eqPointNum": BANDS, "currentDeviceName": self.devname, **body}
        req = QNetworkRequest(QUrl(API + endpoint))
        req.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        reply = self.net.post(req, json.dumps(body).encode())

        def finished():
            reply.deleteLater()
            try:
                if reply.error() != QNetworkReply.NoError:
                    raise OSError(reply.errorString())
                data = json.loads(bytes(reply.readAll()))
                if data.get("code") != 0:
                    raise OSError(data.get("msg"))
                done(data["data"])
            except (OSError, ValueError, KeyError) as e:
                self.statusBar().showMessage(f"TANCHJIM server: {e}")
        reply.finished.connect(finished)

    def fetch_presets(self):
        self.post("getTanchjimEqList", {"page": 1, "pageSize": 50, "sortType": 0},
                  lambda d: self.fill(self.official, d["content"], clear=True))
        self.fetch_community()

    def fetch_games(self, platform):
        # game presets are shared by all models and carry no deviceName
        self.post("getEqGameListByType", {"page": 1, "pageSize": 100, "deviceType": GAME_PLATFORMS[platform]},
                  lambda d: self.fill(self.games, [from_game(x) for x in d["content"]], clear=True, raw=True))

    # AutoEQ
    def fill_autoeq(self, d, save=True):
        if save:
            write_json(CURVE_LIST, d)
        for box, cat, default in ((self.iembox, "test_curve", self.abbr), (self.targetbox, "target", "TANCHJIM")):
            keep = box.currentText() or default  # the cached list filled it first: keep what was picked
            box.blockSignals(True)
            box.clear()
            for x in sorted((x for x in d.get("content", []) if x["category"] == cat), key=lambda x: x["nickname"]):
                box.addItem(x["nickname"], x)
            box.setCurrentIndex(max(0, box.findText(keep)))
            box.blockSignals(False)
        self.fetch_curves()

    def iem_curves(self):
        """Measurement of the IEM picked in AutoEQ (shown on every tab) and the target (AutoEQ tab only), once downloaded."""
        iem, target = (box.currentData() for box in (self.iembox, self.targetbox))
        iem = self.curves.get(iem["id"]) if iem else None
        target = self.curves.get(target["id"]) if target and self.tabs.currentWidget() is self.autotab else None
        return iem, target

    def fetch_curves(self):
        for box in (self.iembox, self.targetbox):
            x = box.currentData()
            if not x or x["id"] in self.curves:
                continue
            cached = CACHE / f"{x['id']}-{x.get('hashVal', '')[:12]}.txt"
            if cached.exists():
                self.curves[x["id"]] = parse_curve(cached.read_text())
                continue
            reply = self.net.get(QNetworkRequest(QUrl(FILES + x["fileUrl"])))

            def done(reply=reply, x=x, cached=cached):
                reply.deleteLater()
                if reply.error() != QNetworkReply.NoError:
                    return self.statusBar().showMessage(f"Could not download {x['nickname']} curve: {reply.errorString()}")
                text = bytes(reply.readAll()).decode(errors="replace")
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(text)
                self.curves[x["id"]] = parse_curve(text)
                self.graph.update()
            reply.finished.connect(done)
        self.graph.update()

    def generate(self):
        iem, target = self.iembox.currentData(), self.targetbox.currentData()
        if not iem or not target:
            return
        self.statusBar().showMessage("Asking TANCHJIM's AutoEQ server…")
        # measurements above ~10 kHz depend on the rig and insertion depth, and chasing them sounds harsh,
        # so only correct the reliable range and keep the bands gentle
        body = {"selectId": iem["id"], "targetId": target["id"], "eq_range": {"low": 20, "high": 10000}, "fs": 44100,
                "config": "10_PEAKING", "preamp": 0, "max_filters": BANDS, "gain_range": {"low": -8, "high": 8},
                "q_range": {"low": 0.3, "high": 5},
                "treble_gain_k": 0.2}  # AutoEq's treble correction strength (server default 1.0 overshoots)
        self.post("autoEq", body, lambda d: (
            self.load(from_autoeq(d, f"AutoEQ: {iem['nickname']} → {target['nickname']}")),
            self.statusBar().showMessage("AutoEQ ready — check the solid curve, then Apply", 5000)))

    def fetch_community(self, page=1, sort=None, search=None):
        self.page = page
        self.sort = self.sort if sort is None else sort
        self.search = self.search if search is None else search
        body = {"page": page, "pageSize": 30, "sortType": self.sort}
        if self.search:
            self.post("searchEqList", {**body, "searchValue": self.search, "currentIndex": 1},
                      lambda d: self.fill(self.community, d["content"], clear=page == 1))
        else:
            self.post("getExcellentEqList", body, lambda d: self.fill(self.community, d["content"], clear=page == 1))

    def fill(self, lst, items, clear=False, raw=False):
        if clear:
            remove(lst, None)
        for x in items:
            if not raw and (x.get("deviceName") != self.abbr or x.get("eqPointNum") != BANDS):
                continue  # server search can return presets built for another model or band count
            p = x if raw else from_api(x)
            it = QListWidgetItem()
            it.setData(Qt.UserRole, p)
            self.label(it)
            lst.addItem(it)
        self.translate(lst)

    # translation
    def t(self, text):
        return self.tr.get(text, text) if self.trbox.isChecked() else text

    def set_translate(self, on):
        self.settings["translate"] = on
        write_json(SETTINGS, self.settings)
        self.relabel()
        for lst in (self.official, self.community, self.favs, self.mine):
            self.translate(lst)

    def relabel(self):
        for lst in (self.official, self.community, self.games, self.favs, self.mine):
            for i in range(lst.count()):
                self.label(lst.item(i))
        if self.current:
            self.show_title(self.current)

    def label(self, it):
        p = it.data(Qt.UserRole)
        stats = f"\n{p['author']}  ♥ {p['likes']}  ⤓ {p['downloads']}" if p.get("author") else ""
        it.setText(self.t(p["name"]) + stats)
        original = f"{p['name']}\n{p.get('detail', '')}" if CJK.search(p["name"] + p.get("detail", "")) else ""
        it.setToolTip("\n\n".join(x for x in (self.t(p.get("detail", "")), original) if x))

    def translate(self, lst):
        if not self.trbox.isChecked():
            return
        todo = sorted({x for i in range(lst.count()) for x in (lst.item(i).data(Qt.UserRole)["name"],
                                                                lst.item(i).data(Qt.UserRole).get("detail", ""))
                       if x and CJK.search(x) and x not in self.tr})
        while todo:  # the endpoint takes ~5000 characters per request
            chunk = []
            while todo and sum(map(len, chunk)) < 3000:
                chunk.append(todo.pop())
            req = QNetworkRequest(QUrl(TRANSLATE))
            req.setHeader(QNetworkRequest.ContentTypeHeader, "application/x-www-form-urlencoded")
            reply = self.net.post(req, urlencode({"q": "\n".join(c.replace("\n", " ") for c in chunk)}).encode())

            def done(reply=reply, chunk=chunk):
                reply.deleteLater()
                try:
                    lines = "".join(seg[0] for seg in json.loads(bytes(reply.readAll()))[0]).split("\n")
                except (ValueError, TypeError, IndexError):
                    return  # offline or blocked: keep the original names
                if len(lines) != len(chunk):
                    return
                self.tr.update(zip(chunk, (ln.strip() for ln in lines)))
                write_json(TCACHE, self.tr)
                self.relabel()
            reply.finished.connect(done)

    # favorites
    def fav_index(self, p):
        key = (p["name"], p["bands"])
        return next((i for i in range(self.favs.count())
                     if (lambda f: (f["name"], f["bands"]))(self.favs.item(i).data(Qt.UserRole)) == key), None)

    def toggle_fav(self):
        if not self.current:
            return
        i = self.fav_index(self.current)
        if i is None:
            self.fill(self.favs, [self.current], raw=True)
        else:
            remove(self.favs, i)
        write_json(FAVS, [self.favs.item(i).data(Qt.UserRole) for i in range(self.favs.count())])
        self.star.setText("★ Favorite" if i is None else "☆ Favorite")

    # my presets
    def load_mine(self):
        if OLD_STORE.exists() and not STORE.exists():
            STORE.parent.mkdir(parents=True, exist_ok=True)
            OLD_STORE.rename(STORE)
        self.fill(self.mine, read_json(STORE), clear=True, raw=True)

    def store_mine(self):
        write_json(STORE, [self.mine.item(i).data(Qt.UserRole) for i in range(self.mine.count())])

    def backup(self, bands, pre):
        """Keep what the device had when first seen, so it can always be restored."""
        mine = [self.mine.item(i).data(Qt.UserRole) for i in range(self.mine.count())]
        if any((m["bands"], m["preamp"]) == (bands, pre) for m in mine):
            return
        name = f"Device backup {datetime.now():%Y-%m-%d %H:%M}"
        self.fill(self.mine, [{"name": name, "detail": self.dev.name, "bands": bands, "preamp": pre}], raw=True)
        self.store_mine()

    def save_mine(self):
        name, ok = QInputDialog.getText(self, "Save preset", "Name:")
        if ok and name.strip():
            self.fill(self.mine, [{"name": name.strip(), "bands": self.bands(), "preamp": self.preamp.value()}], raw=True)
            self.store_mine()
            self.tabs.setCurrentWidget(self.minetab)

    def delete_mine(self):
        if self.mine.currentRow() >= 0:
            remove(self.mine, self.mine.currentRow())
            self.store_mine()

    def copy_code(self):
        QApplication.clipboard().setText(to_code(self.bands()))
        self.statusBar().showMessage("Preset code copied", 3000)

    def paste_code(self):
        text = QApplication.clipboard().text()
        try:
            if "filter" in text.lower():
                self.load_apo("Pasted", text)
            else:
                self.load({"name": "Pasted", "bands": parse_code(text), "preamp": 0})
        except (ValueError, IndexError):
            self.statusBar().showMessage("Clipboard is not a preset code or Equalizer APO text", 4000)

    def load_apo(self, name, text):
        bands, pre, dropped = parse_apo(text)
        note = f"Dropped {dropped} weaker or unsupported filters (the dongle has {BANDS} bands)." if dropped else ""
        self.load({"name": name, "bands": bands, "preamp": pre, "detail": note})

    def import_apo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Equalizer APO / AutoEQ", str(Path.home()), "Text (*.txt);;All files (*)")
        if path:
            try:
                self.load_apo(Path(path).stem, Path(path).read_text(errors="replace"))
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, "Halo EQ", f"Could not import {path}: {e}")

    def export_apo(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Equalizer APO", str(Path.home() / "ParametricEQ.txt"), "Text (*.txt)")
        if path:
            Path(path).write_text(to_apo(*clamp(self.bands(), self.preamp.value())))
            self.statusBar().showMessage(f"Exported {path}", 4000)

    # device
    def connect_device(self, quiet=False, keep_editor=False):
        if self.dev:
            return self.dev
        try:
            self.dev = Device()
            bands, pre = self.dev.read()
            mode = " — unconfirmed model, read-only" if self.dev.readonly else ""
            if sane(bands, pre):
                self.last = (bands, pre)
                if not self.backed_up:
                    self.backed_up = self.backup(bands, pre) or True
                if not keep_editor:
                    self.load({"name": "On device", "bands": bands, "preamp": pre})
            else:  # values we can't explain mean the register layout is wrong for this unit
                self.dev.readonly = (f"The device reported EQ values that make no sense ({bands}, preamp {pre}), "
                                     "so its memory layout may differ. Writing is blocked; run halo-eq --probe and report.")
                mode = " — unexpected EQ values, read-only (run halo-eq --probe and report)"
            try:
                self.miclast = self.dev.mic()
                self.micgain.setValue(self.miclast)
            except TimeoutError:  # model without a mic register
                self.miclast = None
            self.micgain.setEnabled(self.miclast is not None)
            self.micbtn.setEnabled(self.miclast is not None)
            self.statusBar().showMessage(f"Connected: {self.dev.name} ({self.dev.path}){mode}")
            self.edited()
            if self.dev.abbr and (self.abbr, self.devname) != (self.dev.abbr, self.dev.name):
                self.abbr, self.devname = self.dev.abbr, self.dev.name
                if not quiet:  # startup fetches right after this anyway
                    self.fetch_presets()
        except PermissionError:
            self.dev = None
            self.statusBar().showMessage("No permission on /dev/hidraw — install 70-ktmicro-dsp.rules (see README)")
        except (OSError, TimeoutError) as e:
            self.dev = None
            self.statusBar().showMessage(f"Device: {e}")
        if not self.dev and not quiet:
            QMessageBox.warning(self, "Halo EQ", self.statusBar().currentMessage())
        return self.dev

    def on_device(self, fn):
        if not self.connect_device():
            return
        try:
            fn(self.dev)
        except Unsafe as e:
            QMessageBox.warning(self, "Halo EQ — blocked for safety", str(e))
        except (OSError, TimeoutError) as e:
            self.dev = None  # unplugged or wedged; reopen on next action
            QMessageBox.warning(self, "Halo EQ", f"Device error: {e}")

    def read_device(self):
        def go(d):
            bands, pre = d.read()
            self.load({"name": "On device", "bands": bands, "preamp": pre})
        self.on_device(go)

    def apply(self):
        target = clamp(self.bands(), self.preamp.value())

        def go(d):
            if target == self.last:  # spare the flash a pointless write
                return self.statusBar().showMessage("Device already has this EQ, nothing written", 4000)
            d.write(*target)
            self.last = target
            self.saved("Applied, verified by read-back, saved")
        self.on_device(go)

    def set_mic(self):
        db = self.micgain.value()

        def go(d):
            if db == self.miclast:
                return self.statusBar().showMessage("Device already has this mic gain, nothing written", 4000)
            d.set_mic(db)
            self.miclast = db
            self.saved(f"Mic gain {db:+d} dB verified and saved")
        self.on_device(go)

    def saved(self, msg):
        self.dev = None
        self.edited()
        QTimer.singleShot(2500, lambda: self.connect_device(quiet=True, keep_editor=True))
        self.statusBar().showMessage(msg + " — dongle is restarting…")



if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    if "--probe" in sys.argv:
        probe()
        sys.exit()
    app = QApplication(sys.argv)
    app.setApplicationName("Halo EQ")
    app.setDesktopFileName("halo-eq")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().with_name("icon.svg"))))
    w = Window()
    w.show()
    sys.exit(app.exec())
