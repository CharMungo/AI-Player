#!/usr/bin/env python3
"""
drm-player — a DRM/KMS media player for Linux systems without a window manager.

Displays images and videos directly to the display via KMS/DRM.
Reads a TOML playlist config. No audio. No window manager required.

Usage:
    python3 drm_player.py [playlist.toml]

Requirements (Arch):
    sudo pacman -S python-pillow python-pyav ffmpeg

Requirements (Raspberry Pi OS / Debian):
    sudo apt install python3-pillow ffmpeg
    pip3 install av

tomllib is stdlib in Python 3.11+. For older versions:
    pip install tomli
"""

import sys
import os
import time
import ctypes
import ctypes.util
import struct
import fcntl
import mmap
import array
import traceback
from pathlib import Path

# ── TOML loading (stdlib tomllib in 3.11+, else tomli) ───────────────────────
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            print("Install tomli:  pip install tomli   (or use Python 3.11+)", file=sys.stderr)
            sys.exit(1)

# ── Image loading ─────────────────────────────────────────────────────────────
try:
    from PIL import Image
except ImportError:
    print("Install Pillow:  sudo pacman -S python-pillow  /  sudo apt install python3-pillow", file=sys.stderr)
    sys.exit(1)

# ── Video decoding ────────────────────────────────────────────────────────────
try:
    import av
except ImportError:
    print("Install PyAV:  sudo pacman -S python-pyav  /  pip3 install av", file=sys.stderr)
    sys.exit(1)


# =============================================================================
# DRM / KMS constants and ioctl definitions
# =============================================================================

DRM_IOCTL_BASE = ord('d')

def _IOC(dir_, type_, nr, size):
    return (dir_ << 30) | (type_ << 8) | nr | (size << 16)

def _IOWR(type_, nr, size): return _IOC(3, type_, nr, size)
def _IOW(type_, nr, size):  return _IOC(1, type_, nr, size)
def _IOR(type_, nr, size):  return _IOC(2, type_, nr, size)
def _IO(type_, nr):         return _IOC(0, type_, nr, 0)

DRM_IOCTL_SET_MASTER          = _IO(DRM_IOCTL_BASE, 0x1e)
DRM_IOCTL_DROP_MASTER         = _IO(DRM_IOCTL_BASE, 0x1f)
DRM_IOCTL_MODE_GETRESOURCES   = _IOWR(DRM_IOCTL_BASE, 0xA0, 40)
DRM_IOCTL_MODE_GETCONNECTOR   = _IOWR(DRM_IOCTL_BASE, 0xA7, 80)
DRM_IOCTL_MODE_GETENCODER     = _IOWR(DRM_IOCTL_BASE, 0xA6, 16)
DRM_IOCTL_MODE_GETCRTC        = _IOWR(DRM_IOCTL_BASE, 0xA1, 120)
DRM_IOCTL_MODE_SETCRTC        = _IOWR(DRM_IOCTL_BASE, 0xA2, 120)
DRM_IOCTL_MODE_CREATE_DUMB    = _IOWR(DRM_IOCTL_BASE, 0xB2, 32)
DRM_IOCTL_MODE_MAP_DUMB       = _IOWR(DRM_IOCTL_BASE, 0xB3, 24)
DRM_IOCTL_MODE_DESTROY_DUMB   = _IOWR(DRM_IOCTL_BASE, 0xB4, 4)
DRM_IOCTL_MODE_ADDFB          = _IOWR(DRM_IOCTL_BASE, 0xAE, 32)
DRM_IOCTL_MODE_RMFB           = _IOWR(DRM_IOCTL_BASE, 0xAF, 4)

DRM_MODE_CONNECTOR_CONNECTED  = 1


# =============================================================================
# ctypes structs matching kernel DRM uAPI
# =============================================================================

