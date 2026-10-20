"""Tiny ctypes binding to AMD ADLX (amdadlx64.dll, shipped with the Adrenalin driver).

Reads the same numbers Adrenalin's own metrics overlay shows: GPU usage, clocks, temperatures,
board power, fan, VRAM, and the driver's FPS for the game in front. No extra tracing,
no injection: the driver is already collecting these. Vtable slots come from the ADLX SDK
C headers (IPerformanceMonitoring.h / ISystem.h).
"""
import ctypes
from ctypes import c_double, c_int, c_uint, c_uint64, c_void_p, POINTER, byref

OK = 0  # ADLX_OK
P = c_void_p


def _vfunc(obj, index, *argtypes):
    """Method `index` from a C-style ADLX interface (first field = vtable pointer)."""
    vtbl = ctypes.cast(ctypes.cast(obj, POINTER(P))[0], POINTER(P))
    return ctypes.WINFUNCTYPE(c_int, P, *argtypes)(vtbl[index])


def _release(obj):
    if obj:
        _vfunc(obj, 1)(obj)  # IADLXInterface::Release


class ADLX:
    # IADLXSystem
    SYS_GET_GPUS, SYS_GET_PERF = 1, 9
    # IADLXGPUList
    LIST_SIZE, LIST_AT_GPU = 3, 11
    # IADLXPerformanceMonitoringServices
    PM_CURRENT_GPU, PM_CURRENT_FPS = 18, 20
    # IADLXGPUMetrics getters (all double except VRAM/fan/clock/power ints)
    GPU_METRICS = {  # name: (slot, ctype)
        "usage": (4, c_double), "clock": (5, c_int), "vram_clock": (6, c_int), "temp": (7, c_double),
        "hotspot": (8, c_double), "power": (9, c_double), "board_power": (10, c_double),
        "fan": (11, c_int), "vram": (12, c_int),
    }

    def __init__(self):
        self.dll = ctypes.WinDLL("amdadlx64.dll")
        ver = c_uint64()
        self.dll.ADLXQueryFullVersion(byref(ver))
        self.system = P()
        init = self.dll.ADLXInitialize
        init.argtypes = [c_uint64, POINTER(P)]
        if init(ver.value, byref(self.system)) != OK or not self.system:
            raise OSError("ADLX init failed")
        self.perf = P()
        if _vfunc(self.system, self.SYS_GET_PERF, POINTER(P))(self.system, byref(self.perf)) != OK:
            raise OSError("ADLX: no performance monitoring")
        gpus = P()
        _vfunc(self.system, self.SYS_GET_GPUS, POINTER(P))(self.system, byref(gpus))
        self.gpu = P()
        if gpus and _vfunc(gpus, self.LIST_SIZE)(gpus) > 0:
            _vfunc(gpus, self.LIST_AT_GPU, c_uint, POINTER(P))(gpus, 0, byref(self.gpu))
        _release(gpus)
        if not self.gpu:
            raise OSError("ADLX: no AMD GPU")
        name, vram = ctypes.c_char_p(), c_uint()
        _vfunc(self.gpu, 7, POINTER(ctypes.c_char_p))(self.gpu, byref(name))   # IADLXGPU::Name
        _vfunc(self.gpu, 11, POINTER(c_uint))(self.gpu, byref(vram))           # IADLXGPU::TotalVRAM (MB)
        self.name = (name.value or b"AMD GPU").decode(errors="replace")
        self.vram_total = vram.value

    def gpu_metrics(self):
        m = P()
        out = {}
        if _vfunc(self.perf, self.PM_CURRENT_GPU, P, POINTER(P))(self.perf, self.gpu, byref(m)) != OK or not m:
            return out
        try:
            for name, (slot, ct) in self.GPU_METRICS.items():
                v = ct()
                if _vfunc(m, slot, POINTER(ct))(m, byref(v)) == OK:
                    out[name] = v.value
        finally:
            _release(m)
        return out

    def fps(self):
        """Driver FPS of the foreground 3D app, or None when there isn't one."""
        f = P()
        if _vfunc(self.perf, self.PM_CURRENT_FPS, POINTER(P))(self.perf, byref(f)) != OK or not f:
            return None
        try:
            v = c_int()
            ok = _vfunc(f, 4, POINTER(c_int))(f, byref(v)) == OK
            return v.value if ok and v.value >= 0 else None
        finally:
            _release(f)

    def close(self):
        _release(self.gpu)
        _release(self.perf)
        try:
            self.dll.ADLXTerminate()
        except Exception:
            pass

    # ------------------------------------------------------------ 3D graphics settings
    # These are Adrenalin's GLOBAL graphics settings (ADLX has no per-game profiles); the overlay
    # keeps its own per-game profiles on top and restores the global values when a game closes.
    def _services3d(self):
        sv = P()
        if _vfunc(self.system, 7, POINTER(P))(self.system, byref(sv)) != OK:  # Get3DSettingsServices
            return None
        return sv

    def _setting(self, sv, key):
        meta = SETTINGS[key]
        obj = P()
        if key == "afmf":  # lives on IADLX3DSettingsServices1
            sv1 = P()
            if _vfunc(sv, 2, ctypes.c_wchar_p, POINTER(P))(sv, "IADLX3DSettingsServices1", byref(sv1)) != OK or not sv1:
                return None
            rc = _vfunc(sv1, meta["slot"], POINTER(P))(sv1, byref(obj))
            _release(sv1)
        elif meta["gpu"]:
            rc = _vfunc(sv, meta["slot"], P, POINTER(P))(sv, self.gpu, byref(obj))
        else:
            rc = _vfunc(sv, meta["slot"], POINTER(P))(sv, byref(obj))
        return obj if rc == OK and obj else None

    def read_3d(self):
        """{key: {"supported", "enabled", "value", "range": (min, max, step)}} for SETTINGS."""
        out = {}
        sv = self._services3d()
        if not sv:
            return out
        try:
            for key, m in SETTINGS.items():
                obj = self._setting(sv, key)
                if not obj:
                    continue
                try:
                    b, v = ctypes.c_bool(), c_int()
                    st = {"supported": _vfunc(obj, 3, POINTER(ctypes.c_bool))(obj, byref(b)) == OK and b.value}
                    if not st["supported"]:
                        out[key] = st
                        continue
                    if "mode_get" in m:
                        _vfunc(obj, m["mode_get"], POINTER(c_int))(obj, byref(v))
                        st.update(enabled=v.value >= 2, value=v.value, range=(0, 3, 1))
                    else:
                        _vfunc(obj, 4, POINTER(ctypes.c_bool))(obj, byref(b))
                        st["enabled"] = b.value
                        if "get" in m:
                            r = IntRange()
                            _vfunc(obj, m["range"], POINTER(IntRange))(obj, byref(r))
                            _vfunc(obj, m["get"], POINTER(c_int))(obj, byref(v))
                            st.update(value=v.value, range=(r.minValue, r.maxValue, max(1, r.step)))
                    out[key] = st
                finally:
                    _release(obj)
        finally:
            _release(sv)
        return out

    def set_3d(self, key, enabled=None, value=None):
        m = SETTINGS[key]
        sv = self._services3d()
        if not sv:
            return False
        ok = True
        try:
            obj = self._setting(sv, key)
            if not obj:
                return False
            try:
                if "mode_set" in m:
                    if value is not None:
                        ok = _vfunc(obj, m["mode_set"], c_int)(obj, int(value)) == OK
                else:
                    # values only stick while the feature is on: switch on first, set the value, switch off last
                    if enabled:
                        ok &= _vfunc(obj, m["set"], ctypes.c_bool)(obj, True) == OK
                    if value is not None and "setv" in m:
                        v_ok = _vfunc(obj, m["setv"], c_int)(obj, int(value)) == OK
                        ok &= v_ok or enabled is False
                    if enabled is False:
                        ok &= _vfunc(obj, m["set"], ctypes.c_bool)(obj, False) == OK
            finally:
                _release(obj)
        finally:
            _release(sv)
        return ok


