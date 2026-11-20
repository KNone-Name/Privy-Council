// ============================================================================
//  Victoria 3 bridge  (v60 -- clean rewrite)
//
//  Target: Victoria 3 4.5.2 (3270)
//          victoria3.exe 99988600 bytes, FNV-1a64 0x017B6FB764315747
//
//  Capabilities
//    * player country   -- CJominiPlayer+0x58 (authoritative, no per-country flags)
//    * build            -- requests.txt -> engine construction command
//    * state query      -- query.txt -> states.csv
//    * PM read          -- pms.txt -> pms.csv
//    * PM write         -- pmm.txt -> pmm.result.csv (same-group slot replacement)
//
//  Design rules learned the hard way
//    1. Never trust a fixed sleep: poll for readiness.
//    2. Never act on a guessed player id: read it from the engine or stay idle.
//    3. Validate every engine pointer before dereferencing (page + range).
//    4. Restore everything and drop the exception handler before unmapping.
//    5. One driver is fragile: hook every message API and post WM_NULL to wake
//       the pump, so an idle or unfocused window still does work.
//    6. All files live next to this DLL; no absolute paths.
// ============================================================================
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <cstdint>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <string>
#include <unordered_map>
#include <vector>

// ---------------------------------------------------------------------------
// Known-good build
// ---------------------------------------------------------------------------
static constexpr uint64_t kFileSize     = 100221048ULL;          // 1.15.1
static constexpr uint64_t kFileHash     = 0xF8A08D788306BC2FULL;  // FNV-1a 64
static constexpr uint32_t kTimeDate     = 0x6AF3094F;

// Recorded RVAs (only used when the fingerprint proves this exact build).
namespace rva {
    constexpr uintptr_t ResolveCountry = 0x00983A00;
    constexpr uintptr_t ResolveState   = 0x00983660;
    constexpr uintptr_t CountLevels    = 0x0095CB60;
    constexpr uintptr_t BuildCommand   = 0x008C24B0;
    constexpr uintptr_t BldLookup      = 0x00DCBD70;
    // Player vtable measured live (RE record section 10). Only used as an extra
    // scan target - never to validate a pointer.
    // No recorded player vtable for this build: RTTI supplies it. A WRONG hint is
    // worse than none - the scan would happily match objects of that class.
    constexpr uintptr_t VtPlayer[] = { 0 };
    // The type database is built at runtime from common/buildings/*.txt, so its
    // global has no stable static anchor. LoadTypesFromDb() is kept as the fast
    // path; when it fails the code falls back to ScanTypes() (vtable instance
    // scan), which is version independent.
    constexpr uintptr_t DbGlobal       = 0;
    constexpr uintptr_t BuildCtxGlobal = 0x05C0DCA8;   // arg-8 context object (1.15.1)
}

// Prologue signatures for 1.15.1. Every relative displacement / branch target /
// immediate is wildcarded (mask byte 0) - those bytes change on every rebuild,
// which is exactly why the previous fixed-byte signatures all died.
//
// NOTE: the ResolveState and ResolveCountry signatures differ ONLY in the rip
// displacement, so after masking they are identical. They are resolved through the
// recorded RVA fast path; a signature scan alone cannot tell them apart.
// ResolveCountry  rva=0x00983A00  (46 字节, 通配 24)
static const uint8_t kSigResolveCountry[46] = {
    0x48, 0x83, 0xEC, 0x58, 0x48, 0x8B, 0x05, 0x00, 0x00, 0x00, 0x00, 0x8B,
    0x11, 0x48, 0x85, 0xC0, 0x75, 0x00, 0x00, 0x00, 0x00, 0x0F, 0x84, 0x00,
    0x00, 0x00, 0x00, 0x48, 0x8D, 0x15, 0x00, 0x00, 0x00, 0x00, 0x48, 0x8D,
    0x0D, 0x00, 0x00, 0x00, 0x00, 0xE8, 0x00, 0x00, 0x00, 0x00,
};
static const uint8_t kSigResolveCountryMask[46] = {
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00,
};

// ResolveState  rva=0x00983660  (46 字节, 通配 24)
static const uint8_t kSigResolveState[46] = {
    0x48, 0x83, 0xEC, 0x58, 0x48, 0x8B, 0x05, 0x00, 0x00, 0x00, 0x00, 0x8B,
    0x11, 0x48, 0x85, 0xC0, 0x75, 0x00, 0x00, 0x00, 0x00, 0x0F, 0x84, 0x00,
    0x00, 0x00, 0x00, 0x48, 0x8D, 0x15, 0x00, 0x00, 0x00, 0x00, 0x48, 0x8D,
    0x0D, 0x00, 0x00, 0x00, 0x00, 0xE8, 0x00, 0x00, 0x00, 0x00,
};
static const uint8_t kSigResolveStateMask[46] = {
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00,
};

// CountLevels  rva=0x0095CB60  (46 字节, 通配 4)
static const uint8_t kSigCountLevels[46] = {
    0x4C, 0x89, 0x4C, 0x24, 0x20, 0x55, 0x56, 0x57, 0x48, 0x83, 0xEC, 0x20,
    0x49, 0x8B, 0x00, 0x48, 0x8B, 0xE9, 0x49, 0x8B, 0xC8, 0x49, 0x8B, 0xF0,
    0x48, 0x8B, 0xFA, 0xFF, 0x50, 0x08, 0x84, 0xC0, 0x0F, 0x84, 0x00, 0x00,
    0x00, 0x00, 0x48, 0x8B, 0x07, 0x48, 0x8B, 0xCF, 0xFF, 0x10,
};
static const uint8_t kSigCountLevelsMask[46] = {
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00,
    0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
};

// BuildCommand  rva=0x008C24B0  (46 字节, 通配 0)
static const uint8_t kSigBuildCommand[46] = {
    0x48, 0x89, 0x5C, 0x24, 0x10, 0x4C, 0x89, 0x4C, 0x24, 0x20, 0x4C, 0x89,
    0x44, 0x24, 0x18, 0x48, 0x89, 0x4C, 0x24, 0x08, 0x55, 0x56, 0x57, 0x41,
    0x54, 0x41, 0x55, 0x41, 0x56, 0x41, 0x57, 0x48, 0x8D, 0xAC, 0x24, 0xE0,
    0xFE, 0xFF, 0xFF, 0x48, 0x81, 0xEC, 0x20, 0x02, 0x00, 0x00,
};
static const uint8_t kSigBuildCommandMask[46] = {
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
};

// Wrapper  rva=0x008C2410  (40 字节, 无通配) - prologue + request field loads
static const uint8_t kSigWrapper[40] = {
    0x48, 0x89, 0x5C, 0x24, 0x10, 0x48, 0x89, 0x6C, 0x24, 0x18, 0x56, 0x57,
    0x41, 0x54, 0x41, 0x56, 0x41, 0x57, 0x48, 0x83, 0xEC, 0x40, 0x48, 0x8B,
    0x71, 0x48, 0x4C, 0x8B, 0xE1, 0x8B, 0x59, 0x40, 0x8B, 0x69, 0x3C, 0x44,
    0x8B, 0x71, 0x38, 0x4C,
};
static const uint8_t kSigWrapperMask[40] = {
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
};

// ReqBuild  rva=0x01085B90  (44 字节, 末尾 call rel32 通配 4)
static const uint8_t kSigReqBuild[44] = {
    0x48, 0x89, 0x5C, 0x24, 0x10, 0x48, 0x89, 0x74, 0x24, 0x18, 0x55, 0x57,
    0x41, 0x54, 0x41, 0x56, 0x41, 0x57, 0x48, 0x8D, 0x6C, 0x24, 0xD0, 0x48,
    0x81, 0xEC, 0x30, 0x01, 0x00, 0x00, 0x41, 0x8B, 0xF0, 0x48, 0x8B, 0xD9,
    0x33, 0xFF, 0x8B, 0xCA, 0xE8, 0x00, 0x00, 0x00,
};
static const uint8_t kSigReqBuildMask[44] = {
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 0, 0, 0,
};

// BldLookup  rva=0x00DCBD70  (49 字节, 通配 17)
static const uint8_t kSigBldLookup[49] = {
    0x48, 0x89, 0x5C, 0x24, 0x10, 0x57, 0x48, 0x83, 0xEC, 0x30, 0x83, 0x3D,
    0x27, 0x00, 0x00, 0x00, 0x00, 0x48, 0x8B, 0xDA, 0x48, 0x8B, 0xF9, 0x75,
    0x00, 0x48, 0x8D, 0x05, 0x00, 0x00, 0x00, 0x00, 0x48, 0x8D, 0x0D, 0x00,
    0x00, 0x00, 0x00, 0x48, 0x89, 0x44, 0x24, 0x28, 0xE8, 0x00, 0x00, 0x00,
    0x00,
};
static const uint8_t kSigBldLookupMask[49] = {
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x00, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00,
    0x00,
};

// ---------------------------------------------------------------------------
// Data layout
// ---------------------------------------------------------------------------
namespace lay {
    // CState
    constexpr size_t ST_ID      = 0x08;
    constexpr size_t ST_OWNER   = 0xED8;
    // CBuildingType
    constexpr size_t BT_INDEX   = 0x10;
    constexpr size_t BT_NAME    = 0x18;
    constexpr size_t BT_NAME_SZ = 0x28;
    constexpr size_t BT_GROUPS  = 0x98;
    constexpr size_t BT_GCOUNT  = 0xA4;
    // CBuilding
    constexpr size_t BLD_TYPE   = 0x0B0;
    constexpr size_t BLD_PMS    = 0x1E8;
    constexpr size_t BLD_PMCNT  = 0x1F4;
    // CProductionMethodGroup
    constexpr size_t GRP_PMS    = 0x40;
    constexpr size_t GRP_PMCNT  = 0x4C;
    // CJominiPlayer
    constexpr size_t JP_COUNTRY = 0x58;

    // CCountry country id (reverse-engineering record section 3)
    constexpr size_t CT_ID = 0x18;
    // object registries
    constexpr size_t REG_DATA   = 0x20;
    constexpr size_t REG_COUNT  = 0x2C;
}

// Construction argument block. The engine reads past the fields we set, so it must
// be a full 0x40 bytes of zeroed memory.
struct BuildParam {
    uint32_t countryId;
    uint32_t reserved0;
    uint32_t reserved1;
    uint32_t reserved2;
    uint8_t  kind;          // 2
    uint8_t  pad[0x2F];
};
static_assert(sizeof(BuildParam) == 0x40, "param block must be 0x40 bytes");

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------
static HMODULE  g_self = nullptr;    // this DLL
static HMODULE  g_exe  = nullptr;    // victoria3.exe
static uint8_t* g_base = nullptr;    // exe image base
static size_t   g_imageSize = 0;
static bool     g_buildKnown = false;

static char     g_dir[MAX_PATH] = {0};   // directory of this DLL, trailing '\'

static bool     g_ready = false;
static bool     g_secondInstance = false;
static HANDLE   g_instanceMutex = nullptr;
static PVOID    g_veh = nullptr;

static volatile LONG g_unloading = 0;
static volatile LONG g_pumping = 0;
static volatile LONG g_inHook = 0;
static volatile LONG g_typesReady = 0;
static volatile LONG g_wakeAlive = 0;    // 1 while the wake thread runs
static volatile LONG g_unloadStarted = 0; // teardown must run exactly once
static volatile LONG g_threadsAlive = 0;  // DLL-owned threads currently running

// Start a thread that lives in this DLL and count it, so teardown can wait.

static DWORD    g_mainThreadId = 0;
static HWND     g_mainHwnd = nullptr;

// resolved engine pieces
static uintptr_t g_resolveCountry = 0;
static uintptr_t g_resolveState = 0;
static uintptr_t g_countLevels = 0;
static uintptr_t g_buildCommand = 0;
static uintptr_t g_bldLookup = 0;
static uintptr_t g_buildCtx = 0;      // arg-8 context object
static uintptr_t g_regState = 0;      // address of the global holding the registry
static uintptr_t g_regCountry = 0;

// vtables
static uintptr_t g_vtState[4] = {0};      static int g_nState = 0;
static uintptr_t g_vtCountry[4] = {0};    static int g_nCountry = 0;
static uintptr_t g_vtBldType[4] = {0};    static int g_nBldType = 0;
static uintptr_t g_vtBld[4] = {0};        static int g_nBld = 0;
static uintptr_t g_vtPlayer[4] = {0};     static int g_nPlayer = 0;

// building type table
static std::unordered_map<std::string, void*> g_types;

// g_types is appended by the completion thread while the pump reads it.
static SRWLOCK g_typesLock = SRWLOCK_INIT;

static char pScanState[MAX_PATH], pTypesReady[MAX_PATH];

// Human-readable progress, so a human (or a script) can watch the scan.
static void ScanState(const char* fmt, ...) {
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf_s(buf, sizeof(buf), _TRUNCATE, fmt, ap);
    va_end(ap);

    FILE* f = nullptr;
    fopen_s(&f, pScanState, "w");
    if (f) {
        fprintf(f, "%s\n", buf);
        fclose(f);
    }
}

static void TypesAdd(const char* key, void* obj) {
    AcquireSRWLockExclusive(&g_typesLock);
    if (!g_types.count(key)) g_types[key] = obj;
    ReleaseSRWLockExclusive(&g_typesLock);
}

static size_t TypesCount() {
    AcquireSRWLockShared(&g_typesLock);
    const size_t n = g_types.size();
    ReleaseSRWLockShared(&g_typesLock);
    return n;
}


// player
static void*  g_playerObj = nullptr;
static uintptr_t g_playerVt = 0;
static uint32_t g_playerCountry = 0;
static DWORD  g_lastPlayerWrite = 0;

// ---------------------------------------------------------------------------
// Files (all next to this DLL)
// ---------------------------------------------------------------------------
static char pLog[MAX_PATH], pUnload[MAX_PATH], pPlayerOut[MAX_PATH];
static char pReq[MAX_PATH], pReqDone[MAX_PATH], pQuery[MAX_PATH], pQueryDone[MAX_PATH];
static char pStates[MAX_PATH], pPms[MAX_PATH], pPmsDone[MAX_PATH], pPmsOut[MAX_PATH];
static char pPmm[MAX_PATH], pPmmDone[MAX_PATH], pPmmResult[MAX_PATH];
static char pScan[MAX_PATH], pScanDone[MAX_PATH], pScanOut[MAX_PATH];
static char pQueue[MAX_PATH], pQueueDone[MAX_PATH], pQueueOut[MAX_PATH];
static char pWorldOut[MAX_PATH];
static char pProbeOut[MAX_PATH], pProbeOn[MAX_PATH];
static char pBDump[MAX_PATH], pBDumpDone[MAX_PATH], pBDumpOut[MAX_PATH];
static char pFindPtr[MAX_PATH], pFindPtrDone[MAX_PATH], pFindPtrOut[MAX_PATH];
static char pPeek[MAX_PATH], pPeekDone[MAX_PATH], pPeekOut[MAX_PATH];
static char pPmFix[MAX_PATH], pPmFixDone[MAX_PATH], pPmFixOut[MAX_PATH];
static char pUseCap[MAX_PATH], pUseCapDone[MAX_PATH];
static char pTplDir[MAX_PATH];
static char pParamTpl[MAX_PATH];
static char pCapRequest[MAX_PATH];
// Last request object seen by the probe (wrapper entry). Replaying it is the only
// way to obtain the runtime ids the engine wants in the param block.
static uint8_t g_capReq[0x70];
static const uintptr_t kCmdHead = 0x18;   // request = commandObject + 0x18
static volatile LONG g_capReqValid = 0;

// One captured request per building type. The runtime ids inside a request are
// bound to the building type (same type works for any state, a different type
// does not), so a single UI build of a type is enough to enable that type for
// every state afterwards.
struct TypeTemplate { void* btype; uint8_t bytes[0x70]; bool valid; };
static TypeTemplate g_tpl[64];
static volatile LONG g_nTpl = 0;
static volatile LONG g_tplDirty = 0;

// Building types whose build attempt made the engine take its broken assertion
// path (typically a state that cannot host that building, e.g. a mine without the
// resource). Retrying only faults again, so they are refused for the session.
static void* g_badBuild[64];
static volatile LONG g_nBadBuild = 0;

static bool BadBuild(void* btype) {
    const LONG n = g_nBadBuild;
    for (LONG i = 0; i < n; ++i)
        if (g_badBuild[i] == btype) return true;
    return false;
}

static void MarkBadBuild(void* btype) {
    if (!btype || BadBuild(btype)) return;
    const LONG n = InterlockedIncrement(&g_nBadBuild) - 1;
    if (n < 64) g_badBuild[n] = btype;
}
static SRWLOCK g_tplLock = SRWLOCK_INIT;

static TypeTemplate* TemplateFind(void* btype) {
    if (!btype) return nullptr;
    AcquireSRWLockShared(&g_tplLock);
    TypeTemplate* found = nullptr;
    const LONG n = g_nTpl;
    for (LONG i = 0; i < n; ++i)
        if (g_tpl[i].valid && g_tpl[i].btype == btype) { found = &g_tpl[i]; break; }
    ReleaseSRWLockShared(&g_tplLock);
    return found;
}

static void TemplateStore(void* btype, const uint8_t* bytes) {
    if (!btype) return;
    if (TemplateFind(btype)) return;              // already have one
    AcquireSRWLockExclusive(&g_tplLock);
    const LONG n = g_nTpl;
    if (n < 64) {
        g_tpl[n].btype = btype;
        memcpy(g_tpl[n].bytes, bytes, 0x70);
        g_tpl[n].valid = true;
        InterlockedExchange(&g_nTpl, n + 1);
    }
    ReleaseSRWLockExclusive(&g_tplLock);
}

// The UI reaches BuildCommand through a request object:
//   req-build -> [request->vtable + 0] (wrapper 0x8C2410) -> BuildCommand
// Going straight to BuildCommand skips that layer, and the engine then never runs
// the script effect that fully initialises a newly created building. The vtable
// slot 0 is the wrapper; it lives at RVA 0x46B5048.
static const uintptr_t kRequestVtableRva = 0x46B5048;
// Resolved at runtime: the recorded RVA is only valid on the exact build, so the
// vtable is located as "the .rdata table whose slot 0 is the wrapper".
static uintptr_t g_reqVtable = 0;
static const uintptr_t kWrapperRva       = 0x8C2410;
static const uintptr_t kReqBuildRva      = 0x1085B90;
// Opcode the UI's build action was observed passing to req-build (rdx).
static const uint32_t  kBuildOpcode      = 0x30CA;
static uintptr_t g_wrapper = 0;
static uintptr_t g_reqBuild = 0;

// The UI passes a param block carrying runtime ids; ours is mostly zeroed and the
// engine then skips its script effect chain. If a template file exists its bytes
// are used instead (only the country id is overwritten).
static bool LoadParamTemplate(uint8_t* buf, size_t size) {
    FILE* f = nullptr;
    if (fopen_s(&f, pParamTpl, "r") != 0 || !f) return false;
    char line[512];
    bool any = false;
    unsigned long long q[8] = {0};
    int got = 0;
    while (fgets(line, sizeof(line), f) && got < 8) {
        char* p = line;
        while (*p == ' ' || *p == '\t') ++p;
        if (*p == '#' || *p == '\n' || *p == '\r' || !*p) continue;
        q[got] = _strtoui64(p, nullptr, 16);
        ++got;
    }
    fclose(f);
    if (got == 0) return false;
    memset(buf, 0, size);
    for (int i = 0; i < got && (i + 1) * 8 <= static_cast<int>(size); ++i) {
        memcpy(buf + i * 8, &q[i], 8);
        any = true;
    }
    return any;
}

static char pWatch[MAX_PATH], pWatchDone[MAX_PATH];
static char pForceUnknown[MAX_PATH];
static volatile LONG g_watchArmed = 0;
static volatile LONG g_watchHits = 0;
static uintptr_t g_watchA = 0, g_watchB = 0;
static DWORD g_watchUntil = 0;      // auto-disarm deadline

static void JoinPath(char* out, size_t cap, const char* name) {
    _snprintf_s(out, cap, _TRUNCATE, "%s%s", g_dir, name);
}

// Every injection starts a clean log; the previous one is kept as .prev.log.
// Without this the log is append-only across restarts, and stale lines (a
// REFUSED or unloading from an hour ago) look like the current state.
// Every injection starts from an empty log: an append-only log across restarts
// makes hour-old lines look like the current state.
static void ResetLog() {
    if (!g_dir[0]) return;
    char prev[MAX_PATH];
    JoinPath(prev, MAX_PATH, "v3inject.prev.log");
    DeleteFileA(prev);
    DeleteFileA(pLog);
}

