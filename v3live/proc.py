"""Windows 只读进程内存访问（ctypes）。仅 PROCESS_VM_READ，从不写入。"""
import ctypes
import ctypes.wintypes as wt
import struct

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x2
TH32CS_SNAPMODULE = 0x8
TH32CS_SNAPMODULE32 = 0x10

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_IMAGE = 0x1000000
PAGE_GUARD = 0x100
READABLE = 0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80

k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = (wt.HANDLE, wt.LPCVOID, wt.LPVOID,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = (wt.HANDLE, wt.LPCVOID, wt.LPVOID, ctypes.c_size_t)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", wt.WCHAR * 260)]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
                ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)), ("modBaseSize", wt.DWORD),
                ("hModule", wt.HMODULE), ("szModule", wt.WCHAR * 256),
                ("szExePath", wt.WCHAR * 260)]


class MBI64(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD), ("__a1", wt.DWORD),
                ("RegionSize", ctypes.c_ulonglong), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD), ("__a2", wt.DWORD)]


def find_pids(name):
    name = name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, wt.HANDLE(-1).value):
        return []
    out = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower().removesuffix(".exe") == name:
                out.append(int(e.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


class Proc:
    def __init__(self, pid):
        self.pid = pid
        self.h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
        if not self.h:
            raise OSError("OpenProcess pid=%d failed err=%d" % (pid, ctypes.get_last_error()))
        self._regions = None

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- 读 ----
    def _into(self, addr, size, out, off):
        if size <= 0:
            return
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        if k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size,
                                 ctypes.byref(got)) and got.value == size:
            out[off:off + size] = buf.raw
            return
        if size > 0x1000:
            half = (size // 2) & ~0xFFF
            if half <= 0:
                half = size // 2
            self._into(addr, half, out, off)
            self._into(addr + half, size - half, out, off + half)

    def read(self, addr, size):
        """恰好 size 字节，不可读处填 0（保证偏移量不乱）。"""
        if not addr or size <= 0:
            return b""
        out = bytearray(size)
        self._into(addr, size, out, 0)
        return bytes(out)

    def u32(self, a):
        return struct.unpack("<I", self.read(a, 4))[0]

    def i32(self, a):
        return struct.unpack("<i", self.read(a, 4))[0]

    def u64(self, a):
        return struct.unpack("<Q", self.read(a, 8))[0]

    def f32(self, a):
        return struct.unpack("<f", self.read(a, 4))[0]

    def f64(self, a):
        return struct.unpack("<d", self.read(a, 8))[0]

    def start_time(self):
        """进程创建时间（FILETIME 整数），用于给缓存分键。"""
        if getattr(self, "_start_time", None):
            return self._start_time
        val = 0
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x0400, False, self.pid)
            if h:
                c = wintypes.FILETIME(); e = wintypes.FILETIME()
                k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                    ctypes.byref(e), ctypes.byref(e))
                val = (c.dwHighDateTime << 32) | c.dwLowDateTime
                k32.CloseHandle(h)
        except Exception:
            val = 0
        self._start_time = val
        return val

    def msvc_string(self, addr, maxlen=256):
        """读 MSVC std::string：{buf/ptr @+0, size @+0x10, capacity @+0x18}。"""
        size = self.u64(addr + 0x10)
        cap = self.u64(addr + 0x18)
        if size is None or cap is None or size > maxlen:
            return None
        if cap >= 16:
            ptr = self.u64(addr)
            if not ptr:
                return None
            raw = self.read(ptr, size)
        else:
            raw = self.read(addr, size)
        return raw.decode("latin-1", "replace") if raw else None

    def i64(self, a):
        return struct.unpack("<q", self.read(a, 8))[0]

    # 别名：两种命名都支持，避免调用处写错
    read_u32, read_i32, read_u64, read_i64 = u32, i32, u64, i64
    read_f32, read_f64 = f32, f64

    # ---- 区域 ----
    def regions(self, refresh=False, private_only=False):
        if self._regions is not None and not refresh:
            return self._regions
        out, mbi = [], MBI64()
        addr = 0
        while addr < 0x7FFFFFFF0000:
            if not k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                      ctypes.sizeof(mbi)):
                break
            ok = (mbi.State == MEM_COMMIT and (mbi.Protect & READABLE)
                  and not (mbi.Protect & PAGE_GUARD))
            if ok and private_only:
                ok = mbi.Type == MEM_PRIVATE
            if ok:
                out.append((mbi.BaseAddress, mbi.RegionSize, mbi.Protect, mbi.Type))
            nxt = mbi.BaseAddress + mbi.RegionSize
            addr = nxt if nxt > addr else addr + 0x1000
        self._regions = out
        return out

    def modules(self):
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
        if snap in (0, wt.HANDLE(-1).value):
            return []
        out = []
        try:
            e = MODULEENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            ok = k32.Module32FirstW(snap, ctypes.byref(e))
            while ok:
                out.append({"name": e.szModule,
                            "base": ctypes.cast(e.modBaseAddr, ctypes.c_void_p).value or 0,
                            "size": int(e.modBaseSize), "path": e.szExePath})
                ok = k32.Module32NextW(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        return out

    def module_base(self, name):
        n = name.lower().removesuffix(".exe")
        for m in self.modules():
            if m["name"].lower().removesuffix(".exe") == n:
                return m["base"]
        return None


def attach(name="victoria3.exe", pid=None):
    if pid:
        return Proc(pid)
    pids = find_pids(name)
    if not pids:
        raise RuntimeError("进程 %s 不存在或不可见" % name)
    return Proc(pids[0])