class DrmModeRes(ctypes.Structure):
    _fields_ = [
        ("fb_id_ptr",        ctypes.c_uint64),
        ("crtc_id_ptr",      ctypes.c_uint64),
        ("connector_id_ptr", ctypes.c_uint64),
        ("encoder_id_ptr",   ctypes.c_uint64),
        ("count_fbs",        ctypes.c_uint32),
        ("count_crtcs",      ctypes.c_uint32),
        ("count_connectors", ctypes.c_uint32),
        ("count_encoders",   ctypes.c_uint32),
        ("min_width",        ctypes.c_uint32),
        ("max_width",        ctypes.c_uint32),
        ("min_height",       ctypes.c_uint32),
        ("max_height",       ctypes.c_uint32),
    ]

class DrmModeInfo(ctypes.Structure):
    """drm_mode_modeinfo"""
    _fields_ = [
        ("clock",       ctypes.c_uint32),
        ("hdisplay",    ctypes.c_uint16),
        ("hsync_start", ctypes.c_uint16),
        ("hsync_end",   ctypes.c_uint16),
        ("htotal",      ctypes.c_uint16),
        ("hskew",       ctypes.c_uint16),
        ("vdisplay",    ctypes.c_uint16),
        ("vsync_start", ctypes.c_uint16),
        ("vsync_end",   ctypes.c_uint16),
        ("vtotal",      ctypes.c_uint16),
        ("vscan",       ctypes.c_uint16),
        ("vrefresh",    ctypes.c_uint32),
        ("flags",       ctypes.c_uint32),
        ("type",        ctypes.c_uint32),
        ("name",        ctypes.c_char * 32),
    ]

class DrmModeGetConnector(ctypes.Structure):
    _fields_ = [
        ("encoders_ptr",      ctypes.c_uint64),
        ("modes_ptr",         ctypes.c_uint64),
        ("props_ptr",         ctypes.c_uint64),
        ("prop_values_ptr",   ctypes.c_uint64),
        ("count_modes",       ctypes.c_uint32),
        ("count_props",       ctypes.c_uint32),
        ("count_encoders",    ctypes.c_uint32),
        ("encoder_id",        ctypes.c_uint32),
        ("connector_id",      ctypes.c_uint32),
        ("connector_type",    ctypes.c_uint32),
        ("connector_type_id", ctypes.c_uint32),
        ("connection",        ctypes.c_uint32),
        ("mm_width",          ctypes.c_uint32),
        ("mm_height",         ctypes.c_uint32),
        ("subpixel",          ctypes.c_uint32),
        ("pad",               ctypes.c_uint32),
    ]

class DrmModeGetEncoder(ctypes.Structure):
    _fields_ = [
        ("encoder_id",      ctypes.c_uint32),
        ("encoder_type",    ctypes.c_uint32),
        ("crtc_id",         ctypes.c_uint32),
        ("possible_crtcs",  ctypes.c_uint32),
        ("possible_clones", ctypes.c_uint32),
    ]

class DrmModeSetCrtc(ctypes.Structure):
    _fields_ = [
        ("set_connectors_ptr", ctypes.c_uint64),
        ("count_connectors",   ctypes.c_uint32),
        ("crtc_id",            ctypes.c_uint32),
        ("fb_id",              ctypes.c_uint32),
        ("x",                  ctypes.c_uint32),
        ("y",                  ctypes.c_uint32),
        ("gamma_size",         ctypes.c_uint32),
        ("mode_valid",         ctypes.c_uint32),
        ("mode",               DrmModeInfo),
    ]

class DrmModeCreateDumb(ctypes.Structure):
    _fields_ = [
        ("height", ctypes.c_uint32),
        ("width",  ctypes.c_uint32),
        ("bpp",    ctypes.c_uint32),
        ("flags",  ctypes.c_uint32),
        ("handle", ctypes.c_uint32),
        ("pitch",  ctypes.c_uint32),
        ("size",   ctypes.c_uint64),
    ]

class DrmModeMapDumb(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
        ("pad",    ctypes.c_uint32),
        ("offset", ctypes.c_uint64),
    ]

class DrmModeDestroyDumb(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_uint32)]