static void InitPaths() {
    if (g_dir[0]) return;
    char full[MAX_PATH] = {0};
    if (!GetModuleFileNameA(g_self, full, MAX_PATH)) return;
    char* slash = strrchr(full, '\\');
    if (!slash) return;
    slash[1] = 0;
    strncpy_s(g_dir, full, _TRUNCATE);

    JoinPath(pLog,       MAX_PATH, "v3inject.log");
    JoinPath(pUnload,    MAX_PATH, "unload.flag");
    JoinPath(pPlayerOut, MAX_PATH, "player.current.txt");
    JoinPath(pReq,       MAX_PATH, "requests.txt");
    JoinPath(pReqDone,   MAX_PATH, "requests.done.txt");
    JoinPath(pQuery,     MAX_PATH, "query.txt");
    JoinPath(pQueryDone, MAX_PATH, "query.done.txt");
    JoinPath(pStates,    MAX_PATH, "states.csv");
    JoinPath(pPms,       MAX_PATH, "pms.txt");
    JoinPath(pPmsDone,   MAX_PATH, "pms.done.txt");
    JoinPath(pPmsOut,    MAX_PATH, "pms.csv");
    JoinPath(pPmm,       MAX_PATH, "pmm.txt");
    JoinPath(pPmmDone,   MAX_PATH, "pmm.done.txt");
    JoinPath(pPmmResult, MAX_PATH, "pmm.result.csv");
    JoinPath(pScan,      MAX_PATH, "pmscan.txt");
    JoinPath(pScanDone,  MAX_PATH, "pmscan.done.txt");
    JoinPath(pScanOut,   MAX_PATH, "pmscan.csv");
    JoinPath(pQueue,     MAX_PATH, "queue.txt");
    JoinPath(pQueueDone, MAX_PATH, "queue.done.txt");
    JoinPath(pQueueOut,  MAX_PATH, "queue.csv");
    JoinPath(pWorldOut,  MAX_PATH, "world.current.txt");
    JoinPath(pProbeOut,  MAX_PATH, "buildprobe.log");
    JoinPath(pProbeOn,   MAX_PATH, "probe.on");
    JoinPath(pScanState, MAX_PATH, "scan.current.txt");
    JoinPath(pTypesReady,MAX_PATH, "types.ready");
    JoinPath(pBDump,     MAX_PATH, "bdump.txt");
    JoinPath(pBDumpDone, MAX_PATH, "bdump.done.txt");
    JoinPath(pBDumpOut,  MAX_PATH, "bdump.csv");
    JoinPath(pFindPtr,   MAX_PATH, "findptr.txt");
    JoinPath(pFindPtrDone, MAX_PATH, "findptr.done.txt");
    JoinPath(pFindPtrOut, MAX_PATH, "findptr.csv");
    JoinPath(pPeek,      MAX_PATH, "peek.txt");
    JoinPath(pPeekDone,  MAX_PATH, "peek.done.txt");
    JoinPath(pPeekOut,   MAX_PATH, "peek.csv");
    JoinPath(pPmFix,     MAX_PATH, "pmfix.txt");
    JoinPath(pPmFixDone, MAX_PATH, "pmfix.done.txt");
    JoinPath(pPmFixOut,  MAX_PATH, "pmfix.csv");
    CreateDirectoryA(pTplDir, nullptr);
    JoinPath(pParamTpl,  MAX_PATH, "parambytes.txt");
    JoinPath(pCapRequest, MAX_PATH, "requestcap.txt");
    JoinPath(pUseCap,    MAX_PATH, "usecap.txt");
    JoinPath(pUseCapDone,MAX_PATH, "usecap.done.txt");
    JoinPath(pTplDir,    MAX_PATH, "tpl");
    JoinPath(pWatch,     MAX_PATH, "watch.txt");
    JoinPath(pWatchDone, MAX_PATH, "watch.done.txt");
    JoinPath(pForceUnknown, MAX_PATH, "force_unknown");
}

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------
static void Log(const char* fmt, ...) {
    if (!g_dir[0]) return;
    FILE* f = nullptr;
    fopen_s(&f, pLog, "a");
    if (!f) return;

    SYSTEMTIME t;
    GetLocalTime(&t);
    fprintf(f, "[%02d:%02d:%02d] ", t.wHour, t.wMinute, t.wSecond);

    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static HANDLE StartOwnedThread(LPTHREAD_START_ROUTINE fn, const char* what) {
    InterlockedIncrement(&g_threadsAlive);
    HANDLE h = CreateThread(nullptr, 0, fn, nullptr, 0, nullptr);
    if (!h) {
        InterlockedDecrement(&g_threadsAlive);
        Log("thread %s: CreateThread failed", what);
    }
    return h;
}

// ---------------------------------------------------------------------------
// Memory validation
// ---------------------------------------------------------------------------
static bool IsReadable(const void* p, size_t n = 1) {
    if (!p) return false;
    MEMORY_BASIC_INFORMATION mbi{};
    if (VirtualQuery(p, &mbi, sizeof(mbi)) != sizeof(mbi)) return false;
    if (mbi.State != MEM_COMMIT) return false;
    if (mbi.Protect & PAGE_GUARD) return false;
    if (!(mbi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                         PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE |
                         PAGE_EXECUTE_WRITECOPY)))
        return false;
    const uintptr_t from = reinterpret_cast<uintptr_t>(p);
    const uintptr_t end  = reinterpret_cast<uintptr_t>(mbi.BaseAddress) + mbi.RegionSize;
    return from + n <= end;
}

static bool HasVt(const void* p, const uintptr_t* vts, int n) {
    if (!IsReadable(p, 8)) return false;
    const uintptr_t v = *reinterpret_cast<const uintptr_t*>(p);
    for (int i = 0; i < n; ++i) if (v == vts[i]) return true;
    return false;
}

// ---------------------------------------------------------------------------
// PE sections + signature scanning
// ---------------------------------------------------------------------------
struct Section { uintptr_t va; size_t size; };

static Section TextSection() {
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(g_base);
    auto* nt  = reinterpret_cast<IMAGE_NT_HEADERS*>(g_base + dos->e_lfanew);
    auto* sec = IMAGE_FIRST_SECTION(nt);
    for (int i = 0; i < nt->FileHeader.NumberOfSections; ++i) {
        if ((sec[i].Characteristics & IMAGE_SCN_MEM_EXECUTE) &&
            sec[i].SizeOfRawData > 0)
            return { reinterpret_cast<uintptr_t>(g_base + sec[i].VirtualAddress), static_cast<size_t>(sec[i].SizeOfRawData) };
    }
    return { 0, 0 };
}

static std::vector<Section> DataSections() {
    std::vector<Section> out;
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(g_base);
    auto* nt  = reinterpret_cast<IMAGE_NT_HEADERS*>(g_base + dos->e_lfanew);
    auto* sec = IMAGE_FIRST_SECTION(nt);
    for (int i = 0; i < nt->FileHeader.NumberOfSections; ++i) {
        if ((sec[i].Characteristics & IMAGE_SCN_MEM_READ) &&
            !(sec[i].Characteristics & IMAGE_SCN_MEM_EXECUTE) &&
            sec[i].SizeOfRawData > 0)
            out.push_back({ reinterpret_cast<uintptr_t>(g_base + sec[i].VirtualAddress),
                            static_cast<size_t>(sec[i].SizeOfRawData) });
    }
    return out;
}

static uintptr_t FindAscii(const char* needle, size_t len) {
    for (auto& s : DataSections()) {
        const auto* p = reinterpret_cast<const uint8_t*>(s.va);
        const auto* end = p + s.size;
        while (p + len <= end) {
            auto* hit = static_cast<const uint8_t*>(memchr(p, needle[0], end - p));
            if (!hit || hit + len > end) break;
            if (memcmp(hit, needle, len) == 0) return reinterpret_cast<uintptr_t>(hit);
            p = hit + 1;
        }
    }
    return 0;
}

// Locate a function: verify the recorded RVA first (fast, safe), then fall back to
// a unique signature hit. An unverified RVA is only trusted on the known build.
static bool SigMatch(const uint8_t* p, const uint8_t* sig,
                     const uint8_t* mask, size_t len) {
    for (size_t i = 0; i < len; ++i)
        if (mask[i] && p[i] != sig[i]) return false;
    return true;
}

static uintptr_t FindFunc(const uint8_t* sig, const uint8_t* mask, size_t len,
                          uintptr_t fallbackRva, const char* name) {
    // Fast path: the fingerprint already proved this exact build, so the recorded
    // RVA can be trusted as long as the bytes there still match.
    if (fallbackRva && g_buildKnown) {
        auto* p = g_base + fallbackRva;
        if (IsReadable(p, len) && SigMatch(p, sig, mask, len)) {
            Log("sig: %-14s recorded rva 0x%llX verified", name,
                static_cast<unsigned long long>(fallbackRva));
            return reinterpret_cast<uintptr_t>(p);
        }
        Log("sig: %-14s recorded rva 0x%llX does NOT match any more", name,
            static_cast<unsigned long long>(fallbackRva));
    }

    Section text = TextSection();
    if (!text.va) { Log("sig: no .text section"); return 0; }

    // anchor on the first non-wildcard byte so memchr stays usable
    size_t anchor = 0;
    while (anchor < len && !mask[anchor]) ++anchor;
    if (anchor >= len) { Log("sig: %s is all wildcards", name); return 0; }

    const auto* base0 = reinterpret_cast<const uint8_t*>(text.va);
    const auto* p   = base0 + anchor;
    const auto* end = base0 + text.size - len;
    uintptr_t found = 0;
    int matches = 0;

    while (p < end) {
        auto* hit = static_cast<const uint8_t*>(memchr(p, sig[anchor], end - p));
        if (!hit) break;
        const uint8_t* base = hit - anchor;
        if (SigMatch(base, sig, mask, len)) {
            if (!found) found = reinterpret_cast<uintptr_t>(base);
            if (++matches > 1) break;
        }
        p = hit + 1;
    }

    if (matches == 1) return found;
    if (matches == 0 && fallbackRva && g_buildKnown) {
        Log("sig: %-14s no signature match - falling back to recorded rva 0x%llX"
            " (only safe because the fingerprint matched)", name,
            static_cast<unsigned long long>(fallbackRva));
        return reinterpret_cast<uintptr_t>(g_base + fallbackRva);
    }
    Log("sig: %s unresolved (matches=%d)", name, matches);
    return 0;
}

// The resolvers open with  48 83 EC 58 | 48 8B 05 <disp32>  where disp32 points at
// the registry global. Recover it instead of hardcoding an address.
static uintptr_t GlobalFromResolver(uintptr_t fn) {
    if (!fn) return 0;
    auto* p = reinterpret_cast<uint8_t*>(fn);
    if (p[0] != 0x48 || p[1] != 0x83 || p[2] != 0xEC || p[3] != 0x58) return 0;
    if (p[4] != 0x48 || p[5] != 0x8B || p[6] != 0x05) return 0;
    const int32_t disp = *reinterpret_cast<int32_t*>(p + 7);
    return fn + 11 + disp;
}

// Add a recorded vtable RVA if it looks like a real vtable (readable, first entry
// inside .text). Deduplicated.
static void AddRecordedVt(uintptr_t* arr, int* n, int max, uintptr_t vtRva,
                          const Section& text) {
    const uintptr_t vt = reinterpret_cast<uintptr_t>(g_base + vtRva);
    if (!IsReadable(reinterpret_cast<void*>(vt), 16)) return;
    const uint64_t first = *reinterpret_cast<uint64_t*>(vt);
    if (first < text.va || first >= text.va + text.size) return;
    for (int i = 0; i < *n; ++i) if (arr[i] == vt) return;
    if (*n < max) arr[(*n)++] = vt;
}

// ---------------------------------------------------------------------------
// RTTI -> vtable candidates
// ---------------------------------------------------------------------------
static int CollectVt(const char* mangled, uintptr_t* out, int max) {
    int count = 0;
    const uintptr_t nameAddr = FindAscii(mangled, strlen(mangled));
    if (!nameAddr) { Log("rtti: name missing %s", mangled); return 0; }

    const uint32_t tdRva = static_cast<uint32_t>(nameAddr - 16 - reinterpret_cast<uintptr_t>(g_base));
    Section text = TextSection();
    auto secs = DataSections();

    for (auto& s : secs) {
        const auto* p = reinterpret_cast<const uint8_t*>(s.va);
        for (size_t off = 0; off + 20 <= s.size; off += 4) {
            if (*reinterpret_cast<const uint32_t*>(p + off) != 1) continue;          // COL
            if (*reinterpret_cast<const uint32_t*>(p + off + 12) != tdRva) continue; // TD
            const uint64_t colVa = s.va + off;
            for (auto& s2 : secs) {
                const auto* q = reinterpret_cast<const uint8_t*>(s2.va);
                for (size_t o2 = 0; o2 + 8 <= s2.size; o2 += 8) {
                    if (*reinterpret_cast<const uint64_t*>(q + o2) != colVa) continue;
                    const uintptr_t vt = s2.va + o2 + 8;
                    const uint64_t first = *reinterpret_cast<const uint64_t*>(vt);
                    if (first < text.va || first >= text.va + text.size) continue;
                    bool dup = false;
                    for (int k = 0; k < count; ++k) if (out[k] == vt) { dup = true; break; }
                    if (!dup && count < max) out[count++] = vt;
                }
            }
        }
    }
    Log("rtti: %-28s -> %d candidate(s)", mangled, count);
    return count;
}

// ---------------------------------------------------------------------------
// Fault guard
// ---------------------------------------------------------------------------
// Per-thread: the VEH fires on the faulting thread, so both the flag and the
// jmp_buf must belong to that thread.
static thread_local void* t_jmp[5];
static thread_local volatile LONG t_inCall = 0;

static volatile LONG g_faultCount = 0;


// ---------------------------------------------------------------------------
// Build probe: hardware breakpoint on BuildCommand
//
// Nothing in .text is modified. DR0 is armed on the main thread and the existing
// VEH reports the register state at function entry, which is exactly what the
// game itself passes when the build button is clicked.
// ---------------------------------------------------------------------------
static volatile LONG g_probeArmed = 0;
static volatile LONG g_probeHits  = 0;

// Hardware breakpoints need no static caller, which matters here: none of these
// three functions has a direct xref - the chain above them is virtual dispatch.
struct ProbeTarget { const char* name; uintptr_t rva; uintptr_t va; };
static ProbeTarget g_probeTargets[4] = {
    { "wrapper",          0x008C2410, 0 },  // capture point for the request object
    { "BuildCommand",     0x008C24B0, 0 },
    { "order-build",      0x00968397, 0 },  // proves the script effect ran
    { "queue-insert-gov", 0x0095C770, 0 },
};
static const int kProbeTargets = 4;
static DWORD  g_probeLastCheck = 0;

static void ProbeLog(const char* fmt, ...) {
    FILE* f = nullptr;
    fopen_s(&f, pProbeOut, "a");
    if (!f) return;
    SYSTEMTIME st{};
    GetLocalTime(&st);
    fprintf(f, "[%02d:%02d:%02d.%03d] ", st.wHour, st.wMinute, st.wSecond,
            st.wMilliseconds);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static const char* ProbeWhich(uintptr_t rip) {
    for (int i = 0; i < kProbeTargets; ++i)
        if (g_probeTargets[i].va == rip) return g_probeTargets[i].name;
    return "?";
}

static void ProbeCapture(const CONTEXT* c) {
    const LONG n = InterlockedIncrement(&g_probeHits);

    // Keep a copy of the request object whenever the wrapper is entered: it holds
    // the runtime ids the engine needs, which we cannot synthesise.
    if (c->Rip == reinterpret_cast<uintptr_t>(g_base) + 0x8C2410 &&
        IsReadable(reinterpret_cast<const void*>(c->Rcx - kCmdHead), 0x70)) {
        memcpy(g_capReq, reinterpret_cast<const void*>(c->Rcx - kCmdHead), 0x70);
        InterlockedExchange(&g_capReqValid, 1);
        // Remember it per building type so later builds of that type can replay
        // it for any state.
        {
            void* bt = *reinterpret_cast<void**>(g_capReq + kCmdHead + 0x18);
            if (bt) {
                const bool had = TemplateFind(bt) != nullptr;
                TemplateStore(bt, g_capReq);
                if (!had) {
                    // No file IO here: this runs inside the exception handler, and
                    // allocating or touching the filesystem there can deadlock the
                    // game. The pump writes templates out afterwards.
                    InterlockedExchange(&g_tplDirty, 1);
                    Log("template: captured request for type %p", bt);
                }
            }
        }
        FILE* cf = nullptr;
        fopen_s(&cf, pCapRequest, "w");
        if (cf) {
            for (int i = 0; i < 0x50; i += 8)
                fprintf(cf, "%016llX\n",
                        static_cast<unsigned long long>(
                            *reinterpret_cast<const uint64_t*>(g_capReq + i)));
            fclose(cf);
        }
    }
    if (n > 500) return;
    const char* which = ProbeWhich(c->Rip);

    // A hot function can be entered hundreds of times with the same arguments;
    // log the first occurrence and then only every 50th repeat.
    static LONG lastKey = 0;
    static LONG repeats = 0;
    const LONG key = static_cast<LONG>(
        (c->Rcx ^ (c->Rdx << 1) ^ (c->R8 << 2) ^ (c->Rip >> 4)) & 0x7FFFFFFF);
    if (key == lastKey) {
        ++repeats;
        if (repeats % 50 != 0) return;
    } else {
        lastKey = key;
        repeats = 0;
    }

    const auto* sp = reinterpret_cast<const uint64_t*>(c->Rsp);
    uint64_t s5 = 0, s6 = 0, s7 = 0, s8 = 0, ret = 0;
    if (IsReadable(sp, 0x48)) {
        ret = sp[0]; s5 = sp[5]; s6 = sp[6]; s7 = sp[7]; s8 = sp[8];
    }

    ProbeLog("--- hit #%ld  %s (0x%llX)  tid=%lu  ret=0x%llX",
             static_cast<long>(n), which,
             static_cast<unsigned long long>(c->Rip),
             static_cast<unsigned long>(GetCurrentThreadId()),
             static_cast<unsigned long long>(ret));
    ProbeLog("  rcx(param)=0x%llX rdx=0x%llX r8=0x%llX r9=0x%llX",
             static_cast<unsigned long long>(c->Rcx),
             static_cast<unsigned long long>(c->Rdx),
             static_cast<unsigned long long>(c->R8),
             static_cast<unsigned long long>(c->R9));
    ProbeLog("  stack [0x28]=0x%llX [0x30]=0x%llX [0x38]=0x%llX [0x40]=0x%llX",
             static_cast<unsigned long long>(s5),
             static_cast<unsigned long long>(s6),
             static_cast<unsigned long long>(s7),
             static_cast<unsigned long long>(s8));

    const auto* p = reinterpret_cast<const uint8_t*>(c->Rcx);
    if (IsReadable(p, 0x40)) {
        for (int off = 0; off < 0x40; off += 0x10) {
            ProbeLog("  param+0x%02X: %016llX %016llX", off,
                     static_cast<unsigned long long>(
                         *reinterpret_cast<const uint64_t*>(p + off)),
                     static_cast<unsigned long long>(
                         *reinterpret_cast<const uint64_t*>(p + off + 8)));
        }
    } else {
        ProbeLog("  param unreadable");
    }

    // The order path hands its descriptor in rdx, not rcx.
    // Walk the stack for return addresses. The UI's build action runs through
    // several frames we have not identified, so the chain has to be read off the
    // stack rather than guessed.
    {
        const auto textBase = reinterpret_cast<uintptr_t>(g_base);
        char line[512];
        int used = 0;
        used += snprintf(line + used, sizeof(line) - used, "  stack chain:");
        const auto* sp0 = reinterpret_cast<const uint64_t*>(c->Rsp);
        int found = 0;
        for (int i = 0; i < 96 && found < 10; ++i) {
            if (!IsReadable(sp0 + i, 8)) break;
            const uint64_t v = sp0[i];
            if (v < textBase + 0x1000 || v > textBase + 0x5C00000) continue;
            used += snprintf(line + used, sizeof(line) - used, " 0x%llX",
                             static_cast<unsigned long long>(v - textBase));
            ++found;
            if (used > 400) break;
        }
        ProbeLog("%s", line);
    }

    const auto* p2 = reinterpret_cast<const uint8_t*>(c->Rdx);
    if (IsReadable(p2, 0x40)) {
        for (int off = 0; off < 0x40; off += 0x10) {
            ProbeLog("  rdx+0x%02X: %016llX %016llX", off,
                     static_cast<unsigned long long>(
                         *reinterpret_cast<const uint64_t*>(p2 + off)),
                     static_cast<unsigned long long>(
                         *reinterpret_cast<const uint64_t*>(p2 + off + 8)));
        }
    } else {
        ProbeLog("  rdx unreadable");
    }
}

// Arm one thread. Suspending is required: SetThreadContext on a running thread is
// silently ignored often enough that the previous probe never fired.
static bool ProbeArmThread(DWORD tid, bool arm, const char* why) {
    const bool self = (tid == GetCurrentThreadId());

    if (self) {
        // The pump thread is usually the one handling the UI, so it must be armed
        // too - and suspending ourselves would deadlock.
        CONTEXT c{};
        c.ContextFlags = CONTEXT_DEBUG_REGISTERS;
        if (arm) {
            c.Dr0 = static_cast<DWORD64>(g_probeTargets[0].va);
            c.Dr1 = static_cast<DWORD64>(g_probeTargets[1].va);
            c.Dr2 = static_cast<DWORD64>(g_probeTargets[2].va);
            c.Dr3 = static_cast<DWORD64>(g_probeTargets[3].va);
            c.Dr7 = 0x55ULL;             // L0..L3, execute
        }
        c.Dr6 = 0;
        if (!SetThreadContext(GetCurrentThread(), &c)) {
            Log("probe: self SetThreadContext failed (%lu)", GetLastError());
            return false;
        }
        return true;
    }

    HANDLE th = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                           THREAD_SET_CONTEXT, FALSE, tid);
    if (!th) return false;

    SuspendThread(th);
    CONTEXT c{};
    c.ContextFlags = CONTEXT_DEBUG_REGISTERS;
    bool ok = GetThreadContext(th, &c) != 0;
    if (ok) {
        if (arm) {
            c.Dr0 = static_cast<DWORD64>(g_probeTargets[0].va);
            c.Dr1 = static_cast<DWORD64>(g_probeTargets[1].va);
            c.Dr2 = static_cast<DWORD64>(g_probeTargets[2].va);
            c.Dr3 = static_cast<DWORD64>(g_probeTargets[3].va);
            c.Dr7 = (c.Dr7 & ~0xF0003ULL) | 0x55ULL;
        } else {
            c.Dr0 = c.Dr1 = c.Dr2 = c.Dr3 = 0;
            c.Dr7 &= ~0xF0003ULL;
        }
        c.Dr6 = 0;
        ok = SetThreadContext(th, &c) != 0;

        // read back: if the write did not stick, the breakpoint never fires
        CONTEXT v{};
        v.ContextFlags = CONTEXT_DEBUG_REGISTERS;
        if (ok && GetThreadContext(th, &v)) {
            if (arm && (v.Dr0 != c.Dr0 || (v.Dr7 & 0x55ULL) != 0x55ULL)) {
                Log("probe: thread %lu arm did NOT stick (dr0=0x%llX dr7=0x%llX)",
                    tid, static_cast<unsigned long long>(v.Dr0),
                    static_cast<unsigned long long>(v.Dr7));
                ok = false;
            }
        }
    }
    ResumeThread(th);
    CloseHandle(th);
    (void)why;
    return ok;
}

// Capture mode: only the wrapper breakpoint, kept armed all the time so that
// whatever the player builds through the UI teaches the DLL that building type.
static int CaptureArmThread(DWORD tid, bool arm) {
    const bool self = (tid == GetCurrentThreadId());
    CONTEXT c{};
    c.ContextFlags = CONTEXT_DEBUG_REGISTERS;
    HANDLE th = nullptr;

    if (!self) {
        th = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                        THREAD_SET_CONTEXT, FALSE, tid);
        if (!th) return 0;
        SuspendThread(th);
        if (!GetThreadContext(th, &c)) {
            ResumeThread(th);
            CloseHandle(th);
            return 0;
        }
    }

    const uintptr_t wrap = reinterpret_cast<uintptr_t>(g_base) + 0x8C2410;
    if (arm) {
        c.Dr0 = static_cast<DWORD64>(wrap);
        c.Dr7 = (c.Dr7 & ~0xF0003ULL) | 0x1ULL;
    } else {
        c.Dr0 = 0;
        c.Dr7 &= ~0xF0003ULL;
    }
    c.Dr6 = 0;

    bool ok;
    if (self) {
        ok = SetThreadContext(GetCurrentThread(), &c) != 0;
    } else {
        ok = SetThreadContext(th, &c) != 0;
        ResumeThread(th);
        CloseHandle(th);
    }
    return ok ? 1 : 0;
}

