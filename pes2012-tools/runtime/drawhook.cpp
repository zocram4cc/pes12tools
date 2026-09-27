// drawhook (shim): waits for kitserver's device, hooks Present only, and
// hot-loads dllprobe\drawlogic.dll (copied to a fresh name each time) whenever
// flags\reload appears. The logic DLL owns every other device hook.
#include <windows.h>
#include <mmsystem.h>
#include <d3d9.h>

// ---- audio: CRI ADX paces its sound-effect mixer with timeSetEvent(16 ms,
// TIME_PERIODIC | TIME_CALLBACK_EVENT_PULSE). Under fsync/esync Wine's
// PulseEvent loses most pulses (the mixer woke every ~250 ms and its 133 ms
// buffer looped: stuttering, repeating SFX, 27-09), and server-side sync
// exposes a loader race that crashes the game in afsio.dll (exit 5, twice
// 27-09). Rewriting the flag to TIME_CALLBACK_EVENT_SET keeps fsync: the
// timer then sets the event, which every sync backend delivers.
static wchar_t g_root[MAX_PATH];              // see initRoot
typedef MMRESULT (WINAPI *TSE_FN)(UINT, UINT, LPTIMECALLBACK, DWORD_PTR, UINT);
static TSE_FN g_orgTSE = NULL;
static LONG g_tsePatched = 0;
static MMRESULT WINAPI myTSE(UINT delay, UINT res, LPTIMECALLBACK cb, DWORD_PTR user, UINT flags) {
    if (flags & TIME_CALLBACK_EVENT_PULSE) {
        flags = (flags & ~TIME_CALLBACK_EVENT_PULSE) | TIME_CALLBACK_EVENT_SET;
        InterlockedIncrement(&g_tsePatched);
        wchar_t path[MAX_PATH]; lstrcpyW(path, g_root); lstrcatW(path, L"tse.log");
        HANDLE f = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ, NULL, OPEN_ALWAYS, 0, NULL);
        if (f != INVALID_HANDLE_VALUE) {
            char m[96]; DWORD w; int n = wsprintfA(m, "timeSetEvent %u ms: EVENT_PULSE -> EVENT_SET\r\n", delay);
            WriteFile(f, m, n, &w, NULL); CloseHandle(f);
        }
    }
    return g_orgTSE(delay, res, cb, user, flags);
}
// point the exe's import of winmm!timeSetEvent at myTSE
static void hookTimeSetEvent() {
    HMODULE winmm = LoadLibraryA("winmm.dll");
    void* real = winmm ? (void*)GetProcAddress(winmm, "timeSetEvent") : NULL;
    if (!real) return;
    g_orgTSE = (TSE_FN)real;
    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*)(base + ((IMAGE_DOS_HEADER*)base)->e_lfanew);
    IMAGE_DATA_DIRECTORY dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress) return;
    for (IMAGE_IMPORT_DESCRIPTOR* d = (IMAGE_IMPORT_DESCRIPTOR*)(base + dir.VirtualAddress); d->Name; d++) {
        for (void** slot = (void**)(base + d->FirstThunk); *slot; slot++) {
            if (*slot != real) continue;
            DWORD prot;
            VirtualProtect(slot, sizeof(void*), PAGE_READWRITE, &prot);
            *slot = (void*)myTSE;
            VirtualProtect(slot, sizeof(void*), prot, &prot);
        }
    }
}

// Everything the player runtime reads or writes lives under one root:
// <folder of drawhook.dll>\4cc-players\ (drawlogic.dll, custom\, flags\, logs).
static const wchar_t* ROOT_NAME = L"4cc-players\\";
static wchar_t SRC[MAX_PATH], LIVEDIR[MAX_PATH], RELOAD[MAX_PATH];
extern "C" __declspec(dllexport) const wchar_t* hook_root() { return g_root; }
static void initRoot(HINSTANCE self) {
    GetModuleFileNameW(self, g_root, MAX_PATH);
    wchar_t* slash = g_root; for (wchar_t* q = g_root; *q; q++) if (*q == L'\\' || *q == L'/') slash = q + 1;
    *slash = 0; lstrcatW(g_root, ROOT_NAME);
    lstrcpyW(SRC, g_root); lstrcatW(SRC, L"drawlogic.dll");
    lstrcpyW(LIVEDIR, g_root); lstrcatW(LIVEDIR, L"live\\");
    lstrcpyW(RELOAD, g_root); lstrcatW(RELOAD, L"flags\\reload");
}
static const LONG FLAG_POLL_FRAMES = 30;

typedef HRESULT (STDMETHODCALLTYPE *PRESENT_FN)(IDirect3DDevice9*, const RECT*, const RECT*, HWND, const RGNDATA*);
typedef void (*INST_FN)(IDirect3DDevice9*);
typedef void (*UNINST_FN)();
typedef void (*PRES_FN)(IDirect3DDevice9*);
static PRESENT_FN g_orgPresent = NULL;
static HMODULE g_logic = NULL;
static PRES_FN g_logicPresent = NULL;
static LONG g_frame = 0, g_gen = 0;

static void reload(IDirect3DDevice9* d) {
    if (g_logic) {
        UNINST_FN u = (UNINST_FN)GetProcAddress(g_logic, "logic_uninstall");
        if (u) u();
        g_logicPresent = NULL;
        FreeLibrary(g_logic); g_logic = NULL;
    }
    CreateDirectoryW(LIVEDIR, NULL);
    wchar_t dst[MAX_PATH];
    wsprintfW(dst, L"%sdrawlogic_%d.dll", LIVEDIR, (int)++g_gen);
    if (!CopyFileW(SRC, dst, FALSE)) return;
    g_logic = LoadLibraryW(dst);
    if (!g_logic) return;
    INST_FN i = (INST_FN)GetProcAddress(g_logic, "logic_install");
    g_logicPresent = (PRES_FN)GetProcAddress(g_logic, "logic_present");
    if (i) i(d);
}

static HRESULT STDMETHODCALLTYPE myPresent(IDirect3DDevice9* d, const RECT* a,
        const RECT* b, HWND c, const RGNDATA* e) {
    g_frame++;
    if (g_frame == 1 || (g_frame % FLAG_POLL_FRAMES == 0 &&
            GetFileAttributesW(RELOAD) != INVALID_FILE_ATTRIBUTES)) {
        DeleteFileW(RELOAD);
        reload(d);
    }
    if (g_logicPresent) g_logicPresent(d);
    return g_orgPresent(d, a, b, c, e);
}

typedef IDirect3DDevice9* (*GAD_FN)();
static DWORD WINAPI waitDevice(LPVOID) {
    HMODULE kl = GetModuleHandleA("kload.dll");
    GAD_FN gad = kl ? (GAD_FN)GetProcAddress(kl, "getActiveDevice") : NULL;
    if (!gad) return 0;
    for (;;) {
        IDirect3DDevice9* d = gad();
        if (d) {
            void** vt = *(void***)d;
            DWORD prot;
            VirtualProtect(&vt[17], 4, PAGE_EXECUTE_READWRITE, &prot);
            g_orgPresent = (PRESENT_FN)vt[17];
            vt[17] = (void*)myPresent;
            VirtualProtect(&vt[17], 4, prot, &prot);
            return 0;
        }
        Sleep(200);
    }
}

extern "C" __declspec(dllexport) BOOL WINAPI DllMain(HINSTANCE h, DWORD r, LPVOID x) {
    if (r == DLL_PROCESS_ATTACH) { initRoot(h); hookTimeSetEvent(); CreateThread(NULL, 0, waitDevice, NULL, 0, NULL); }
    return TRUE;
}
