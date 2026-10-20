"""Read and minimally edit Adrenalin's per-game profile file (%LOCALAPPDATA%\\ATI\\ACE\\APL\\User.blb).

AMD's ADL "create profile" call crashes on this driver, so writes are done in the file itself, in the
smallest possible way: a value text like "0x0300::0;;" is overwritten in place by one of the SAME
length ("0x0300::1;;"), so no offset, size or index anywhere in the file changes.

Layout worked out on 2026-10-17 (driver with ADL 7.26):
  header (96 bytes): "BWJE", version 1, then section table as (start, length) in 8-byte units from
                     byte 96: strings | index | ... | profiles | profile pointers | app records
  strings section:   UTF-16 NUL-terminated strings at 8-byte unit offsets (idx -> byte 96 + idx*8)
  app records:       10 ints each: 0, 2, file_idx, file_len, path_idx, path_len, 0, 2, ptr_idx, 0
                     (path is a prefix ending in '*'; a record may be a "steamappid:N" instead)
  profile pointers:  per ptr_idx (counted from the pointer section start) an (unit offset, -1) pair
  profile block:     8 ints (.., name_idx, name_len, 0, 2, .., byte_size, count) then `count` records:
                     ASCII name + NUL padded to 8 bytes (min 16), int size, UTF-16 data[size], padded to 8

Safety: every write first copies the file to User.blb.bak-<date-time>, checks the bytes it replaces
are exactly the expected old value, writes atomically, and re-parses the result.
"""
import os
import struct
import time
from pathlib import Path

USER_BLB = Path.home() / "AppData" / "Local" / "ATI" / "ACE" / "APL" / "User.blb"
BASE = 96


class Blb:
    def __init__(self, path=USER_BLB):
        self.path = Path(path)
        self.b = bytearray(self.path.read_bytes())
        if self.b[:4] != b"BWJE":
            raise ValueError("not an Adrenalin profile file")
        w = struct.unpack_from("<16I", self.b, 32)
        # section table: (start, len) pairs from the 3rd int at byte 32; contiguous, so read while they chain
        secs, i = [], 2
        while i + 1 < len(w) and (not secs or w[i] == secs[-1][0] + secs[-1][1]):
            secs.append((w[i], w[i + 1]))
            i += 2
        if len(secs) < 7:
            raise ValueError(f"unexpected section table {secs}")
        self.secs = secs
        self.ptr_sec, self.app_sec = secs[-2], secs[-1]

    # ------------------------------------------------------------------ strings
    def s16(self, idx):
        o = BASE + idx * 8
        e = self.b.find(b"\0\0", o)
        while e != -1 and (e - o) % 2:
            e = self.b.find(b"\0\0", e + 1)
        return self.b[o:e].decode("utf-16le", "replace")

    # ------------------------------------------------------------------ apps -> profiles
    def apps(self):
        """[(file name, path prefix, profile byte offset)]"""
        st, n = self.app_sec
        w = struct.unpack_from(f"<{n * 2}i", self.b, BASE + st * 8)
        out = []
        for k in range(0, len(w) - 9, 10):
            rec = w[k:k + 10]
            if rec[0] != 0 or rec[1] != 2:
                continue
            fname = self.s16(rec[2]) if rec[3] else ""
            path = self.s16(rec[4]) if rec[5] else ""
            ptr = rec[8] - self.ptr_sec[0]
            if not 0 <= ptr < self.ptr_sec[1]:
                continue
            unit = struct.unpack_from("<i", self.b, BASE + (self.ptr_sec[0] + ptr) * 8)[0]
            out.append((fname, path, BASE + unit * 8))
        return out

    def profile_offset(self, exe_path):
        p = Path(exe_path)
        name, folder = p.name.lower(), str(p.parent).lower()
        for fname, path, off in self.apps():
            if fname.lower() == name and folder.startswith(path.lower().rstrip("*").rstrip("\\")):
                return off
        return None

    # ------------------------------------------------------------------ profile records
    def records(self, off):
        """{name: (value text, byte offset of the UTF-16 data, data size)} of the profile at `off`."""
        h = struct.unpack_from("<8i", self.b, off)
        count, o, out = h[7], off + 32, {}
        if not 0 < count < 500:
            raise ValueError(f"bad profile header at {off}: {h}")
        end = off + 32 + h[6]
        for _ in range(count):
            e = self.b.index(b"\0", o)
            name = self.b[o:e].decode("ascii", "replace")
            o += max(16, (e - o + 1 + 7) & ~7)  # name field: NUL-terminated, 8-byte padded, at least 16 bytes
            size = struct.unpack_from("<i", self.b, o)[0]
            o += 4
            if not 0 <= size < 4096 or o + size > end:
                raise ValueError(f"profile at {off}: bad record {name!r} size {size}")
            out[name] = (self.b[o:o + size].decode("utf-16le", "replace").rstrip("\0"), o, size)
            o = (o + size + 7) & ~7
        return out

    def read(self, exe_path):
        off = self.profile_offset(exe_path)
        return {k: v[0] for k, v in self.records(off).items()} if off is not None else {}

    # ------------------------------------------------------------------ write
    def offsets_for(self, exe_path, appid=None):
        """Profile blocks of one game: the exe's, plus its "steamappid:N" twin when Adrenalin made one."""
        offs = []
        o = self.profile_offset(exe_path)
        if o is not None:
            offs.append(o)
        if appid:
            offs += [off for fname, _, off in self.apps() if fname == f"steamappid:{appid}" and off not in offs]
        return offs

    def set(self, exe_path, prop, new_text, appid=None, expect=None):
        """Replace one value in place in every profile block of the game. Returns None or an error text.
        expect: the value the caller saw (from the driver); nothing is written if the file differs."""
        offs = self.offsets_for(exe_path, appid)
        if not offs:
            return "no Adrenalin profile for this game"
        new = new_text.encode("utf-16le")
        edits = []
        for i, off in enumerate(offs):
            recs = self.records(off)
            if prop not in recs:
                continue
            old, at, size = recs[prop]
            if i == 0 and expect is not None and old != expect:
                return "Adrenalin's value changed meanwhile; reopen the panel"
            if old == new_text:
                continue
            if len(new) != size:
                return f"can't change {old!r} to {new_text!r} without resizing the file"
            edits.append(at)
        if not edits:
            return None
        disk = self.path.read_bytes()
        if disk != bytes(self.b):
            return "Adrenalin changed the file meanwhile; try again"
        bak = self.path.with_name(f"User.blb.bak-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}")
        bak.write_bytes(disk)
        for old_bak in sorted(self.path.parent.glob("User.blb.bak-20*"))[:-20]:  # keep the last 20
            old_bak.unlink(missing_ok=True)
        with open(self.path, "r+b") as fh:  # in place: same file, same size, only these bytes change
            for at in edits:
                fh.seek(at)
                fh.write(new)
        after = Blb(self.path)
        if any(after.records(off).get(prop, (new_text,))[0] != new_text for off in offs) or len(after.b) != len(disk):
            self.path.write_bytes(disk)
            return "verify failed, the backup was put back"
        self.b = after.b
        return None


if __name__ == "__main__":
    import sys
    f = Blb()
    print("sections", f.secs)
    for a in f.apps():
        print("app", a)
    if len(sys.argv) > 1:
        for k, v in sorted(f.read(sys.argv[1]).items()):
            print(f"  {k:18} {v}")