static int CaptureArmAll(bool arm) {
    int done = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    const DWORD me = GetCurrentProcessId();
    THREADENTRY32 te{};
    te.dwSize = sizeof(te);
    if (Thread32First(snap, &te)) {
        do {
            if (te.th32OwnerProcessID != me) continue;
            done += CaptureArmThread(te.th32ThreadID, arm);
            if (done >= 64) break;
        } while (Thread32Next(snap, &te));
    }
    CloseHandle(snap);
    return done;
}

static int ProbeArmAll(bool arm) {
    int done = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;

    const DWORD me = GetCurrentProcessId();
    THREADENTRY32 te{};
    te.dwSize = sizeof(te);
    if (Thread32First(snap, &te)) {
        do {
            if (te.th32OwnerProcessID != me) continue;
            if (ProbeArmThread(te.th32ThreadID, arm, "")) ++done;
            if (done >= 64) break;
        } while (Thread32Next(snap, &te));
    }
    CloseHandle(snap);
    return done;
}


static void ProbeTick() {
    const DWORD now = GetTickCount();
    if (g_probeLastCheck && now - g_probeLastCheck < 1000) return;
    g_probeLastCheck = now;

    const bool want  = GetFileAttributesA(pProbeOn) != INVALID_FILE_ATTRIBUTES;
    const bool armed = (g_probeArmed != 0);
    if (want == armed) return;

    if (want) {
        // Every target comes from its recorded RVA. Target[0] used to be
        // forced to g_buildCommand, which silently pointed the queue-append
        // breakpoint at BuildCommand instead.
        if (!g_probeTargets[0].va) {
            for (int i = 0; i < kProbeTargets; ++i)
                g_probeTargets[i].va = reinterpret_cast<uintptr_t>(g_base) +
                                       g_probeTargets[i].rva;
            Log("probe: targets -> append=0x%llX build=0x%llX wrap=0x%llX "
                "req=0x%llX",
                static_cast<unsigned long long>(g_probeTargets[0].va),
                static_cast<unsigned long long>(g_probeTargets[1].va),
                static_cast<unsigned long long>(g_probeTargets[2].va),
                static_cast<unsigned long long>(g_probeTargets[3].va));
        }
        InterlockedExchange(&g_probeHits, 0);   // counter is cumulative otherwise
        const int n = ProbeArmAll(true);
        Log("probe: armed on %d thread(s)", n);
        if (n > 0) {
            InterlockedExchange(&g_probeArmed, 1);
            for (int i = 0; i < kProbeTargets; ++i)
                ProbeLog("=== armed: %-12s 0x%llX ===", g_probeTargets[i].name,
                         static_cast<unsigned long long>(g_probeTargets[i].va));
        }
    } else {
        const int n = ProbeArmAll(false);
        InterlockedExchange(&g_probeArmed, 0);
        (void)n;
        Log("probe: disarmed on %d thread(s) (hits=%ld)", n,
            static_cast<long>(g_probeHits));
    }
}

static void WatchLog(const char* fmt, ...);

static LONG CALLBACK FaultGuard(EXCEPTION_POINTERS* ep) {
    if (ep->ExceptionRecord->ExceptionCode == EXCEPTION_SINGLE_STEP) {
        if (g_watchArmed && (ep->ContextRecord->Dr6 & 0xF)) {
            auto* ctx = ep->ContextRecord;
            const LONG n = InterlockedIncrement(&g_watchHits);
            ctx->EFlags |= 0x10000;
            if (n <= 40) {
                WatchLog("write #%ld dr6=0x%llX rip=0x%llX rcx=0x%llX rdx=0x%llX",
                         static_cast<long>(n),
                         static_cast<unsigned long long>(ctx->Dr6),
                         static_cast<unsigned long long>(ctx->Rip),
                         static_cast<unsigned long long>(ctx->Rcx),
                         static_cast<unsigned long long>(ctx->Rdx));
            }
            ctx->Dr6 = 0;
            return EXCEPTION_CONTINUE_EXECUTION;
        }
        if (g_probeArmed && ProbeWhich(
                reinterpret_cast<uintptr_t>(ep->ExceptionRecord->ExceptionAddress))
                != nullptr &&
            ProbeWhich(reinterpret_cast<uintptr_t>(
                ep->ExceptionRecord->ExceptionAddress))[0] != '?') {
            auto* ctx = ep->ContextRecord;
            ctx->Dr6 = 0;
            // Resume Flag: without it the instruction re-triggers immediately and
            // the target function never runs a single instruction.
            ctx->EFlags |= 0x10000;
            ProbeCapture(ctx);
            return EXCEPTION_CONTINUE_EXECUTION;
        }
        return EXCEPTION_CONTINUE_SEARCH;
    }

    if (!t_inCall) return EXCEPTION_CONTINUE_SEARCH;
    if (ep->ExceptionRecord->ExceptionCode != EXCEPTION_ACCESS_VIOLATION)
        return EXCEPTION_CONTINUE_SEARCH;
    const CONTEXT* c = ep->ContextRecord;
    // Faults are expected while walking a live heap; only log the first few of
    // each scan, otherwise a single scan can emit hundreds of identical lines.
    if (++g_faultCount <= 3) {
        Log("FAULT av at 0x%llX rax=%p rbx=%p rdi=%p rsi=%p",
            static_cast<unsigned long long>(c->Rip),
            reinterpret_cast<void*>(c->Rax), reinterpret_cast<void*>(c->Rbx),
            reinterpret_cast<void*>(c->Rdi), reinterpret_cast<void*>(c->Rsi));
    } else if (g_faultCount == 4) {
        Log("FAULT av ... (further faults in this scan are counted, not logged)");
    }
    t_inCall = 0;
    __builtin_longjmp(t_jmp, 1);
    return EXCEPTION_CONTINUE_SEARCH;
}

// Run a piece of code under the guard. Returns false when it faulted.
#define GUARDED(expr) \
    (t_inCall = 1, \
     (__builtin_setjmp(t_jmp) == 0) ? ((expr), t_inCall = 0, true) \
                                    : (t_inCall = 0, false))

static void InstallFaultGuard() {
    g_veh = AddVectoredExceptionHandler(1, FaultGuard);
    Log("fault guard installed (%p)", g_veh);
}

// ---------------------------------------------------------------------------
// Registry access
// ---------------------------------------------------------------------------
static void* RegAt(uintptr_t regGlobal, uint32_t id) {
    if (!regGlobal) return nullptr;
    auto* reg = *reinterpret_cast<void**>(regGlobal);
    if (!IsReadable(reg, 0x30)) return nullptr;
    auto* b = static_cast<uint8_t*>(reg);
    auto** entries = *reinterpret_cast<void***>(b + lay::REG_DATA);
    const uint32_t count = *reinterpret_cast<uint32_t*>(b + lay::REG_COUNT);
    if (!entries || id >= count) return nullptr;
    void** slot = reinterpret_cast<void**>(
        reinterpret_cast<uint8_t*>(entries) + 16 * id + 8);
    if (!IsReadable(slot, 8)) return nullptr;
    return *slot;
}

static void* StateById(uint32_t id)   { return RegAt(g_regState, id); }
static void* CountryById(uint32_t id) { return RegAt(g_regCountry, id); }

// The level counter at rva 0x954B40 returns 0 for every (country, btype, state) on
// this build, so nothing depends on it. Existence is decided by the building
// lookup returning a real CBuilding object.


// ---------------------------------------------------------------------------
// Building types
// ---------------------------------------------------------------------------
static const char* TypeKey(void* obj) {
    if (!obj) return nullptr;
    auto* b = static_cast<uint8_t*>(obj);
    if (!IsReadable(b + lay::BT_NAME_SZ, 8)) return nullptr;
    const uint64_t size = *reinterpret_cast<uint64_t*>(b + lay::BT_NAME_SZ);
    if (size > 64) return nullptr;
    const char* p = (size <= 15)
        ? reinterpret_cast<const char*>(b + lay::BT_NAME)
        : *reinterpret_cast<const char**>(b + lay::BT_NAME);
    if (!IsReadable(p, size + 1)) return nullptr;
    return p;
}

static bool LoadTypesFromDb() {
    const DWORD t0 = GetTickCount();
    if (!g_buildKnown || !rva::DbGlobal) return false;

    auto* slot = reinterpret_cast<void**>(g_base + rva::DbGlobal);
    if (!IsReadable(slot, 8)) return false;
    void* db = *slot;
    if (!IsReadable(db, 0xA0)) return false;

    auto* b = static_cast<uint8_t*>(db);
    auto* array = *reinterpret_cast<void**>(b + 0x80);
    const uint32_t count = *reinterpret_cast<uint32_t*>(b + 0x8C);
    if (!IsReadable(array, 8) || count == 0 || count > 8192) return false;

    // Only objects sharing array[0]'s vtable are the class the engine expects.
    void* first = *reinterpret_cast<void**>(array);
    if (!IsReadable(first, 8)) return false;
    const uintptr_t liveVt = *reinterpret_cast<uintptr_t*>(first);

    g_types.clear();
    for (uint32_t i = 0; i < count; ++i) {
        void** slot2 = reinterpret_cast<void**>(
            reinterpret_cast<uint8_t*>(array) + 8 * i);
        if (!IsReadable(slot2, 8)) continue;
        void* obj = *slot2;
        if (!IsReadable(obj, 8)) continue;
        if (*reinterpret_cast<uintptr_t*>(obj) != liveVt) continue;
        const char* k = TypeKey(obj);
        if (!k || strncmp(k, "building_", 9) != 0) continue;
        if (!g_types.count(k)) g_types[k] = obj;
    }
    Log("db: %zu building types (count=%u) in %lu ms",
        g_types.size(), count, GetTickCount() - t0);
    return !g_types.empty();
}

// Fallback: scan private heap for objects whose vtable matches a CBuildingType
// candidate. Slower, only used when the database is not readable yet.
static bool ScanTypesOne(uintptr_t vt) {
    const DWORD t0 = GetTickCount();
    if (!vt) return false;

    uint8_t* addr = nullptr;
    MEMORY_BASIC_INFORMATION mbi{};
    while (VirtualQuery(addr, &mbi, sizeof(mbi)) == sizeof(mbi)) {
        auto* base = static_cast<uint8_t*>(mbi.BaseAddress);
        const size_t size = mbi.RegionSize;
        addr = base + size;
        if (mbi.State != MEM_COMMIT || mbi.Type != MEM_PRIVATE) continue;
        if (!(mbi.Protect & (PAGE_READWRITE | PAGE_EXECUTE_READWRITE))) continue;
        if (mbi.Protect & PAGE_GUARD) continue;
        if (size < 16) continue;

        t_inCall = 1;
        if (__builtin_setjmp(t_jmp) == 0) {
            const size_t n = size / sizeof(uintptr_t);
            auto* q = reinterpret_cast<const uintptr_t*>(base);
            for (size_t i = 0; i < n; ++i) {
                if ((i & 0xFFFFF) == 0 && g_unloading) { t_inCall = 0; return !g_types.empty(); }
                if (q[i] != vt) continue;
                auto* cand = const_cast<uint8_t*>(reinterpret_cast<const uint8_t*>(q + i));
                const char* k = TypeKey(cand);
                if (k && strncmp(k, "building_", 9) == 0) TypesAdd(k, cand);
            }
            t_inCall = 0;
        } else {
            /* region vanished mid-scan: skip it */
        }
    }
    Log("scan: %zu types via vt 0x%llX in %lu ms", g_types.size(),
        static_cast<unsigned long long>(vt), GetTickCount() - t0);
    return !g_types.empty();
}

// clear=false keeps what the fast pass already found and only adds to it.
static bool ScanTypes(bool clear = true) {
    if (g_nBldType == 0) return false;
    if (clear) {
        AcquireSRWLockExclusive(&g_typesLock);
        g_types.clear();
        ReleaseSRWLockExclusive(&g_typesLock);
    }
    for (int i = 0; i < g_nBldType; ++i)
        if (ScanTypesOne(g_vtBldType[i])) break;
    return !g_types.empty();
}


// Log the closest keys to a name that is not in the type table. Typing
// "buildin_tooling_workshop" (one letter short) previously produced a message
// that looked like "this state has no such building".
// ---------------------------------------------------------------------------
// Capabilities, decided by the self-check
// ---------------------------------------------------------------------------
static bool g_capPlayer = true;    // read the player country id
static bool g_capBuild  = true;    // issue construction orders
static bool g_capPm     = true;    // read / switch production methods
static bool g_capQueue  = true;    // read the construction queue
static bool g_selfChecked = false;

// Live player country id: the cached g_playerCountry refreshes only every 2 s,
// so a `tag` switch could otherwise let a write through for the old country.
static uint32_t LivePlayerCountry() {
    if (!g_playerObj) return 0;
    if (!HasVt(g_playerObj, &g_playerVt, 1)) return 0;

    uint32_t id = 0;
    t_inCall = 1;
    if (__builtin_setjmp(t_jmp) == 0) {
        id = *reinterpret_cast<uint32_t*>(
            static_cast<uint8_t*>(g_playerObj) + lay::JP_COUNTRY);
        t_inCall = 0;
    } else {
        return 0;
    }
    return (id > 0 && id < 65535) ? id : 0;
}

static uint32_t StateOwner(void* state) {
    if (!state || !HasVt(state, g_vtState, g_nState)) return 0;
    uint32_t owner = 0;
    t_inCall = 1;
    if (__builtin_setjmp(t_jmp) == 0) {
        owner = *reinterpret_cast<uint32_t*>(
            static_cast<uint8_t*>(state) + lay::ST_OWNER);
        t_inCall = 0;
    } else {
        return 0;
    }
    return (owner > 0 && owner < 65535) ? owner : 0;
}

// WRITE lock. Returns the confirmed owner, or 0 when the write must not happen.
// Fails closed: an unresolved player id denies everything.
static uint32_t LockWrite(void* state, const char* what, uint32_t sid) {
    const uint32_t player = LivePlayerCountry();
    if (!player) {
        Log("LOCK: denied %s for state %u - player country not resolved yet "
            "(wait for ready / player.current.txt)", what, sid);
        return 0;
    }
    if (!state || !HasVt(state, g_vtState, g_nState)) {
        Log("LOCK: denied %s for state %u - not a valid state object", what, sid);
        return 0;
    }
    const uint32_t owner = StateOwner(state);
    if (!owner) {
        Log("LOCK: denied %s for state %u - owner unreadable", what, sid);
        return 0;
    }
    if (owner != player) {
        Log("LOCK: denied %s for state %u - owner=%u, player=%u "
            "(writes are limited to the player's own country)",
            what, sid, owner, player);
        return 0;
    }
    return owner;
}

// READ scope. `all` is an explicit opt-in that is only available to read paths.
static bool LockRead(void* state, const char* what, uint32_t sid, bool all) {
    if (all) return true;
    const uint32_t player = LivePlayerCountry();
    if (!player) {
        Log("LOCK: %s for state %u skipped - player country not resolved yet",
            what, sid);
        return false;
    }
    const uint32_t owner = StateOwner(state);
    if (owner != player) return false;         // silently skipped in bulk scans
    return true;
}

static void SuggestKeys(const char* bad) {
    if (!bad || !bad[0] || g_types.empty()) return;
    const size_t n = strlen(bad);
    int shown = 0;
    for (const auto& t : g_types) {
        const char* k = t.first.c_str();          // g_types is vector<pair<string,void*>>
        if (!k || !k[0]) continue;
        const size_t m = strlen(k);
        const size_t cmp = (n < m ? n : m);
        size_t same = 0;
        while (same < cmp && bad[same] == k[same]) ++same;
        if (same < 8 && !(n < m && strncmp(k, bad, n) == 0)) continue;
        Log("  did you mean: %s", k);
        if (++shown >= 3) break;
    }
    if (!shown) Log("  (no similar key in the type table)");
}

static void* TypeByName(const char* key) {
    AcquireSRWLockShared(&g_typesLock);
    auto it = g_types.find(key);
    void* r = (it == g_types.end()) ? nullptr : it->second;
    ReleaseSRWLockShared(&g_typesLock);
    return r;
}


static size_t ScanTypesFull();   // defined below CollectRegions()

static volatile LONG g_typeScanning = 0;
static DWORD g_lastTypeScan = 0;

// Runs on its own thread: ScanTypes() walks the whole private heap, which takes
// seconds - doing that on the pump thread would visibly hitch the game.
static DWORD WINAPI TypeScanThread(LPVOID) {
    const DWORD t0 = GetTickCount();
    const size_t got = ScanTypesFull();
    if (got >= 100) {
        InterlockedExchange(&g_typesReady, 1);
        Log("types ready (%zu keys, %lu ms)", got, GetTickCount() - t0);
    } else {
        Log("type scan: only %zu keys - will retry", got);
    }
    InterlockedExchange(&g_typeScanning, 0);
    InterlockedDecrement(&g_threadsAlive);
    return 0;
}

static void RequestTypeScan() {
    if (g_typesReady || g_unloading) return;
    if (InterlockedCompareExchange(&g_typeScanning, 1, 0) != 0) return;
    if (!StartOwnedThread(TypeScanThread, "type-scan"))
        InterlockedExchange(&g_typeScanning, 0);
}

static bool WaitForTypes() {
    const DWORD t0 = GetTickCount();
    if (rva::DbGlobal) {
        for (int i = 0; i < 300 && !g_unloading; ++i) {  // up to ~30 s
            if (LoadTypesFromDb()) {
                Log("types ready in %lu ms", GetTickCount() - t0);
                return true;
            }
            Sleep(100);
        }
        Log("db not ready after %lu ms, scanning memory", GetTickCount() - t0);
    } else {
        Log("type database global unknown on this build - types come from the "
            "vtable scan once a save is loaded");
    }
    return false;                       // the pump retries via RequestTypeScan()
}

// ---------------------------------------------------------------------------
// Player country
// ---------------------------------------------------------------------------
struct Region { uint8_t* base; size_t size; };

// Collect readable private regions once. Walking the region list is cheap; the
// expensive part is touching the pages.
static void CollectRegions(std::vector<Region>& out) {
    out.clear();
    uint8_t* addr = nullptr;
    MEMORY_BASIC_INFORMATION mbi{};
    while (VirtualQuery(addr, &mbi, sizeof(mbi)) == sizeof(mbi)) {
        auto* base = static_cast<uint8_t*>(mbi.BaseAddress);
        const size_t size = mbi.RegionSize;
        addr = base + size;
        if (mbi.State != MEM_COMMIT) continue;
        if (mbi.Type == MEM_IMAGE) continue;
        if (!(mbi.Protect & (PAGE_READWRITE | PAGE_READONLY | PAGE_WRITECOPY |
                             PAGE_EXECUTE_READWRITE)))
            continue;
        if (mbi.Protect & PAGE_GUARD) continue;
        if (size < 64) continue;
        out.push_back({ base, size });
    }
}

// Full type discovery.
//
// Measurements on 1.15.1: every building type lives in the LAST few hundred
// private regions (2048 regions in: 0 keys; 2304: 64; 2560: 633), so walking from
// the highest address downwards finds them almost immediately. The sweep keeps
// going past them for a long quiet stretch before it stops, so nothing further
// down is skipped by accident.
//
// If the quiet run ends and the set still looks short, the rest of the heap is
// walked as well - correctness first, speed second.
static size_t ScanTypesFull() {
    if (g_nBldType == 0) return 0;
    const DWORD t0 = GetTickCount();

    std::vector<Region> regions;
    CollectRegions(regions);
    Log("type scan: %zu regions to walk (high address first)", regions.size());

    {
        AcquireSRWLockExclusive(&g_typesLock);
        g_types.clear();
        g_types.reserve(1024);
        ReleaseSRWLockExclusive(&g_typesLock);
    }
    DeleteFileA(pTypesReady);
    ScanState("scanning 0/%zu regions, 0 keys", regions.size());

    const size_t kEnough     = 600;   // close to the expected 633
    const size_t kQuietLimit = 64;    // regions with no new key before stopping

    size_t visited = 0;
    size_t quiet = 0;
    size_t firstHitRegion = 0;
    bool   haveHit = false;
    size_t stopAt = 0;                // index (exclusive) we stopped before

    for (size_t idx = regions.size(); idx-- > 0;) {
        if (g_unloading) break;
        const Region& r = regions[idx];

        const size_t before = TypesCount();
        t_inCall = 1;
        if (__builtin_setjmp(t_jmp) == 0) {
            const size_t cnt = r.size / sizeof(uintptr_t);
            auto* q = reinterpret_cast<const uintptr_t*>(r.base);
            for (size_t k = 0; k < cnt; ++k) {
                const uintptr_t v = q[k];
                bool hit = false;
                for (int j = 0; j < g_nBldType; ++j)
                    if (v == g_vtBldType[j]) { hit = true; break; }
                if (!hit) continue;
                auto* cand = const_cast<uint8_t*>(reinterpret_cast<const uint8_t*>(q + k));
                const char* key = TypeKey(cand);
                if (key && strncmp(key, "building_", 9) == 0) TypesAdd(key, cand);
            }
            t_inCall = 0;
        }
        ++visited;

        const size_t after = TypesCount();
        if (after > before) {
            if (!haveHit) {
                haveHit = true;
                firstHitRegion = idx;
                Log("type scan: first hit at region %zu/%zu (%lu ms)",
                    idx, regions.size(), GetTickCount() - t0);
            }
            quiet = 0;
        } else {
            ++quiet;
        }

        if ((visited & 0x7F) == 0) {
            Log("type scan: %zu regions visited (idx %zu), %zu keys, %lu ms",
                visited, idx, after, GetTickCount() - t0);
            ScanState("scanning %zu/%zu regions, %zu keys, %lu ms",
                      visited, regions.size(), after, GetTickCount() - t0);
        }

        if (after >= kEnough && quiet >= kQuietLimit) {
            stopAt = idx;
            Log("type scan: stopping after %zu quiet regions (idx %zu, %zu keys)",
                quiet, idx, after);
            break;
        }
    }

    size_t got = TypesCount();
    Log("type scan: %zu keys after %zu regions in %lu ms", got, visited,
        GetTickCount() - t0);

    // Not enough? Walk whatever is left below the stop point.
    if (got < kEnough && stopAt > 0 && !g_unloading) {
        Log("type scan: only %zu keys - walking the remaining %zu regions",
            got, stopAt);
        for (size_t idx = stopAt; idx-- > 0;) {
            if (g_unloading) break;
            const Region& r = regions[idx];
            t_inCall = 1;
            if (__builtin_setjmp(t_jmp) == 0) {
                const size_t cnt = r.size / sizeof(uintptr_t);
                auto* q = reinterpret_cast<const uintptr_t*>(r.base);
                for (size_t k = 0; k < cnt; ++k) {
                    const uintptr_t v = q[k];
                    bool hit = false;
                    for (int j = 0; j < g_nBldType; ++j)
                        if (v == g_vtBldType[j]) { hit = true; break; }
                    if (!hit) continue;
                    auto* cand = const_cast<uint8_t*>(
                        reinterpret_cast<const uint8_t*>(q + k));
                    const char* key = TypeKey(cand);
                    if (key && strncmp(key, "building_", 9) == 0) TypesAdd(key, cand);
                }
                t_inCall = 0;
            }
        }
        got = TypesCount();
        Log("type scan: %zu keys after the full walk in %lu ms", got,
            GetTickCount() - t0);
    }

    const DWORD ms = GetTickCount() - t0;
    if (got >= 100) {
        ScanState("done %zu keys in %lu ms", got, ms);
        FILE* f = nullptr;
        fopen_s(&f, pTypesReady, "w");
        if (f) {
            fprintf(f, "%zu\n", got);
            fclose(f);
        }
        Beep(1200, 150);            // audible cue: the bridge is usable now
        Beep(1600, 200);
        Log("type scan: FINISHED - %zu keys in %lu ms (see types.ready)", got, ms);
    } else {
        ScanState("failed %zu keys in %lu ms (will retry)", got, ms);
        Log("type scan: FINISHED SHORT - %zu keys in %lu ms", got, ms);
    }

    (void)firstHitRegion;
    return got;
}