class IntRange(ctypes.Structure):
    _fields_ = [("minValue", c_int), ("maxValue", c_int), ("step", c_int)]


# Vtable slots per setting interface (I3DSettings*.h). slot = getter on IADLX3DSettingsServices(1),
# gpu = whether that getter takes the GPU. Common: 3 IsSupported, 4 IsEnabled.
SETTINGS = {
    "afmf":       {"label": "Fluid Motion Frames", "slot": 17, "gpu": False, "set": 5},
    "antilag":    {"label": "Anti-Lag", "slot": 3, "gpu": True, "set": 5},
    "rsr":        {"label": "Radeon Super Resolution", "slot": 14, "gpu": False, "set": 5, "range": 6, "get": 7, "setv": 8,
                   "unit": "% sharp"},
    "sharpening": {"label": "Image Sharpening", "slot": 6, "gpu": True, "range": 5, "get": 6, "set": 7, "setv": 8,
                   "unit": "%"},
    "boost":      {"label": "Radeon Boost", "slot": 5, "gpu": True, "range": 5, "get": 6, "set": 7, "setv": 8,
                   "unit": "% min res"},
    "chill":      {"label": "Radeon Chill", "slot": 4, "gpu": True, "range": 5, "get": 7, "set": 8, "setv": 10,
                   "unit": " max FPS"},
    "frtc":       {"label": "Frame Rate Target", "slot": 9, "gpu": True, "range": 5, "get": 6, "set": 7, "setv": 8,
                   "unit": " FPS"},
    "esync":      {"label": "Enhanced Sync", "slot": 7, "gpu": True, "set": 5},
    "vsync":      {"label": "Wait for VSync", "slot": 8, "gpu": True, "mode_get": 5, "mode_set": 6},
}
VSYNC_MODES = ["Always off", "Off, unless app says", "On, unless app says", "Always on"]


if __name__ == "__main__":
    import time
    a = ADLX()
    for _ in range(2):
        print(a.gpu_metrics(), "fps:", a.fps())
        time.sleep(0.5)
    for k, v in a.read_3d().items():
        print(k, v)
    a.close()
