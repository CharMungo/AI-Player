#!/usr/bin/env python3
"""
drm-player — DRM/KMS media player for Linux without a window manager.

Usage:  python3 drm_player.py [playlist.toml]

Requirements (Raspberry Pi OS / Debian):
    sudo apt install python3-pillow ffmpeg
    pip3 install av

Requirements (Arch):
    sudo pacman -S python-pillow python-pyav ffmpeg
"""

import sys, os, time, ctypes, mmap, traceback
from pathlib import Path

# ── TOML ─────────────────────────────────────────────────────────────────────
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            sys.exit("Install tomli:  pip install tomli  (or use Python 3.11+)")

# ── Pillow ───────────────────────────────────────────────────────────────────
try:
    from PIL import Image
except ImportError:
    sys.exit("Install Pillow:  sudo apt install python3-pillow")

# ── PyAV ─────────────────────────────────────────────────────────────────────
try:
    import av
except ImportError:
    sys.exit("Install PyAV:  pip3 install av")

# ── libc ioctl ────────────────────────────────────────────────────────────────
_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.ioctl.restype  = ctypes.c_int
_libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]

def _ioctl(fd: int, request: int, arg) -> None:
    ret = _libc.ioctl(fd, request, ctypes.addressof(arg))
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


# =============================================================================
# DRM ioctl numbers
# =============================================================================

def _IOC(d, t, n, s): return (d<<30)|(t<<8)|n|(s<<16)
def _IO(t,n):         return _IOC(0,t,n,0)
def _IOW(t,n,s):      return _IOC(1,t,n,s)
def _IOWR(t,n,s):     return _IOC(3,t,n,s)

_D = ord('d')

DRM_IOCTL_SET_MASTER         = _IO  (_D, 0x1e)
DRM_IOCTL_DROP_MASTER        = _IO  (_D, 0x1f)
DRM_IOCTL_SET_CLIENT_CAP     = _IOW (_D, 0x0d, 16)
DRM_IOCTL_MODE_GETRESOURCES  = _IOWR(_D, 0xA0, 64)
DRM_IOCTL_MODE_GETCONNECTOR  = _IOWR(_D, 0xA7, 80)
DRM_IOCTL_MODE_GETENCODER    = _IOWR(_D, 0xA6, 20)
DRM_IOCTL_MODE_SETCRTC       = _IOWR(_D, 0xA2, 104)
DRM_IOCTL_MODE_CREATE_DUMB   = _IOWR(_D, 0xB2, 32)
DRM_IOCTL_MODE_MAP_DUMB      = _IOWR(_D, 0xB3, 24)
DRM_IOCTL_MODE_DESTROY_DUMB  = _IOWR(_D, 0xB4, 4)
DRM_IOCTL_MODE_ADDFB         = _IOWR(_D, 0xAE, 32)
DRM_IOCTL_MODE_RMFB          = _IOWR(_D, 0xAF, 4)

DRM_CLIENT_CAP_UNIVERSAL_PLANES = 2
DRM_CLIENT_CAP_ATOMIC           = 3
DRM_MODE_CONNECTOR_CONNECTED    = 1


# =============================================================================
# ctypes structs
# =============================================================================

class SetClientCap(ctypes.Structure):
    _fields_ = [("capability", ctypes.c_uint64),
                ("value",      ctypes.c_uint64)]

class ModeRes(ctypes.Structure):
    _fields_ = [("fb_id_ptr",        ctypes.c_uint64),
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
                ("max_height",       ctypes.c_uint32)]

class ModeInfo(ctypes.Structure):
    _fields_ = [("clock",       ctypes.c_uint32),
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
                ("name",        ctypes.c_char * 32)]

class GetConnector(ctypes.Structure):
    _fields_ = [("encoders_ptr",      ctypes.c_uint64),
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
                ("pad",               ctypes.c_uint32)]

class GetEncoder(ctypes.Structure):
    _fields_ = [("encoder_id",      ctypes.c_uint32),
                ("encoder_type",    ctypes.c_uint32),
                ("crtc_id",         ctypes.c_uint32),
                ("possible_crtcs",  ctypes.c_uint32),
                ("possible_clones", ctypes.c_uint32)]

class SetCrtc(ctypes.Structure):
    _fields_ = [("set_connectors_ptr", ctypes.c_uint64),
                ("count_connectors",   ctypes.c_uint32),
                ("crtc_id",            ctypes.c_uint32),
                ("fb_id",              ctypes.c_uint32),
                ("x",                  ctypes.c_uint32),
                ("y",                  ctypes.c_uint32),
                ("gamma_size",         ctypes.c_uint32),
                ("mode_valid",         ctypes.c_uint32),
                ("mode",               ModeInfo)]