// Look for an object whose first qword is `vt`. Descending order: the player object
// is allocated during a save load, so it usually sits in more recently used memory.
static void* ScanForVtInstance(uintptr_t vt, size_t idOff, size_t budget,
                               const void* skipA = nullptr, const void* skipB = nullptr) {
    if (!vt) return nullptr;
    const DWORD t0 = GetTickCount();

    std::vector<Region> regions;
    CollectRegions(regions);
    Log("scan: %zu candidate regions", regions.size());

    // descending by base address
    for (size_t i = regions.size(); i-- > 0;) {
        Region& r = regions[i];
        if (g_unloading) return nullptr;
        if (GetTickCount() - t0 > 20000) { Log("scan: time cap"); break; }

        void* volatile found = nullptr;
        t_inCall = 1;
        if (__builtin_setjmp(t_jmp) == 0) {
            const size_t n = r.size / sizeof(uintptr_t);
            auto* q = reinterpret_cast<const uintptr_t*>(r.base);
            for (size_t k = 0; k < n; ++k) {
                if ((k & 0xFFFFF) == 0 && g_unloading) { t_inCall = 0; return nullptr; }
                if (q[k] != vt) continue;
                void* cand = const_cast<uint8_t*>(reinterpret_cast<const uint8_t*>(q + k));
                if (cand == skipA || cand == skipB) continue;
                found = cand;
                break;
            }
            t_inCall = 0;
        }
        if (found) {
            Log("scan: hit after %lu ms", GetTickCount() - t0);
            return const_cast<void*>(found);
        }
        (void)idOff;
    }
    (void)budget;
    Log("scan: no object with vt 0x%llX in %zu regions (%lu ms)",
        static_cast<unsigned long long>(vt), regions.size(), GetTickCount() - t0);
    return nullptr;
}


static volatile LONG g_playerScanning = 0;
static DWORD g_lastPlayerScan = 0;

// Runs on its own thread: it only reads memory, so it is safe off the main thread
// and cannot stall the game.
static DWORD WINAPI PlayerScanThread(LPVOID) {
    InterlockedExchange(&g_faultCount, 0);
    Log("scan: start (%d candidate(s))", g_nPlayer);
    for (int i = 0; i < g_nPlayer && !g_unloading; ++i) {
        void* o = ScanForVtInstance(g_vtPlayer[i], lay::JP_COUNTRY, 0);
        if (!o) continue;

        // Diagnostic only: +0x18 of the CCountry is NOT the country id on this
        // build (measured 31995 where the id was 10), so it cannot validate a
        // candidate. Log it once and trust +0x58.
        if (!IsReadable(o, 0x80)) {
            Log("scan: candidate %p is not readable - skipped", o);
            continue;
        }

        void* country = *reinterpret_cast<void**>(static_cast<uint8_t*>(o) + 0x50);
        if (IsReadable(country, 0x1C)) {
            Log("player: +0x50 -> %p (its +0x18 = %u, informational only)",
                country,
                *reinterpret_cast<uint32_t*>(static_cast<uint8_t*>(country) + 0x18));
        }

        g_playerVt = g_vtPlayer[i];
        g_playerObj = o;

        // Dump the head of the object: if +0x58 is not the country id, the real
        // field is visible here instead of being silently rejected.
        Log("player: object @ %p (vt 0x%llX)", o,
            static_cast<unsigned long long>(g_playerVt));
        auto* b = static_cast<uint8_t*>(o);
        for (int off = 0x20; off < 0x80; off += 0x10) {
            if (!IsReadable(b + off, 16)) break;
            Log("  +0x%02X: %016llX %016llX", off,
                static_cast<unsigned long long>(*reinterpret_cast<uint64_t*>(b + off)),
                static_cast<unsigned long long>(*reinterpret_cast<uint64_t*>(b + off + 8)));
        }
        const uint32_t id = *reinterpret_cast<uint32_t*>(b + lay::JP_COUNTRY);
        Log("player: +0x%X = %u (%s)", static_cast<unsigned>(lay::JP_COUNTRY), id,
            (id > 0 && id < 65535) ? "plausible country id" : "NOT a plausible id");
        break;
    }
    if (!g_playerObj) {
        static volatile LONG miss = 0;
        const long m = InterlockedIncrement(&miss);
        Log("player: not found (attempt %ld)", m);
    }
    if (g_faultCount > 3)
        Log("scan: %ld page fault(s) skipped", static_cast<long>(g_faultCount));
    InterlockedExchange(&g_playerScanning, 0);
    InterlockedDecrement(&g_threadsAlive);
    return 0;
}

static void RequestPlayerScan() {
    if (g_nPlayer == 0 || g_unloading) return;
    if (InterlockedCompareExchange(&g_playerScanning, 1, 0) != 0) return;
    if (!StartOwnedThread(PlayerScanThread, "player-scan"))
        InterlockedExchange(&g_playerScanning, 0);
}

// A world is live when the state registry has entries. At the main menu it is
// empty, and the player object does not exist yet.
// A save is live when the state registry holds entries; at the main menu it is
// empty. Everything world-scoped hangs off this.
// Capacity of the registry array. +0x2C is a capacity (it doubles 1024 -> 2048),
// NOT the number of states.
static uint32_t RegistryCapacity() {
    if (!g_regState) return 0;
    auto* reg = *reinterpret_cast<void**>(g_regState);
    if (!IsReadable(reg, 0x28)) return 0;
    const uint32_t c = *reinterpret_cast<uint32_t*>(
        static_cast<uint8_t*>(reg) + lay::REG_COUNT);
    return (c > 0 && c <= 100000) ? c : 0;
}

// Cheap: does the registry hold at least one real CState? Stops at the first hit,
// so this is safe to call on every pump frame.
static bool HasAnyState() {
    const uint32_t cap = RegistryCapacity();
    if (!cap) return false;
    auto* reg = *reinterpret_cast<void**>(g_regState);
    auto* b = static_cast<uint8_t*>(reg);
    auto** entries = *reinterpret_cast<void***>(b + lay::REG_DATA);
    if (!entries) return false;

    int nulls = 0;
    for (uint32_t id = 0; id < cap; ++id) {
        void** slot = reinterpret_cast<void**>(
            reinterpret_cast<uint8_t*>(entries) + 16 * id + 8);
        if (!IsReadable(slot, 8)) continue;
        void* o = *slot;
        if (!o) { if (++nulls > 256) return false; continue; }   // still loading
        return HasVt(o, g_vtState, g_nState);
    }
    return false;
}

// Expensive: count every valid state. Only called from the throttled log line.
static int CountStates() {
    const uint32_t cap = RegistryCapacity();
    if (!cap) return 0;
    auto* reg = *reinterpret_cast<void**>(g_regState);
    auto* b = static_cast<uint8_t*>(reg);
    auto** entries = *reinterpret_cast<void***>(b + lay::REG_DATA);
    if (!entries) return 0;

    int filled = 0;
    for (uint32_t id = 0; id < cap; ++id) {
        void** slot = reinterpret_cast<void**>(
            reinterpret_cast<uint8_t*>(entries) + 16 * id + 8);
        if (!IsReadable(slot, 8)) continue;
        void* o = *slot;
        if (o && HasVt(o, g_vtState, g_nState)) ++filled;
    }
    return filled;
}


static volatile LONG g_inSave = 0;
static DWORD g_inSaveSince = 0;
static DWORD g_deferSince = 0;
static DWORD g_lastWorldWrite = 0;

static void DumpRegistryOnce() {
    // At most 3 dumps, and only while the capacity is still growing. The old
    // version assigned `done = true` before the checks, which reset it to 1 on
    // every frame - the dump then ran continuously (3 log lines + a full
    // CountStates() per frame).
    static int  dumps = 0;
    static int  lastCap = 0;
    if (dumps >= 3 || !g_regState) return;

    auto* reg = *reinterpret_cast<void**>(g_regState);
    if (!IsReadable(reg, 0x40)) return;

    auto* b = static_cast<uint8_t*>(reg);
    const int cap = *reinterpret_cast<int32_t*>(b + lay::REG_COUNT);
    if (cap < 1000) return;                      // still loading
    if (lastCap && cap <= lastCap) return;       // nothing new
    lastCap = cap;
    ++dumps;

    Log("registry: obj=%p  +00=%08X +04=%08X +08=%08X +0C=%08X",
        reg,
        *reinterpret_cast<uint32_t*>(b + 0x00), *reinterpret_cast<uint32_t*>(b + 0x04),
        *reinterpret_cast<uint32_t*>(b + 0x08), *reinterpret_cast<uint32_t*>(b + 0x0C));
    Log("registry: +10=%08X +14=%08X +18=%08X +1C=%08X",
        *reinterpret_cast<uint32_t*>(b + 0x10), *reinterpret_cast<uint32_t*>(b + 0x14),
        *reinterpret_cast<uint32_t*>(b + 0x18), *reinterpret_cast<uint32_t*>(b + 0x1C));
    Log("registry: +20=%p +24=%d +28=%d +2C=%d (capacity)  validStates=%d",
        *reinterpret_cast<void**>(b + lay::REG_DATA),
        *reinterpret_cast<int32_t*>(b + 0x24),
        *reinterpret_cast<int32_t*>(b + 0x28),
        *reinterpret_cast<int32_t*>(b + lay::REG_COUNT),
        CountStates());

    auto** entries = *reinterpret_cast<void***>(b + lay::REG_DATA);
    const uint32_t count = *reinterpret_cast<uint32_t*>(b + lay::REG_COUNT);
    if (!entries) return;

    // Probe past the reported count: if slots keep being valid, 1024 is a
    // capacity (or the first chunk) and the real total is larger.
    const uint32_t probes[] = { 0, count ? count - 1 : 0, count, count + 64,
                                count + 128, count + 160, count + 256, count + 512 };
    for (uint32_t id : probes) {
        void** slot = reinterpret_cast<void**>(
            reinterpret_cast<uint8_t*>(entries) + 16 * id + 8);
        void* o = IsReadable(slot, 8) ? *slot : nullptr;
        Log("registry: entry[%u] = %p%s", id, o,
            (o && HasVt(o, g_vtState, g_nState)) ? " (state)" : "");
    }
}

// Nothing engine-facing runs for a moment after a world transition: while a save
// loads (or the country selection screen is built) the objects we cache are being
// destroyed and recreated, and a stale dereference takes the whole game down.
static volatile LONG g_worldChangedAt = 0;
// A fixed delay proved too short: a save can still be rebuilding its registries
// several seconds in, and scanning the heap at that moment crashed the game. The
// window is therefore extended for as long as the state count keeps changing.
static const DWORD kSettleMs = 5000;
static volatile LONG g_lastStateCount = -1;

static bool WorldSettled() {
    const DWORD t = static_cast<DWORD>(
        InterlockedCompareExchange(&g_worldChangedAt, 0, 0));
    if (!t) return true;
    return (GetTickCount() - t) > kSettleMs;
}

static void RunWorld() {
    // This runs on every pump frame. HasAnyState() can walk up to 256 slots while
    // a save is loading, so only re-check every 500 ms.
    static DWORD lastCheck = 0;
    static bool  cached = false;
    const DWORD now0 = GetTickCount();
    if (!lastCheck || now0 - lastCheck >= 500) {
        lastCheck = now0;
        cached = HasAnyState();
        if (cached) {
            // While the registry is still growing the world is mid-load: keep
            // pushing the settle deadline forward.
            const LONG n = static_cast<LONG>(CountStates());
            const LONG prev = InterlockedExchange(&g_lastStateCount, n);
            if (n != prev) {
                InterlockedExchange(&g_worldChangedAt,
                                    static_cast<LONG>(GetTickCount()));
            }
        }
    }
    const bool inSave = cached;
    const bool was = (g_inSave != 0);

    if (inSave != was) {
        InterlockedExchange(&g_inSave, inSave ? 1 : 0);
        InterlockedExchange(&g_worldChangedAt,
                            static_cast<LONG>(GetTickCount()));
        Log("world: %s (states=%d, capacity=%u)",
            inSave ? "in save" : "main menu", CountStates(), RegistryCapacity());

        if (inSave) g_inSaveSince = GetTickCount();

        if (!inSave) {
            // Nothing world-scoped is valid any more: drop the stale outputs so a
            // consumer cannot read the previous game's values.
            g_playerCountry = 0;
            g_playerObj = nullptr;
            g_playerVt = 0;
            g_lastPlayerScan = 0;
            g_lastPlayerWrite = 0;
            g_deferSince = 0;              // stale timestamp would skip the hold
            DeleteFileA(pPlayerOut);
            Log("world: cleared player state (main menu)");
        }
    }

    const DWORD now = GetTickCount();
    if (g_lastWorldWrite && now - g_lastWorldWrite < 2000) return;
    g_lastWorldWrite = now;

    FILE* f = nullptr;
    fopen_s(&f, pWorldOut, "w");
    if (!f) return;
    fprintf(f, "%d\n", inSave ? 1 : 0);
    fclose(f);
}

static void RunPlayer() {
    if (g_nPlayer == 0) return;

    static bool announced = false;
    if (!g_inSave) {              // single source of truth, already throttled
        announced = false;
        return;
    }
    if (!announced) {
        announced = true;
        Log("world ready - looking for the player object");
    }

    if (g_playerObj && !HasVt(g_playerObj, &g_playerVt, 1)) g_playerObj = nullptr;
    if (!g_playerObj) {
        const DWORD now = GetTickCount();
        // Wait for the load to settle (the heap shrinks from ~3800 regions to
        // ~2700), otherwise the first scan spends its whole budget on a
        // half-built world and finds nothing.
        if (g_inSaveSince && now - g_inSaveSince < 2000) return;
        // Not resolved yet: retry every 4 s on a worker thread.
        if (!g_lastPlayerScan || now - g_lastPlayerScan > 4000) {
            g_lastPlayerScan = now;
            RequestPlayerScan();
        }
        return;
    }
    void* obj = g_playerObj;

    const uint32_t id = *reinterpret_cast<uint32_t*>(
        static_cast<uint8_t*>(obj) + lay::JP_COUNTRY);
    if (id > 0 && id < 65535 && id != g_playerCountry) {
        Log("player country = %u (CJominiPlayer+0x%X, was %u)",
            id, static_cast<unsigned>(lay::JP_COUNTRY), g_playerCountry);
        g_playerCountry = id;
    }
    if (id == 0 || id >= 65535) return;   // layout not confirmed yet

    const DWORD now = GetTickCount();
    if (g_lastPlayerWrite && now - g_lastPlayerWrite < 2000) return;
    g_lastPlayerWrite = now;

    FILE* f = nullptr;
    fopen_s(&f, pPlayerOut, "w");
    if (!f) return;
    fprintf(f, "%u\n", id);
    fclose(f);
}

// ---------------------------------------------------------------------------
// Production methods
// ---------------------------------------------------------------------------
// Read a std::string-style key at +0x18 (ptr) / +0x28 (size), like CBuildingType.
static const char* KeyAt18(const void* obj, const char* prefix) {
    if (!IsReadable(obj, 0x30)) return nullptr;
    const auto* b = static_cast<const uint8_t*>(obj);
    const uint64_t size = *reinterpret_cast<const uint64_t*>(b + 0x28);
    if (size == 0 || size > 64) return nullptr;

    const char* p = (size <= 15)
        ? reinterpret_cast<const char*>(b + 0x18)
        : *reinterpret_cast<const char* const*>(b + 0x18);
    if (!IsReadable(p, size + 1)) return nullptr;

    const size_t plen = strlen(prefix);
    if (size < plen || memcmp(p, prefix, plen) != 0) return nullptr;
    for (uint64_t i = 0; i < size; ++i) {
        const unsigned char c = static_cast<unsigned char>(p[i]);
        if (c < 0x20 || c > 0x7E) return nullptr;
    }
    return p;
}

static const char* KeyOf(const void* obj, const char* prefix, size_t maxOff) {
    if (!IsReadable(obj, 8)) return nullptr;
    const auto* b = static_cast<const uint8_t*>(obj);
    const size_t plen = strlen(prefix);
    for (size_t off = 0; off + 8 <= maxOff; off += 8) {
        const uintptr_t p = *reinterpret_cast<const uintptr_t*>(b + off);
        if (!IsReadable(reinterpret_cast<const void*>(p), plen + 1)) continue;
        const char* c = reinterpret_cast<const char*>(p);
        if (memcmp(c, prefix, plen) != 0) continue;
        int n = 0;
        bool ok = true;
        while (n < 64 && c[n]) {
            const unsigned char ch = static_cast<unsigned char>(c[n]);
            if (ch < 0x20 || ch > 0x7E) { ok = false; break; }
            ++n;
        }
        if (ok && n > static_cast<int>(plen)) return c;
    }
    return nullptr;
}

struct PmArray { void** data; int32_t count; };

static PmArray BuildingPms(void* bld) {
    PmArray a{nullptr, 0};
    auto* b = static_cast<uint8_t*>(bld);
    if (!IsReadable(b + lay::BLD_PMCNT, 4)) return a;
    a.count = *reinterpret_cast<int32_t*>(b + lay::BLD_PMCNT);
    a.data = *reinterpret_cast<void***>(b + lay::BLD_PMS);
    if (a.count < 0 || a.count > 64 ||
        !IsReadable(a.data, sizeof(void*) * static_cast<size_t>(a.count)))
        return {nullptr, 0};
    return a;
}

struct GroupArray { void** data; int32_t count; };

static GroupArray TypeGroups(void* btype) {
    GroupArray a{nullptr, 0};
    auto* b = static_cast<uint8_t*>(btype);
    if (!IsReadable(b + lay::BT_GCOUNT, 4)) return a;
    a.count = *reinterpret_cast<int32_t*>(b + lay::BT_GCOUNT);
    a.data = *reinterpret_cast<void***>(b + lay::BT_GROUPS);
    if (a.count < 0 || a.count > 32 ||
        !IsReadable(a.data, sizeof(void*) * static_cast<size_t>(a.count)))
        return {nullptr, 0};
    return a;
}

static PmArray GroupPms(void* grp) {
    PmArray a{nullptr, 0};
    auto* b = static_cast<uint8_t*>(grp);
    if (!IsReadable(b + lay::GRP_PMCNT, 4)) return a;
    a.count = *reinterpret_cast<int32_t*>(b + lay::GRP_PMCNT);
    a.data = *reinterpret_cast<void***>(b + lay::GRP_PMS);
    if (a.count < 0 || a.count > 64 ||
        !IsReadable(a.data, sizeof(void*) * static_cast<size_t>(a.count)))
        return {nullptr, 0};
    return a;
}

static bool GroupHas(void* grp, void* pm) {
    PmArray g = GroupPms(grp);
    for (int32_t i = 0; i < g.count; ++i) if (g.data[i] == pm) return true;
    return false;
}

static void* PmByKey(void* btype, const char* key, void** groupOut) {
    GroupArray groups = TypeGroups(btype);
    for (int32_t g = 0; g < groups.count; ++g) {
        void* grp = groups.data[g];
        if (!grp) continue;
        PmArray pms = GroupPms(grp);
        for (int32_t k = 0; k < pms.count; ++k) {
            const char* kk = KeyAt18(pms.data[k], "pm_");
            if (!kk) kk = KeyOf(pms.data[k], "pm_", 0x200);
            if (kk && strcmp(kk, key) == 0) {
                if (groupOut) *groupOut = grp;
                return pms.data[k];
            }
        }
    }
    return nullptr;
}

static void* BuildingOf(void* state, void* btype) {
    if (!g_bldLookup || !state || !btype) return nullptr;

    using Fn = void* (*)(void*, void*);
    void* bld = nullptr;
    t_inCall = 1;
    if (__builtin_setjmp(t_jmp) == 0) {
        bld = reinterpret_cast<Fn>(g_bldLookup)(state, btype);
        t_inCall = 0;
    } else {
        Log("pm: building lookup faulted");
        return nullptr;
    }

    // The lookup returns a sentinel instead of null when the building is absent,
    // so the result must be validated. The object keeps its own type pointer at
    // +0xB0: comparing that with the requested type is exact and does not depend
    // on which vtable it happens to use.
    if (!IsReadable(bld, 0xC0)) return nullptr;
    if (*reinterpret_cast<void**>(static_cast<uint8_t*>(bld) + lay::BLD_TYPE) == btype)
        return bld;

    // Fall back to the vtable whitelist for the (unlikely) case where +0xB0 is not
    // the type pointer on some object variant.
    if (HasVt(bld, g_vtBld, g_nBld)) return bld;
    return nullptr;
}

