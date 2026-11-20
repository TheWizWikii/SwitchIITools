#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Switch II Tools (by TheWizWiki)

Envía payloads JavaScript por IP/puerto, detecta placas Raspberry Pi Pico /
RP2040 / RP2350 y flashea archivos .uf2

Detecta:
  * Placas en modo BOOTSEL (unidad USB "RPI-RP2" / "RP2350" con INFO_UF2.TXT)
  * Placas en ejecución por puerto serie (VID 0x2E8A de Raspberry Pi y placas
    compatibles RP2040/RP2350 de Adafruit, Seeed, Pimoroni, Arduino, etc.)

Funciones:
  * Validación del .uf2 (familia RP2040 / RP2350, nº de bloques, tamaño)
  * Aviso si el .uf2 no corresponde al chip de la placa
  * Reinicio a BOOTSEL desde el puerto serie (truco de 1200 baudios o machine.bootloader() en MicroPython)
  * Flasheo con barra de progreso
  * Modo "auto-flash": flashea cada placa que se conecte en BOOTSEL (producción en serie)
  * Modo línea de comandos (ver --help)

Requisitos: Python 3.8+ (tkinter incluido). Opcional: pyserial (pip install pyserial)
"""

import argparse
import os
import queue
import string
import struct
import sys
import threading
import time

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
    HAVE_SERIAL = True
except ImportError:  # la herramienta sigue funcionando sin pyserial (solo BOOTSEL)
    HAVE_SERIAL = False

APP_NAME = "Switch II Tools"
APP_AUTHOR = "TheWizWiki"
APP_VERSION = "1.0.0"
IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------------------
# UF2
# --------------------------------------------------------------------------------------
UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_NOT_MAIN_FLASH = 0x00000001
UF2_FLAG_FAMILY_ID = 0x00002000

UF2_FAMILIES = {
    0xE48BFF56: ("RP2040", "RP2040"),
    0xE48BFF57: ("ABSOLUTE", "Absoluto (cualquier RP2)"),
    0xE48BFF58: ("DATA", "Datos (cualquier RP2)"),
    0xE48BFF59: ("RP2350", "RP2350 ARM Secure"),
    0xE48BFF5A: ("RP2350", "RP2350 RISC-V"),
    0xE48BFF5B: ("RP2350", "RP2350 ARM Non-Secure"),
}


class UF2Info:
    def __init__(self):
        self.path = ""
        self.size = 0
        self.blocks = 0
        self.declared_blocks = 0
        self.families = set()
        self.payload_bytes = 0
        self.min_addr = None
        self.max_addr = None
        self.errors = []

    @property
    def chips(self):
        """Conjunto de chips compatibles ('RP2040', 'RP2350', 'ABSOLUTE', 'DATA', '?')."""
        return {UF2_FAMILIES.get(f, ("?", ""))[0] for f in self.families}

    def family_text(self):
        if not self.families:
            return "sin family ID"
        parts = []
        for f in sorted(self.families):
            name = UF2_FAMILIES.get(f, (None, "Desconocida"))[1]
            parts.append(f"{name} (0x{f:08X})")
        return ", ".join(parts)

    def summary(self):
        if self.errors:
            return "UF2 NO VÁLIDO: " + "; ".join(self.errors[:3])
        rng = ""
        if self.min_addr is not None:
            rng = f"  ·  0x{self.min_addr:08X}-0x{self.max_addr:08X}"
        return (f"{self.blocks} bloques  ·  {self.payload_bytes / 1024:.1f} KiB de firmware  ·  "
                f"{self.family_text()}{rng}")

    @property
    def valid(self):
        return not self.errors and self.blocks > 0


def parse_uf2(path):
    info = UF2Info()
    info.path = path
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        info.errors.append(f"no se puede leer: {e}")
        return info
    info.size = len(data)
    if len(data) == 0 or len(data) % 512:
        info.errors.append("el tamaño no es múltiplo de 512 bytes")
        if len(data) < 512:
            return info
    for off in range(0, len(data) - 511, 512):
        blk = data[off:off + 512]
        (m0, m1, flags, addr, psize, blkno, nblocks, fam) = struct.unpack_from("<8I", blk, 0)
        (mend,) = struct.unpack_from("<I", blk, 508)
        if m0 != UF2_MAGIC_START0 or m1 != UF2_MAGIC_START1 or mend != UF2_MAGIC_END:
            info.errors.append(f"bloque {off // 512}: números mágicos incorrectos")
            break
        info.blocks += 1
        info.declared_blocks = max(info.declared_blocks, nblocks)
        if flags & UF2_FLAG_FAMILY_ID:
            info.families.add(fam)
        if not flags & UF2_FLAG_NOT_MAIN_FLASH:
            info.payload_bytes += psize
            info.min_addr = addr if info.min_addr is None else min(info.min_addr, addr)
            end = addr + psize - 1
            info.max_addr = end if info.max_addr is None else max(info.max_addr, end)
    return info


# --------------------------------------------------------------------------------------
# Detección de dispositivos
# --------------------------------------------------------------------------------------
class Device:
    """Placa detectada. kind = 'BOOTSEL' (unidad) o 'SERIAL' (puerto)."""

    def __init__(self, kind, location, chip, description, extra=""):
        self.kind = kind
        self.location = location    # "E:\\" o "COM5"
        self.chip = chip            # "RP2040", "RP2350" o "RP2?"
        self.description = description
        self.extra = extra
        self.serial_number = ""
        self.micropython = False

    @property
    def key(self):
        return f"{self.kind}:{self.location}"


def _windows_removable_drives():
    import ctypes
    k32 = ctypes.windll.kernel32
    # Evita los diálogos "No hay disco en la unidad"
    k32.SetErrorMode(0x0001 | 0x8000)
    mask = k32.GetLogicalDrives()
    drives = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i) or letter in "AB":
            continue
        root = f"{letter}:\\"
        if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 2:  # DRIVE_REMOVABLE
            drives.append(root)
    return drives


def _windows_volume_label(root):
    import ctypes
    buf = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), buf, 261, None, None, None, fs, 261)
    return buf.value if ok else ""


def _posix_candidate_mounts():
    """Para pruebas en Linux/macOS (la herramienta está pensada para Windows)."""
    roots = []
    user = os.environ.get("USER", "")
    for base in ("/Volumes", f"/media/{user}", f"/run/media/{user}", "/media", "/mnt"):
        if os.path.isdir(base):
            for name in os.listdir(base):
                p = os.path.join(base, name)
                if os.path.isdir(p):
                    roots.append(p)
    extra = os.environ.get("RP2_FLASHER_EXTRA_MOUNTS", "")
    roots += [p for p in extra.split(os.pathsep) if p]
    return roots


def read_info_uf2(root):
    path = os.path.join(root, "INFO_UF2.TXT")
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            text = fh.read(2048)
    except OSError:
        return None
    fields = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    fields["_raw"] = text
    return fields


def chip_from_info(fields, label=""):
    blob = (fields.get("Model", "") + " " + fields.get("Board-ID", "") + " " + label).upper()
    if "RP2350" in blob:
        return "RP2350"
    if "RP2" in blob or "RP2040" in blob:
        return "RP2040"
    return "RP2?"


def find_bootsel_drives():
    devices = []
    roots = _windows_removable_drives() if IS_WINDOWS else _posix_candidate_mounts()
    for root in roots:
        fields = read_info_uf2(root)
        if fields is None:
            continue
        label = _windows_volume_label(root) if IS_WINDOWS else os.path.basename(root.rstrip("/\\"))
        chip = chip_from_info(fields, label)
        bootloader = fields["_raw"].splitlines()[0].strip() if fields["_raw"] else ""
        desc = fields.get("Model", "Bootloader UF2")
        extra = f"{label}  ·  Board-ID: {fields.get('Board-ID', '?')}  ·  {bootloader}"
        devices.append(Device("BOOTSEL", root, chip, desc, extra))
    return devices


RPI_VID = 0x2E8A
RPI_PIDS = {
    0x0003: ("RP2040", "RP2040 bootloader USB"),
    0x0004: ("RP2040", "Raspberry Pi PicoProbe"),
    0x0005: ("RP2040", "MicroPython (RP2040)"),
    0x0009: ("RP2?", "Pico SDK CDC"),
    0x000A: ("RP2?", "Pico SDK / Arduino (stdio USB)"),
    0x000C: ("RP2040", "Debug Probe (CMSIS-DAP)"),
    0x000F: ("RP2350", "RP2350 bootloader USB"),
    0x0010: ("RP2350", "MicroPython (RP2350)"),
}
# Otros fabricantes con placas RP2040/RP2350 (se filtran además por texto)
OTHER_VIDS = {0x239A: "Adafruit", 0x2886: "Seeed", 0x1209: "pid.codes", 0x2341: "Arduino",
              0x16D0: "MCS", 0x1B4F: "SparkFun", 0x2E8A: "Raspberry Pi", 0x303A: "Espressif"}
RP_KEYWORDS = ("RP2040", "RP2350", "PICO", "RP2 ", "XIAO RP", "FEATHER RP", "QT PY RP",
               "ITSYBITSY RP", "KB2040", "TINY2040", "TINY2350", "PLASMA", "MICROPYTHON BOARD")


def find_serial_devices():
    if not HAVE_SERIAL:
        return []
    devices = []
    for p in list_ports.comports():
        if p.vid is None:
            continue
        text = " ".join(filter(None, [p.description, p.product, p.manufacturer])).upper()
        chip, desc = None, None
        if p.vid == RPI_VID:
            chip, desc = RPI_PIDS.get(p.pid, ("RP2?", f"Raspberry Pi (PID 0x{p.pid:04X})"))
            if p.pid in (0x0004, 0x000C):  # sondas de depuración: no son destinos de flasheo
                continue
        elif any(k in text for k in RP_KEYWORDS) and p.vid in OTHER_VIDS:
            chip = "RP2350" if "2350" in text else ("RP2040" if "2040" in text else "RP2?")
            desc = f"{OTHER_VIDS[p.vid]} {p.product or p.description}"
        if chip is None:
            continue
        if p.product and p.product not in desc:
            desc = f"{desc} - {p.product}"
        dev = Device("SERIAL", p.device, chip, desc,
                     f"VID:PID {p.vid:04X}:{p.pid:04X}  ·  S/N {p.serial_number or '-'}")
        dev.serial_number = p.serial_number or ""
        dev.micropython = "MICROPYTHON" in text or (p.vid == RPI_VID and p.pid in (0x0005, 0x0010))
        devices.append(dev)
    return devices


def scan_devices():
    return find_bootsel_drives() + find_serial_devices()


# --------------------------------------------------------------------------------------
# Acciones
# --------------------------------------------------------------------------------------
def reboot_to_bootsel(port, micropython=False, log=print):
    """Reinicia la placa a BOOTSEL. Devuelve True si se envió la orden."""
    if not HAVE_SERIAL:
        raise RuntimeError("pyserial no está instalado (pip install pyserial)")
    if micropython:
        try:
            log(f"{port}: enviando machine.bootloader() (MicroPython)…")
            with serial.Serial(port, 115200, timeout=1, write_timeout=1) as s:
                s.write(b"\r\x03\x03")          # Ctrl-C: interrumpe el programa en curso
                time.sleep(0.2)
                s.write(b"\r\x02")              # Ctrl-B: REPL normal
                time.sleep(0.1)
                s.write(b"import machine; machine.bootloader()\r")
                time.sleep(0.3)
            return True
        except (serial.SerialException, OSError) as e:
            # el puerto desaparece al reiniciar: suele ser buena señal
            log(f"{port}: {e} (normal si la placa ya se está reiniciando)")
            return True
    log(f"{port}: abriendo a 1200 baudios (reset a BOOTSEL)…")
    try:
        s = serial.Serial()
        s.port = port
        s.baudrate = 1200
        s.dtr = False
        s.open()
        time.sleep(0.1)
        s.close()
    except (serial.SerialException, OSError) as e:
        log(f"{port}: {e} (normal si la placa ya se está reiniciando)")
    return True


def wait_for_new_bootsel(before_keys, timeout=10.0, stop_event=None):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if stop_event and stop_event.is_set():
            return None
        for d in find_bootsel_drives():
            if d.key not in before_keys:
                return d
        time.sleep(0.3)
    return None


def check_compatibility(uf2, chip):
    """Devuelve un texto de aviso si el UF2 parece no ser para ese chip, o '' si OK."""
    chips = uf2.chips - {"ABSOLUTE", "DATA"}
    if not chips or chip == "RP2?":
        return ""
    if "?" in chips:
        return "El UF2 tiene una familia desconocida (puede que no sea para RP2040/RP2350)."
    if chip not in chips:
        return (f"El UF2 es para {'/'.join(sorted(chips))} pero la placa es {chip}. "
                f"El bootloader lo ignorará y la placa no cambiará.")
    return ""


def flash_uf2(uf2_path, drive_root, progress=None, log=print):
    """Copia el .uf2 a la unidad BOOTSEL. progress(fracción 0..1)."""
    dest = os.path.join(drive_root, os.path.basename(uf2_path))
    total = os.path.getsize(uf2_path)
    written = 0
    chunk = 64 * 512
    t0 = time.time()
    log(f"Copiando {os.path.basename(uf2_path)} → {drive_root} ({total / 1024:.1f} KiB)…")
    try:
        with open(uf2_path, "rb") as src, open(dest, "wb", buffering=0) as dst:
            while True:
                buf = src.read(chunk)
                if not buf:
                    break
                dst.write(buf)
                written += len(buf)
                if progress:
                    progress(written / total)
            try:
                os.fsync(dst.fileno())
            except OSError:
                pass
    except OSError as e:
        # La placa se reinicia en cuanto recibe el último bloque y la unidad
        # desaparece: un error al cerrar con todo escrito es normal.
        if written < total:
            raise RuntimeError(f"Error escribiendo en {drive_root}: {e}") from e
    dt = time.time() - t0
    log(f"✔ Flasheo completado en {dt:.1f} s ({total / 1024 / max(dt, 0.001):.0f} KiB/s). "
        f"La placa se reiniciará con el nuevo firmware.")
    return True


# --------------------------------------------------------------------------------------
# Interfaz gráfica
# --------------------------------------------------------------------------------------
DEFAULT_JS_PORT = 9026
JS_IDLE_TIMEOUT = 120

# URL "raw" de un .txt en tu GitHub con la lista de firmwares (.uf2 / .bin / .js).
# Cámbiala por la tuya; también se puede editar luego en %APPDATA%\switch2tools.json.
# Formato del .txt (una entrada por línea; # para comentarios):
#     Nombre visible | https://.../archivo.uf2
#     https://.../firmware.bin           (el nombre se toma del propio archivo)
DEFAULT_CATALOG_URL = "https://raw.githubusercontent.com/TheWizWikii/SwitchIITools/main/files.txt"


def send_js(host, port, code, on_data=None, stop_event=None, connect_timeout=8,
            idle_timeout=JS_IDLE_TIMEOUT):
    """Envía un payload JS al Remote JS Loader y devuelve (en streaming) la salida.

    Igual que send_js.py: envía el código tal cual, cierra la escritura (SHUT_WR)
    y lee hasta que el servidor cierra o pasan `idle_timeout` segundos sin datos.
    """
    import codecs
    import socket
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    out = []
    s = socket.create_connection((host, int(port)), timeout=connect_timeout)
    try:
        s.sendall(code.encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        s.settimeout(1.0)
        last = time.time()
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                if time.time() - last > idle_timeout:
                    break
                continue
            if not chunk:
                break
            last = time.time()
            text = dec.decode(chunk)
            out.append(text)
            if on_data and text:
                on_data(text)
        tail = dec.decode(b"", final=True)
        if tail:
            out.append(tail)
            if on_data:
                on_data(tail)
    finally:
        s.close()
    return "".join(out)


def send_bin(host, port, data, on_log=None, stop_event=None, connect_timeout=8,
             idle_timeout=JS_IDLE_TIMEOUT):
    """Envía un .bin (bytes en bruto) por la misma conexión IP:puerto.

    Igual que send_js pero sin decodificar: envía los bytes tal cual, cierra la
    escritura y devuelve la respuesta del servidor como texto (si la hay).
    """
    import codecs
    import socket
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    out = []
    s = socket.create_connection((host, int(port)), timeout=connect_timeout)
    try:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        s.settimeout(1.0)
        last = time.time()
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                if time.time() - last > idle_timeout:
                    break
                continue
            if not chunk:
                break
            last = time.time()
            text = dec.decode(chunk)
            if text:
                out.append(text)
                if on_log:
                    on_log(text)
        tail = dec.decode(b"", final=True)
        if tail:
            out.append(tail)
            if on_log:
                on_log(tail)
    finally:
        s.close()
    return "".join(out)


def parse_catalog(text):
    """Lee el .txt del catálogo. Devuelve [(nombre, url, ext), ...]."""
    import urllib.parse
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, _, url = line.partition("|")
        elif "=" in line and "://" in line.split("=", 1)[1]:
            name, _, url = line.partition("=")
        else:
            name, url = "", line
        name, url = name.strip(), url.strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        fname = os.path.basename(urllib.parse.urlparse(url).path)
        if not name:
            name = fname or url
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        items.append((name, url, ext))
    return items


def fetch_catalog(url, timeout=15):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(1 << 20)
    return parse_catalog(raw.decode("utf-8", "replace"))


def download_file(url, dest_dir, timeout=60, progress=None):
    """Descarga `url` a `dest_dir` y devuelve la ruta local."""
    import urllib.parse
    import urllib.request
    os.makedirs(dest_dir, exist_ok=True)
    fname = os.path.basename(urllib.parse.urlparse(url).path) or "download.bin"
    dest = os.path.join(dest_dir, fname)
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    os.replace(tmp, dest)
    return dest


def run_gui(initial_file=None):
    import json
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import tkinter.font as tkfont

    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TheWizWiki.SwitchIITools")
        except Exception:
            pass

    # ---------------- paleta minimalista
    THEMES = {
        "light": dict(BG="#f5f6f8", CARD="#ffffff", LINE="#e3e6ea", TEXT="#16181d", MUTED="#6b7280",
                      ACCENT="#2563eb", ACCENT_H="#1d4ed8", FIELD="#ffffff", RED="#dc2626",
                      GREEN="#16a34a", AMBER="#d97706", TRACK="#eef0f3", HOVER="#f3f4f6",
                      DIS_BG="#e5e7eb", DIS_FG="#a3a9b3", BORDER2="#d5d9de", OUTBG="#fafafa",
                      SEL="#dbe5fb", TS="#9ca3af"),
        "dark": dict(BG="#0e1015", CARD="#171a21", LINE="#272c36", TEXT="#e6e8ec", MUTED="#8b93a1",
                     ACCENT="#3b82f6", ACCENT_H="#2563eb", FIELD="#11141a", RED="#f87171",
                     GREEN="#4ade80", AMBER="#fbbf24", TRACK="#252a34", HOVER="#222732",
                     DIS_BG="#232832", DIS_FG="#5b6372", BORDER2="#353b47", OUTBG="#11141a",
                     SEL="#1e3a6e", TS="#5b6372"),
    }
    BG = CARD = LINE = TEXT = MUTED = ACCENT = ACCENT_H = FIELD = RED = GREEN = AMBER = None
    TRACK = HOVER = DIS_BG = DIS_FG = BORDER2 = OUTBG = SEL = TS = None

    def apply_palette(name):
        nonlocal BG, CARD, LINE, TEXT, MUTED, ACCENT, ACCENT_H, FIELD, RED, GREEN, AMBER
        nonlocal TRACK, HOVER, DIS_BG, DIS_FG, BORDER2, OUTBG, SEL, TS
        t = THEMES[name]
        (BG, CARD, LINE, TEXT, MUTED, ACCENT, ACCENT_H, FIELD, RED, GREEN, AMBER,
         TRACK, HOVER, DIS_BG, DIS_FG, BORDER2, OUTBG, SEL, TS) = (
            t[k] for k in ("BG", "CARD", "LINE", "TEXT", "MUTED", "ACCENT", "ACCENT_H", "FIELD", "RED",
                           "GREEN", "AMBER", "TRACK", "HOVER", "DIS_BG", "DIS_FG", "BORDER2", "OUTBG",
                           "SEL", "TS"))

    CONFIG_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "switch2tools.json")

    TR = {
        "es": {
            "board": "PLACA", "conn": "CONEXIÓN", "out": "SALIDA",
            "no_board": "Sin placa detectada", "program": "Programar",
            "bootsel": "Reiniciar a BOOTSEL", "auto": "Auto-flash",
            "no_file": "Ningún archivo .uf2 seleccionado", "no_js": "Ningún archivo .js seleccionado",
            "boot_state": "{chip} en BOOTSEL · {loc}", "run_state": "{chip} en {loc}",
            "flashing": "Programando… {p:.0f}%", "done": "Programada",
            "count": "{n} programadas", "need_file": "Selecciona primero un archivo .uf2 válido.",
            "force": "¿Programar de todos modos?", "waiting": "Esperando la unidad BOOTSEL…",
            "ip": "IP", "port": "Puerto", "send": "Enviar", "cancel": "Cancelar",
            "sending": "Enviando a {host}:{port}…", "sent": "Conexión cerrada · {n} bytes recibidos",
            "conn_err": "No se pudo conectar: {e}", "bad_ip": "Introduce una IP válida.",
            "bad_port": "El puerto debe ser un número entre 1 y 65535.",
            "clear": "Limpiar", "ready": "Listo", "file": "Archivo", "cancelled": "Cancelado",
            "dark": "☾  Modo oscuro", "light": "☀  Modo claro", "blocks": "bloques",
            "js_file": "Archivo .js", "bin_file": "Archivo .bin",
            "no_bin": "Ningún archivo .bin seleccionado", "save": "Instalar Savedata",
            "saving": "Enviando .bin a {host}:{port}…", "saved": "Enviado · {n} bytes de respuesta",
            "catalog": "☁ GitHub", "cat_loading": "Leyendo el catálogo de GitHub…",
            "cat_ok": "Catálogo actualizado · {n} archivos", "cat_none": "El catálogo está vacío.",
            "cat_err": "No se pudo leer el catálogo: {e}",
            "downloading": "Descargando {name}…", "downloaded": "Descargado {name}",
            "dl_err": "No se pudo descargar {name}: {e}",
            "clear_cache": "🗑  Borrar caché",
            "cache_title": "Borrar caché",
            "cache_confirm": "Se eliminará la carpeta de archivos descargados:\n\n{path}\n\n¿Continuar?",
            "cache_ok": "Caché borrada · {n} archivo(s) eliminado(s)",
            "cache_empty": "La caché ya está vacía.",
            "cache_err": "No se pudo borrar del todo la caché: {e}",
        },
        "en": {
            "board": "BOARD", "conn": "CONNECTION", "out": "OUTPUT",
            "no_board": "No board detected", "program": "Flash",
            "bootsel": "Reboot to BOOTSEL", "auto": "Auto-flash",
            "no_file": "No .uf2 file selected", "no_js": "No .js file selected",
            "boot_state": "{chip} in BOOTSEL · {loc}", "run_state": "{chip} on {loc}",
            "flashing": "Flashing… {p:.0f}%", "done": "Flashed",
            "count": "{n} flashed", "need_file": "Select a valid .uf2 file first.",
            "force": "Flash anyway?", "waiting": "Waiting for BOOTSEL drive…",
            "ip": "IP", "port": "Port", "send": "Send", "cancel": "Cancel",
            "sending": "Sending to {host}:{port}…", "sent": "Connection closed · {n} bytes received",
            "conn_err": "Could not connect: {e}", "bad_ip": "Enter a valid IP.",
            "bad_port": "Port must be a number between 1 and 65535.",
            "clear": "Clear", "ready": "Ready", "file": "File", "cancelled": "Cancelled",
            "dark": "☾  Dark mode", "light": "☀  Light mode", "blocks": "blocks",
            "js_file": ".js file", "bin_file": ".bin file",
            "no_bin": "No .bin file selected", "save": "Install Savedata",
            "saving": "Sending .bin to {host}:{port}…", "saved": "Sent · {n} bytes of response",
            "catalog": "☁ GitHub", "cat_loading": "Reading the GitHub catalog…",
            "cat_ok": "Catalog updated · {n} files", "cat_none": "The catalog is empty.",
            "cat_err": "Could not read the catalog: {e}",
            "downloading": "Downloading {name}…", "downloaded": "Downloaded {name}",
            "dl_err": "Could not download {name}: {e}",
            "clear_cache": "🗑  Clear cache",
            "cache_title": "Clear cache",
            "cache_confirm": "The downloaded files folder will be deleted:\n\n{path}\n\nContinue?",
            "cache_ok": "Cache cleared · {n} file(s) deleted",
            "cache_empty": "The cache is already empty.",
            "cache_err": "Cache could not be fully cleared: {e}",
        },
    }
    LANGS = [("es", "Español"), ("en", "English")]

    class ThinBar(tk.Canvas):
        """Barra de progreso fina (4 px) de estilo plano."""

        def __init__(self, master):
            super().__init__(master, height=4, bg=TRACK, highlightthickness=0, bd=0)
            self._v = 0
            self.fill = self.create_rectangle(0, 0, 0, 4, fill=ACCENT, outline="")
            self.bind("<Configure>", lambda e: self._draw())

        def __setitem__(self, key, value):
            if key == "value":
                self._v = max(0, min(1000, value))
                self._draw()
            else:
                super().__setitem__(key, value)

        def _draw(self):
            self.coords(self.fill, 0, 0, self.winfo_width() * self._v / 1000, 4)

    class App:
        def __init__(self, root):
            self.root = root
            self.cfg = self.load_cfg()
            self.lang = self.cfg.get("lang", "es")
            self.uf2 = None
            self.devices, self.dev_keys = {}, []
            self.busy = False
            self.sending = False
            self.send_stop = threading.Event()
            self.flashed = 0
            self.seen_bootsel = set()
            self.q = queue.Queue()
            self.stop = threading.Event()
            self.i18n = []          # (widget/callable, clave)
            self.theme = self.cfg.get("theme", "light")
            if self.theme not in THEMES:
                self.theme = "light"
            apply_palette(self.theme)
            self.board_status = ("ready", {}, MUTED)
            self.conn_status = ("ready", {}, MUTED)
            self.records = []       # historial de la salida [(texto, tag)] para redibujar
            self.js_path = ""
            self.bin_path = ""
            self.saving = False
            self.save_stop = threading.Event()
            self.catalog = []       # [(nombre, url, ext)] leído del .txt de GitHub
            self.catalog_url = self.cfg.get("catalog_url", DEFAULT_CATALOG_URL)
            _base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                     or os.path.expanduser("~"))
            self.cache_dir = os.path.join(_base, "SwitchIITools", "cache")
            self.cache_root = os.path.join(_base, "SwitchIITools")
            self._uf2_entries = self._js_entries = self._bin_entries = []
            self._uf2_txt = None
            self._prog_v = 0

            ui = "Segoe UI" if IS_WINDOWS else "DejaVu Sans"
            mono = "Consolas" if IS_WINDOWS else "DejaVu Sans Mono"
            self.f = (ui, 10)
            self.f_b = (ui, 10, "bold")
            self.f_h = (ui, 8, "bold")
            self.f_s = (ui, 9)
            self.f_state = (ui, 11, "bold")
            self.f_mono = (mono, 9)
            self.f_title = (ui, 15, "bold")
            self.f_author = (ui, 9, "bold")

            root.title(f"{APP_NAME} — by {APP_AUTHOR}")
            # Tamaño de diseño base (pensado para 100% de escala / 96 DPI).
            self.base_w, self.base_h = 880, 620
            root.geometry(f"{self.base_w}x{self.base_h}")
            self.set_icon()
            self.ip_var = tk.StringVar(value=self.cfg.get("ip", "192.168.1.100"))
            self.port_var = tk.StringVar(value=str(self.cfg.get("port", DEFAULT_JS_PORT)))
            self.auto_var = tk.BooleanVar(value=False)
            self.build_ui()
            if not HAVE_SERIAL:
                self.log("pyserial no instalado: solo se detectan placas en BOOTSEL.")
            rec = [f for f in self.cfg.get("recent", []) if os.path.isfile(f)]
            if initial_file and initial_file.lower().endswith(".js"):
                self.set_js(initial_file)
            elif initial_file and initial_file.lower().endswith(".bin"):
                self.set_bin(initial_file)
            elif initial_file:
                self.load_uf2(initial_file)
            elif rec:
                self.load_uf2(rec[0])
            self.refresh_uf2_list()
            self.refresh_js_list()
            self.refresh_bin_list()
            js = [f for f in self.cfg.get("recent_js", []) if os.path.isfile(f)]
            if js and not self.js_path:
                self.set_js(js[0])
            bn = [f for f in self.cfg.get("recent_bin", []) if os.path.isfile(f)]
            if bn and not self.bin_path:
                self.set_bin(bn[0])

            root.after(100, self.process_queue)
            threading.Thread(target=self.scan_loop, daemon=True).start()
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            # Ajustar la ventana al contenido una vez que todo está renderizado,
            # teniendo en cuenta el escalado de pantalla (DPI) de cada equipo.
            root.after(0, self.fit_window)

        # ================= ajuste de tamaño según pantalla / escalado
        def fit_window(self):
            """Dimensiona la ventana al contenido real, respetando el escalado
            de Windows (100/125/150/200%) y sin salirse de la pantalla.
            Si todo cabe, la deja fija (no redimensionable). Si la pantalla es
            demasiado pequeña para el contenido, permite redimensionar para que
            nada quede oculto."""
            r = self.root
            try:
                r.update_idletasks()
                # Factor de escalado real (DPI del equipo). 96 DPI = 100%.
                try:
                    scale = r.winfo_fpixels("1i") / 96.0
                except Exception:
                    scale = 1.0
                if scale <= 0:
                    scale = 1.0
                # Ancho = diseño base escalado (reproduce la intención del autor a
                # cualquier escala; no usamos el ancho pedido porque las rutas de
                # archivo largas lo inflarían sin límite).
                w = int(round(self.base_w * scale))
                # Alto = diseño base escalado, con un suelo igual a lo que Tk pide
                # para mostrar TODO en vertical (traducciones largas, etc.). Esto
                # garantiza que los botones de abajo nunca queden cortados.
                h = max(int(round(self.base_h * scale)), r.winfo_reqheight())
                # Nunca mayor que el área utilizable de la pantalla.
                sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
                max_w, max_h = int(sw * 0.96), int(sh * 0.92)
                cabe = (w <= max_w) and (h <= max_h)
                w, h = min(w, max_w), min(h, max_h)
                # Centrar (un poco por encima del centro vertical).
                x = max(0, (sw - w) // 2)
                y = max(0, (sh - h) // 3)
                r.geometry(f"{w}x{h}+{x}+{y}")
                if cabe:
                    r.minsize(w, h)
                    r.maxsize(w, h)
                    r.resizable(False, False)
                else:
                    # Pantalla pequeña o escalado muy alto: dejar que el usuario
                    # ajuste para ver todo el contenido.
                    r.minsize(min(760, w), min(520, h))
                    r.maxsize(max_w, max_h)
                    r.resizable(True, True)
            except Exception:
                pass

        # ================= construcción / tema
        def build_ui(self):
            self.i18n = []
            self.root.configure(bg=BG)
            self.setup_style()
            self.main = tk.Frame(self.root, bg=BG)
            self.main.pack(fill="both", expand=True)

            # cabecera: nombre + autor | tema + idioma
            head = tk.Frame(self.main, bg=BG)
            head.pack(fill="x", padx=16, pady=(14, 0))
            tk.Label(head, text=APP_NAME, bg=BG, fg=TEXT, font=self.f_title).pack(side="left")
            tk.Label(head, text=f"by {APP_AUTHOR}", bg=BG, fg=ACCENT,
                     font=self.f_author).pack(side="left", padx=(8, 0), pady=(6, 0))
            self.lang_cb = ttk.Combobox(head, state="readonly", width=9, values=[n for _, n in LANGS],
                                        style="Head.TCombobox")
            self.lang_cb.current([c for c, _ in LANGS].index(self.lang))
            self.lang_cb.bind("<<ComboboxSelected>>", self.on_lang)
            # Boton "Borrar cache": al final del todo (extremo derecho), rojo discreto.
            self.btn_cache = ttk.Button(head, style="HeadDanger.TButton", command=self.clear_cache)
            self.tr(self.btn_cache, "clear_cache")
            self.btn_cache.pack(side="right", padx=(8, 0))
            self.lang_cb.pack(side="right")
            self.btn_theme = ttk.Button(head, style="HeadGhost.TButton", command=self.toggle_theme)
            self.tr(self.btn_theme, "light" if self.theme == "dark" else "dark")
            self.btn_theme.pack(side="right", padx=(0, 8))
            self.btn_cat = ttk.Button(head, style="HeadGhost.TButton", command=self.load_catalog)
            self.tr(self.btn_cat, "catalog")
            self.btn_cat.pack(side="right", padx=(0, 8))

            outer = tk.Frame(self.main, bg=BG)
            outer.pack(fill="both", expand=True, padx=14, pady=(12, 0))
            outer.columnconfigure(0, weight=1, uniform="c")
            outer.columnconfigure(1, weight=1, uniform="c")
            outer.rowconfigure(1, weight=1)
            self.build_board(self.card(outer, 0, 0, "board", padx=(0, 7)))
            self.build_conn(self.card(outer, 0, 1, "conn", padx=(7, 0)))
            self.build_output(self.card(outer, 1, 0, "out", colspan=2, pady=(14, 0)))

            # pie: versión | firma del autor
            foot = tk.Frame(self.main, bg=BG)
            foot.pack(fill="x", padx=16, pady=(6, 8))
            tk.Label(foot, text=f"v{APP_VERSION}", bg=BG, fg=MUTED, font=self.f_s).pack(side="left")
            tk.Label(foot, text=f"{APP_NAME} · by {APP_AUTHOR}", bg=BG, fg=MUTED,
                     font=self.f_s).pack(side="right")

            self.dark_titlebar()
            self.apply_lang()

        def dark_titlebar(self):
            if not IS_WINDOWS:
                return
            try:
                import ctypes
                self.root.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                val = ctypes.c_int(1 if self.theme == "dark" else 0)
                for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 2004+ / anteriores)
                    if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                        break
            except Exception:
                pass

        def toggle_theme(self):
            old = {v: k for k, v in THEMES[self.theme].items()}
            self.theme = "dark" if self.theme == "light" else "light"
            self.cfg["theme"] = self.theme
            self.save_cfg()
            apply_palette(self.theme)
            new = THEMES[self.theme]

            def remap(st):
                key, kw, color = st
                return (key, kw, new.get(old.get(color, "MUTED"), MUTED))
            self.board_status = remap(self.board_status)
            self.conn_status = remap(self.conn_status)
            if self._uf2_txt:
                self._uf2_txt = (self._uf2_txt[0], new.get(old.get(self._uf2_txt[1], "MUTED")))
            self.main.destroy()
            self.build_ui()
            # restaurar estado visible (refresh_uf2_list ya reselecciona el archivo actual)
            self.refresh_uf2_list()
            self.show_uf2_info()
            self.refresh_js_list()
            if self.js_path:
                self.show_js_info()
            self.refresh_bin_list()
            if self.bin_path:
                self.show_bin_info()
            self.prog["value"] = self._prog_v
            self.populate_devices()
            self.update_send_btn()
            self.update_ui()
            self.out.configure(state="normal")
            for text, tag in self.records:
                self.out.insert("end", text, tag)
            self.out.see("end")
            self.out.configure(state="disabled")

        # ================= estilo y helpers de layout
        def setup_style(self):
            st = ttk.Style()
            st.theme_use("clam")
            st.configure(".", background=CARD, foreground=TEXT, font=self.f, bordercolor=LINE,
                         focuscolor=ACCENT)
            st.configure("TEntry", fieldbackground=FIELD, bordercolor=LINE, lightcolor=FIELD,
                         darkcolor=FIELD, padding=6, insertcolor=TEXT)
            st.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
            st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, bordercolor=LINE,
                         lightcolor=FIELD, darkcolor=FIELD, arrowcolor=MUTED, padding=5,
                         selectbackground=FIELD, selectforeground=TEXT)
            st.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", HOVER)],
                   foreground=[("disabled", DIS_FG)], bordercolor=[("focus", ACCENT)],
                   arrowcolor=[("disabled", DIS_FG)])
            self.root.option_add("*TCombobox*Listbox.font", self.f)
            self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
            self.root.option_add("*TCombobox*Listbox.relief", "flat")
            st.configure("Accent.TButton", background=ACCENT, foreground="white", font=self.f_b,
                         bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT, padding=(14, 8),
                         relief="flat")
            st.map("Accent.TButton",
                   background=[("disabled", DIS_BG), ("pressed", ACCENT_H), ("active", ACCENT_H)],
                   foreground=[("disabled", DIS_FG)],
                   bordercolor=[("disabled", DIS_BG), ("active", ACCENT_H)],
                   lightcolor=[("disabled", DIS_BG), ("active", ACCENT_H)],
                   darkcolor=[("disabled", DIS_BG), ("active", ACCENT_H)])
            st.configure("Ghost.TButton", background=CARD, foreground=TEXT, bordercolor=BORDER2,
                         lightcolor=CARD, darkcolor=CARD, padding=(10, 5), relief="solid", borderwidth=1)
            st.map("Ghost.TButton", background=[("active", HOVER), ("disabled", CARD)],
                   foreground=[("disabled", DIS_FG)], bordercolor=[("active", BORDER2)])
            st.configure("Link.TButton", background=CARD, foreground=MUTED, borderwidth=0,
                         padding=(4, 0), font=self.f_s, relief="flat", lightcolor=CARD, darkcolor=CARD,
                         bordercolor=CARD)
            st.map("Link.TButton", foreground=[("active", ACCENT)], background=[("active", CARD)])
            st.configure("TCheckbutton", background=CARD, foreground=TEXT, font=self.f_s)
            st.map("TCheckbutton", background=[("active", CARD)],
                   indicatorcolor=[("selected", ACCENT), ("!selected", FIELD)])
            st.configure("Thin.Horizontal.TProgressbar", troughcolor=TRACK, background=ACCENT,
                         bordercolor=TRACK, lightcolor=ACCENT, darkcolor=ACCENT, thickness=4)
            st.configure("Vertical.TScrollbar", background=CARD, troughcolor=OUTBG, bordercolor=OUTBG,
                         lightcolor=CARD, darkcolor=CARD, arrowcolor=MUTED, gripcount=0)
            st.map("Vertical.TScrollbar", background=[("active", HOVER)])
            st.configure("Head.TCombobox", fieldbackground=CARD, background=CARD, padding=4)
            st.map("Head.TCombobox", fieldbackground=[("readonly", CARD)])
            st.configure("HeadGhost.TButton", background=BG, foreground=TEXT, bordercolor=BORDER2,
                         lightcolor=BG, darkcolor=BG, padding=(10, 4), relief="solid", borderwidth=1)
            st.map("HeadGhost.TButton", background=[("active", CARD)], bordercolor=[("active", MUTED)])
            # Boton de accion destructiva (Borrar cache): rojo apagado, discreto.
            danger_fg = "#c0352b" if self.theme == "light" else "#e06c6c"
            danger_hover = "#f7e8e6" if self.theme == "light" else "#2a1b1e"
            st.configure("HeadDanger.TButton", background=BG, foreground=danger_fg, bordercolor=BORDER2,
                         lightcolor=BG, darkcolor=BG, padding=(10, 4), relief="solid", borderwidth=1)
            st.map("HeadDanger.TButton", background=[("active", danger_hover)],
                   bordercolor=[("active", danger_fg)], foreground=[("active", danger_fg)])
            self.root.option_add("*TCombobox*Listbox.background", FIELD)
            self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
            self.root.option_add("*TCombobox*Listbox.selectForeground", "white")

        def card(self, parent, r, c, title_key, colspan=1, padx=0, pady=0):
            wrap = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE,
                            highlightcolor=LINE)
            wrap.grid(row=r, column=c, columnspan=colspan, sticky="nsew", padx=padx, pady=pady)
            head = tk.Frame(wrap, bg=CARD)
            head.pack(fill="x", padx=16, pady=(12, 6))
            lbl = tk.Label(head, bg=CARD, fg=MUTED, font=self.f_h)
            lbl.pack(side="left")
            self.tr(lbl, title_key)
            body = tk.Frame(wrap, bg=CARD)
            body.pack(fill="both", expand=True, padx=16, pady=(0, 14))
            body.head = head
            return body

        def tr(self, widget, key):
            self.i18n.append((widget, key))

        def label(self, parent, key=None, **kw):
            opts = dict(bg=CARD, fg=MUTED, font=self.f_s, anchor="w")
            opts.update(kw)
            w = tk.Label(parent, **opts)
            if key:
                self.tr(w, key)
            return w

        def T(self, k, **kw):
            return TR[self.lang][k].format(**kw)

        # ================= panel PLACA
        def build_board(self, b):
            b.columnconfigure(0, weight=1)
            row = tk.Frame(b, bg=CARD)
            row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(2, 10))
            self.dot = tk.Canvas(row, width=12, height=12, bg=CARD, highlightthickness=0)
            self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=RED, outline="")
            self.dot.pack(side="left", padx=(0, 8))
            self.state_lbl = tk.Label(row, bg=CARD, fg=TEXT, font=self.f_state, anchor="w")
            self.state_lbl.pack(side="left", fill="x", expand=True)

            self.dev_cb = ttk.Combobox(b, state="disabled")
            self.dev_cb.bind("<<ComboboxSelected>>", lambda e: self.update_ui())
            self.dev_cb.grid(row=1, column=0, columnspan=2, sticky="ew")

            self.label(b, "file").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 2))
            urow = tk.Frame(b, bg=CARD)
            urow.grid(row=3, column=0, columnspan=2, sticky="ew")
            self.uf2_cb = ttk.Combobox(urow, state="readonly")
            self.uf2_cb.bind("<<ComboboxSelected>>", self.on_uf2_pick)
            self.uf2_cb.pack(side="left", fill="x", expand=True)
            ttk.Button(urow, text="…", width=3, style="Ghost.TButton",
                       command=self.browse_uf2).pack(side="left", padx=(6, 0))
            self.uf2_info = self.label(b)
            self.uf2_info.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

            self.prog = ThinBar(b)
            self.prog.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 0))

            bottom = tk.Frame(b, bg=CARD)
            bottom.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 0))
            self.btn_flash = ttk.Button(bottom, style="Accent.TButton", command=self.flash_selected)
            self.tr(self.btn_flash, "program")
            self.btn_flash.pack(side="left")
            self.btn_boot = ttk.Button(bottom, style="Ghost.TButton", command=self.reboot_selected)
            self.tr(self.btn_boot, "bootsel")
            self.btn_boot.pack(side="left", padx=(8, 0))
            self.chk_auto = ttk.Checkbutton(b, variable=self.auto_var, command=self.on_auto)
            self.tr(self.chk_auto, "auto")
            self.chk_auto.grid(row=7, column=1, sticky="e", pady=(8, 0))

            self.board_lbl = self.label(b)
            self.board_lbl.grid(row=7, column=0, sticky="w", pady=(8, 0))

        # ================= panel CONEXIÓN
        def build_conn(self, b):
            b.columnconfigure(0, weight=1)
            self.label(b, "ip").grid(row=0, column=0, sticky="w", pady=(2, 2))
            self.label(b, "port").grid(row=0, column=1, sticky="w", pady=(2, 2), padx=(8, 0))
            ip_e = ttk.Entry(b, textvariable=self.ip_var, font=self.f)
            ip_e.grid(row=1, column=0, sticky="ew")
            port_e = ttk.Entry(b, textvariable=self.port_var, width=8, font=self.f, justify="center")
            port_e.grid(row=1, column=1, sticky="ew", padx=(8, 0))
            for e in (ip_e, port_e):
                e.bind("<Return>", lambda ev: self.send_clicked())

            self.label(b, "js_file").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 2))
            jrow = tk.Frame(b, bg=CARD)
            jrow.grid(row=3, column=0, columnspan=2, sticky="ew")
            self.js_cb = ttk.Combobox(jrow, state="readonly")
            self.js_cb.bind("<<ComboboxSelected>>", self.on_js_pick)
            self.js_cb.pack(side="left", fill="x", expand=True)
            ttk.Button(jrow, text="…", width=3, style="Ghost.TButton",
                       command=self.browse_js).pack(side="left", padx=(6, 0))
            self.js_info = self.label(b)
            self.js_info.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

            self.label(b, "bin_file").grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 2))
            brow = tk.Frame(b, bg=CARD)
            brow.grid(row=6, column=0, columnspan=2, sticky="ew")
            self.bin_cb = ttk.Combobox(brow, state="readonly")
            self.bin_cb.bind("<<ComboboxSelected>>", self.on_bin_pick)
            self.bin_cb.pack(side="left", fill="x", expand=True)
            ttk.Button(brow, text="…", width=3, style="Ghost.TButton",
                       command=self.browse_bin).pack(side="left", padx=(6, 0))
            self.bin_info = self.label(b)
            self.bin_info.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))

            bottom = tk.Frame(b, bg=CARD)
            bottom.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(12, 0))
            self.btn_send = ttk.Button(bottom, style="Accent.TButton", command=self.send_clicked)
            self.btn_send.pack(side="left")
            self.btn_save = ttk.Button(bottom, style="Ghost.TButton", command=self.save_clicked)
            self.btn_save.pack(side="left", padx=(8, 0))
            self.conn_lbl = self.label(b)
            self.conn_lbl.grid(row=9, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # ================= panel SALIDA
        def build_output(self, b):
            clr = ttk.Button(b.head, style="Link.TButton", command=self.clear_log)
            self.tr(clr, "clear")
            clr.pack(side="right")

            frame = tk.Frame(b, bg=OUTBG, highlightthickness=1, highlightbackground=LINE,
                             highlightcolor=LINE)
            frame.pack(fill="both", expand=True)
            self.out = tk.Text(frame, wrap="word", bg=OUTBG, fg=TEXT, relief="flat", bd=0,
                               font=self.f_mono, padx=10, pady=8, height=8, state="disabled", highlightthickness=0,
                               insertbackground=TEXT, selectbackground=SEL)
            sb = ttk.Scrollbar(frame, orient="vertical", command=self.out.yview)
            self.out.configure(yscrollcommand=sb.set)
            self.out.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.out.tag_configure("ts", foreground=TS)
            self.out.tag_configure("info", foreground=MUTED)
            self.out.tag_configure("ok", foreground=GREEN)
            self.out.tag_configure("err", foreground=RED)
            self.out.tag_configure("js", foreground=TEXT)

        # ================= utilidades
        def set_icon(self):
            try:
                img = tk.PhotoImage(width=32, height=32)
                img.put("#16181d", to=(0, 0, 32, 32))
                img.put("#e60012", to=(3, 5, 11, 27))     # mando izquierdo
                img.put("#2563eb", to=(21, 5, 29, 27))    # mando derecho
                img.put("#3a3f4b", to=(11, 7, 21, 25))    # pantalla
                img.put("white", to=(14, 13, 18, 19))
                self.root.iconphoto(True, img)
                self._icon = img
            except Exception:
                pass

        def load_cfg(self):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return {}

        def save_cfg(self):
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                    json.dump(self.cfg, fh, ensure_ascii=False, indent=1)
            except Exception:
                pass

        def post(self, fn, *a, **kw):
            self.q.put((fn, a, kw))

        def process_queue(self):
            try:
                while True:
                    fn, a, kw = self.q.get_nowait()
                    fn(*a, **kw)
            except queue.Empty:
                pass
            self.root.after(60, self.process_queue)

        def write(self, text, tag="js"):
            self.records.append((text, tag))
            if len(self.records) > 6000:
                del self.records[:1000]
            self.out.configure(state="normal")
            self.out.insert("end", text, tag)
            self.out.see("end")
            self.out.configure(state="disabled")

        def log(self, msg, tag="info"):
            if self.out.index("end-1c") != "1.0" and self.out.get("end-2c") != "\n":
                self.write("\n")
            self.write(time.strftime("%H:%M:%S  "), "ts")
            self.write(msg + "\n", tag)

        def tlog(self, msg, tag="info"):
            self.post(self.log, msg, tag)

        def clear_log(self):
            self.records.clear()
            self.out.configure(state="normal")
            self.out.delete("1.0", "end")
            self.out.configure(state="disabled")

        def clear_cache(self):
            """Elimina la carpeta SwitchIITools (archivos descargados) de
            %LOCALAPPDATA%/%APPDATA%, con confirmacion previa."""
            import shutil
            root = self.cache_root
            n = 0
            if os.path.isdir(root):
                for _d, _subs, files in os.walk(root):
                    n += len(files)
            if not os.path.isdir(root) or n == 0:
                if os.path.isdir(root):
                    shutil.rmtree(root, ignore_errors=True)
                self.log(self.T("cache_empty"))
                return
            if not messagebox.askyesno(self.T("cache_title"),
                                       self.T("cache_confirm", path=root), icon="warning"):
                return
            errores = []
            def _onerr(func, path, exc):
                errores.append(path)
            shutil.rmtree(root, onerror=_onerr)
            if errores:
                self.log(self.T("cache_err", e=f"{len(errores)} en uso"), "err")
            else:
                self.log(self.T("cache_ok", n=n), "ok")

            def _dentro(p):
                try:
                    return os.path.commonpath([os.path.abspath(p), root]) == root
                except Exception:
                    return False
            for key in ("recent", "recent_js", "recent_bin"):
                if key in self.cfg:
                    self.cfg[key] = [f for f in self.cfg[key] if not _dentro(f)]
            self.save_cfg()
            if self.js_path and _dentro(self.js_path):
                self.js_path = ""
                self.js_info.configure(text=self.T("no_js"))
                self.update_send_btn()
            if self.bin_path and _dentro(self.bin_path):
                self.bin_path = ""
                self.bin_info.configure(text=self.T("no_bin"))
                self.update_save_btn()
            if getattr(self, "_cur_uf2", "") and _dentro(self._cur_uf2):
                self._cur_uf2 = ""
                self.uf2 = None
                self._uf2_txt = None
                self.uf2_info.configure(text=self.T("no_file"))
                self.update_ui()
            self.refresh_uf2_list()
            self.refresh_js_list()
            self.refresh_bin_list()

        def set_board_status(self, key, color=MUTED, **kw):
            self.board_status = (key, kw, color)
            self.board_lbl.configure(text=self.T(key, **kw) if key else kw.get("text", ""), fg=color)

        def set_conn_status(self, key, color=MUTED, **kw):
            self.conn_status = (key, kw, color)
            self.conn_lbl.configure(text=self.T(key, **kw) if key else kw.get("text", ""), fg=color)

        # ================= idioma
        def on_lang(self, e=None):
            self.lang = LANGS[self.lang_cb.current()][0]
            self.cfg["lang"] = self.lang
            self.save_cfg()
            self.apply_lang()

        def apply_lang(self):
            for w, k in self.i18n:
                w.configure(text=self.T(k))
            self.set_board_status(self.board_status[0], self.board_status[2], **self.board_status[1])
            self.set_conn_status(self.conn_status[0], self.conn_status[2], **self.conn_status[1])
            if not self.uf2 and not getattr(self, "_cur_uf2", ""):
                self.uf2_info.configure(text=self.T("no_file"))
            self.show_uf2_info()
            if not self.js_path:
                self.js_info.configure(text=self.T("no_js"))
            if self.bin_path:
                self.show_bin_info()
            else:
                self.bin_info.configure(text=self.T("no_bin"))
            self.update_send_btn()
            self.update_save_btn()
            self.update_ui()

        # ================= listas (recientes locales + catálogo de GitHub)
        def _entries(self, recent_key, exts):
            """Entradas de un combo: archivos recientes locales + los del catálogo."""
            local = [("local", f) for f in self.cfg.get(recent_key, []) if os.path.isfile(f)]
            cloud = [("cloud", (n, u)) for (n, u, x) in self.catalog if x in exts]
            return local + cloud

        def _fill_combo(self, cb, entries, current_path):
            labels = [os.path.basename(d) if k == "local" else "☁ " + d[0] for k, d in entries]
            cb.configure(values=labels, state="readonly")
            sel = next((i for i, (k, d) in enumerate(entries)
                        if k == "local" and d == current_path), None)
            if sel is not None:
                cb.current(sel)
            elif current_path:
                cb.set(os.path.basename(current_path))
            else:
                cb.set("")

        def _pick(self, cb, entries, loader):
            i = cb.current()
            if not 0 <= i < len(entries):
                return
            kind, data = entries[i]
            if kind == "local":
                loader(data)
            else:
                self._download_then(data[1], loader)

        def _download_then(self, url, loader):
            import urllib.parse
            name = os.path.basename(urllib.parse.urlparse(url).path) or url
            self.log(self.T("downloading", name=name))

            def work():
                try:
                    path = download_file(url, self.cache_dir)
                    self.tlog(self.T("downloaded", name=name), "ok")
                    self.post(loader, path)
                except Exception as e:
                    msg = getattr(e, "reason", None) or getattr(e, "strerror", None) or str(e)
                    self.tlog(self.T("dl_err", name=name, e=msg), "err")
            threading.Thread(target=work, daemon=True).start()

        # ----- catálogo de GitHub
        def load_catalog(self):
            self.log(self.T("cat_loading"))
            url = self.catalog_url

            def work():
                try:
                    items = fetch_catalog(url)
                    self.post(self.apply_catalog, items)
                except Exception as e:
                    msg = getattr(e, "reason", None) or getattr(e, "strerror", None) or str(e)
                    self.tlog(self.T("cat_err", e=msg), "err")
            threading.Thread(target=work, daemon=True).start()

        def apply_catalog(self, items):
            self.catalog = items
            self.log(self.T("cat_ok", n=len(items)) if items else self.T("cat_none"),
                     "ok" if items else "err")
            self.refresh_uf2_list()
            self.refresh_js_list()
            self.refresh_bin_list()

        # ================= UF2
        def refresh_uf2_list(self):
            self._uf2_entries = self._entries("recent", ("uf2",))
            self._fill_combo(self.uf2_cb, self._uf2_entries, getattr(self, "_cur_uf2", ""))

        def browse_uf2(self):
            cur = getattr(self, "_cur_uf2", "")
            p = filedialog.askopenfilename(initialdir=os.path.dirname(cur) if cur else None,
                                           filetypes=[("UF2", "*.uf2"), ("*", "*.*")])
            if p:
                self.load_uf2(p)

        def on_uf2_pick(self, e=None):
            self._pick(self.uf2_cb, self._uf2_entries, self.load_uf2)

        def load_uf2(self, path):
            path = os.path.abspath(path.strip().strip('"'))
            info = parse_uf2(path)
            self._cur_uf2 = path
            self.uf2 = info if info.valid else None
            if info.valid:
                self.cfg["recent"] = ([path] + [f for f in self.cfg.get("recent", []) if f != path])[:10]
                self.save_cfg()
                txt = f"{info.blocks} {{blocks}} · {info.payload_bytes / 1024:.1f} KiB · {info.family_text()}"
            else:
                txt = info.summary()
            self.refresh_uf2_list()
            if not info.valid:
                self.uf2_cb.set(os.path.basename(path))
            self._uf2_txt = (txt, MUTED if info.valid else RED)
            self.show_uf2_info()
            self.log(f"UF2 {os.path.basename(path)} — {info.summary()}", "info" if info.valid else "err")
            self.update_ui()

        # ================= JS
        def refresh_js_list(self):
            self._js_entries = self._entries("recent_js", ("js", "mjs"))
            self._fill_combo(self.js_cb, self._js_entries, self.js_path)

        def browse_js(self):
            p = filedialog.askopenfilename(
                initialdir=os.path.dirname(self.js_path) if self.js_path else None,
                filetypes=[("JavaScript", "*.js *.mjs"), ("*", "*.*")])
            if p:
                self.set_js(p)

        def on_js_pick(self, e=None):
            self._pick(self.js_cb, self._js_entries, self.set_js)

        def set_js(self, path):
            path = os.path.abspath(path)
            if not os.path.isfile(path):
                return
            self.js_path = path
            self.cfg["recent_js"] = ([path] + [f for f in self.cfg.get("recent_js", []) if f != path])[:10]
            self.save_cfg()
            self.refresh_js_list()
            self.show_js_info()
            self.update_send_btn()

        # ================= BIN (para el botón Save)
        def refresh_bin_list(self):
            self._bin_entries = self._entries("recent_bin", ("bin",))
            self._fill_combo(self.bin_cb, self._bin_entries, self.bin_path)

        def browse_bin(self):
            p = filedialog.askopenfilename(
                initialdir=os.path.dirname(self.bin_path) if self.bin_path else None,
                filetypes=[("Binario", "*.bin"), ("*", "*.*")])
            if p:
                self.set_bin(p)

        def on_bin_pick(self, e=None):
            self._pick(self.bin_cb, self._bin_entries, self.set_bin)

        def set_bin(self, path):
            path = os.path.abspath(path)
            if not os.path.isfile(path):
                return
            self.bin_path = path
            self.cfg["recent_bin"] = ([path] + [f for f in self.cfg.get("recent_bin", []) if f != path])[:10]
            self.save_cfg()
            self.refresh_bin_list()
            self.show_bin_info()
            self.update_save_btn()

        def show_bin_info(self):
            try:
                size = os.path.getsize(self.bin_path)
            except OSError:
                size = 0
            self.bin_info.configure(text=f"{size / 1024:.1f} KiB · {os.path.dirname(self.bin_path)}",
                                    fg=MUTED)

        def show_uf2_info(self):
            if self._uf2_txt:
                self.uf2_info.configure(text=self._uf2_txt[0].replace("{blocks}", self.T("blocks")),
                                        fg=self._uf2_txt[1])

        def show_js_info(self):
            try:
                size = os.path.getsize(self.js_path)
            except OSError:
                size = 0
            self.js_info.configure(text=f"{size / 1024:.1f} KiB · {os.path.dirname(self.js_path)}", fg=MUTED)

        # ================= envío por red
        def _target(self):
            """Valida IP y puerto. Devuelve (host, port) o None (y avisa)."""
            host = self.ip_var.get().strip()
            port_s = self.port_var.get().strip()
            if not host or " " in host:
                messagebox.showwarning(APP_NAME, self.T("bad_ip"))
                return None
            if not port_s.isdigit() or not 1 <= int(port_s) <= 65535:
                messagebox.showwarning(APP_NAME, self.T("bad_port"))
                return None
            port = int(port_s)
            self.cfg["ip"], self.cfg["port"] = host, port
            self.save_cfg()
            return host, port

        def update_send_btn(self):
            self.btn_send.configure(text=self.T("cancel") if self.sending else self.T("send"))
            self.btn_send.state(["!disabled"] if (self.sending or self.js_path) else ["disabled"])

        def update_save_btn(self):
            self.btn_save.configure(text=("Instalando…" if self.lang == "es" else "Installing…") if self.saving else self.T("save"))
            self.btn_save.state(["disabled"] if self.saving else ["!disabled"])

        def send_clicked(self):
            if self.sending:
                self.send_stop.set()
                return
            target = self._target()
            if not target:
                return
            host, port = target
            if not self.js_path:
                self.browse_js()
                if not self.js_path:
                    return
            try:
                with open(self.js_path, encoding="utf-8") as fh:
                    code = fh.read()
            except (OSError, UnicodeDecodeError) as e:
                self.log(f"{self.js_path}: {e}", "err")
                return
            self.sending = True
            self.send_stop.clear()
            self.update_send_btn()
            self.set_conn_status("sending", AMBER, host=host, port=port)
            self.log(f"→ {host}:{port}  {os.path.basename(self.js_path)} ({len(code.encode())} bytes)")
            threading.Thread(target=self.send_worker, args=(host, port, code), daemon=True).start()

        def send_worker(self, host, port, code):
            received = [0]

            def on_data(t):
                received[0] += len(t.encode())
                self.post(self.write, t, "js")
            try:
                send_js(host, port, code, on_data, self.send_stop)
                if self.send_stop.is_set():
                    self.post(self.set_conn_status, "cancelled", AMBER)
                    self.tlog(self.T("cancelled"), "err")
                else:
                    self.post(self.set_conn_status, "sent", GREEN, n=received[0])
                    self.tlog(self.T("sent", n=received[0]), "ok")
            except OSError as e:
                msg = getattr(e, "strerror", None) or str(e) or e.__class__.__name__
                self.post(self.set_conn_status, "conn_err", RED, e=msg)
                self.tlog(self.T("conn_err", e=msg), "err")
            finally:
                self.post(self.send_done)

        def send_done(self):
            self.sending = False
            self.update_send_btn()

        # ----- Savedata CIV7: ejecutar install.py (NO enviar .bin en bruto)
        def save_clicked(self):
            if self.saving:
                return
            target = self._target()
            if not target:
                return
            host, port = target
            # Savedata instala el core.js integrado; no utiliza el JS del panel Enviar.
            if not messagebox.askyesno(
                "Civilization VII — Savedata",
                "ATENCIÓN: Esto ELIMINA los archivos existentes en "
                "save:/saves/Single/ antes de instalar XSTRIG.Civ7Save.\n\n"
                "Esto puede borrar tus partidas de Civilization VII. Haz una copia "
                "de seguridad antes de continuar.\n\n"
                "La consola debe tener Remote JS Loader activo. ¿Continuar?",
                icon="warning"):
                return
            self.saving = True
            self.update_save_btn()
            self.log(f"Instalando Savedata CIV7 en {host}:{port} con core.js integrado")
            threading.Thread(target=self.save_worker, args=(host, port), daemon=True).start()

        def save_worker(self, host, port):
            import subprocess
            import shutil
            try:
                # En un .exe PyInstaller los recursos se extraen en sys._MEIPASS.
                base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
                installer_dir = os.path.join(base, "installer")
                installer = os.path.join(installer_dir, "install.py")
                payload_path = os.path.join(installer_dir, "core.js")
                for name in ("install.py", "compress_stage.js", "save_template.bin", "core.js"):
                    if not os.path.isfile(os.path.join(installer_dir, name)):
                        raise FileNotFoundError(f"Falta installer/{name}")
                if not shutil.which("node"):
                    raise RuntimeError("Node.js no está instalado o no aparece en PATH")
                if not os.path.isdir(os.path.join(installer_dir, "node_modules", "terser")):
                    raise RuntimeError("Falta Terser: ejecuta npm ci dentro de la carpeta installer antes de compilar")
                python = shutil.which("python") or shutil.which("py")
                if not python:
                    raise RuntimeError("Se necesita Python instalado en Windows para ejecutar install.py")
                cmd = [python, "-u", installer, host, str(port), payload_path]
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = subprocess.Popen(cmd, cwd=installer_dir, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                        errors="replace", bufsize=1, creationflags=flags)
                for line in proc.stdout:
                    self.tlog(line.rstrip())
                code = proc.wait()
                if code == 0:
                    self.tlog("Instalación completada: reinicia el juego y entra en Load Game.", "ok")
                else:
                    self.tlog(f"La instalación terminó con error (código {code}).", "err")
            except Exception as e:
                self.tlog(f"Error al instalar Savedata: {e}", "err")
            finally:
                self.post(self.save_done)

        def save_done(self):
            self.saving = False
            self.update_save_btn()

        # ================= escaneo de placas
        def scan_loop(self):
            while not self.stop.is_set():
                try:
                    self.post(self.apply_scan, scan_devices())
                except Exception as e:
                    self.tlog(f"Error: {e}", "err")
                self.stop.wait(1.0)

        def apply_scan(self, devs):
            new = {d.key: d for d in devs}
            for k in set(self.devices) - set(new):
                self.log(f"− {self.devices[k].location}")
            for k in set(new) - set(self.devices):
                d = new[k]
                self.log(f"+ {d.kind} {d.location} · {d.chip} · {d.description}")
            prev = self.current_key()
            self.devices = new
            self.populate_devices(prev)
            boot = {k for k, d in new.items() if d.kind == "BOOTSEL"}
            fresh = boot - self.seen_bootsel
            self.seen_bootsel = boot
            if self.auto_var.get() and fresh and self.uf2 and not self.busy:
                self.start_flash(new[sorted(fresh)[0]], confirm=False)
            self.update_ui()

        def populate_devices(self, prev=None):
            new = self.devices
            if prev is None:
                prev = getattr(self, "_last_key", None)
            self.dev_keys = sorted(new, key=lambda k: (new[k].kind != "BOOTSEL", k))
            labels = [f"{new[k].location.rstrip(chr(92))}  ·  {new[k].chip}  ·  {new[k].description}"
                      for k in self.dev_keys]
            self.dev_cb.configure(values=labels, state="readonly" if labels else "disabled")
            if labels:
                self.dev_cb.current(self.dev_keys.index(prev) if prev in self.dev_keys else 0)
            else:
                self.dev_cb.set("")

        def current_key(self):
            i = self.dev_cb.current() if self.dev_keys else -1
            k = self.dev_keys[i] if 0 <= i < len(self.dev_keys) else None
            self._last_key = k
            return k

        def selected(self):
            return self.devices.get(self.current_key())

        def update_ui(self):
            d = self.selected()
            if not d:
                color, text = RED, self.T("no_board")
            elif d.kind == "BOOTSEL":
                color, text = GREEN, self.T("boot_state", chip=d.chip, loc=d.location.rstrip("\\"))
            else:
                color, text = AMBER, self.T("run_state", chip=d.chip, loc=d.location)
            fnt = tkfont.Font(font=self.f_state)
            maxw = max(self.state_lbl.winfo_width(), 280) - 4
            while fnt.measure(text) > maxw and len(text) > 4:
                text = text.rstrip("…")[:-1] + "…"
            self.dot.itemconfigure(self.dot_id, fill=color)
            self.state_lbl.configure(text=text)
            self.btn_flash.state(["!disabled"] if (d and self.uf2 and not self.busy) else ["disabled"])
            self.btn_boot.state(["!disabled"] if (d and d.kind == "SERIAL" and not self.busy) else ["disabled"])

        def on_auto(self):
            if self.auto_var.get():
                if not self.uf2:
                    messagebox.showwarning(APP_NAME, self.T("need_file"))
                    self.auto_var.set(False)
                    return
                self.seen_bootsel = set()
                self.log("Auto-flash ON")
            else:
                self.log("Auto-flash OFF")

        # ================= flasheo
        def reboot_selected(self):
            d = self.selected()
            if not d or d.kind != "SERIAL":
                return
            self.busy = True
            self.update_ui()

            def work():
                try:
                    reboot_to_bootsel(d.location, d.micropython, self.tlog)
                except Exception as e:
                    self.tlog(f"Error: {e}", "err")
                finally:
                    self.post(self.flash_done, None)
            threading.Thread(target=work, daemon=True).start()

        def flash_selected(self):
            d = self.selected()
            if d and self.uf2 and not self.busy:
                self.start_flash(d, confirm=True)

        def start_flash(self, dev, confirm=True):
            warn = check_compatibility(self.uf2, dev.chip)
            if warn:
                self.log("⚠ " + warn, "err")
                if not confirm or not messagebox.askyesno(APP_NAME, warn + "\n\n" + self.T("force")):
                    self.set_board_status(None, RED, text="⚠ " + warn)
                    return
            self.busy = True
            self.prog["value"] = self._prog_v = 0
            self.set_board_status("flashing", ACCENT, p=0)
            self.update_ui()
            threading.Thread(target=self.flash_worker, args=(dev, self.uf2.path), daemon=True).start()

        def on_progress(self, f):
            self.prog["value"] = self._prog_v = int(f * 1000)
            self.set_board_status("flashing", ACCENT, p=f * 100)

        def flash_worker(self, dev, path):
            ok = False
            try:
                target = dev
                if dev.kind == "SERIAL":
                    before = {d.key for d in find_bootsel_drives()}
                    reboot_to_bootsel(dev.location, dev.micropython, self.tlog)
                    self.post(self.set_board_status, "waiting", AMBER)
                    target = wait_for_new_bootsel(before, 12.0, self.stop)
                    if not target:
                        raise RuntimeError("La placa no entró en BOOTSEL. Desconéctala, mantén pulsado "
                                           "BOOTSEL y vuelve a conectarla.")
                    warn = check_compatibility(self.uf2, target.chip)
                    if warn:
                        raise RuntimeError(warn)
                flash_uf2(path, target.location, lambda f: self.post(self.on_progress, f),
                          lambda m: self.tlog(m, "ok" if m.startswith("✔") else "info"))
                ok = True
            except Exception as e:
                msg = str(e)
                self.tlog(f"✖ {msg}", "err")
                self.post(self.set_board_status, None, RED, text=f"✖ {msg}")
            finally:
                self.post(self.flash_done, ok)

        def flash_done(self, ok):
            self.busy = False
            if ok:
                self.flashed += 1
                self.prog["value"] = self._prog_v = 1000
                self.set_board_status(None, GREEN, text=f"✔ {self.T('done')} ({self.flashed})")
            self.update_ui()

        def on_close(self):
            self.stop.set()
            self.send_stop.set()
            self.save_stop.set()
            self.root.destroy()

    root = tk.Tk()
    App(root)
    root.mainloop()


# --------------------------------------------------------------------------------------
# Línea de comandos
# --------------------------------------------------------------------------------------
def run_cli(args):
    if args.list:
        devs = scan_devices()
        if not devs:
            print("No se ha detectado ninguna placa.")
        for d in devs:
            print(f"{d.kind:8} {d.location:12} {d.chip:7} {d.description}  [{d.extra}]")
        if not HAVE_SERIAL:
            print("(pyserial no instalado: no se listan puertos serie)")
        return 0

    if args.info:
        info = parse_uf2(args.info)
        print(info.summary())
        return 0 if info.valid else 1

    if args.reboot:
        reboot_to_bootsel(args.reboot, args.micropython)
        return 0

    if args.flash:
        info = parse_uf2(args.flash)
        print(info.summary())
        if not info.valid:
            return 1
        count = 0
        while True:
            target = None
            if args.drive:
                root = args.drive if args.drive.endswith(("\\", "/")) else args.drive + os.sep
                if IS_WINDOWS and len(args.drive.rstrip(":\\")) == 1:
                    root = args.drive.rstrip(":\\") + ":\\"
                fields = read_info_uf2(root)
                if fields is None:
                    print(f"{root} no es una unidad BOOTSEL (falta INFO_UF2.TXT)")
                    return 1
                target = Device("BOOTSEL", root, chip_from_info(fields), fields.get("Model", ""))
            else:
                drives = find_bootsel_drives()
                if drives:
                    target = drives[0]
                elif args.wait or args.loop:
                    print("Esperando una placa en modo BOOTSEL… (Ctrl+C para salir)")
                    target = wait_for_new_bootsel(set(), timeout=10 ** 9)
                else:
                    print("No hay ninguna placa en BOOTSEL. Usa --wait para esperar.")
                    return 1
            warn = check_compatibility(info, target.chip)
            if warn and not args.force:
                print("AVISO: " + warn + " (usa --force para ignorarlo)")
                return 2

            def bar(f):
                n = int(f * 40)
                sys.stdout.write(f"\r[{'#' * n}{'.' * (40 - n)}] {f * 100:5.1f}%")
                sys.stdout.flush()
            flash_uf2(args.flash, target.location, bar, lambda m: print("\n" + m))
            count += 1
            if not args.loop or args.drive:
                return 0
            print(f"Placas flasheadas: {count}. Conecta la siguiente…")
            # esperar a que desaparezca la unidad antes de buscar otra
            while any(d.location == target.location for d in find_bootsel_drives()):
                time.sleep(0.3)
    return None


def main():
    p = argparse.ArgumentParser(
        prog="switch2tools",
        description=f"{APP_NAME} {APP_VERSION} by {APP_AUTHOR} - envía .js por IP y flashea .uf2 en RP2040/RP2350. "
                    "Sin argumentos abre la interfaz gráfica.")
    p.add_argument("file", nargs="?", help="archivo .uf2 a abrir en la interfaz gráfica")
    p.add_argument("--list", action="store_true", help="lista las placas detectadas")
    p.add_argument("--info", metavar="UF2", help="muestra la información de un .uf2")
    p.add_argument("--flash", metavar="UF2", help="flashea el .uf2 en la primera placa BOOTSEL")
    p.add_argument("--drive", help="unidad BOOTSEL concreta (p. ej. E:)")
    p.add_argument("--wait", action="store_true", help="espera a que se conecte una placa")
    p.add_argument("--loop", action="store_true", help="flashea placas una tras otra (producción)")
    p.add_argument("--force", action="store_true", help="ignora el aviso de chip incompatible")
    p.add_argument("--reboot", metavar="COMx", help="reinicia a BOOTSEL la placa de ese puerto")
    p.add_argument("--micropython", action="store_true", help="usar machine.bootloader() con --reboot")
    p.add_argument("--send", nargs=2, metavar=("IP[:PUERTO]", "ARCHIVO.js"),
                   help=f"envía un .js al Remote JS Loader (puerto por defecto {DEFAULT_JS_PORT})")
    p.add_argument("--save", nargs=2, metavar=("IP[:PUERTO]", "ARCHIVO.bin"),
                   help="envía un .bin en bruto por la misma IP:puerto")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION} by {APP_AUTHOR}")
    args = p.parse_args()

    if args.send or args.save:
        target, path = args.send or args.save
        host, _, port = target.rpartition(":") if target.count(":") == 1 else (target, "", "")
        host = host or target
        port = int(port) if port else DEFAULT_JS_PORT
        out = lambda t: (sys.stdout.write(t), sys.stdout.flush())
        try:
            if args.send:
                with open(path, encoding="utf-8") as fh:
                    send_js(host, port, fh.read(), out)
            else:
                with open(path, "rb") as fh:
                    send_bin(host, port, fh.read(), out)
        except OSError as e:
            print(f"No se pudo conectar a {host}:{port}: {e}")
            sys.exit(1)
        except KeyboardInterrupt:
            sys.exit(130)
        print()
        sys.exit(0)
    if args.list or args.info or args.flash or args.reboot:
        try:
            sys.exit(run_cli(args))
        except KeyboardInterrupt:
            print("\nCancelado.")
            sys.exit(130)
    run_gui(args.file)


if __name__ == "__main__":
    main()