class DrmModeFbCmd(ctypes.Structure):
    _fields_ = [
        ("fb_id",  ctypes.c_uint32),
        ("width",  ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pitch",  ctypes.c_uint32),
        ("bpp",    ctypes.c_uint32),
        ("depth",  ctypes.c_uint32),
        ("handle", ctypes.c_uint32),
    ]


# =============================================================================
# DRM Display
# =============================================================================

class DRMDisplay:
    def __init__(self, device: str, connector_index: int = 0):
        self.fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
        self.device = device
        self.width = 0
        self.height = 0
        self._mode = None
        self._crtc_id = None
        self._connector_id = None
        self._init(connector_index)

    def _ioctl(self, request, arg):
        return fcntl.ioctl(self.fd, request, arg)

    def _init(self, connector_index: int):
        # Acquire DRM master
        try:
            fcntl.ioctl(self.fd, DRM_IOCTL_SET_MASTER, 0)
        except OSError as e:
            raise RuntimeError(
                f"Cannot acquire DRM master on {self.device}. "
                "Run as root or add yourself to the 'video' group."
            ) from e

        # ── Get resource IDs ─────────────────────────────────────────────────
        res = DrmModeRes()
        self._ioctl(DRM_IOCTL_MODE_GETRESOURCES, res)

        fb_arr        = (ctypes.c_uint32 * res.count_fbs)()
        crtc_arr      = (ctypes.c_uint32 * res.count_crtcs)()
        connector_arr = (ctypes.c_uint32 * res.count_connectors)()
        encoder_arr   = (ctypes.c_uint32 * res.count_encoders)()

        res.fb_id_ptr        = ctypes.addressof(fb_arr)
        res.crtc_id_ptr      = ctypes.addressof(crtc_arr)
        res.connector_id_ptr = ctypes.addressof(connector_arr)
        res.encoder_id_ptr   = ctypes.addressof(encoder_arr)
        self._ioctl(DRM_IOCTL_MODE_GETRESOURCES, res)

        self._crtc_ids = list(crtc_arr)

        # ── Find a connected connector ────────────────────────────────────────
        connected = []
        for conn_id in connector_arr:
            info, modes, encoder_ids = self._get_connector(conn_id)
            if info.connection == DRM_MODE_CONNECTOR_CONNECTED and modes:
                connected.append((conn_id, info, modes, encoder_ids))

        if not connected:
            raise RuntimeError(f"No connected display found on {self.device}")

        idx = min(connector_index, len(connected) - 1)
        conn_id, conn_info, modes, encoder_ids = connected[idx]
        print(f"Connector #{idx} id={conn_id} type={conn_info.connector_type}", file=sys.stderr)

        # Best mode = first listed
        mode = modes[0]
        self.width  = mode.hdisplay
        self.height = mode.vdisplay
        self._mode  = mode
        self._connector_id = conn_id
        print(f"Mode: {self.width}x{self.height}@{mode.vrefresh}Hz", file=sys.stderr)

        # ── Find a CRTC ───────────────────────────────────────────────────────
        self._crtc_id = self._find_crtc(conn_info, encoder_ids)
        if self._crtc_id is None:
            raise RuntimeError("No CRTC available for connector")

    def _get_connector(self, conn_id):
        info = DrmModeGetConnector()
        info.connector_id = conn_id
        self._ioctl(DRM_IOCTL_MODE_GETCONNECTOR, info)

        modes_arr   = (DrmModeInfo * info.count_modes)()
        encoder_arr = (ctypes.c_uint32 * info.count_encoders)()

        info.modes_ptr    = ctypes.addressof(modes_arr)
        info.encoders_ptr = ctypes.addressof(encoder_arr)
        self._ioctl(DRM_IOCTL_MODE_GETCONNECTOR, info)

        return info, list(modes_arr), list(encoder_arr)

    def _find_crtc(self, conn_info, encoder_ids):
        # Try current encoder's CRTC first
        if conn_info.encoder_id:
            enc = DrmModeGetEncoder()
            enc.encoder_id = conn_info.encoder_id
            try:
                self._ioctl(DRM_IOCTL_MODE_GETENCODER, enc)
                if enc.crtc_id:
                    return enc.crtc_id
            except OSError:
                pass

        # Scan all encoders for an available CRTC
        for enc_id in encoder_ids:
            enc = DrmModeGetEncoder()
            enc.encoder_id = enc_id
            try:
                self._ioctl(DRM_IOCTL_MODE_GETENCODER, enc)
            except OSError:
                continue
            possible = enc.possible_crtcs
            for i, crtc_id in enumerate(self._crtc_ids):
                if possible & (1 << i):
                    return crtc_id
        return None

    def present(self, rgba_bytes: bytes, src_w: int, src_h: int, position: str):
        """Blit an RGBA frame to the display at native resolution."""

        # ── Create dumb buffer ────────────────────────────────────────────────
        create = DrmModeCreateDumb()
        create.width  = self.width
        create.height = self.height
        create.bpp    = 32
        self._ioctl(DRM_IOCTL_MODE_CREATE_DUMB, create)

        handle = create.handle
        pitch  = create.pitch
        size   = create.size

        try:
            # ── Map it ────────────────────────────────────────────────────────
            map_dumb = DrmModeMapDumb()
            map_dumb.handle = handle
            self._ioctl(DRM_IOCTL_MODE_MAP_DUMB, map_dumb)

            with mmap.mmap(self.fd, size, offset=map_dumb.offset) as buf:
                buf.seek(0)
                buf.write(b'\x00' * size)  # black background

                off_x, off_y = _compute_offset(src_w, src_h, self.width, self.height, position)

                copy_w = min(src_w, self.width  - off_x)
                copy_h = min(src_h, self.height - off_y)

                src_stride = src_w * 4
                for row in range(copy_h):
                    dst_offset = (off_y + row) * pitch + off_x * 4
                    src_offset = row * src_stride
                    row_rgba   = rgba_bytes[src_offset : src_offset + copy_w * 4]

                    # Swap R↔B for XRGB8888 (stored as B G R X in memory)
                    bgrx = bytearray(copy_w * 4)
                    bgrx[0::4] = row_rgba[2::4]  # B ← R
                    bgrx[1::4] = row_rgba[1::4]  # G
                    bgrx[2::4] = row_rgba[0::4]  # R ← B
                    bgrx[3::4] = b'\x00' * copy_w

                    buf.seek(dst_offset)
                    buf.write(bytes(bgrx))

            # ── Create framebuffer ────────────────────────────────────────────
            fb = DrmModeFbCmd()
            fb.width  = self.width
            fb.height = self.height
            fb.pitch  = pitch
            fb.bpp    = 32
            fb.depth  = 24
            fb.handle = handle
            self._ioctl(DRM_IOCTL_MODE_ADDFB, fb)
            fb_id = fb.fb_id

            try:
                # ── Set CRTC ──────────────────────────────────────────────────
                conn_arr = (ctypes.c_uint32 * 1)(self._connector_id)
                set_crtc = DrmModeSetCrtc()
                set_crtc.crtc_id            = self._crtc_id
                set_crtc.fb_id              = fb_id
                set_crtc.x                  = 0
                set_crtc.y                  = 0
                set_crtc.set_connectors_ptr = ctypes.addressof(conn_arr)
                set_crtc.count_connectors   = 1
                set_crtc.mode_valid         = 1
                set_crtc.mode               = self._mode
                self._ioctl(DRM_IOCTL_MODE_SETCRTC, set_crtc)
            finally:
                fb_id_val = ctypes.c_uint32(fb_id)
                try:
                    self._ioctl(DRM_IOCTL_MODE_RMFB, fb_id_val)
                except OSError:
                    pass

        finally:
            destroy = DrmModeDestroyDumb()
            destroy.handle = handle
            try:
                self._ioctl(DRM_IOCTL_MODE_DESTROY_DUMB, destroy)
            except OSError:
                pass

    def close(self):
        try:
            fcntl.ioctl(self.fd, DRM_IOCTL_DROP_MASTER, 0)
        except OSError:
            pass
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _compute_offset(src_w, src_h, disp_w, disp_h, position):
    if position == "center":
        ox = (disp_w - src_w) // 2 if src_w < disp_w else 0
        oy = (disp_h - src_h) // 2 if src_h < disp_h else 0
        return ox, oy
    return 0, 0  # top_left


# =============================================================================
# Image playback
# =============================================================================

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"}

def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS

def play_image(display: DRMDisplay, path: Path, duration: float, position: str):
    print(f"Image: {path}", file=sys.stderr)
    img = Image.open(path).convert("RGBA")
    rgba = img.tobytes()
    display.present(rgba, img.width, img.height, position)
    time.sleep(duration)


# =============================================================================
# Video playback
# =============================================================================

def play_video(display: DRMDisplay, path: Path, position: str):
    print(f"Video: {path}", file=sys.stderr)

    container = av.open(str(path))
    stream = next((s for s in container.streams if s.type == "video"), None)
    if stream is None:
        print(f"  No video stream in {path}, skipping", file=sys.stderr)
        return

    # Discard audio — we don't need it
    for s in container.streams:
        if s.type == "audio":
            s.discard = "all"

    # Frame rate for pacing
    if stream.average_rate and float(stream.average_rate) > 0:
        frame_duration = 1.0 / float(stream.average_rate)
    elif stream.guessed_rate and float(stream.guessed_rate) > 0:
        frame_duration = 1.0 / float(stream.guessed_rate)
    else:
        frame_duration = 1 / 30  # fallback

    print(f"  {stream.width}x{stream.height} @ ~{1/frame_duration:.2f}fps", file=sys.stderr)

    for packet in container.demux(stream):
        for frame in packet.decode():
            t0 = time.monotonic()

            rgba_frame = frame.reformat(format="rgba")
            rgba_bytes = rgba_frame.planes[0].to_bytes()
            w = rgba_frame.width
            h = rgba_frame.height

            display.present(rgba_bytes, w, h, position)

            elapsed = time.monotonic() - t0
            remaining = frame_duration - elapsed
            if remaining > 0:
                time.sleep(remaining)

    container.close()


# =============================================================================
# Config
# =============================================================================

DEFAULT_CONFIG = "playlist.toml"

def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)