// pms.txt:  <state_id> <building_key>
static void RunPmProbe() {
    if (GetFileAttributesA(pPms) == INVALID_FILE_ATTRIBUTES) return;
    if (!g_capPm) {
        Log("pms: refused - pm capability disabled by the self-check");
        MoveFileExA(pPms, pPmsDone, MOVEFILE_REPLACE_EXISTING);
        return;
    }

    FILE* out = nullptr;
    fopen_s(&out, pPmsOut, "w");
    if (out) fprintf(out, "state_id,building,slot,pm_group,pm_key\n");

    FILE* f = nullptr;
    fopen_s(&f, pPms, "r");
    if (f) {
        char line[256];
        while (fgets(line, sizeof(line), f)) {
            char* p = line;
            while (*p == ' ' || *p == '\t') ++p;
            if (!*p || *p == '\n' || *p == '\r' || *p == '#') continue;

            unsigned sid = 0;
            char name[128] = {0}, extra[32] = {0};
            const int nf = sscanf_s(p, "%u %127s %31s", &sid,
                                    name, static_cast<unsigned>(sizeof(name)),
                                    extra, static_cast<unsigned>(sizeof(extra)));
            if (nf < 2) {
                Log("pms: bad line: %.60s", p);
                continue;
            }
            const bool all = (nf >= 3 && _stricmp(extra, "all") == 0);
            void* state = StateById(sid);
            void* btype = TypeByName(name);
            if (state && !LockRead(state, "read-product-methods", sid, all)) {
                Log("pms: state %u is not yours (owner=%u) - denied by scope",
                    sid, StateOwner(state));
                continue;
            }
            if (!state || !btype) {
                if (!btype) {
        Log("pms: unknown building key '%s' (not in the type table)", name);
        SuggestKeys(name);
    } else {
        Log("pms: state %u has no %s (the lookup returned the 'absent' sentinel)",
            sid, name);
    }
                continue;
            }
            void* bld = BuildingOf(state, btype);
            if (!bld) { Log("pms: state %u has no %s", sid, name); continue; }

            PmArray pms = BuildingPms(bld);
            Log("pms: state=%u bld=%p active=%d", sid, bld, pms.count);
            for (int32_t i = 0; i < pms.count; ++i) {
                const char* k = KeyAt18(pms.data[i], "pm_");
                const char* how = k ? "+0x18" : "-";
                if (!k) { k = KeyOf(pms.data[i], "pm_", 0x200); how = k ? "scan" : "-"; }
                Log("  [%d] %p %s (%s)", i, pms.data[i], k ? k : "?", how);
                if (out) fprintf(out, "%u,%s,%d,,%s\n", sid, name, i, k ? k : "?");
            }

            GroupArray groups = TypeGroups(btype);
            Log("pms: groups=%d", groups.count);
            for (int32_t g = 0; g < groups.count; ++g) {
                void* grp = groups.data[g];
                const char* gk = KeyAt18(grp, "pmg_");
                const char* ghow = gk ? "+0x18" : "-";
                if (!gk) { gk = KeyOf(grp, "pmg_", 0x200); ghow = gk ? "scan" : "-"; }
                Log("  group[%d] %p %s (%s)", g, grp, gk ? gk : "?", ghow);
                if (!grp) continue;
                PmArray gp = GroupPms(grp);
                for (int32_t k = 0; k < gp.count; ++k) {
                    const char* pk = KeyAt18(gp.data[k], "pm_");
                    if (!pk) pk = KeyOf(gp.data[k], "pm_", 0x200);
                    Log("    (%d) %p %s", k, gp.data[k], pk ? pk : "?");
                    if (out) fprintf(out, "%u,%s,,%s,%s\n",
                                     sid, name, gk ? gk : "?", pk ? pk : "?");
                }
            }
        }
        fclose(f);
    }
    if (out) fclose(out);
    MoveFileExA(pPms, pPmsDone, MOVEFILE_REPLACE_EXISTING);
}

static const char* SwitchPm(unsigned sid, const char* bkey, const char* pmkey,
                            int* slotOut, char* oldOut, size_t oldCap) {
    *slotOut = -1;
    oldOut[0] = 0;

    void* state = StateById(sid);
    void* btype = TypeByName(bkey);
    if (!g_capPm) return "disabled";
    if (!state || !btype) return "poor_refs";
    if (!LockWrite(state, "switch-product-method", sid)) return "denied";

    void* bld = BuildingOf(state, btype);
    if (!bld) return "no_building";

    void* grp = nullptr;
    void* want = PmByKey(btype, pmkey, &grp);
    if (!want) return "no_such_pm";

    PmArray pms = BuildingPms(bld);
    if (!pms.data || pms.count <= 0) return "bad_array";

    int slot = -1;
    for (int32_t i = 0; i < pms.count; ++i) {
        if (pms.data[i] && GroupHas(grp, pms.data[i])) { slot = i; break; }
    }
    if (slot < 0) return "no_group_slot";

    const char* cur = KeyAt18(pms.data[slot], "pm_");
    if (!cur) cur = KeyOf(pms.data[slot], "pm_", 0x200);
    if (cur) strncpy_s(oldOut, oldCap, cur, _TRUNCATE);
    *slotOut = slot;

    if (pms.data[slot] == want) return "already";

    DWORD oldProt = 0;
    if (!VirtualProtect(&pms.data[slot], sizeof(void*), PAGE_READWRITE, &oldProt))
        return "write_failed";
    pms.data[slot] = want;
    VirtualProtect(&pms.data[slot], sizeof(void*), oldProt, &oldProt);
    return "ok";
}

namespace q {
    constexpr size_t GOV     = 0xB00;
    constexpr size_t PRIV    = 0xB78;
    constexpr size_t DATA    = 0x60;
    constexpr size_t CAP     = 0x68;
    constexpr size_t COUNT   = 0x6C;
    constexpr size_t ELEM    = 0x48;
    constexpr size_t E_STATE = 0x00;
    constexpr size_t E_TYPE  = 0x08;
    constexpr size_t E_TOTAL = 0x28;
    constexpr size_t E_LEFT  = 0x30;
}

struct QueueView { uint8_t* data; int32_t count; int32_t cap; };

static QueueView GetQueue(void* country, size_t off) {
    QueueView v{nullptr, 0, 0};
    if (!country) return v;
    auto* c = static_cast<uint8_t*>(country) + off;
    if (!IsReadable(c + q::COUNT, 4)) return v;
    v.count = *reinterpret_cast<int32_t*>(c + q::COUNT);
    if (IsReadable(c + q::CAP, 4))
        v.cap = *reinterpret_cast<int32_t*>(c + q::CAP);
    v.data  = *reinterpret_cast<uint8_t**>(c + q::DATA);
    if (v.count < 0 || v.count > 4096) v.count = 0;
    if (v.cap < 0 || v.cap > 4096) v.cap = v.count;
    // 1.15.1 的 queue.count 有时只表示“当前活动项”，队列里其余项目仍在
    // DATA 的 cap 容量内；优先按 cap 校验整段数组。
    size_t n = static_cast<size_t>(v.cap > v.count ? v.cap : v.count);
    if (n && !IsReadable(v.data, q::ELEM * n)) {
        n = static_cast<size_t>(v.count);
        if (n && !IsReadable(v.data, q::ELEM * n)) return {nullptr, 0, 0};
        v.cap = v.count;
    }
    return v;
}

static bool IsKnownBuildingType(void* p) {
    if (!p) return false;
    for (const auto& kv : g_types) if (kv.second == p) return true;
    return false;
}

// How many queue entries for (state, btype) - the real "did the build register" test.
static int QueueMatches(void* country, uint32_t stateId, void* btype) {
    int n = 0;
    for (size_t off : { q::GOV, q::PRIV }) {
        QueueView v = GetQueue(country, off);
        for (int32_t i = 0; i < v.cap; ++i) {
            auto* e = v.data + q::ELEM * i;
            void* et = *reinterpret_cast<void**>(e + q::E_TYPE);
            if (!IsKnownBuildingType(et)) continue;
            if (*reinterpret_cast<uint32_t*>(e + q::E_STATE) != stateId) continue;
            if (btype && et != btype) continue;
            ++n;
        }
    }
    return n;
}

// queue.txt -> queue.csv  (queue,index,state_id,building,pts_total,pts_remaining)
static void RunQueueDump() {
    if (GetFileAttributesA(pQueue) == INVALID_FILE_ATTRIBUTES) return;
    if (!g_capQueue) {
        Log("queue: refused - queue capability disabled by the self-check");
        MoveFileExA(pQueue, pQueueDone, MOVEFILE_REPLACE_EXISTING);
        return;
    }

    FILE* out = nullptr;
    fopen_s(&out, pQueueOut, "w");
    if (out) fprintf(out, "queue,index,state_id,building,pts_total,pts_remaining\n");

    const uint32_t me = g_playerCountry;
    if (!me) { Log("queue: player unknown"); }
    void* country = me ? CountryById(me) : nullptr;
    if (country && !HasVt(country, g_vtCountry, g_nCountry)) country = nullptr;

    int total = 0;
    for (size_t off : { q::GOV, q::PRIV }) {
        const char* tag = (off == q::GOV) ? "government" : "private";
        QueueView v = GetQueue(country, off);
        const auto* cbase = reinterpret_cast<uint8_t*>(country) + off;
        Log("queue %s: data=%p cap=%d count=%d",
            tag,
            IsReadable(cbase + q::DATA, 8)
                ? *reinterpret_cast<void**>(const_cast<uint8_t*>(cbase) + q::DATA)
                : nullptr,
            IsReadable(cbase + q::CAP, 4)
                ? *reinterpret_cast<int32_t*>(const_cast<uint8_t*>(cbase) + q::CAP)
                : -1,
            v.count);
        if (v.count > 0) {
            auto* e0 = v.data;
            Log("queue %s[0]: state=%u type=%p total=%lld left=%lld",
                tag,
                *reinterpret_cast<uint32_t*>(e0 + q::E_STATE),
                *reinterpret_cast<void**>(e0 + q::E_TYPE),
                static_cast<long long>(*reinterpret_cast<int64_t*>(e0 + q::E_TOTAL)),
                static_cast<long long>(*reinterpret_cast<int64_t*>(e0 + q::E_LEFT)));
        }
        for (int32_t i = 0; i < v.cap; ++i) {
            auto* e = v.data + q::ELEM * i;
            const uint32_t sid = *reinterpret_cast<uint32_t*>(e + q::E_STATE);
            void* bt = *reinterpret_cast<void**>(e + q::E_TYPE);
            if (!IsKnownBuildingType(bt) || sid == 0 || sid > 4096) continue;
            const int64_t tot = *reinterpret_cast<int64_t*>(e + q::E_TOTAL);
            const int64_t left = *reinterpret_cast<int64_t*>(e + q::E_LEFT);
            if (tot < 0 || tot > 1000000000000LL || left < 0 || left > 1000000000000LL) continue;
            const char* key = TypeKey(bt);
            if (out) fprintf(out, "%s,%d,%u,%s,%.2f,%.2f\n", tag, i, sid,
                             key ? key : "?", tot / 100000.0, left / 100000.0);
            ++total;
        }
    }
    if (out) fclose(out);
    Log("queue: %d item(s) -> queue.csv", total);
    MoveFileExA(pQueue, pQueueDone, MOVEFILE_REPLACE_EXISTING);
}

// pmscan.txt:  <building_key>   ->   pmscan.csv (state_id, owner)
static void RunPmScan() {
    if (GetFileAttributesA(pScan) == INVALID_FILE_ATTRIBUTES) return;

    char key[128] = {0}, extra[32] = {0};
    bool all = false;
    FILE* f = nullptr;
    fopen_s(&f, pScan, "r");
    if (f) {
        char line[256];
        while (fgets(line, sizeof(line), f)) {
            const int n = sscanf_s(line, "%127s %31s", key,
                                   static_cast<unsigned>(sizeof(key)),
                                   extra, static_cast<unsigned>(sizeof(extra)));
            if (n >= 1) {
                if (n >= 2 && _stricmp(extra, "all") == 0) all = true;
                break;
            }
        }
        fclose(f);
    }
    MoveFileExA(pScan, pScanDone, MOVEFILE_REPLACE_EXISTING);
    if (!key[0]) return;

    void* btype = TypeByName(key);
    if (!btype) {
        Log("pmscan: unknown building key '%s' (not in the type table)", key);
        SuggestKeys(key);
        return;
    }
    Log("pmscan: %s -> %p (scope=%s)", key, btype, all ? "ALL" : "player");

    auto* reg = g_regState ? *reinterpret_cast<void**>(g_regState) : nullptr;
    if (!IsReadable(reg, 0x30)) return;
    auto* rb = static_cast<uint8_t*>(reg);
    auto** entries = *reinterpret_cast<void***>(rb + lay::REG_DATA);
    const uint32_t count = *reinterpret_cast<uint32_t*>(rb + lay::REG_COUNT);
    if (!entries) return;

    FILE* out = nullptr;
    fopen_s(&out, pScanOut, "w");
    if (!out) return;
    fprintf(out, "state_id,owner\n");

    int hits = 0, rejected = 0, nulls = 0;
    int shown = 0;
    const uint32_t player = LivePlayerCountry();   // hoisted: one lookup per scan
    struct Sample { uint32_t id, owner; void* raw; uintptr_t vt; void* type; };
    Sample samples[3] = {};
    for (uint32_t id = 0; id < count; ++id) {
        void** slot = reinterpret_cast<void**>(
            reinterpret_cast<uint8_t*>(entries) + 16 * id + 8);
        if (!IsReadable(slot, 8)) continue;
        void* state = *slot;
        if (!HasVt(state, g_vtState, g_nState)) continue;

        const uint32_t stateOwner = StateOwner(state);
        if (!all && (!player || stateOwner != player)) continue;   // out of scope

        // call the raw lookup so a rejection can be explained
        using Fn = void* (*)(void*, void*);
        void* raw = nullptr;
        t_inCall = 1;
        if (__builtin_setjmp(t_jmp) == 0) {
            raw = reinterpret_cast<Fn>(g_bldLookup)(state, btype);
            t_inCall = 0;
        } else {
            raw = nullptr;
        }

        const uint32_t owner = stateOwner;
        if (!raw) { ++nulls; continue; }

        bool ok = false;
        if (IsReadable(raw, 0xC0)) {
            if (*reinterpret_cast<void**>(static_cast<uint8_t*>(raw) + lay::BLD_TYPE) == btype)
                ok = true;
            else if (HasVt(raw, g_vtBld, g_nBld))
                ok = true;
        }
        if (!ok) {
            ++rejected;
            if (shown < 3) {
                samples[shown].id    = id;
                samples[shown].owner = owner;
                samples[shown].raw   = raw;
                samples[shown].vt    = IsReadable(raw, 8)
                    ? *reinterpret_cast<uintptr_t*>(raw) : 0;
                samples[shown].type  = IsReadable(raw, 0xC0)
                    ? *reinterpret_cast<void**>(static_cast<uint8_t*>(raw) + lay::BLD_TYPE)
                    : nullptr;
                ++shown;
            }
            continue;
        }
        fprintf(out, "%u,%u\n", id, owner);
        ++hits;
    }
    fclose(out);
    Log("pmscan: %d state(s) HAVE %s, %d do NOT (no such building there), %d no state "
        "[scope=%s]", hits, key, rejected, nulls, all ? "ALL" : "player");
    if (hits == 0) {
        // Nothing matched: show the raw lookup results so the reason is visible.
        for (int i = 0; i < shown; ++i) {
            Log("pmscan: sample state=%u owner=%u lookup_returned=%p vt=0x%llX "
                "type0xB0=%p wanted=%p",
                samples[i].id, samples[i].owner, samples[i].raw,
                static_cast<unsigned long long>(samples[i].vt),
                samples[i].type, btype);
        }
    }
}

// pmm.txt:  <state_id> <building_key> <pm_key>
static void RunPmWrite() {
    if (GetFileAttributesA(pPmm) == INVALID_FILE_ATTRIBUTES) return;

    FILE* out = nullptr;
    fopen_s(&out, pPmmResult, "w");
    if (out) fprintf(out, "state_id,building,pm,status,slot,old_pm\n");

    FILE* f = nullptr;
    fopen_s(&f, pPmm, "r");
    if (f) {
        char line[320];
        int ok = 0, fail = 0, denied = 0;
        while (fgets(line, sizeof(line), f)) {
            char* p = line;
            while (*p == ' ' || *p == '\t') ++p;
            if (!*p || *p == '\n' || *p == '\r' || *p == '#') continue;

            unsigned sid = 0;
            char bkey[128] = {0}, pmkey[128] = {0};
            if (sscanf_s(p, "%u %127s %127s", &sid, bkey,
                         static_cast<unsigned>(sizeof(bkey)),
                         pmkey, static_cast<unsigned>(sizeof(pmkey))) < 3) {
                Log("pmm: bad line: %.60s", p);
                ++fail;
                continue;
            }

            int slot = -1;
            char old[128] = {0};
            const char* status = SwitchPm(sid, bkey, pmkey, &slot, old, sizeof(old));
            if (strcmp(status, "ok") == 0 || strcmp(status, "already") == 0) ++ok;
            else if (strcmp(status, "denied") == 0) ++denied;
            else ++fail;

            Log("pmm: %u %s -> %s : %s (slot=%d old=%s)",
                sid, bkey, pmkey, status, slot, old[0] ? old : "-");
            if (out) fprintf(out, "%u,%s,%s,%s,%d,%s\n",
                             sid, bkey, pmkey, status, slot, old);
        }
        fclose(f);
        Log("pmm done: ok=%d fail=%d denied=%d", ok, fail, denied);
    }
    if (out) fclose(out);
    MoveFileExA(pPmm, pPmmDone, MOVEFILE_REPLACE_EXISTING);
}

// ---------------------------------------------------------------------------
// Build
// ---------------------------------------------------------------------------
// Verified this session: CBuilding+0xE8 holds the level (both dwords).
static uint32_t BuildingLevel(void* bld) {
    auto* p = static_cast<const uint8_t*>(bld);
    if (!p || !IsReadable(p + 0xE8, 4)) return 0;
    return *reinterpret_cast<const uint32_t*>(p + 0xE8);
}


static int BuildOne(void* state, void* btype, int levels) {
    if (!g_capBuild) {
        Log("build: refused - the self-check disabled this capability "
            "(game update changed the layout?)");
        return 0;
    }
    if (!g_buildCommand || !g_buildCtx || !state || !btype) return 0;
    if (!HasVt(state, g_vtState, g_nState)) { Log("build: bad state %p", state); return 0; }

    const uint32_t sid = *reinterpret_cast<uint32_t*>(
        static_cast<uint8_t*>(state) + lay::ST_ID);
    const uint32_t owner = LockWrite(state, "build", sid);
    if (!owner) return -1;                   // -1 = refused by the lock

    void* country = CountryById(owner);
    if (!HasVt(country, g_vtCountry, g_nCountry)) {
        Log("build: country %u unresolved", owner);
        return 0;
    }

    // 0x40 bytes: the engine reads past the two fields we understand.
    alignas(16) uint8_t paramBuf[0x40] = {0};
    bool usedTemplate = false;
    if (GetFileAttributesA(pParamTpl) != INVALID_FILE_ATTRIBUTES)
        usedTemplate = LoadParamTemplate(paramBuf, sizeof(paramBuf));
    if (!usedTemplate) {
        *reinterpret_cast<uint32_t*>(paramBuf + 0x00) = owner;
        *reinterpret_cast<uint32_t*>(paramBuf + 0x10) = 2;
    } else {
        *reinterpret_cast<uint32_t*>(paramBuf + 0x00) = owner;
        Log("build: using param template (first qword 0x%llX)",
            static_cast<unsigned long long>(
                *reinterpret_cast<uint64_t*>(paramBuf)));
    }

    using Fn = int64_t (*)(void*, void*, void*, void*, int, int, uint32_t, void*);
    auto fn = reinterpret_cast<Fn>(g_buildCommand);

    if (BadBuild(btype)) {
        Log("build: refused - this type already faulted in this session "
            "(the state cannot host it, e.g. a mine without the resource)");
        return 0;
    }

    void* bldBefore = BuildingOf(state, btype);
    const bool existed = bldBefore != nullptr;
    const uint32_t levelBefore = BuildingLevel(bldBefore);
    const int queuedBefore = QueueMatches(country, sid, btype);

    volatile int64_t rc = -999;
    bool usedWrapper = false;

    // Prefer a captured template for this building type: it carries the runtime
    // ids that make the engine run its script effect, which is what fully
    // initialises a newly created building.
    TypeTemplate* tpl = TemplateFind(btype);
    if (tpl && g_reqBuild) {
        // Replay the request exactly as the wrapper receives it. Feeding the whole
        // 0x70-byte command object instead (v145) made the engine fault, so the
        // captured head is kept only for analysis.
        alignas(16) uint8_t cmd[0x50];
        memcpy(cmd, tpl->bytes + kCmdHead, sizeof(cmd));
        *reinterpret_cast<uint32_t*>(cmd + 0x08) = owner;
        *reinterpret_cast<uint32_t*>(cmd + 0x10) = sid;
        *reinterpret_cast<void**>(cmd + 0x18) = btype;
        *reinterpret_cast<uint32_t*>(cmd + 0x38) =
            static_cast<uint32_t>(levels);
        *reinterpret_cast<uint32_t*>(cmd + 0x3C) = 1;
        *reinterpret_cast<uint32_t*>(cmd + 0x40) = 0xFFFFFFFFu;
        *reinterpret_cast<uint64_t*>(cmd + 0x48) = g_buildCtx;
        uint8_t* req = cmd;

        using ReqFn = void* (*)(void*, uint32_t, void*, uintptr_t);
        auto reqFn = reinterpret_cast<ReqFn>(g_reqBuild);
        t_inCall = 1;
        if (__builtin_setjmp(t_jmp) == 0) {
            reqFn(req, kBuildOpcode, nullptr, 0x3FFF);
            t_inCall = 0;
            usedWrapper = true;
            rc = 0;
            Log("build: used the captured template for this type");
        } else {
            t_inCall = 0;
            rc = -1;
            MarkBadBuild(btype);
            Log("build: template replay faulted - this type is now refused");
        }
    }

    if (!usedWrapper && g_wrapper) {
        // Rebuild the request object the UI would hand over, then call its slot 0.
        alignas(16) uint8_t request[0x50] = {0};
        *reinterpret_cast<uintptr_t*>(request + 0x00) =
            g_reqVtable ? g_reqVtable
                        : reinterpret_cast<uintptr_t>(g_base) + kRequestVtableRva;
        *reinterpret_cast<uint64_t*>(request + 0x08) = owner;   // country id
        *reinterpret_cast<uint64_t*>(request + 0x10) = sid;     // state id
        *reinterpret_cast<void**>(request + 0x18) = btype;
        memcpy(request + 0x20, paramBuf, 0x20);                 // param block
        *reinterpret_cast<uint32_t*>(request + 0x38) = levels;
        *reinterpret_cast<uint32_t*>(request + 0x3C) = 1;
        *reinterpret_cast<uint32_t*>(request + 0x40) = 0xFFFFFFFFu;
        *reinterpret_cast<uint64_t*>(request + 0x48) = g_buildCtx;

        // The UI's own path: req-build(request, opcode, 0, 0x3FFF). req-build then
        // dispatches through request->vtable[0] (the wrapper) and the engine runs
        // its script effect, which is what fully initialises a new building.
        using ReqFn = void* (*)(void*, uint32_t, void*, uintptr_t);
        auto req = reinterpret_cast<ReqFn>(g_reqBuild);
        t_inCall = 1;
        if (__builtin_setjmp(t_jmp) == 0) {
            req(request, kBuildOpcode, nullptr, 0x3FFF);
            t_inCall = 0;
            usedWrapper = true;
            rc = 0;
            Log("build: entered via req-build(opcode 0x%X) (request @ %p)",
                kBuildOpcode, request);
        } else {
            t_inCall = 0;
            rc = -1;
            MarkBadBuild(btype);
            Log("build: req-build path faulted - this type is now refused "
                "(the state probably cannot host it)");
        }
    }

    if (!usedWrapper) {
        t_inCall = 1;
        if (__builtin_setjmp(t_jmp) == 0) {
            rc = fn(paramBuf, country, state, btype, levels, 1, 0xFFFFFFFFu,
                    reinterpret_cast<void*>(g_buildCtx));
            t_inCall = 0;
        } else {
            rc = -1;
        }
    }
    int queuedNow = QueueMatches(country, sid, btype);
    void* bldAfter = BuildingOf(state, btype);
    Log("build: owner=%u existed=%d rc=%d queued=%d->%d level=%u->%u",
        owner, existed ? 1 : 0, static_cast<int>(rc), queuedBefore, queuedNow,
        levelBefore, BuildingLevel(bldAfter));

    // The first call on a fresh site creates the CBuilding object but does NOT
    // enqueue the construction item, so the queue is the only trustworthy signal.
    // A second call takes the "already queued" branch and registers it.
    if (queuedNow <= queuedBefore) {
        t_inCall = 1;
        if (__builtin_setjmp(t_jmp) == 0) {
            rc = fn(paramBuf, country, state, btype, levels, 1, 0xFFFFFFFFu,
                    reinterpret_cast<void*>(g_buildCtx));
            t_inCall = 0;
        } else {
            rc = -1;
        }
        queuedNow = QueueMatches(country, sid, btype);
        Log("build: retry rc=%d queued=%d->%d level=%u->%u",
            static_cast<int>(rc), queuedBefore, queuedNow,
            levelBefore, BuildingLevel(BuildingOf(state, btype)));
    }
    return queuedNow - queuedBefore;
}