class CreateDumb(ctypes.Structure):
    _fields_ = [("height", ctypes.c_uint32), ("width",  ctypes.c_uint32),
                ("bpp",    ctypes.c_uint32), ("flags",  ctypes.c_uint32),
                ("handle", ctypes.c_uint32), ("pitch",  ctypes.c_uint32),
                ("size",   ctypes.c_uint64)]

class MapDumb(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_uint32), ("pad", ctypes.c_uint32),
                ("offset", ctypes.c_uint64)]

class DestroyDumb(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_uint32)]

class AddFB(ctypes.Structure):
    _fields_ = [("fb_id",  ctypes.c_uint32), ("width",  ctypes.c_uint32),
                ("height", ctypes.c_uint32), ("pitch",  ctypes.c_uint32),
                ("bpp",    ctypes.c_uint32), ("depth",  ctypes.c_uint32),
                ("handle", ctypes.c_uint32)]

class RmFB(ctypes.Structure):
    _fields_ = [("fb_id", ctypes.c_uint32)]


# =============================================================================
# DRM Display
# =============================================================================

class DRMDisplay:
    def __init__(self, device: str, connector_index: int = 0):
        self.fd     = os.open(device, os.O_RDWR | os.O_CLOEXEC)
        self.device = device
        self.width  = 0
        self.height = 0
        self._mode         = None
        self._crtc_id      = None
        self._connector_id = None
        # Track the currently displayed fb and dumb buffer so we can free
        # them only when replaced — keeps the image on screen between frames.
        self._current_fb     = None   # fb_id (int)
        self._current_handle = None   # dumb buffer handle (int)
        self._init(connector_index)

    def _ioctl(self, req, arg):
        _ioctl(self.fd, req, arg)

    def _set_cap(self, cap, value):
        s = SetClientCap()
        s.capability = cap
        s.value      = value
        try:
            self._ioctl(DRM_IOCTL_SET_CLIENT_CAP, s)
        except OSError as e:
            print(f"  cap {cap}={value}: {e}", file=sys.stderr)

    def _init(self, connector_index: int):
        import fcntl as _fcntl
        try:
            _fcntl.ioctl(self.fd, DRM_IOCTL_SET_MASTER, 0)
        except OSError as e:
            if e.errno not in (0, 16):
                raise RuntimeError(
                    f"Cannot acquire DRM master on {self.device}. "
                    "Run as root or: sudo usermod -aG video,render $USER"
                ) from e

        # Must set these caps before GETRESOURCES on Pi/VC4
        self._set_cap(DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1)
        self._set_cap(DRM_CLIENT_CAP_ATOMIC, 1)

        res = ModeRes()
        self._ioctl(DRM_IOCTL_MODE_GETRESOURCES, res)

        fb_arr   = (ctypes.c_uint32 * max(res.count_fbs,        1))()
        crtc_arr = (ctypes.c_uint32 * max(res.count_crtcs,      1))()
        conn_arr = (ctypes.c_uint32 * max(res.count_connectors, 1))()
        enc_arr  = (ctypes.c_uint32 * max(res.count_encoders,   1))()

        res.fb_id_ptr        = ctypes.addressof(fb_arr)
        res.crtc_id_ptr      = ctypes.addressof(crtc_arr)
        res.connector_id_ptr = ctypes.addressof(conn_arr)
        res.encoder_id_ptr   = ctypes.addressof(enc_arr)
        self._ioctl(DRM_IOCTL_MODE_GETRESOURCES, res)

        self._crtc_ids = list(crtc_arr)[:res.count_crtcs]
        print(f"DRM: {res.count_connectors} connectors, {res.count_crtcs} crtcs",
              file=sys.stderr)

        connected = []
        for conn_id in list(conn_arr)[:res.count_connectors]:
            info, modes, enc_ids = self._get_connector(conn_id)
            if info.connection == DRM_MODE_CONNECTOR_CONNECTED and modes:
                connected.append((conn_id, info, modes, enc_ids))

        if not connected:
            raise RuntimeError(f"No connected display found on {self.device}")

        idx = min(connector_index, len(connected) - 1)
        conn_id, conn_info, modes, enc_ids = connected[idx]
        print(f"Connector #{idx} id={conn_id}", file=sys.stderr)

        mode = modes[0]
        self.width         = mode.hdisplay
        self.height        = mode.vdisplay
        self._mode         = mode
        self._connector_id = conn_id
        print(f"Mode: {self.width}x{self.height}@{mode.vrefresh}Hz", file=sys.stderr)

        self._crtc_id = self._find_crtc(conn_info, enc_ids)
        if self._crtc_id is None:
            raise RuntimeError("No CRTC available for connector")

    def _get_connector(self, conn_id):
        info = GetConnector()
        info.connector_id = conn_id
        try:
            self._ioctl(DRM_IOCTL_MODE_GETCONNECTOR, info)
        except OSError:
            return info, [], []

        print(f"  conn {conn_id}: connection={info.connection} "
              f"modes={info.count_modes} encoders={info.count_encoders}",
              file=sys.stderr)

        modes_a = (ModeInfo        * max(info.count_modes,    1))()
        encs_a  = (ctypes.c_uint32 * max(info.count_encoders, 1))()
        props_a = (ctypes.c_uint32 * max(info.count_props,    1))()
        pvals_a = (ctypes.c_uint64 * max(info.count_props,    1))()

        info.modes_ptr       = ctypes.addressof(modes_a)
        info.encoders_ptr    = ctypes.addressof(encs_a)
        info.props_ptr       = ctypes.addressof(props_a)
        info.prop_values_ptr = ctypes.addressof(pvals_a)

        try:
            self._ioctl(DRM_IOCTL_MODE_GETCONNECTOR, info)
        except OSError:
            return info, [], []

        return (info,
                list(modes_a)[:info.count_modes],
                list(encs_a)[:info.count_encoders])

    def _find_crtc(self, conn_info, enc_ids):
        if conn_info.encoder_id:
            enc = GetEncoder()
            enc.encoder_id = conn_info.encoder_id
            try:
                self._ioctl(DRM_IOCTL_MODE_GETENCODER, enc)
                if enc.crtc_id:
                    return enc.crtc_id
            except OSError:
                pass
        for enc_id in enc_ids:
            enc = GetEncoder()
            enc.encoder_id = enc_id
            try:
                self._ioctl(DRM_IOCTL_MODE_GETENCODER, enc)
            except OSError:
                continue
            for i, crtc_id in enumerate(self._crtc_ids):
                if enc.possible_crtcs & (1 << i):
                    return crtc_id
        return None

    def _free_current(self):
        """Release the previously displayed framebuffer and dumb buffer."""
        if self._current_fb is not None:
            rm = RmFB()
            rm.fb_id = self._current_fb
            try: self._ioctl(DRM_IOCTL_MODE_RMFB, rm)
            except OSError: pass
            self._current_fb = None

        if self._current_handle is not None:
            dd = DestroyDumb()
            dd.handle = self._current_handle
            try: self._ioctl(DRM_IOCTL_MODE_DESTROY_DUMB, dd)
            except OSError: pass
            self._current_handle = None

    def present(self, rgba_bytes: bytes, src_w: int, src_h: int, position: str):
        """
        Blit RGBA pixels onto a new dumb buffer, flip to it, then release
        the *previous* buffer.  This keeps the image visible until the next
        call to present() — no more black flashes.
        """
        # Build new dumb buffer
        create        = CreateDumb()
        create.width  = self.width
        create.height = self.height
        create.bpp    = 32
        self._ioctl(DRM_IOCTL_MODE_CREATE_DUMB, create)

        new_handle = create.handle
        pitch      = create.pitch
        size       = create.size

        # Map and fill
        md        = MapDumb()
        md.handle = new_handle
        self._ioctl(DRM_IOCTL_MODE_MAP_DUMB, md)

        with mmap.mmap(self.fd, size, offset=md.offset) as buf:
            buf.seek(0)
            buf.write(b'\x00' * size)

            off_x, off_y = _offset(src_w, src_h, self.width, self.height, position)
            copy_w = min(src_w, self.width  - off_x)
            copy_h = min(src_h, self.height - off_y)
            stride = src_w * 4

            for row in range(copy_h):
                src_off  = row * stride
                dst_off  = (off_y + row) * pitch + off_x * 4
                row_rgba = rgba_bytes[src_off : src_off + copy_w * 4]

                bgrx       = bytearray(copy_w * 4)
                bgrx[0::4] = row_rgba[2::4]   # B ← R
                bgrx[1::4] = row_rgba[1::4]   # G
                bgrx[2::4] = row_rgba[0::4]   # R ← B
                bgrx[3::4] = b'\x00' * copy_w

                buf.seek(dst_off)
                buf.write(bytes(bgrx))

        # Attach new framebuffer
        fb        = AddFB()
        fb.width  = self.width
        fb.height = self.height
        fb.pitch  = pitch
        fb.bpp    = 32
        fb.depth  = 24
        fb.handle = new_handle
        self._ioctl(DRM_IOCTL_MODE_ADDFB, fb)
        new_fb_id = fb.fb_id

        # Flip to new framebuffer
        conn_arr = (ctypes.c_uint32 * 1)(self._connector_id)
        sc = SetCrtc()
        sc.crtc_id            = self._crtc_id
        sc.fb_id              = new_fb_id
        sc.x                  = 0
        sc.y                  = 0
        sc.set_connectors_ptr = ctypes.addressof(conn_arr)
        sc.count_connectors   = 1
        sc.mode_valid         = 1
        sc.mode               = self._mode
        self._ioctl(DRM_IOCTL_MODE_SETCRTC, sc)

        # NOW it's safe to free the old buffer — the display is scanning the new one
        self._free_current()

        # Remember new buffer so we can free it on the next frame
        self._current_fb     = new_fb_id
        self._current_handle = new_handle

    def close(self):
        self._free_current()
        import fcntl as _fcntl
        try: _fcntl.ioctl(self.fd, DRM_IOCTL_DROP_MASTER, 0)
        except OSError: pass
        os.close(self.fd)

    def __enter__(self):  return self
    def __exit__(self, *_): self.close()