def validate_config(cfg: dict) -> None:
    if "display" not in cfg:
        raise ValueError("Config missing [display] section")
    if "playlist" not in cfg:
        raise ValueError("Config missing [playlist] section")
    if "items" not in cfg["playlist"]:
        raise ValueError("Config missing [[playlist.items]]")
    pos = cfg["display"].get("position", "top_left")
    if pos not in ("center", "top_left"):
        print(f"Warning: unknown position '{pos}', using 'top_left'", file=sys.stderr)


# =============================================================================
# Main
# =============================================================================

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG
    print(f"drm-player — config: {config_path}", file=sys.stderr)

    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error reading config: {e}", file=sys.stderr)
        sys.exit(1)

    validate_config(cfg)

    display_cfg = cfg["display"]
    device      = display_cfg.get("device", "/dev/dri/card0")
    position    = display_cfg.get("position", "top_left")
    conn_index  = display_cfg.get("connector_index", 0)
    items       = cfg["playlist"]["items"]

    print(f"Initialising DRM on {device}…", file=sys.stderr)

    try:
        with DRMDisplay(device, conn_index) as display:
            print(f"Display ready: {display.width}x{display.height}", file=sys.stderr)

            for item in items:
                path = Path(item["path"])
                if not path.exists():
                    print(f"Skipping (not found): {path}", file=sys.stderr)
                    continue

                try:
                    if is_image(path):
                        duration = float(item.get("duration", 5.0))
                        play_image(display, path, duration, position)
                    else:
                        play_video(display, path, position)
                except KeyboardInterrupt:
                    print("\nInterrupted.", file=sys.stderr)
                    break
                except Exception as e:
                    print(f"Error playing {path}: {e}", file=sys.stderr)
                    traceback.print_exc()
                    continue

    except RuntimeError as e:
        print(f"Display error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nExiting.", file=sys.stderr)

    print("Playlist complete.", file=sys.stderr)


if __name__ == "__main__":
    main()