// requests.txt:  <state_id> <building_key> <levels>
static void RunRequests() {
    if (GetFileAttributesA(pReq) == INVALID_FILE_ATTRIBUTES) return;
    FILE* f = nullptr;
    fopen_s(&f, pReq, "r");
    if (f) {
        char line[256];
        int ok = 0, fail = 0, denied = 0;
        while (fgets(line, sizeof(line), f)) {
            char* p = line;
            while (*p == ' ' || *p == '\t') ++p;
            if (!*p || *p == '\n' || *p == '\r' || *p == '#') continue;

            unsigned sid = 0;
            char name[128] = {0};
            int levels = 1;
            if (sscanf_s(p, "%u %127s %d", &sid, name,
                         static_cast<unsigned>(sizeof(name)), &levels) < 2) {
                Log("req: bad line: %.60s", p);
                ++fail;
                continue;
            }
            void* state = StateById(sid);
            void* btype = TypeByName(name);
            if (!state || !btype) {
                if (!btype) {
        Log("req: unknown building key '%s' (not in the type table)", name);
        SuggestKeys(name);
    } else {
        Log("req: state %u has no %s (the lookup returned the 'absent' sentinel)",
            sid, name);
    }
                ++fail;
                continue;
            }
            const int got = BuildOne(state, btype, levels);
            Log("req: %u %s x%d -> %d", sid, name, levels, got);
            if (got > 0) ++ok;
            else if (got < 0) ++denied;
            else ++fail;
        }
        fclose(f);
        Log("requests done: ok=%d fail=%d denied=%d", ok, fail, denied);
    }
    MoveFileExA(pReq, pReqDone, MOVEFILE_REPLACE_EXISTING);
}

// query.txt:  building <building_key>
static void RunQuery() {
    if (GetFileAttributesA(pQuery) == INVALID_FILE_ATTRIBUTES) return;

    char key[128] = {0};
    bool all = false;
    FILE* f = nullptr;
    fopen_s(&f, pQuery, "r");
    if (f) {
        char line[256];
        while (fgets(line, sizeof(line), f)) {
            char a[64] = {0}, b[128] = {0}, c[32] = {0};
            const int nf = sscanf_s(line, "%63s %127s %31s",
                                    a, static_cast<unsigned>(sizeof(a)),
                                    b, static_cast<unsigned>(sizeof(b)),
                                    c, static_cast<unsigned>(sizeof(c)));
            if (nf >= 2 && _stricmp(a, "building") == 0) {
                strncpy_s(key, b, _TRUNCATE);
                if (nf >= 3 && _stricmp(c, "all") == 0) all = true;
                break;
            }
        }
        fclose(f);
    }
    MoveFileExA(pQuery, pQueryDone, MOVEFILE_REPLACE_EXISTING);
    if (!key[0]) return;

    void* btype = TypeByName(key);
    Log("query: %s -> %p (scope=%s)", key, btype, all ? "ALL" : "player");
    if (!btype) return;

    auto* reg = g_regState ? *reinterpret_cast<void**>(g_regState) : nullptr;
    if (!IsReadable(reg, 0x30)) return;
    auto* rb = static_cast<uint8_t*>(reg);
    auto** entries = *reinterpret_cast<void***>(rb + lay::REG_DATA);
    const uint32_t count = *reinterpret_cast<uint32_t*>(rb + lay::REG_COUNT);
    if (!entries) return;

    void* countryOfFirst = nullptr;
    (void)countryOfFirst;

    FILE* out = nullptr;
    fopen_s(&out, pStates, "w");
    if (!out) return;
    fprintf(out, "state_id,owner,has_building\n");
    const uint32_t player = LivePlayerCountry();   // hoisted

    int written = 0, hits = 0;
    for (uint32_t id = 0; id < count; ++id) {
        void** slot = reinterpret_cast<void**>(
            reinterpret_cast<uint8_t*>(entries) + 16 * id + 8);
        if (!IsReadable(slot, 8)) continue;
        void* state = *slot;
        if (!HasVt(state, g_vtState, g_nState)) continue;

        const uint32_t owner = StateOwner(state);
        if (!all && (!player || owner != player)) continue;   // out of scope
        const bool has = BuildingOf(state, btype) != nullptr;
        if (has) ++hits;
        fprintf(out, "%u,%u,%d\n", id, owner, has ? 1 : 0);
        ++written;
    }
    fclose(out);
    Log("query: wrote %d states, %d have the building [scope=%s]",
        written, hits, all ? "ALL" : "player");
}

// ---------------------------------------------------------------------------
// Work pump
// ---------------------------------------------------------------------------
// `ready` is emitted when the DLL is usable for the world state that is current
// right now: immediately at the main menu, and only after the player id resolves
// inside a save. It is re-emitted on every world transition.
static char      g_lastDriver[24] = "none";

static void MaybeAnnounceReady() {
    static int   announced = 0;          // 0 none, 1 main menu, 2 in save
    static DWORD menuSince = 0;

    if (!g_inSave) {
        if (!menuSince) menuSince = GetTickCount();
        // Debounce: a save load briefly reports "no world".
        if (GetTickCount() - menuSince < 1500) return;
        if (announced == 1) return;
        announced = 1;
        Log("ready (types=%zu player=- driver=%s world=main-menu)",
            g_types.size(), g_lastDriver);
        return;
    }

    menuSince = 0;
    if (!g_playerCountry) return;        // hold the ready until the scan lands
    if (announced == 2) return;
    announced = 2;
    Log("ready (types=%zu player=%u driver=%s world=in-save)",
        g_types.size(), g_playerCountry, g_lastDriver);
}

static bool MemIsExecutable(const void* p) {
    MEMORY_BASIC_INFORMATION mbi{};
    if (VirtualQuery(p, &mbi, sizeof(mbi)) != sizeof(mbi)) return false;
    if (mbi.State != MEM_COMMIT) return false;
    return (mbi.Protect & (PAGE_EXECUTE | PAGE_EXECUTE_READ |
                           PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)) != 0;
}

// Production methods: the array must look like a small vector of readable
// pointers, and at least one entry must resolve to a "pm_" key. A shifted offset
// after an update shows up as a silly count or unreadable entries.
static bool CheckPmLayout(const char* why) {
    void* bld = nullptr;
    const uint32_t me = LivePlayerCountry();
    void* sampleType = TypeByName("building_urban_center");
    if (!sampleType) {
        // any key we happen to have; the check only needs one real building
        AcquireSRWLockShared(&g_typesLock);
        for (const auto& kv : g_types) {
            if (kv.first.rfind("building_", 0) == 0) { sampleType = kv.second; break; }
        }
        ReleaseSRWLockShared(&g_typesLock);
    }
    if (!sampleType) {
        Log("selfcheck: %s skipped (no building type available)", why);
        return true;
    }
    for (uint32_t sid = 0; sid < 4096 && !bld; ++sid) {
        void* st = StateById(sid);
        if (!st || !HasVt(st, g_vtState, g_nState)) continue;
        if (me && StateOwner(st) != me) continue;
        bld = BuildingOf(st, sampleType);
        if (bld) break;
    }
    if (!bld) {
        Log("selfcheck: %s skipped (no sample building found)", why);
        return true;                      // cannot test - do not disable
    }

    PmArray pms = BuildingPms(bld);
    if (!pms.data || pms.count < 1 || pms.count > 64) {
        Log("selfcheck: FAIL %s - active PM array count=%d", why, pms.count);
        return false;
    }
    int withKey = 0;
    for (int32_t i = 0; i < pms.count; ++i) {
        if (!IsReadable(&pms.data[i], sizeof(void*))) {
            Log("selfcheck: FAIL %s - active PM array entry %d unreadable", why, i);
            return false;
        }
        const char* k = KeyAt18(pms.data[i], "pm_");
        if (!k) k = KeyOf(pms.data[i], "pm_", 0x200);
        if (k) ++withKey;
    }
    if (withKey == 0) {
        Log("selfcheck: FAIL %s - no active PM resolved to a pm_ key", why);
        return false;
    }

    void* btype = TypeByName("building_urban_center");
    GroupArray groups = TypeGroups(btype);
    if (!groups.data || groups.count < 1 || groups.count > 32) {
        Log("selfcheck: FAIL %s - PM group count=%d", why, groups.count);
        return false;
    }
    Log("selfcheck: %s ok (active=%d groups=%d keys=%d)",
        why, pms.count, groups.count, withKey);
    return true;
}

// Construction: the command address must be executable and the context readable.
static bool CheckBuildLayout() {
    if (!g_buildCommand || !MemIsExecutable(reinterpret_cast<const void*>(g_buildCommand))) {
        Log("selfcheck: FAIL build - command address %p is not executable",
            reinterpret_cast<void*>(g_buildCommand));
        return false;
    }
    if (!g_buildCtx || !IsReadable(reinterpret_cast<void*>(g_buildCtx), 0x40)) {
        Log("selfcheck: FAIL build - context %p unreadable",
            reinterpret_cast<void*>(g_buildCtx));
        return false;
    }
    Log("selfcheck: build ok");
    return true;
}

// Queue: counts must be sane and the element array readable.
static bool CheckQueueLayout() {
    const uint32_t me = LivePlayerCountry();
    void* country = me ? CountryById(me) : nullptr;
    if (!country || !HasVt(country, g_vtCountry, g_nCountry)) {
        Log("selfcheck: queue skipped (country unresolved)");
        return true;
    }
    for (size_t off : { q::GOV, q::PRIV }) {
        QueueView v = GetQueue(country, off);
        if (v.count < 0 || v.count > 4096) {
            Log("selfcheck: FAIL queue - count=%d at +0x%zX", v.count, off);
            return false;
        }
    }
    Log("selfcheck: queue ok");
    return true;
}

// Type table: the keys must look like building keys.
static bool CheckTypes() {
    if (g_types.size() < 100) {
        Log("selfcheck: FAIL types - only %zu entries", g_types.size());
        return false;
    }
    int named = 0;
    for (const auto& kv : g_types) {
        if (named >= 8) break;
        if (kv.first.rfind("building_", 0) == 0) ++named;
    }
    if (named < 3) {
        Log("selfcheck: FAIL types - keys do not look like building keys");
        return false;
    }
    Log("selfcheck: types ok (%zu entries)", g_types.size());
    return true;
}

// Player: only a range check plus "the id owns at least one state". Deliberately
// no registry/offset assumptions here, because a wrong assumption would disable
// the feature that everything else depends on.
static bool CheckPlayer() {
    const uint32_t id = LivePlayerCountry();
    if (!id) {
        Log("selfcheck: player pending (id not resolved yet)");
        return false;                     // not a failure - retried later
    }
    int owned = 0;
    const uint32_t cap = RegistryCapacity();
    for (uint32_t sid = 0; sid < cap && owned < 1; ++sid) {
        void* st = StateById(sid);
        if (st && HasVt(st, g_vtState, g_nState) && StateOwner(st) == id) ++owned;
    }
    if (!owned) {
        Log("selfcheck: WARN player - id %u owns no state (observers are fine)", id);
    } else {
        Log("selfcheck: player ok (id=%u, owns states)", id);
    }
    return true;
}

static void RunSelfCheck() {
    if (g_selfChecked || !g_typesReady) return;

    const bool types = CheckTypes();
    if (!types) { g_capBuild = g_capPm = g_capQueue = false; }

    if (!CheckPlayer()) return;           // wait until the id is known
    g_selfChecked = true;

    g_capBuild = types && CheckBuildLayout() && g_buildCommand && g_buildCtx;
    g_capPm    = types && CheckPmLayout("pm");
    g_capQueue = types && CheckQueueLayout();

    g_capPlayer = (LivePlayerCountry() != 0);
    Log("capabilities: build=%s pm=%s player=%s queue=%s",
        g_capBuild ? "on" : "OFF", g_capPm ? "on" : "OFF",
        g_capPlayer ? "on" : "OFF", g_capQueue ? "on" : "OFF");
}


// bdump.txt: <state_id> <building_key>  ->  bdump.csv
// Dumps the CBuilding object byte by byte. A building created by our direct
// BuildCommand call and one created normally can then be diffed field by field:
// the engine rebuilds its runtime indices on load, which is why a reloaded save
// makes the former usable.
static void RunBDump() {
    if (GetFileAttributesA(pBDump) == INVALID_FILE_ATTRIBUTES) return;

    unsigned sid = 0;
    char key[128] = {0};
    FILE* f = nullptr;
    fopen_s(&f, pBDump, "r");
    if (f) {
        char line[256];
        while (fgets(line, sizeof(line), f)) {
            if (sscanf_s(line, "%u %127s", &sid, key,
                         static_cast<unsigned>(sizeof(key))) >= 2) break;
        }
        fclose(f);
    }
    MoveFileExA(pBDump, pBDumpDone, MOVEFILE_REPLACE_EXISTING);
    if (!key[0]) return;

    void* btype = TypeByName(key);
    void* state = StateById(sid);
    Log("bdump: state=%u key=%s btype=%p state=%p", sid, key, btype, state);
    if (!btype || !state) return;

    void* bld = BuildingOf(state, btype);
    Log("bdump: building = %p", bld);

    FILE* out = nullptr;
    fopen_s(&out, pBDumpOut, "w");
    if (!out) return;

    if (!bld) {
        fprintf(out, "state,building,offset,qword\n");
        fclose(out);
        Log("bdump: no building object for that state/type");
        return;
    }

    const auto* b = static_cast<const uint8_t*>(bld);
    fprintf(out, "state=%u,building=%s,addr=%p\n", sid, key, bld);
    for (int off = 0; off < 0x200; off += 8) {
        if (!IsReadable(b + off, 8)) {
            fprintf(out, "0x%03X,<unreadable>\n", off);
            break;
        }
        const uint64_t v = *reinterpret_cast<const uint64_t*>(b + off);
        fprintf(out, "0x%03X,0x%016llX\n", off,
                static_cast<unsigned long long>(v));
    }
    fclose(out);
    Log("bdump: 0x200 bytes -> bdump.csv (level +0x%X = %u)",
        static_cast<unsigned>(0xE0),
        IsReadable(b + 0xE0, 4) ? *reinterpret_cast<const uint32_t*>(b + 0xE0) : 0u);
}


// findptr.txt: <hex pointer>  ->  findptr.csv
// Reports every writable data section slot that holds that value, as an RVA.
// Used once to turn a live heap pointer (the building manager) into a stable
// module-relative anchor.
static void RunFindPtr() {
    if (GetFileAttributesA(pFindPtr) == INVALID_FILE_ATTRIBUTES) return;

    unsigned long long want = 0;
    FILE* f = nullptr;
    fopen_s(&f, pFindPtr, "r");
    if (f) {
        char line[128];
        while (fgets(line, sizeof(line), f)) {
            if (sscanf_s(line, "%llx", &want) == 1 && want) break;
        }
        fclose(f);
    }
    MoveFileExA(pFindPtr, pFindPtrDone, MOVEFILE_REPLACE_EXISTING);
    if (!want) return;

    FILE* out = nullptr;
    fopen_s(&out, pFindPtrOut, "w");
    if (out) fprintf(out, "rva,section,value\n");

    int hits = 0;
    auto* base = static_cast<uint8_t*>(g_base);
    MEMORY_BASIC_INFORMATION mbi{};
    uint8_t* addr = base;
    const uint8_t* limit = base + 0x8000000;      // whole image
    while (addr < limit && VirtualQuery(addr, &mbi, sizeof(mbi)) == sizeof(mbi)) {
        auto* regionBase = static_cast<uint8_t*>(mbi.BaseAddress);
        const size_t size = mbi.RegionSize;
        addr = regionBase + size;
        if (mbi.State != MEM_COMMIT) continue;
        if (!(mbi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY)))
            continue;
        if (mbi.Protect & PAGE_GUARD) continue;

        const size_t n = size / sizeof(uint64_t);
        auto* q = reinterpret_cast<const uint64_t*>(regionBase);
        for (size_t i = 0; i < n; ++i) {
            if (q[i] != want) continue;
            const uintptr_t rva = reinterpret_cast<uintptr_t>(regionBase + i * 8) -
                                  reinterpret_cast<uintptr_t>(base);
            if (rva >= 0x8000000) continue;
            ++hits;
            if (out) fprintf(out, "0x%llX,?,0x%llX\n",
                             static_cast<unsigned long long>(rva),
                             static_cast<unsigned long long>(want));
            if (hits <= 12)
                Log("findptr: hit at rva 0x%llX", static_cast<unsigned long long>(rva));
        }
    }
    if (out) fclose(out);
    Log("findptr: %d slot(s) hold 0x%llX", hits,
        static_cast<unsigned long long>(want));
}


// peek.txt: <hexaddr> <count>   ->  peek.csv (offset,qword,as_ptr,first bytes)
// Read-only inspection helper: lets the layout be explored step by step
// (manager -> chunk array -> chunk -> slot) without another research tool.
static void RunPeek() {
    if (GetFileAttributesA(pPeek) == INVALID_FILE_ATTRIBUTES) return;

    unsigned long long addr = 0;
    int count = 16;
    FILE* f = nullptr;
    fopen_s(&f, pPeek, "r");
    if (f) {
        char line[128];
        while (fgets(line, sizeof(line), f)) {
            if (sscanf_s(line, "%llx %d", &addr, &count) >= 1) break;
        }
        fclose(f);
    }
    MoveFileExA(pPeek, pPeekDone, MOVEFILE_REPLACE_EXISTING);
    if (!addr) return;
    if (count <= 0 || count > 4096) count = 16;

    FILE* out = nullptr;
    fopen_s(&out, pPeekOut, "w");
    if (out) fprintf(out, "offset,qword,as_ptr,bytes\n");

    auto* p = reinterpret_cast<const uint8_t*>(static_cast<uintptr_t>(addr));
    for (int i = 0; i < count; ++i) {
        const uint8_t* at = p + i * 8;
        if (!IsReadable(at, 8)) {
            if (out) fprintf(out, "0x%X,<unreadable>,,\n", i * 8);
            break;
        }
        const uint64_t v = *reinterpret_cast<const uint64_t*>(at);
        char bytes[40] = {0};
        if (IsReadable(at, 8)) {
            for (int b = 0; b < 8 && b < 8; ++b) {
                const unsigned char ch = at[b];
                bytes[b * 3] = (ch >= 0x20 && ch < 0x7F) ? static_cast<char>(ch) : '.';
                bytes[b * 3 + 1] = ' ';
                bytes[b * 3 + 2] = 0;
            }
        }
        if (out) fprintf(out, "0x%X,0x%016llX,0x%llX,%s\n", i * 8,
                         static_cast<unsigned long long>(v),
                         static_cast<unsigned long long>(v), bytes);
    }
    if (out) fclose(out);
    Log("peek: %d qword(s) at 0x%llX -> peek.csv", count,
        static_cast<unsigned long long>(addr));
}


// watch.txt: <state> <building_key>
//
// Arms two write breakpoints on that building: +0xE0 (the 0xFFFF sentinel) and
// +0x138 (the field a save reload fills in). If anything assigns them at runtime
// we catch the writer; if nothing does, the assignment is load-only.
static void WatchLog(const char* fmt, ...) {
    FILE* f = nullptr;
    fopen_s(&f, pProbeOut, "a");
    if (!f) return;
    SYSTEMTIME st{};
    GetLocalTime(&st);
    fprintf(f, "[%02d:%02d:%02d.%03d] WATCH ", st.wHour, st.wMinute, st.wSecond,
            st.wMilliseconds);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static bool WatchArmThread(DWORD tid, bool arm, uintptr_t a, uintptr_t b) {
    const bool self = (tid == GetCurrentThreadId());
    CONTEXT c{};
    c.ContextFlags = CONTEXT_DEBUG_REGISTERS;
    HANDLE th = nullptr;

    if (!self) {
        th = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                        THREAD_SET_CONTEXT, FALSE, tid);
        if (!th) return false;
        SuspendThread(th);
        if (!GetThreadContext(th, &c)) {
            ResumeThread(th);
            CloseHandle(th);
            return false;
        }
    }

    if (arm) {
        c.Dr0 = static_cast<DWORD64>(a);   // +0xE0, 4 bytes, write
        c.Dr1 = static_cast<DWORD64>(b);   // +0x138, 8 bytes, write
        // L0|L1 = 1, RW0=RW1=01 (write), LEN0=LEN1=11 (4 / 8 bytes)
        c.Dr7 = 0x1D0005ULL;
    } else {
        c.Dr0 = c.Dr1 = 0;
        c.Dr7 = 0;
    }
    c.Dr6 = 0;

    bool ok;
    if (self) {
        ok = SetThreadContext(GetCurrentThread(), &c) != 0;
    } else {
        ok = SetThreadContext(th, &c) != 0;
        ResumeThread(th);
        CloseHandle(th);
    }
    return ok;
}

static int WatchArmAll(bool arm) {
    int done = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    const DWORD me = GetCurrentProcessId();
    THREADENTRY32 te{};
    te.dwSize = sizeof(te);
    if (Thread32First(snap, &te)) {
        do {
            if (te.th32OwnerProcessID != me) continue;
            if (WatchArmThread(te.th32ThreadID, arm, g_watchA, g_watchB)) ++done;
            if (done >= 64) break;
        } while (Thread32Next(snap, &te));
    }
    CloseHandle(snap);
    return done;
}