def _offset(sw, sh, dw, dh, pos):
    if pos == "center":
        return ((dw-sw)//2 if sw < dw else 0,
                (dh-sh)//2 if sh < dh else 0)
    return 0, 0


# =============================================================================
# Image
# =============================================================================

IMAGE_EXTS = {".jpg",".jpeg",".png",".gif",".bmp",".tiff",".tif",".webp"}

def play_image(display, path, duration, position):
    print(f"Image: {path}", file=sys.stderr)
    img = Image.open(path).convert("RGBA")
    display.present(img.tobytes(), img.width, img.height, position)
    time.sleep(duration)   # buffer stays alive — image stays on screen


# =============================================================================
# Video
# =============================================================================

def play_video(display, path, position):
    print(f"Video: {path}", file=sys.stderr)
    container = av.open(str(path))
    stream = next((s for s in container.streams if s.type == "video"), None)
    if not stream:
        print("  No video stream, skipping", file=sys.stderr)
        return

    for s in container.streams:
        if s.type == "audio":
            s.discard = "all"

    fps = float(stream.average_rate or stream.guessed_rate or 30)
    frame_dur = 1.0 / fps if fps > 0 else 1/30
    print(f"  {stream.width}x{stream.height} @ ~{fps:.2f}fps", file=sys.stderr)

    for packet in container.demux(stream):
        for frame in packet.decode():
            t0 = time.monotonic()
            f  = frame.reformat(format="rgba")
            display.present(f.planes[0].to_bytes(), f.width, f.height, position)
            remaining = frame_dur - (time.monotonic() - t0)
            if remaining > 0:
                time.sleep(remaining)

    container.close()


# =============================================================================
# Main
# =============================================================================

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "playlist.toml"
    print(f"drm-player — {config_path}", file=sys.stderr)

    try:
        with open(config_path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        sys.exit(f"Config not found: {config_path}")
    except Exception as e:
        sys.exit(f"Config error: {e}")

    dcfg     = cfg.get("display", {})
    device   = dcfg.get("device",          "/dev/dri/card1")
    position = dcfg.get("position",        "top_left")
    conn_idx = dcfg.get("connector_index", 0)
    items    = cfg.get("playlist", {}).get("items", [])

    if position not in ("center", "top_left"):
        print(f"Warning: unknown position '{position}', using 'top_left'", file=sys.stderr)

    print(f"Opening {device}…", file=sys.stderr)
    try:
        with DRMDisplay(device, conn_idx) as display:
            print(f"Display ready: {display.width}x{display.height}", file=sys.stderr)
            for item in items:
                path = Path(item["path"])
                if not path.exists():
                    print(f"Skipping (not found): {path}", file=sys.stderr)
                    continue
                try:
                    if path.suffix.lower() in IMAGE_EXTS:
                        play_image(display, path, float(item.get("duration", 5.0)), position)
                    else:
                        play_video(display, path, position)
                except KeyboardInterrupt:
                    print("\nInterrupted.", file=sys.stderr)
                    break
                except Exception as e:
                    print(f"Error playing {path}: {e}", file=sys.stderr)
                    traceback.print_exc()
    except RuntimeError as e:
        sys.exit(f"Display error: {e}")
    except KeyboardInterrupt:
        print("\nExiting.", file=sys.stderr)

    print("Done.", file=sys.stderr)

if __name__ == "__main__":
    main()
