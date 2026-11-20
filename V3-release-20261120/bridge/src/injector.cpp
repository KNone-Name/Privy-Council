// ============================================================================
//  Minimal DLL injector (x64)
//  usage: injector.exe <process-name|pid> <full-path-to-dll>
//
//  Method: VirtualAllocEx + WriteProcessMemory + CreateRemoteThread(LoadLibraryA).
//  This is the classic user-mode path; no driver, no manual mapping.
//
//  Guards added:
//    * refuses if the same DLL file is already loaded in the target
//    * verifies the DLL exists and is x86-64
//    * asks for the minimal access rights first, falls back to PROCESS_ALL_ACCESS
//    * frees the remote path buffer
// ============================================================================
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

static DWORD FindPidByName(const char* name) {
    char target[MAX_PATH] = {0};
    size_t n = 0;
    while (name[n] && n < MAX_PATH - 1) { target[n] = name[n]; ++n; }
    target[n] = 0;
    if (n < 4 || _stricmp(target + n - 4, ".exe") != 0) {
        if (n + 4 < MAX_PATH) {
            target[n] = '.'; target[n+1] = 'e'; target[n+2] = 'x'; target[n+3] = 'e';
            target[n+4] = 0;
        }
    }

    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;

    PROCESSENTRY32 pe{};
    pe.dwSize = sizeof(pe);
    DWORD pid = 0;

    printf("looking for process: %s\n", target);

    if (Process32First(snap, &pe)) {
        do {
            if (_stricmp(pe.szExeFile, target) == 0) { pid = pe.th32ProcessID; break; }
        } while (Process32Next(snap, &pe));
    }

    if (!pid) {
        printf("not found. processes containing '%s':\n", name);
        if (Process32First(snap, &pe)) {
            do {
                if (strstr(pe.szExeFile, name)) {
                    printf("  pid=%lu  %s\n", pe.th32ProcessID, pe.szExeFile);
                }
            } while (Process32Next(snap, &pe));
        }
    }

    CloseHandle(snap);
    return pid;
}

// Is a module with this file name already loaded in the target?
static bool IsModuleLoaded(DWORD pid, const char* dllPath) {
    const char* base = strrchr(dllPath, '\\');
    base = base ? base + 1 : dllPath;

    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
    if (snap == INVALID_HANDLE_VALUE) return false;

    MODULEENTRY32 me{};
    me.dwSize = sizeof(me);
    bool found = false;
    if (Module32First(snap, &me)) {
        do {
            if (_stricmp(me.szModule, base) == 0) { found = true; break; }
        } while (Module32Next(snap, &me));
    }
    CloseHandle(snap);
    return found;
}

int main(int argc, char** argv) {
    if (argc < 3) {
        printf("usage: injector.exe <process-name|pid> <dll-path>\n");
        return 1;
    }

    DWORD pid = 0;
    if (argv[1][0] >= '0' && argv[1][0] <= '9') pid = (DWORD)atoi(argv[1]);
    if (!pid) pid = FindPidByName(argv[1]);
    if (!pid) { printf("process not found: %s\n", argv[1]); return 1; }

    // A relative path is resolved against the TARGET's working directory by
// LoadLibraryA, not against ours - that silently produced
// "LoadLibraryA -> 0x0 (failed)". Always hand the remote side a full path.
char dllAbs[MAX_PATH] = {0};
if (!GetFullPathNameA(argv[2], MAX_PATH, dllAbs, nullptr)) {
    printf("cannot resolve path: %s\n", argv[2]);
    return 1;
}
const char* dll = dllAbs;
if (strchr(argv[2], ':') == nullptr)
    printf("note: relative path given, using %s\n", dllAbs);

    // --- sanity: the DLL must exist and be x86-64 ---
    HANDLE hf = CreateFileA(dll, GENERIC_READ, FILE_SHARE_READ, nullptr,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (hf == INVALID_HANDLE_VALUE) {
        printf("dll not found: %s (err=%lu)\n", dll, GetLastError());
        return 1;
    }
    IMAGE_DOS_HEADER dos{};
    IMAGE_NT_HEADERS nt{};
    DWORD read = 0;
    bool okPe = ReadFile(hf, &dos, sizeof(dos), &read, nullptr) && dos.e_magic == IMAGE_DOS_SIGNATURE;
    if (okPe) {
        SetFilePointer(hf, dos.e_lfanew, nullptr, FILE_BEGIN);
        okPe = ReadFile(hf, &nt, sizeof(nt), &read, nullptr) &&
               nt.Signature == IMAGE_NT_SIGNATURE;
    }
    CloseHandle(hf);
    if (!okPe) { printf("not a valid PE file: %s\n", dll); return 1; }
    if (nt.FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64) {
        printf("dll is not x86-64 (machine=0x%04X)\n", nt.FileHeader.Machine);
        return 1;
    }

    printf("target pid = %lu\ndll        = %s\n", pid, dll);

    if (IsModuleLoaded(pid, dll)) {
        printf("REFUSED: %s is already loaded in pid %lu.\n"
               "         unload it first (write D:\\Game\\ai\\V3\\unload.flag), or the\n"
               "         second copy would fight the first for the same control files.\n",
               strrchr(dll, '\\') ? strrchr(dll, '\\') + 1 : dll, pid);
        return 2;
    }

    // --- minimal rights first ---
    const DWORD need = PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION |
                       PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION | PROCESS_VM_READ;
    HANDLE h = OpenProcess(need, FALSE, pid);
    if (!h) h = OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid);
    if (!h) {
        printf("OpenProcess failed (err=%lu). Run as administrator?\n", GetLastError());
        return 1;
    }

    const size_t len = strlen(dll) + 1;
    void* remote = VirtualAllocEx(h, nullptr, len, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remote) {
        printf("VirtualAllocEx failed (err=%lu)\n", GetLastError());
        CloseHandle(h);
        return 1;
    }

    if (!WriteProcessMemory(h, remote, dll, len, nullptr)) {
        printf("WriteProcessMemory failed (err=%lu)\n", GetLastError());
        VirtualFreeEx(h, remote, 0, MEM_RELEASE);
        CloseHandle(h);
        return 1;
    }

    auto loadLibraryA = reinterpret_cast<LPTHREAD_START_ROUTINE>(
        GetProcAddress(GetModuleHandleA("kernel32.dll"), "LoadLibraryA"));

    HANDLE th = CreateRemoteThread(h, nullptr, 0, loadLibraryA, remote, 0, nullptr);
    if (!th) {
        printf("CreateRemoteThread failed (err=%lu)\n", GetLastError());
        VirtualFreeEx(h, remote, 0, MEM_RELEASE);
        CloseHandle(h);
        return 1;
    }

    const DWORD wr = WaitForSingleObject(th, 30000);
    DWORD exitCode = 0;
    GetExitCodeThread(th, &exitCode);
    if (wr == WAIT_TIMEOUT)
        printf("timed out waiting for LoadLibraryA (30 s) - module may still be loading\n");
    printf("LoadLibraryA -> 0x%lX %s\n", exitCode, exitCode ? "(ok)" : "(failed)");

    CloseHandle(th);
    VirtualFreeEx(h, remote, 0, MEM_RELEASE);
    CloseHandle(h);
    return exitCode ? 0 : 1;
}