static void WatchTick() {
    static DWORD lastCheck = 0;
    const DWORD now = GetTickCount();
    if (lastCheck && now - lastCheck < 1000) return;
    lastCheck = now;

    bool want = GetFileAttributesA(pWatch) != INVALID_FILE_ATTRIBUTES;
    const bool armed = (g_watchArmed != 0);
    if (armed && g_watchUntil && GetTickCount() > g_watchUntil) {
        Log("watch: auto-disarm timer expired");
        want = false;
    }
    if (want == armed) return;

    if (want) {
        unsigned sid = 0;
        char key[128] = {0};
        FILE* f = nullptr;
        fopen_s(&f, pWatch, "r");
        if (f) {
            char line[256];
            while (fgets(line, sizeof(line), f))
                if (sscanf_s(line, "%u %127s", &sid, key,
                             static_cast<unsigned>(sizeof(key))) >= 2) break;
            fclose(f);
        }

        // "queue" mode: watch the construction queue counters of the player's
        // country. Adding an order increments them, so the write that fires is
        // the engine's own "queue a construction order" routine - the step our
        // direct BuildCommand call skips (and the reason freshly built buildings
        // come out with only one product method).
        if (sid == 0 && _stricmp(key, "queue") == 0) {
            const uint32_t owner = LivePlayerCountry();
            void* country = owner ? CountryById(owner) : nullptr;
            if (!country) {
                Log("watch: queue mode - player country unresolved");
                return;
            }
            auto* c = static_cast<uint8_t*>(country);
            // Queue layout (verified): container + 0x60 data, +0x68 capacity,
            // +0x6C count. 0xB68 was the capacity - watching it never fires.
            g_watchA = reinterpret_cast<uintptr_t>(c + 0xB6C);   // government count
            g_watchB = reinterpret_cast<uintptr_t>(c + 0xBE4);   // private count
            InterlockedExchange(&g_watchHits, 0);
            const int nq = WatchArmAll(true);
            if (nq > 0) {
                InterlockedExchange(&g_watchArmed, 1);
                g_watchUntil = GetTickCount() + 300 * 1000;
                Log("watch: queue mode armed on %d thread(s), country %p "
                    "(gov count=0x%llX private count=0x%llX)",
                    nq, country,
                    static_cast<unsigned long long>(g_watchA),
                    static_cast<unsigned long long>(g_watchB));
                WatchLog("queue watch: gov=0x%llX private=0x%llX gov_count=%d "
                         "private_count=%d",
                         static_cast<unsigned long long>(g_watchA),
                         static_cast<unsigned long long>(g_watchB),
                         IsReadable(c + 0xB6C, 4)
                             ? *reinterpret_cast<int32_t*>(c + 0xB6C) : -1,
                         IsReadable(c + 0xBE4, 4)
                             ? *reinterpret_cast<int32_t*>(c + 0xBE4) : -1);
            } else {
                Log("watch: queue mode - could not arm");
            }
            return;
        }
        // NOTE: the control file must stay in place. Consuming it here made
        // the next poll see "no file" and disarm after one second, which
        // produced a silent false negative. It is disarmed by deleting it.
        // Consume the file and stop retrying: leaving it in place made the pump
        // re-run these lookups every second, and during a world transition those
        // pointers are stale - which crashed the game.
        MoveFileExA(pWatch, pWatchDone, MOVEFILE_REPLACE_EXISTING);

        void* btype = nullptr;
        void* state = nullptr;
        void* bld = nullptr;
        t_inCall = 1;
        if (__builtin_setjmp(t_jmp) == 0) {
            btype = key[0] ? TypeByName(key) : nullptr;
            state = StateById(sid);
            bld = (btype && state) ? BuildingOf(state, btype) : nullptr;
            t_inCall = 0;
        } else {
            t_inCall = 0;
            Log("watch: lookup faulted (world probably changed) - ignored");
            return;
        }
        if (!bld) {
            Log("watch: no building for state=%u key=%s", sid, key);
            return;
        }
        auto* b = static_cast<uint8_t*>(bld);
        // Watch the active product-method vector: its data pointer and its count.
        // Changing a PM in the UI must write both, and whatever code does it is
        // the engine's own "set product method" routine.
        g_watchA = reinterpret_cast<uintptr_t>(b + 0x1E8);   // 8 bytes, pointer
        g_watchB = reinterpret_cast<uintptr_t>(b + 0x1F4);   // 4 bytes, count
        InterlockedExchange(&g_watchHits, 0);

        int seconds = 60;
        {
            FILE* f = nullptr;
            fopen_s(&f, pWatch, "r");
            if (f) {
                char line[256];
                while (fgets(line, sizeof(line), f)) {
                    unsigned s2 = 0;
                    char k2[128] = {0};
                    int sec = 0;
                    if (sscanf_s(line, "%u %127s %d", &s2, k2,
                                 static_cast<unsigned>(sizeof(k2)), &sec) >= 3 &&
                        sec > 0 && sec <= 600)
                        seconds = sec;
                    break;
                }
                fclose(f);
            }
        }

        const int n = WatchArmAll(true);
        if (n > 0) {
            InterlockedExchange(&g_watchArmed, 1);
            g_watchUntil = GetTickCount() + static_cast<DWORD>(seconds) * 1000;

            // Self test: write the same value back. If the breakpoint is armed
            // correctly the VEH must report exactly this write; if it does not,
            // the experiment would be a silent false negative again.
            const uint64_t before = *reinterpret_cast<uint64_t*>(b + 0xE0);
            *reinterpret_cast<volatile uint64_t*>(b + 0xE0) = before;
            Log("watch: self-test wrote +0xE0 back, seen=%ld (expected 1)",
                static_cast<long>(g_watchHits));
            Log("watch: armed on %d thread(s) for building %p (+0xE0 / +0x138)", n,
                bld);
            WatchLog("auto-disarm in %d s", seconds);
            WatchLog("armed: PM array=0x%llX count=0x%llX",
                     static_cast<unsigned long long>(g_watchA),
                     static_cast<unsigned long long>(g_watchB));
            WatchLog("  PM data = 0x%llX  cap/count = 0x%llX",
                     static_cast<unsigned long long>(
                         *reinterpret_cast<uint64_t*>(b + 0x1E8)),
                     static_cast<unsigned long long>(
                         *reinterpret_cast<uint64_t*>(b + 0x1F0)));
        } else {
            Log("watch: could not arm any thread");
        }
    } else {
        const int n = WatchArmAll(false);
        InterlockedExchange(&g_watchArmed, 0);
        Log("watch: disarmed on %d thread(s), %ld write(s) seen", n,
            static_cast<long>(g_watchHits));
    }
}


// pmfix.txt: <dst_state> <building_key> <src_state> [dry]
//
// A building created by calling BuildCommand directly has only the first product
// method initialised (its vector has capacity 1). A reload fixes it because the
// queue replay initialises all groups. This rebuilds the vector from a reference
// building of the same type, allocating through the GAME's own allocator - the
// one the engine uses for this very vector (building + 0x1F8 -> vtable). Memory
// therefore comes from the same pool and stays valid for the engine.
static void RunPmFix() {
    if (GetFileAttributesA(pPmFix) == INVALID_FILE_ATTRIBUTES) return;

    unsigned dstId = 0, srcId = 0;
    char key[128] = {0}, extra[32] = {0};
    FILE* f = nullptr;
    fopen_s(&f, pPmFix, "r");
    if (f) {
        char line[256];
        while (fgets(line, sizeof(line), f)) {
            int n = sscanf_s(line, "%u %127s %u %31s", &dstId, key,
                             static_cast<unsigned>(sizeof(key)), &srcId, extra,
                             static_cast<unsigned>(sizeof(extra)));
            if (n >= 3) break;
            key[0] = 0;
        }
        fclose(f);
    }
    MoveFileExA(pPmFix, pPmFixDone, MOVEFILE_REPLACE_EXISTING);
    if (!key[0]) return;

    const bool dry = (extra[0] && _stricmp(extra, "dry") == 0);

    void* btype = TypeByName(key);
    void* dstState = StateById(dstId);
    void* srcState = StateById(srcId);
    if (!btype || !dstState || !srcState) {
        Log("pmfix: bad refs (btype=%p dst=%p src=%p)", btype, dstState, srcState);
        return;
    }
    void* dst = BuildingOf(dstState, btype);
    void* src = BuildingOf(srcState, btype);
    if (!dst || !src) {
        Log("pmfix: missing building (dst=%p src=%p)", dst, src);
        return;
    }
    // The type pointer sits at +0xF0 (the constructor stores its argument there and
    // both dumped buildings agree). lay::BLD_TYPE (0xB0) is a different field, so
    // test 0xF0 first and only fall back to 0xB0.
    auto typeAt = [](void* b) -> void* {
        auto* p = static_cast<uint8_t*>(b);
        if (!IsReadable(p + 0xF0, 8)) return nullptr;
        return *reinterpret_cast<void**>(p + 0xF0);
    };
    auto typeAtAlt = [](void* b) -> void* {
        auto* p = static_cast<uint8_t*>(b);
        if (!IsReadable(p + lay::BLD_TYPE, 8)) return nullptr;
        return *reinterpret_cast<void**>(p + lay::BLD_TYPE);
    };
    void* st = typeAt(src);
    void* dt = typeAt(dst);
    Log("pmfix: type check src[+0xF0]=%p dst[+0xF0]=%p (btype=%p) "
        "alt src[+0xB0]=%p dst[+0xB0]=%p", st, dt, btype, typeAtAlt(src),
        typeAtAlt(dst));
    const bool ok = (st == btype && dt == btype) ||
                    (typeAtAlt(src) == btype && typeAtAlt(dst) == btype);
    if (!ok) {
        Log("pmfix: type mismatch, refusing");
        return;
    }

    PmArray sv = BuildingPms(src);
    if (!sv.data || sv.count <= 0 || sv.count > 32) {
        Log("pmfix: source PM array unusable (count=%d)", sv.count);
        return;
    }

    auto* d = static_cast<uint8_t*>(dst);
    void** dstData = *reinterpret_cast<void***>(d + lay::BLD_PMS);
    const int32_t dstCap = *reinterpret_cast<int32_t*>(d + lay::BLD_PMS + 8);
    const int32_t dstCnt = *reinterpret_cast<int32_t*>(d + lay::BLD_PMCNT);

    Log("pmfix: dst count=%d cap=%d data=%p | src count=%d data=%p",
        dstCnt, dstCap, dstData, sv.count, sv.data);

    FILE* out = nullptr;
    fopen_s(&out, pPmFixOut, "w");
    if (out) fprintf(out, "slot,pm\n");
    for (int i = 0; i < sv.count; ++i) {
        const char* k = KeyAt18(sv.data[i], "pm_");
        if (!k) k = KeyOf(sv.data[i], "pm_", 0x200);
        if (out) fprintf(out, "%d,%s\n", i, k ? k : "?");
        Log("pmfix:   src[%d] = %p %s", i, sv.data[i], k ? k : "?");
    }

    if (dry) {
        if (out) fclose(out);
        Log("pmfix: dry run, nothing written");
        return;
    }

    void** target = dstData;
    if (dstCap < sv.count || !dstData) {
        void* allocator = *reinterpret_cast<void**>(d + 0x1F8);
        if (!IsReadable(allocator, 0x10)) {
            Log("pmfix: allocator at +0x1F8 unreadable");
            if (out) fclose(out);
            return;
        }
        // The engine allocates this vector with vtable[1](allocator, bytes, 8).
        using AllocFn = void* (*)(void*, size_t, size_t);
        auto vtbl = *reinterpret_cast<void***>(allocator);
        auto alloc = reinterpret_cast<AllocFn>(vtbl[1]);
        void* buf = nullptr;
        t_inCall = 1;
        if (__builtin_setjmp(t_jmp) == 0) {
            buf = alloc(allocator, sizeof(void*) * static_cast<size_t>(sv.count), 8);
            t_inCall = 0;
        } else {
            t_inCall = 0;
            buf = nullptr;
        }
        if (!buf) {
            Log("pmfix: allocation failed");
            if (out) fclose(out);
            return;
        }
        Log("pmfix: allocated %p via game allocator %p (%d pointers)",
            buf, allocator, sv.count);
        target = static_cast<void**>(buf);
        *reinterpret_cast<void***>(d + lay::BLD_PMS) = target;
        *reinterpret_cast<int32_t*>(d + lay::BLD_PMS + 8) = sv.count;
    }

    for (int i = 0; i < sv.count; ++i) target[i] = sv.data[i];
    *reinterpret_cast<int32_t*>(d + lay::BLD_PMCNT) = sv.count;

    Log("pmfix: done, count=%d data=%p (level +0xE8 = %u)", sv.count, target,
        *reinterpret_cast<uint32_t*>(d + 0xE8));
    if (out) fclose(out);
}


// usecap.txt: <state_id> <building_key> [levels]
// Replays the last captured request object against another state/building, keeping
// its runtime ids so the engine's script effect runs.
static void RunUseCap() {
    if (GetFileAttributesA(pUseCap) == INVALID_FILE_ATTRIBUTES) return;

    unsigned sid = 0;
    int levels = 1;
    char key[128] = {0};
    FILE* f = nullptr;
    fopen_s(&f, pUseCap, "r");
    if (f) {
        char line[256];
        while (fgets(line, sizeof(line), f))
            if (sscanf_s(line, "%u %127s %d", &sid, key,
                         static_cast<unsigned>(sizeof(key)), &levels) >= 2) break;
        fclose(f);
    }
    MoveFileExA(pUseCap, pUseCapDone, MOVEFILE_REPLACE_EXISTING);
    if (!sid || !key[0]) return;

    if (!g_capReqValid) {
        Log("usecap: no request captured yet - click build once with the probe on");
        return;
    }
    if (!g_reqBuild) {
        Log("usecap: req-build unavailable");
        return;
    }
    void* btype = TypeByName(key);
    void* state = StateById(sid);
    if (!btype || !state) {
        Log("usecap: bad refs (btype=%p state=%p)", btype, state);
        return;
    }
    const uint32_t owner = LivePlayerCountry();
    if (!owner || !LockWrite(state, "usecap", sid)) {
        Log("usecap: state %u not yours", sid);
        return;
    }

    alignas(16) uint8_t cmd[0x50];
    memcpy(cmd, g_capReq + kCmdHead, sizeof(cmd));
    *reinterpret_cast<uint32_t*>(cmd + 0x08) = owner;
    *reinterpret_cast<uint32_t*>(cmd + 0x10) = sid;
    *reinterpret_cast<void**>(cmd + 0x18) = btype;
    *reinterpret_cast<uint32_t*>(cmd + 0x38) =
        static_cast<uint32_t>(levels);
    *reinterpret_cast<uint32_t*>(cmd + 0x3C) = 1;
    *reinterpret_cast<uint32_t*>(cmd + 0x40) = 0xFFFFFFFFu;
    *reinterpret_cast<uint64_t*>(cmd + 0x48) = g_buildCtx;
    uint8_t* req = cmd;

    Log("usecap: replay -> state=%u key=%s levels=%d (vtable=0x%llX)", sid, key,
        levels, static_cast<unsigned long long>(
            *reinterpret_cast<uint64_t*>(req)));

    using ReqFn = void* (*)(void*, uint32_t, void*, uintptr_t);
    auto reqFn = reinterpret_cast<ReqFn>(g_reqBuild);
    t_inCall = 1;
    if (__builtin_setjmp(t_jmp) == 0) {
        reqFn(req, kBuildOpcode, nullptr, 0x3FFF);
        t_inCall = 0;
        Log("usecap: replay returned without fault");
    } else {
        t_inCall = 0;
        Log("usecap: replay FAULTED");
    }
}

// Written from the pump thread only.
static void FlushTemplates() {
    if (InterlockedExchange(&g_tplDirty, 0) == 0) return;
    AcquireSRWLockShared(&g_tplLock);
    const LONG n = g_nTpl;
    for (LONG i = 0; i < n; ++i) {
        if (!g_tpl[i].valid) continue;
        char path[MAX_PATH];
        snprintf(path, sizeof(path), "%s\\tpl_%llX.txt", pTplDir,
                 static_cast<unsigned long long>(
                     reinterpret_cast<uintptr_t>(g_tpl[i].btype)));
        FILE* f = nullptr;
        fopen_s(&f, path, "w");
        if (!f) continue;
        for (int k = 0; k < 0x70; k += 8)
            fprintf(f, "%016llX\n",
                    static_cast<unsigned long long>(
                        *reinterpret_cast<const uint64_t*>(g_tpl[i].bytes + k)));
        fclose(f);
        Log("template: wrote %s", path);
    }
    ReleaseSRWLockShared(&g_tplLock);
}

static void PumpWork() {
    if (g_unloading || !g_ready) return;
    if (InterlockedExchange(&g_pumping, 1) != 0) return;

    RunWorld();
    ProbeTick();
    DumpRegistryOnce();
    MaybeAnnounceReady();

    if (!WorldSettled()) {
        static DWORD lastNote = 0;
        const DWORD n = GetTickCount();
        if (!lastNote || n - lastNote > 5000) {
            lastNote = n;
            Log("world: settling after a transition - holding engine work");
        }
        InterlockedExchange(&g_pumping, 0);
        return;
    }

    if (!g_inSave) {
        g_deferSince = 0;             // nothing is being deferred outside a save
        InterlockedExchange(&g_pumping, 0);
        return;                       // main menu: nothing world-scoped is valid
    }

    RunPlayer();

    // Hold every command while the player object is being (re)resolved. A command
    // executed with a stale or unknown id could act on the wrong country, so it is
    // better to let the control file sit on disk and run it a moment later. The
    // 30 s cap keeps a permanently unresolvable state from stalling everything -
    // after that the lock takes over and refuses the writes.
    const bool resolved = LivePlayerCountry() != 0;
    if (!resolved || g_playerScanning) {
        if (!g_deferSince) {
            g_deferSince = GetTickCount();
            Log("defer: holding commands until the player object resolves");
        }
        if (GetTickCount() - g_deferSince < 30000) {
            InterlockedExchange(&g_pumping, 0);
            return;
        }
        if (g_deferSince) {
            Log("defer: 30 s without resolving - processing anyway (writes will be refused)");
            g_deferSince = 0;
        }
    } else if (g_deferSince) {
        g_deferSince = 0;
        Log("defer: resumed");
    }

    if (!g_typesReady && WorldSettled()) {
        // Types exist from game start (the database is built from game files), so
        // this runs at the main menu too - no need to wait for a save. It must not
        // start mid-transition, where the heap is being rebuilt.
        const DWORD now = GetTickCount();
        if (!g_lastTypeScan || now - g_lastTypeScan > 5000) {
            g_lastTypeScan = now;
            RequestTypeScan();
        }
    }

    if (g_typesReady && !g_selfChecked) RunSelfCheck();

    if (g_typesReady) {
        RunQueueDump();
        RunRequests();
        RunQuery();
        RunPmScan();
        RunPmProbe();
        RunPmWrite();
        RunBDump();
        RunFindPtr();
        RunPeek();
        RunPmFix();
        RunUseCap();
        FlushTemplates();
        WatchTick();
    }

    InterlockedExchange(&g_pumping, 0);
}

// ---------------------------------------------------------------------------
// Driver: several message APIs + a WM_NULL nudge
// ---------------------------------------------------------------------------
using FnPeek    = BOOL (WINAPI*)(LPMSG, HWND, UINT, UINT, UINT);
using FnGetMsg  = BOOL (WINAPI*)(LPMSG, HWND, UINT, UINT);
using FnMsgWait = DWORD (WINAPI*)(DWORD, const HANDLE*, DWORD, DWORD, DWORD);

static FnPeek    g_origPeek = nullptr;
static FnGetMsg  g_origGet = nullptr;
static FnMsgWait g_origMsgWait = nullptr;
static void*     g_iatSlot[4] = {0};
static void*     g_iatOrig[4] = {0};
static int       g_iatCount = 0;
static void*     g_iatHook[4] = {0};   // our detour addresses, for a re-install

static void CheckUnloadFlag();   // fwd

static void Tick(const char* who) {
    if (g_unloading) return;
    if (g_mainThreadId && GetCurrentThreadId() != g_mainThreadId) return;
    strncpy_s(g_lastDriver, who, _TRUNCATE);
    CheckUnloadFlag();           // runs regardless of readiness
    PumpWork();
}

static BOOL WINAPI HookPeek(LPMSG m, HWND h, UINT a, UINT b, UINT c) {
    InterlockedIncrement(&g_inHook);
    Tick("PeekMessageW");
    InterlockedDecrement(&g_inHook);
    return g_origPeek(m, h, a, b, c);
}

static BOOL WINAPI HookGet(LPMSG m, HWND h, UINT a, UINT b) {
    InterlockedIncrement(&g_inHook);
    Tick("GetMessageW");
    const BOOL r = g_origGet(m, h, a, b);
    Tick("GetMessageW+");
    InterlockedDecrement(&g_inHook);
    return r;
}

static DWORD WINAPI HookMsgWait(DWORD n, const HANDLE* hs, DWORD ms, DWORD mask, DWORD fl) {
    InterlockedIncrement(&g_inHook);
    Tick("MsgWaitEx");
    const DWORD r = g_origMsgWait(n, hs, ms, mask, fl);
    Tick("MsgWaitEx+");
    InterlockedDecrement(&g_inHook);
    return r;
}

struct Target { const char* name; void* detour; void** orig; };

static bool HookMessageApis(HMODULE mod) {
    auto* base = reinterpret_cast<uint8_t*>(mod);
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    auto* nt  = reinterpret_cast<IMAGE_NT_HEADERS*>(base + dos->e_lfanew);
    auto& dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress) return false;

    Target targets[] = {
        { "PeekMessageW",  reinterpret_cast<void*>(&HookPeek),
          reinterpret_cast<void**>(&g_origPeek) },
        { "GetMessageW",   reinterpret_cast<void*>(&HookGet),
          reinterpret_cast<void**>(&g_origGet) },
        { "MsgWaitForMultipleObjectsEx", reinterpret_cast<void*>(&HookMsgWait),
          reinterpret_cast<void**>(&g_origMsgWait) },
    };
    const int nTargets = static_cast<int>(sizeof(targets) / sizeof(targets[0]));
    int hooked = 0;

    auto* imp = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR*>(base + dir.VirtualAddress);
    for (; imp->Name; ++imp) {
        const char* dll = reinterpret_cast<const char*>(base + imp->Name);
        char low[MAX_PATH];
        size_t i = 0;
        for (; dll[i] && i < MAX_PATH - 1; ++i)
            low[i] = static_cast<char>(tolower(static_cast<unsigned char>(dll[i])));
        low[i] = 0;
        if (strcmp(low, "user32.dll") != 0) continue;

        auto* thunk = reinterpret_cast<IMAGE_THUNK_DATA*>(base + imp->FirstThunk);
        auto* orig  = reinterpret_cast<IMAGE_THUNK_DATA*>(base + imp->OriginalFirstThunk);
        for (; thunk->u1.Function; ++thunk, ++orig) {
            if (!orig || !orig->u1.AddressOfData) continue;
            auto* byName = reinterpret_cast<IMAGE_IMPORT_BY_NAME*>(base + orig->u1.AddressOfData);
            const char* fname = reinterpret_cast<const char*>(byName->Name);
            for (int k = 0; k < nTargets; ++k) {
                if (strcmp(fname, targets[k].name) != 0) continue;
                if (g_iatCount >= 4) break;
                DWORD old = 0;
                VirtualProtect(&thunk->u1.Function, sizeof(void*), PAGE_READWRITE, &old);
                *targets[k].orig = reinterpret_cast<void*>(thunk->u1.Function);
                g_iatSlot[g_iatCount] = &thunk->u1.Function;
                g_iatOrig[g_iatCount] = *targets[k].orig;
                g_iatHook[g_iatCount] = targets[k].detour;
                ++g_iatCount;
                thunk->u1.Function = reinterpret_cast<ULONG_PTR>(targets[k].detour);
                VirtualProtect(&thunk->u1.Function, sizeof(void*), old, &old);
                Log("driver: hooked user32!%s", fname);
                ++hooked;
                break;
            }
        }
    }
    return hooked > 0;
}

