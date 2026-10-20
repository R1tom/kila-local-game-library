"""Adrenalin per-game settings, read and written through AMD's own ADL application-profile API
(atiadlxx.dll, shipped with the driver). These are the profiles Adrenalin itself shows under
Gaming -> <game>: Anti-Lag, Chill, Boost, Image Sharpening, Fluid Motion Frames, Enhanced Sync...

Profiles live in the "3D_User" area. Every value is stored as text like "0x0300::1;;"
(format marker "0x0300", then the value). Record layout returned by the search call:
    int count; then per record: char name[64]; int type; int size; byte data[size]  (no padding)
"""
import ctypes
from ctypes import byref, c_int, c_void_p, c_wchar_p
from pathlib import Path

AREA = "3D_User"
USER_BLB = Path.home() / "AppData" / "Local" / "ATI" / "ACE" / "APL" / "User.blb"

# Settings shown in the overlay: (property, label, kind). Empty value = the game follows the global setting.
KNOWN = [
    ("Dlg_PFEnable", "Anti-Lag", "bool"),
    ("UFG_PFEnabled", "Fluid Motion Frames", "bool"),
    ("FsrOverride", "FSR upgrade", "bool"),
    ("Ris_PFEnable", "Image Sharpening", "bool"),
    ("Ris_SHDegree", "Sharpness", "fraction"),
    ("Bst_PFEnable", "Radeon Boost", "bool"),
    ("Chil_PFEnable", "Radeon Chill", "bool"),
    ("Chil_MinFRate", "Chill min FPS", "num"),
    ("Chil_MaxFRate", "Chill max FPS", "num"),
    ("TurboSync", "Enhanced Sync", "bool"),
    ("VSyncControl", "Wait for VSync", "vsync"),
    ("Vsr_PFEnable", "Virtual Super Resolution", "bool"),
]
VSYNC = {"0": "Always off", "1": "Off, unless app says", "2": "On, unless app says", "3": "Always on"}


def describe(kind, v):
    """Human text for a stored value; None/'' = follows the global setting."""
    if v in (None, ""):
        return "Global"
    if kind == "bool":
        return "On" if v not in ("0", "false") else "Off"
    if kind == "fraction":
        try:
            return f"{float(v) * 100:.0f}%"
        except ValueError:
            return v
    if kind == "vsync":
        return VSYNC.get(v, v)
    return v


class ADLProfiles:
    def __init__(self):
        self.dll = ctypes.WinDLL("atiadlxx.dll")
        self._crt = ctypes.CDLL("msvcrt")
        self._crt.malloc.restype = c_void_p
        self._malloc = ctypes.WINFUNCTYPE(c_void_p, c_int)(lambda n: self._crt.malloc(n))
        self.ctx = c_void_p()
        if self.dll.ADL2_Main_Control_Create(self._malloc, 1, byref(self.ctx)) != 0:
            raise OSError("ADL init failed")
        self.dll.ADL2_ApplicationProfiles_User_Load(self.ctx)

    # ------------------------------------------------------------------ read
    def read(self, exe_path):
        """{property: value string} of the game's Adrenalin profile, or {} when it has none."""
        p = Path(exe_path)
        prof = c_void_p()
        rc = self.dll.ADL2_ApplicationProfiles_ProfileOfAnApplicationX2_Search(
            self.ctx, c_wchar_p(p.name), c_wchar_p(str(p.parent)), c_wchar_p(""), c_wchar_p(AREA), byref(prof))
        if rc != 0 or not prof.value:
            return {}
        base, out = prof.value, {}
        n, off = c_int.from_address(base).value, 4
        for _ in range(max(0, min(n, 1000))):
            name = ctypes.string_at(base + off, 64).split(b"\0", 1)[0].decode("ascii", "replace")
            size = c_int.from_address(base + off + 68).value
            data = ctypes.string_at(base + off + 72, size) if size > 0 else b""
            out[name] = data.decode("utf-16le", "replace").rstrip("\0")
            off += 72 + size
        self._crt.free(c_void_p(base))
        return out

    @staticmethod
    def value(raw):
        """'0x0300::140;;' -> '140'"""
        if raw is None:
            return None
        v = raw.split("::", 1)[1] if "::" in raw else raw
        return v[:-2] if v.endswith(";;") else v

    @staticmethod
    def raw(v):
        return f"0x0300::{v};;"

    # Writing is NOT done: ADL2_ApplicationProfiles_Profile_Create (and the ADL1 version) crash with
    # an access violation inside atiadlxx.dll on this driver (tested 2026-10-17, even with one record),
    # so per-game profiles are read-only here.

    def close(self):
        try:
            self.dll.ADL2_Main_Control_Destroy(self.ctx)
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    a = ADLProfiles()
    prof = a.read(sys.argv[1])
    print(len(prof), "properties")
    for k, v in sorted(prof.items()):
        print(f"  {k:18} {a.value(v)}")
