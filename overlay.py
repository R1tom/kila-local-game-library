"""Xbox-style achievement pop-up at the top middle of the screen.

A layered, click-through, never-focused Win32 window drawn frame by frame with Pillow (per-pixel
alpha, so edges are smooth). Animation: the badge drops in, expands into a pill with the text,
a shine sweeps across, then it collapses and rises away. show() blocks until it has finished.

Test:  venv\\Scripts\\python.exe overlay.py
"""
import ctypes
import ctypes.wintypes as W
import math
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

HERE = Path(__file__).resolve().parent
FONTS = Path(r"C:\Windows\Fonts")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32")
winmm = ctypes.WinDLL("winmm")
shell32 = ctypes.WinDLL("shell32")

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE),
                ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR),
                ("hIconSm", W.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


user32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
user32.CreateWindowExW.restype = W.HWND
user32.UpdateLayeredWindow.argtypes = [W.HWND, W.HDC, ctypes.POINTER(W.POINT), ctypes.POINTER(W.SIZE), W.HDC,
                                       ctypes.POINTER(W.POINT), W.COLORREF, ctypes.POINTER(BLENDFUNCTION), W.DWORD]
user32.GetDC.argtypes = [W.HWND]
user32.GetDC.restype = W.HDC
user32.ReleaseDC.argtypes = [W.HWND, W.HDC]
user32.DestroyWindow.argtypes = [W.HWND]
user32.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
user32.PeekMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(W.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(W.MSG)]
gdi32.CreateCompatibleDC.argtypes = [W.HDC]
gdi32.CreateCompatibleDC.restype = W.HDC
gdi32.CreateDIBSection.argtypes = [W.HDC, ctypes.c_void_p, W.UINT, ctypes.POINTER(ctypes.c_void_p), W.HANDLE, W.DWORD]
gdi32.CreateDIBSection.restype = W.HBITMAP
gdi32.SelectObject.argtypes = [W.HDC, W.HGDIOBJ]
gdi32.SelectObject.restype = W.HGDIOBJ
gdi32.DeleteObject.argtypes = [W.HGDIOBJ]
gdi32.DeleteDC.argtypes = [W.HDC]
kernel32.GetModuleHandleW.restype = W.HMODULE

CLASS = "KilaLibraryPopup"
_wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
_registered = False

GREEN, GOLD = (16, 124, 16), (227, 179, 65)
BG = (24, 24, 26, 238)

# timeline (seconds)
DROP, EXPAND, TEXT_IN, SHINE, HOLD_END, TEXT_OUT, COLLAPSE, RISE = 0.45, 0.45, 0.35, 0.8, 7.0, 0.25, 0.4, 0.4


def ease_out_back(t):
    c = 1.70158
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def ease_in_out(t):
    return 3 * t * t - 2 * t ** 3


def clamp(t):
    return 0.0 if t < 0 else 1.0 if t > 1 else t


def font(names, size):
    for n in names:
        try:
            return ImageFont.truetype(str(FONTS / n), size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def fit(draw, text, f, maxw):
    if draw.textlength(text, font=f) <= maxw:
        return text
    while text and draw.textlength(text + "…", font=f) > maxw:
        text = text[:-1]
    return text.rstrip() + "…"


def aa_circle(d):
    big = Image.new("L", (d * 4, d * 4), 0)
    ImageDraw.Draw(big).ellipse((0, 0, d * 4 - 1, d * 4 - 1), fill=255)
    return big.resize((d, d), Image.LANCZOS)


def stadium(circ, w):
    d = circ.width
    h = d // 2
    m = Image.new("L", (max(w, d), d), 0)
    m.paste(circ.crop((0, 0, h, d)), (0, 0))
    m.paste(circ.crop((h, 0, d, d)), (m.width - (d - h), 0))
    if m.width - d > 0:
        ImageDraw.Draw(m).rectangle((h, 0, m.width - (d - h) - 1, d), fill=255)
    return m


def with_alpha(im, a):
    if a >= 1:
        return im
    out = im.copy()
    out.putalpha(im.getchannel("A").point(lambda v: int(v * a)))
    return out


class Card:
    def __init__(self, name, game, icon=None, pct=None, s=1.0, head=None, hold=None, accent=None):
        rare = pct is not None and pct < 10
        head_rgb = (240, 200, 110) if rare else (155, 229, 155)
        if accent:
            head_rgb = tuple(round(c + (255 - c) * 0.55) for c in accent)
        accent = tuple(accent) if accent else GOLD if rare else GREEN
        self.s = s
        D = self.D = round(84 * s)
        pad, right = round(16 * s), round(30 * s)
        f_head = font(["seguisb.ttf", "segoeuib.ttf"], round(12.5 * s))
        f_name = font(["segoeuib.ttf", "seguisb.ttf"], round(19 * s))
        f_sub = font(["segoeui.ttf"], round(14 * s))
        if head is None:
            head = "RARE ACHIEVEMENT UNLOCKED" if rare else "ACHIEVEMENT UNLOCKED"
        sub = game + (f"  ·  {pct:.1f}% of players" if pct is not None else "")
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        maxw = round(440 * s)
        name, sub = fit(probe, name, f_name, maxw), fit(probe, sub, f_sub, maxw)
        tw = int(max(probe.textlength(head, font=f_head) + len(head) * 1.2 * s, probe.textlength(name, font=f_name),
                     probe.textlength(sub, font=f_sub)))
        tw = max(tw, round(220 * s))
        self.full = D + pad + tw + right
        self.text_x = D + pad

        # text block, vertically centred in the pill
        self.text = Image.new("RGBA", (tw + 4, D), (0, 0, 0, 0))
        td = ImageDraw.Draw(self.text)
        rows = [r for r in (("head", 17), ("name", 26), ("sub", 19)) if r[0] == "name" or (head if r[0] == "head" else sub)]
        y = round(13 * s) if len(rows) == 3 else round((D - sum(h for _, h in rows) * s) / 2 + 2 * s)
        for kind, h in rows:
            if kind == "head":
                x = 0
                for ch in head:  # letter-spaced header
                    td.text((x, y), ch, font=f_head, fill=head_rgb)
                    x += td.textlength(ch, font=f_head) + 1.2 * s
            elif kind == "name":
                td.text((0, y), name, font=f_name, fill=(255, 255, 255))
            else:
                td.text((0, y), sub, font=f_sub, fill=(178, 178, 182))
            y += round(h * s)

        # badge: accent ring + round achievement icon
        self.circ = aa_circle(D)
        ring = round(4 * s)
        badge = Image.new("RGBA", (D, D), accent + (255,))
        inner = D - ring * 2
        pic = None
        if icon and Path(icon).exists():
            try:
                pic = ImageOps.fit(Image.open(icon).convert("RGB"), (inner, inner), Image.LANCZOS)
            except Exception:
                pic = None
        if pic is None:
            pic = Image.new("RGB", (inner, inner), (30, 30, 32))
            try:
                logo = Image.open(HERE / "ui" / "icon.png").convert("RGBA")
                logo = ImageOps.contain(logo, (int(inner * 0.62),) * 2, Image.LANCZOS)
                pic.paste(logo, ((inner - logo.width) // 2, (inner - logo.height) // 2), logo)
            except Exception:
                pass
        badge.paste(pic, (ring, ring), aa_circle(inner))
        badge.putalpha(self.circ)
        self.badge = badge

        # soft white diagonal band for the shine sweep
        bw = round(70 * s)
        band = Image.new("L", (bw, 1))
        band.putdata([int(70 * math.sin(math.pi * i / (bw - 1))) for i in range(bw)])
        band = band.resize((bw, D * 2)).rotate(-20, resample=Image.BICUBIC, expand=True)
        self.shine = Image.new("RGBA", band.size, (255, 255, 255, 0))
        self.shine.putalpha(band)

        self.margin = round(26 * s)
        self.top = round(30 * s)
        self.W = self.full + self.margin * 2
        self.H = self.top + D + self.margin
        self.hold = hold or HOLD_END
        self.total = DROP + EXPAND + self.hold + TEXT_OUT + COLLAPSE + RISE

    def static(self, t):
        """True during the still part of the hold: after the shine sweep, before the text fades out."""
        return DROP + EXPAND + TEXT_IN + SHINE <= t < DROP + EXPAND + self.hold

    def frame(self, t):
        D, cx = self.D, self.W // 2
        e = 1.0  # pill expansion 0..1
        by = self.top  # badge y
        a = 1.0  # overall alpha
        text_a, shine_p = 0.0, None
        t_exp, t_hold = DROP, DROP + EXPAND
        t_out = t_hold + self.hold
        t_col = t_out + TEXT_OUT
        t_rise = t_col + COLLAPSE
        if t < t_exp:
            p = clamp(t / DROP)
            e, by, a = 0.0, round(self.top - (self.top + D) * (1 - ease_out_back(p))), clamp(p * 2)
        elif t < t_hold:
            e = ease_out_cubic(clamp((t - t_exp) / EXPAND))
        if t >= t_exp + EXPAND * 0.6:
            text_a = clamp((t - t_exp - EXPAND * 0.6) / TEXT_IN)
        sh0 = t_hold + TEXT_IN
        if sh0 <= t < sh0 + SHINE:
            shine_p = ease_in_out((t - sh0) / SHINE)
        if t >= t_out:
            text_a = 1 - clamp((t - t_out) / TEXT_OUT)
        if t >= t_col:
            e = 1 - ease_in_out(clamp((t - t_col) / COLLAPSE))
        if t >= t_rise:
            p = ease_in_out(clamp((t - t_rise) / RISE))
            e, by, a = 0.0, round(self.top - (self.top + D) * p), 1 - p

        w = round(D + (self.full - D) * e)
        px = cx - w // 2
        mask = Image.new("L", (self.W, self.H), 0)
        if e > 0:
            mask.paste(stadium(self.circ, w), (px, by))
        else:
            mask.paste(self.circ, (cx - D // 2, by))
            px = cx - D // 2

        out = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        shadow = ImageChops.offset(mask, 0, round(5 * self.s)).filter(ImageFilter.GaussianBlur(10 * self.s))
        out.putalpha(shadow.point(lambda v: int(v * 0.55 * a)))
        bg = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        bg.paste(BG, (0, 0, self.W, self.H), mask)
        out.alpha_composite(bg)

        if text_a > 0 or shine_p is not None:
            layer = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
            if text_a > 0:
                layer.paste(with_alpha(self.text, text_a), (px + self.text_x - round(10 * self.s * (1 - text_a)), by))
            if shine_p is not None:
                sx = round(px - self.shine.width + (w + self.shine.width) * shine_p)
                sl = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
                sl.paste(self.shine, (sx, by - (self.shine.height - D) // 2))
                layer.alpha_composite(sl)
            layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
            out.alpha_composite(layer)

        bl = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        bl.paste(with_alpha(self.badge, a), (px, by))
        out.alpha_composite(bl)
        return out


def _bgra_premultiplied(im):
    r, g, b, a = im.convert("RGBa").split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


def show(name, game, icon=None, pct=None, sound=None, head=None, hold=None, scale=1.0, accent=None, position="top-center"):
    """Show one pop-up (blocking, ~9 s). sound: optional .wav played as it appears.
    head: replaces the small "ACHIEVEMENT UNLOCKED" header (used by the game-launch welcome)."""
    global _registered
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    try:
        dpi = user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    card = Card(name, game, icon, pct, s=dpi / 96 * scale, head=head, hold=hold, accent=accent)
    sw = user32.GetSystemMetrics(0)
    wa = W.RECT()
    if not user32.SystemParametersInfoW(0x30, 0, ctypes.byref(wa), 0):  # SPI_GETWORKAREA (above the taskbar)
        wa = W.RECT(0, 0, sw, user32.GetSystemMetrics(1))
    vert, _, horiz = (position or "top-center").partition("-")
    x = wa.left if horiz == "left" else wa.right - card.W if horiz == "right" else (wa.left + wa.right - card.W) // 2
    y = wa.bottom - card.H if vert == "bottom" else wa.top

    hinst = kernel32.GetModuleHandleW(None)
    if not _registered:
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=_wndproc, hInstance=hinst,
                         lpszClassName=CLASS)
        user32.RegisterClassExW(ctypes.byref(wc))
        _registered = True
    ex = 0x80000 | 0x20 | 0x8 | 0x80 | 0x8000000  # LAYERED|TRANSPARENT|TOPMOST|TOOLWINDOW|NOACTIVATE
    hwnd = user32.CreateWindowExW(ex, CLASS, "Achievement", 0x80000000, x, y, card.W, card.H, None, None, hinst, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFOHEADER(biSize=40, biWidth=card.W, biHeight=-card.H, biPlanes=1, biBitCount=32)
    bits = ctypes.c_void_p()
    dib = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mem, dib)
    blend = BLENDFUNCTION(0, 0, 255, 1)
    pt_dst, pt_src, size = W.POINT(x, y), W.POINT(0, 0), W.SIZE(card.W, card.H)
    msg = W.MSG()
    winmm.timeBeginPeriod(1)
    try:
        if sound and Path(sound).exists():
            import winsound
            winsound.PlaySound(str(sound), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        shown = still = False
        start = time.perf_counter()
        while True:
            t = time.perf_counter() - start
            if t > card.total:
                break
            if not (still and card.static(t)):  # the still part of the hold is drawn once
                data = _bgra_premultiplied(card.frame(t))
                ctypes.memmove(bits, data, len(data))
                user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(pt_dst), ctypes.byref(size), mem,
                                           ctypes.byref(pt_src), 0, ctypes.byref(blend), 2)
                still = card.static(t)
            if not shown:
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                shown = True
                top_at = 0.0
            # a starting game often puts its own window on top: take the top back at show and once ~1 s later,
            # never repeatedly (re-asserting TOPMOST over an exclusive-fullscreen game can minimize it)
            if top_at is not None and t >= top_at:
                user32.SetWindowPos(hwnd, W.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # TOPMOST, no activate
                top_at = 1.0 if top_at == 0.0 else None
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            spare = 1 / 60 - (time.perf_counter() - start - t)
            if spare > 0:
                time.sleep(spare)
    finally:
        winmm.timeEndPeriod(1)
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(dib)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
        user32.DestroyWindow(hwnd)


if __name__ == "__main__":
    import sys
    icons = sorted((HERE / "data" / "cache" / "icons").glob("*/*.jpg"))
    pct = float(sys.argv[1]) if len(sys.argv) > 1 else 23.4
    show("Sound test", "Test Game", icons[0] if icons else None, pct,
         HERE / "sounds" / ("achievement-rare.wav" if pct is not None and pct < 10 else "achievement.wav"))