// installed=false puts the original imports back, true re-installs our detours.
// The slot list is kept either way so an aborted unload can recover.
static void SetIatHooks(bool installed) {
    for (int i = 0; i < g_iatCount; ++i) {
        if (!g_iatSlot[i]) continue;
        DWORD old = 0;
        VirtualProtect(g_iatSlot[i], sizeof(void*), PAGE_READWRITE, &old);
        *static_cast<void**>(g_iatSlot[i]) =
            installed ? g_iatHook[i] : g_iatOrig[i];
        VirtualProtect(g_iatSlot[i], sizeof(void*), old, &old);
    }
}

static void RestoreIat()     { SetIatHooks(false); }
static void ReinstallIat()   { SetIatHooks(true); }

// Posts WM_NULL so an idle / unfocused window still services the pump.
static DWORD WINAPI WakeThread(LPVOID) {
    while (!g_unloading) {
        for (int i = 0; i < 10 && !g_unloading; ++i) Sleep(25);
        if (g_mainHwnd && !g_unloading) PostMessageW(g_mainHwnd, WM_NULL, 0, 0);
    }
    InterlockedExchange(&g_wakeAlive, 0);
    InterlockedDecrement(&g_threadsAlive);
    return 0;
}

static BOOL CALLBACK EnumWnd(HWND h, LPARAM lp) {
    DWORD pid = 0;
    const DWORD tid = GetWindowThreadProcessId(h, &pid);
    if (!tid || pid != GetCurrentProcessId()) return TRUE;
    if (!IsWindowVisible(h)) return TRUE;
    if (GetWindow(h, GW_OWNER)) return TRUE;
    *reinterpret_cast<HWND*>(lp) = h;
    return FALSE;
}

static void FindMainWindow() {
    HWND h = nullptr;
    EnumWindows(&EnumWnd, reinterpret_cast<LPARAM>(&h));
    g_mainHwnd = h;
    g_mainThreadId = h ? GetWindowThreadProcessId(h, nullptr) : 0;
}

// ---------------------------------------------------------------------------
// Unload
// ---------------------------------------------------------------------------
// Unmapping the image while any of our code still runs is a guaranteed crash, so
// this never forces. If the threads do not stop, the unload is abandoned and the
// DLL stays resident (a later attempt can retry) - crashing the game is worse.
static DWORD WINAPI UnloadThread(LPVOID) {
    // No thread of this DLL may still be executing when the image goes away.
    for (int i = 0; i < 400 && g_inHook > 0; ++i) Sleep(5);
    for (int i = 0; i < 100 && g_wakeAlive > 0; ++i) Sleep(10);
    // Scans can take seconds; give them a chance to notice g_unloading and return.
    for (int i = 0; i < 3000 && g_threadsAlive > 0; ++i) Sleep(10);   // up to 30 s

    if (g_threadsAlive > 0 || g_inHook > 0) {
        // Roll back so the DLL keeps working and the caller can try again.
        Log("unload: ABORTED - %ld thread(s) and %ld hook(s) still active after "
            "30 s; the DLL stays loaded (nothing was unmapped)",
            static_cast<long>(g_threadsAlive), static_cast<long>(g_inHook));
        ReinstallIat();          // the DLL must keep working after an abort
        InterlockedExchange(&g_unloading, 0);
        InterlockedExchange(&g_unloadStarted, 0);
        DeleteFileA(pUnload);
        return 0;
    }

    if (g_instanceMutex) { CloseHandle(g_instanceMutex); g_instanceMutex = nullptr; }
    if (g_veh) { RemoveVectoredExceptionHandler(g_veh); g_veh = nullptr; }
    Sleep(50);

    // Nothing may run after this point except the unmap itself: without the VEH a
    // fault here is unhandled. The log line is written before it is dropped.
    Log("unloading");
    FreeLibraryAndExitThread(g_self, 0);
    return 0;
}

// Teardown, callable from any thread. Runs once.
static void BeginUnload(const char* who) {
    if (InterlockedCompareExchange(&g_unloadStarted, 1, 0) != 0) return;
    Log("unload flag detected (%s)", who);
    InterlockedExchange(&g_unloading, 1);
    RestoreIat();                 // plain memory writes: safe off the main thread
    HANDLE th = CreateThread(nullptr, 0, UnloadThread, nullptr, 0, nullptr);
    if (!th) {
        // Without this thread nothing will ever unmap us; say so and stay usable.
        Log("unload: CreateThread failed (%lu) - staying loaded", GetLastError());
        InterlockedExchange(&g_unloading, 0);
        InterlockedExchange(&g_unloadStarted, 0);
    } else {
        CloseHandle(th);
    }
}

// Polled from the driver as well, but the watchdog below is what makes unload
// work even when the game's message pump is stuck.
static void CheckUnloadFlag() {
    static DWORD last = 0;
    const DWORD now = GetTickCount();
    if (now - last < 250) return;
    last = now;
    if (GetFileAttributesA(pUnload) == INVALID_FILE_ATTRIBUTES) return;
    DeleteFileA(pUnload);
    BeginUnload(g_lastDriver);
}

// A dedicated thread: unloading must never depend on the pump being alive.
static DWORD WINAPI WatchdogThread(LPVOID) {
    while (!g_unloading) {
        Sleep(250);
        if (g_unloading) break;
        if (GetFileAttributesA(pUnload) != INVALID_FILE_ATTRIBUTES) {
            DeleteFileA(pUnload);
            BeginUnload("watchdog");
            break;
        }
    }
    InterlockedDecrement(&g_threadsAlive);
    return 0;
}

// ---------------------------------------------------------------------------
// Engine resolution
// ---------------------------------------------------------------------------
// Scan read-only data for a pointer table whose first entry is `fn` and whose
// entry +0x30 also lands in .text. That identifies the request object's vtable
// without trusting a recorded address.
static uintptr_t FindVtableFor(uintptr_t fn, const Section& text) {
    if (!fn) return 0;
    const uintptr_t textLo = text.va;
    const uintptr_t textHi = text.va + text.size;

    MEMORY_BASIC_INFORMATION mbi{};
    uint8_t* addr = static_cast<uint8_t*>(g_base);
    const uint8_t* limit = static_cast<uint8_t*>(g_base) + 0x8000000;
    while (addr < limit && VirtualQuery(addr, &mbi, sizeof(mbi)) == sizeof(mbi)) {
        auto* regionBase = static_cast<uint8_t*>(mbi.BaseAddress);
        const size_t size = mbi.RegionSize;
        addr = regionBase + size;
        if (mbi.State != MEM_COMMIT) continue;
        if (!(mbi.Protect & (PAGE_READONLY | PAGE_READWRITE)) ||
            (mbi.Protect & PAGE_GUARD))
            continue;

        const size_t n = size / sizeof(uint64_t);
        auto* q = reinterpret_cast<const uint64_t*>(regionBase);
        for (size_t i = 0; i < n; ++i) {
            if (q[i] != fn) continue;
            auto* vt = regionBase + i * 8;
            if (!IsReadable(vt, 0x38)) continue;
            const uint64_t slot30 = *reinterpret_cast<uint64_t*>(vt + 0x30);
            if (slot30 < textLo || slot30 >= textHi) continue;
            return reinterpret_cast<uintptr_t>(vt);
        }
    }
    return 0;
}

// Walk `fn` for direct call rel32 instructions and return their targets in order.
// The wrapper calls ResolveState, ResolveCountry and BuildCommand in that order,
// which disambiguates them even though their signatures are identical after
// masking.
static int CallTargets(uintptr_t fn, uintptr_t* out, int max, size_t window) {
    int n = 0;
    if (!fn || !IsReadable(reinterpret_cast<void*>(fn), window)) return 0;
    const auto* p = reinterpret_cast<const uint8_t*>(fn);
    for (size_t i = 0; i + 5 <= window && n < max; ++i) {
        if (p[i] != 0xE8) continue;
        const int32_t rel = *reinterpret_cast<const int32_t*>(p + i + 1);
        out[n++] = fn + i + 5 + rel;
        i += 4;
    }
    return n;
}

static bool ResolveEngine() {
    char path[MAX_PATH] = {0};
    GetModuleFileNameA(g_exe, path, MAX_PATH);

    // fingerprint
    {
        HANDLE h = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, nullptr,
                               OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h == INVALID_HANDLE_VALUE) { Log("fingerprint: cannot open exe"); return false; }
        LARGE_INTEGER sz{};
        GetFileSizeEx(h, &sz);
        uint64_t hash = 0xCBF29CE484222325ULL;
        uint8_t buf[1 << 16];
        DWORD rd = 0;
        while (ReadFile(h, buf, sizeof(buf), &rd, nullptr) && rd) {
            for (DWORD i = 0; i < rd; ++i) {
                hash ^= buf[i];
                hash *= 0x100000001B3ULL;
            }
        }
        CloseHandle(h);

        auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(g_base);
        auto* nt  = reinterpret_cast<IMAGE_NT_HEADERS*>(g_base + dos->e_lfanew);
        Log("build: size=%llu hash=0x%016llX tds=0x%08X",
            static_cast<unsigned long long>(sz.QuadPart),
            static_cast<unsigned long long>(hash), nt->FileHeader.TimeDateStamp);
        g_buildKnown = (static_cast<uint64_t>(sz.QuadPart) == kFileSize &&
                        hash == kFileHash);
        if (GetFileAttributesA(pForceUnknown) != INVALID_FILE_ATTRIBUTES) {
            g_buildKnown = false;
            Log("build: force_unknown is set - pretending the build is unknown "
                "(self-test of the signature path)");
        }
        if (!g_buildKnown) {
            // A game update changes the fingerprint. Refusing outright made every
            // update a manual re-port; instead fall through to signature scanning
            // and let the self-check disable only what cannot be found.
            Log("build UNKNOWN (expected %llu / 0x%016llX) - resolving by signature",
                kFileSize, kFileHash);
            Log("build UNKNOWN - recorded RVAs will NOT be trusted");
        } else {
            Log("build fingerprint OK");
        }
    }

    Section text = TextSection();
    if (!text.va) { Log("no .text"); return false; }

    g_resolveCountry = FindFunc(kSigResolveCountry, kSigResolveCountryMask, sizeof(kSigResolveCountry),
                                rva::ResolveCountry, "ResolveCountry");
    g_resolveState   = FindFunc(kSigResolveState, kSigResolveStateMask, sizeof(kSigResolveState),
                                rva::ResolveState, "ResolveState");
    g_countLevels    = FindFunc(kSigCountLevels, kSigCountLevelsMask, sizeof(kSigCountLevels),
                                rva::CountLevels, "CountLevels");
    g_buildCommand   = FindFunc(kSigBuildCommand, kSigBuildCommandMask, sizeof(kSigBuildCommand),
                                rva::BuildCommand, "BuildCommand");
    // The wrapper is reached through the request object's vtable, so on an unknown
    // build it is located by signature and the vtable is then found by scanning
    // .rdata for a table whose slot 0 is the wrapper.
    g_wrapper = FindFunc(kSigWrapper, kSigWrapperMask, sizeof(kSigWrapper),
                         kWrapperRva, "Wrapper");
    if (g_wrapper) {
        g_reqBuild = FindFunc(kSigReqBuild, kSigReqBuildMask, sizeof(kSigReqBuild),
                              kReqBuildRva, "ReqBuild");
        Log("wrapper: found at rva 0x%llX, req-build at 0x%llX",
            static_cast<unsigned long long>(g_wrapper -
                reinterpret_cast<uintptr_t>(g_base)),
            static_cast<unsigned long long>(g_reqBuild -
                reinterpret_cast<uintptr_t>(g_base)));
    } else {
        Log("wrapper: not found - builds fall back to a direct BuildCommand call");
    }

    if (g_wrapper) {
        uintptr_t calls[8] = {0};
        const int nc = CallTargets(g_wrapper, calls, 8, 0x80);
        Log("wrapper: %d direct call(s) in its first 0x80 bytes", nc);
        for (int i = 0; i < nc && i < 5; ++i)
            Log("  call[%d] -> rva 0x%llX", i,
                static_cast<unsigned long long>(
                    calls[i] - reinterpret_cast<uintptr_t>(g_base)));
        // Identical signatures make the two resolvers ambiguous; the wrapper's call
        // order settles it.
        if (nc >= 2) {
            if (!g_resolveState) {
                g_resolveState = calls[0];
                Log("sig: ResolveState   recovered from wrapper call[0]");
            }
            if (!g_resolveCountry) {
                g_resolveCountry = calls[1];
                Log("sig: ResolveCountry recovered from wrapper call[1]");
            }
            if (!g_buildCommand && nc >= 3) {
                g_buildCommand = calls[2];
                Log("sig: BuildCommand   recovered from wrapper call[2]");
            }
        }

        Section t2 = TextSection();
        g_reqVtable = FindVtableFor(g_wrapper, t2);
        if (g_reqVtable)
            Log("request vtable: 0x%llX (rva 0x%llX)",
                static_cast<unsigned long long>(g_reqVtable),
                static_cast<unsigned long long>(g_reqVtable -
                    reinterpret_cast<uintptr_t>(g_base)));
        else
            Log("request vtable: not found - template/wrapper builds disabled");
    }
    g_bldLookup = FindFunc(kSigBldLookup, kSigBldLookupMask, sizeof(kSigBldLookup),
                           rva::BldLookup, "BldLookup");
    if (!IsReadable(reinterpret_cast<void*>(g_bldLookup), 16)) {
        Log("building lookup at rva 0x%llX is not readable",
            static_cast<unsigned long long>(rva::BldLookup));
        g_bldLookup = 0;
    }
    auto* ctxSlot = reinterpret_cast<uintptr_t*>(g_base + rva::BuildCtxGlobal);
    g_buildCtx = IsReadable(ctxSlot, 8) ? *ctxSlot : 0;
    if (g_buildCtx) {
        // The context must be a real object: its first qword is a vtable inside
        // .text. If not, the recorded RVA is stale - disable it rather than
        // handing the engine a wild pointer.
        // A context object starts with a vtable pointer. The vtable itself lives in
        // read-only DATA (.rdata), not .text - only its first entry is code.
        Section t = TextSection();
        const auto* c = reinterpret_cast<uint8_t*>(g_buildCtx);
        const bool ok = IsReadable(c, 8);
        const uint64_t vt = ok ? *reinterpret_cast<const uint64_t*>(c) : 0;
        bool vtOk = false;
        if (vt) {
            const auto* vtbl = reinterpret_cast<const uint8_t*>(
                static_cast<uintptr_t>(vt));
            if (IsReadable(vtbl, 8)) {
                const uint64_t first = *reinterpret_cast<const uint64_t*>(vtbl);
                vtOk = t.va && first >= t.va && first < t.va + t.size;
            }
        }
        if (!ok || !vtOk) {
            Log("ctx: global 0x%llX rejected (vt=0x%llX, vt[0] not in "
                "text=[0x%llX,0x%llX)) - disabled",
                static_cast<unsigned long long>(rva::BuildCtxGlobal),
                static_cast<unsigned long long>(vt),
                static_cast<unsigned long long>(t.va),
                static_cast<unsigned long long>(t.va + t.size));
            g_buildCtx = 0;
        } else {
            Log("ctx: global 0x%llX -> 0x%llX (vt ok)",
                static_cast<unsigned long long>(rva::BuildCtxGlobal),
                static_cast<unsigned long long>(g_buildCtx));
        }
    }

    Log("resolve: country=%p state=%p count=%p build=%p lookup=%p ctx=0x%llX",
        reinterpret_cast<void*>(g_resolveCountry),
        reinterpret_cast<void*>(g_resolveState),
        reinterpret_cast<void*>(g_countLevels),
        reinterpret_cast<void*>(g_buildCommand),
        reinterpret_cast<void*>(g_bldLookup),
        static_cast<unsigned long long>(g_buildCtx));

    if (!g_resolveState || !g_resolveCountry) { Log("core resolvers missing"); return false; }

    g_regState   = GlobalFromResolver(g_resolveState);
    g_regCountry = GlobalFromResolver(g_resolveCountry);
    Log("globals: regState=0x%llX regCountry=0x%llX",
        static_cast<unsigned long long>(g_regState),
        static_cast<unsigned long long>(g_regCountry));
    if (!g_regState || !g_regCountry) return false;

    g_nState   = CollectVt(".?AVCState@@", g_vtState, 4);
    g_nCountry = CollectVt(".?AVCCountry@@", g_vtCountry, 4);
    g_nBldType = CollectVt(".?AVCBuildingType@@", g_vtBldType, 4);
    g_nBld     = CollectVt(".?AVCBuilding@@", g_vtBld, 4);
    g_nPlayer  = CollectVt(".?AVCJominiPlayer@@", g_vtPlayer, 4);

    // RTTI may return a base-class COL that live objects never use; add the
    // measured ones from the reverse-engineering record.
    // NOTE: recorded vtables are used for the PLAYER only. Using an unverified
    // address to validate state/country/building pointers crashed the game: a bogus
    // vtable makes HasVt() accept an unrelated object and the PM writer then writes
    // into it. RTTI candidates are good enough for those (they come from the class
    // name, not from a hardcoded address).
    if (rva::VtPlayer[0])
        for (uintptr_t r : rva::VtPlayer) AddRecordedVt(g_vtPlayer, &g_nPlayer, 4, r, text);

    for (int i = 0; i < g_nPlayer; ++i)
        Log("  player vt[%d] = 0x%llX", i, static_cast<unsigned long long>(g_vtPlayer[i]));

    Log("vtables: state=%d country=%d bldType=%d bld=%d player=%d",
        g_nState, g_nCountry, g_nBldType, g_nBld, g_nPlayer);

    Log("capabilities: build=%s pm=%s player=%s",
        (g_buildCommand && g_buildCtx && g_nState && g_nCountry) ? "on" : "off",
        (g_bldLookup && g_nBld && g_nBldType) ? "on" : "off",
        g_nPlayer ? "on" : "off");
    return true;
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------
static void BootstrapBody() {
    InitPaths();
    if (g_secondInstance) {
        Log("=== v3bridge 0.2 REFUSED: another instance is already loaded ===");
        return;
    }
    ResetLog();                   // must run before the first Log() call
    Log("=== v3bridge 0.3 (build 158) ===");
    Log("bootstrap: Victoria 3 4.5.2(3270); every data offset is checked once at startup and a failed check disables that capability");

    // Start the unload watchdog first: if anything below wedges, the flag still works.
    StartOwnedThread(WatchdogThread, "watchdog");

    while (!(g_exe = GetModuleHandleA("victoria3.exe"))) Sleep(200);
    g_base = reinterpret_cast<uint8_t*>(g_exe);
    {
        auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(g_base);
        auto* nt  = reinterpret_cast<IMAGE_NT_HEADERS*>(g_base + dos->e_lfanew);
        g_imageSize = nt->OptionalHeader.SizeOfImage;
    }

    for (int i = 0; i < 120 && !g_mainThreadId; ++i) {
        FindMainWindow();
        if (!g_mainThreadId) Sleep(500);
    }
    Log("main thread = %lu hwnd = %p", g_mainThreadId, g_mainHwnd);
    if (!g_mainThreadId)
        Log("WARNING: main thread unknown - pump hooks will not run");

    if (HookMessageApis(g_exe)) Log("driver hooks installed (%d)", g_iatCount);
    else Log("driver hooks FAILED");
    if (g_mainHwnd) {
        InterlockedExchange(&g_wakeAlive, 1);
        StartOwnedThread(WakeThread, "wake");
    }

    if (!ResolveEngine()) {
        Log("resolve failed - inert, but unload.flag still works");
        return;
    }

    InstallFaultGuard();

    // Ready for the player id right now; the type table only gates build/PM.
    g_ready = true;
    // No scan here: at the main menu there is no player object, and walking the
    // whole heap costs ~20 s for nothing. RunPlayer starts it once a save is live.

    if (WaitForTypes()) InterlockedExchange(&g_typesReady, 1);
    else Log("no building types - build/pm disabled");

    RunWorld();                 // classify main menu vs in-save right away

    // A value from the previous instance must never look like a live reading.
    DeleteFileA(pPlayerOut);
    g_playerCountry = 0;
    g_playerObj = nullptr;
    g_lastPlayerWrite = 0;
    g_lastPlayerScan = 0;
    Log("loaded (types=%zu driver=%s world=%s) - waiting for the first ready",
        g_types.size(), g_lastDriver, g_inSave ? "in-save" : "main-menu");
    Log("ready: cleared player.current.txt - it reappears once the id resolves");
    return;
}


// Bootstrap is a DLL-owned thread as well: unloading while it is still running
// (for example during the 30 s type-table wait) would unmap live code.
static DWORD WINAPI Bootstrap(LPVOID) {
    BootstrapBody();
    InterlockedDecrement(&g_threadsAlive);
    return 0;
}

BOOL APIENTRY DllMain(HMODULE mod, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = mod;
        DisableThreadLibraryCalls(mod);

        char name[64] = {0};
        wsprintfA(name, "Local\\v3bridge_%lu", GetCurrentProcessId());
        SetLastError(0);
        g_instanceMutex = CreateMutexA(nullptr, FALSE, name);
        if (g_instanceMutex && GetLastError() == ERROR_ALREADY_EXISTS) {
            g_secondInstance = true;
            CloseHandle(g_instanceMutex);
            g_instanceMutex = nullptr;
        }
        InterlockedIncrement(&g_threadsAlive);
        if (!CreateThread(nullptr, 0, Bootstrap, nullptr, 0, nullptr))
            InterlockedDecrement(&g_threadsAlive);
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_instanceMutex) { CloseHandle(g_instanceMutex); g_instanceMutex = nullptr; }
    }
    return TRUE;
}
