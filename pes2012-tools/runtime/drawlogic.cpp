// drawhook: IDirect3DDevice9 vtable interception.
// Installed as a kitserver DLL. On load: IAT-patch the game's
// Direct3DCreate9 import to our wrapper; chain IDirect3D9::CreateDevice;
// chain device methods: SetStreamSource, SetIndices, SetFVF,
// SetVertexDeclaration, SetTexture, SetRenderState,
// DrawIndexedPrimitive, Present.
// Control files in <kitserver>\4cc-players\flags\ (drawhook.cpp initRoot):
//   wire (exists) -> force wireframe on body-ish draws
//   dump (exists) -> log draws per frame; removed when dump ends
#include <windows.h>
#include <stdio.h>
#include <d3d9.h>
#include <math.h>
#include "kitmap.h"
#include "officialmap.h"   // tools/pes12_rig.py: OFFICIAL_*, CLOSE_*, HEAD_*
#include "custom_ps.h"

// Paths hang off drawhook's root (<kitserver>\4cc-players\, drawhook.cpp initRoot).
static wchar_t g_root[MAX_PATH], LOGPATH[MAX_PATH], FLAGDIR[MAX_PATH], CUSTOM_DIR[MAX_PATH], KIT_DIR[MAX_PATH], SHOT_PATH[MAX_PATH];
static const wchar_t* rootFile(wchar_t* out, const wchar_t* rel) { lstrcpyW(out, g_root); lstrcatW(out, rel); return out; }
static void initPaths() {
    typedef const wchar_t* (*ROOT_FN)();
    HMODULE hook = GetModuleHandleA("drawhook.dll");
    ROOT_FN root = hook ? (ROOT_FN)GetProcAddress(hook, "hook_root") : NULL;
    lstrcpyW(g_root, root ? root() : L".\\");
    rootFile(LOGPATH, L"drawhook.log"); rootFile(FLAGDIR, L"flags\\"); rootFile(CUSTOM_DIR, L"custom\\");
    rootFile(KIT_DIR, L"custom\\kits\\"); rootFile(SHOT_PATH, L"shots\\frame.bmp");
}
static HANDLE g_log = INVALID_HANDLE_VALUE;

static void logline(const char* s) {
    DWORD w = 0; char b[768]; int n = 0;
    while (s[n] && n < 740) { b[n] = s[n]; n++; }
    b[n++] = '\r'; b[n++] = '\n';
    if (g_log != INVALID_HANDLE_VALUE) WriteFile(g_log, b, n, &w, NULL);
}

// --- state ---
static IDirect3DDevice9* g_dev = NULL;
static IDirect3DVertexBuffer9* g_vb = NULL;
static UINT g_stride = 0;
static DWORD g_fvf = 0;
static IDirect3DIndexBuffer9* g_ib = NULL;
static IDirect3DBaseTexture9* g_tex0 = NULL;
static IDirect3DBaseTexture9* g_texStage[8];
static IDirect3DBaseTexture9* g_prevStage[8];
static IDirect3DVertexDeclaration9* g_decl = NULL;
static UINT g_vbOff = 0;
static float g_vsc[256][4];

// Player groups: in the colour pass each player is a contiguous run of draws
// opening with the first body packet. Signature measured from the 24-09
// tunnel dump (drawhook.log): nV=374, primCount=609, stride=44.
static const UINT GROUP_START_NV = 374;
static const UINT GROUP_START_NP = 609;
static const UINT GROUP_START_STRIDE = 44;
// A player group never contains the pitch/crowd style draws.
static const UINT NONPLAYER_STRIDE = 32;
static const UINT NONPLAYER_MIN_NV = 5000;
static const int MAX_GROUP_DRAWS = 24;

static volatile LONG g_group = -1;     // current group index this frame
static volatile LONG g_groupDraw = 0;  // draw index inside current group
static unsigned g_hideMask = 0;        // bit k -> hide group k
static LONG g_grabGroup = -1;          // group to dump once
static const LONG GRAB_ALL = 99;       // grab flag value: every draw of one frame
static LONG g_grabAll = 0;

// In-match body: dt09 #349 block 2 (2131 verts / 5740 strip idx, stride 48),
// drawn with nV=2133 nP=5729 for every outfield player from ONE shared VB.
static const UINT BODY_NV = 2133;
static const UINT BODY_NP = 5729;
static const UINT BODY_STRIDE = 48;
// Keeper body: dt09 #349 block 2 verbatim (2131 verts, 5740 strip idx) with
// the same 19-bone palette; drawn nV=2131 nP=5739 in the 24-09 frame dumps.
static const UINT GK_BODY_NV = 2131;
static const UINT GK_BODY_NP = 5739;
// LOD0 kit (dt0c #3 block 0), per player run in the colour pass (24-09 dumps):
// packet 5 (755 verts, strip nP 1857, 80 B) carries the hip; packet 18
// (696 / 1929 / 72 B) the other 18 main bones. Bones live at c20 + 3*slot.
static const UINT KIT_HIP_NV = 755, KIT_HIP_NP = 1857, KIT_HIP_STRIDE = 80;
static const UINT KIT_MAIN_NV = 696, KIT_MAIN_NP = 1929, KIT_MAIN_STRIDE = 72;
// stock detail hands/boots, drawn apart from the kit run (30-09 grab)
static const UINT DETAIL_HANDS_NV = 466, DETAIL_HANDS_NP = 565;        // both hands, stride 68
static const UINT DETAIL_BOOTS_NV = 930, DETAIL_BOOTS_NP = 1907;       // both feet, stride 64
static const UINT DETAIL_BOOT_NV = 228, DETAIL_BOOT_L_NP = 579, DETAIL_BOOT_R_NP = 573;   // one foot each, stride 68
// The bare hands and keeper gloves (dt0d.img #589 blocks 2-5, 02-10: bytes =
// the in-game draw), one per hand, each on its own 12-bone hand rig (wrist
// root, 3-bone thumb, 2 bones per finger); they draw beside the boots, before
// the player's skin draw, so no kit run holds them (Kurisu's stock palms).
static const UINT DETAIL_HAND_STRIDE = 80, DETAIL_HAND_L_NV = 305, DETAIL_HAND_L_NP = 731, DETAIL_HAND_R_NV = 302, DETAIL_HAND_R_NP = 715;
static const UINT DETAIL_GLOVE_STRIDE = 72, DETAIL_GLOVE_NV = 293, DETAIL_GLOVE_L_NP = 711, DETAIL_GLOVE_R_NP = 703;
// A detail rig's root (the wrist / ankle, bind at the mesh origin) sits in
// any of its first palette slots (#589: slot 0 for the right hand, slot 2
// for the left), so ownership takes the nearest of them.
static const UINT DETAIL_RIG_BONES = 12;
// owner match: 30-09 replay, hidden players' boots sat 0.17-0.33 m from their
// projected feet, every other player's 3 m or more
static const float DETAIL_OWNER_MAX_M = 0.5f;
static const UINT BONE_REG0 = 20;
// Adboards: the board faces sample the DoubleFusion ad sheet (1024x512
// A8R8G8B8, a 4x8 grid of 256x64 ads, built at runtime; no file in the game
// holds it). A stadium draws one of 8 board layouts (dt07 2923-2930; Karasuno
// loads 2924, slot 2693's stadium 2927 - bserv.log 01-10), each 10 face
// blocks: far, both goal ends, two near-side pieces, and their floor
// reflections, every one its own draw with its own vertex count. What they
// share, and nothing else in stock dt07/dt08 or the installed overrides has
// (all 82 face packets, scanned 01-10): stride 48 and the declaration
// POSITION, NORMAL, TEXCOORD0..2 (d_bill_face00, d_bill_wrap00, e_cmap00).
// (The 10442/8434/40 banner block draw is the boards' backs, not the ads.)
static const UINT ADBOARD_STRIDE = 48;
static IDirect3DTexture9* g_adTex = NULL;   // the board sheet (custom\boards\board_0.tex)
static bool g_adTried = false;              // tried loading this session
static bool g_declBoard = false;            // the bound declaration is the board face one
static bool g_declSkinned = false;          // the bound declaration has BLENDINDICES
static bool declHasUsage(IDirect3DVertexDeclaration9* p, BYTE usage) {
    D3DVERTEXELEMENT9 el[MAXD3DDECLLENGTH + 1]; UINT ne = 0;
    if (!p || FAILED(p->GetDeclaration(NULL, &ne)) || ne > MAXD3DDECLLENGTH + 1 || FAILED(p->GetDeclaration(el, &ne))) return false;
    for (UINT i = 0; i + 1 < ne; i++) if (el[i].Usage == usage) return true;
    return false;
}
static bool isBoardDecl(IDirect3DVertexDeclaration9* p) {
    static const BYTE USAGE[] = { D3DDECLUSAGE_POSITION, D3DDECLUSAGE_NORMAL,
                                  D3DDECLUSAGE_TEXCOORD, D3DDECLUSAGE_TEXCOORD, D3DDECLUSAGE_TEXCOORD };
    static const BYTE INDEX[] = { 0, 0, 0, 1, 2 };
    const UINT N = sizeof(USAGE);
    D3DVERTEXELEMENT9 el[MAXD3DDECLLENGTH + 1]; UINT ne = 0;
    if (!p || FAILED(p->GetDeclaration(NULL, &ne)) || ne != N + 1 || FAILED(p->GetDeclaration(el, &ne))) return false;
    for (UINT i = 0; i < N; i++) if (el[i].Stream != 0 || el[i].Usage != USAGE[i] || el[i].UsageIndex != INDEX[i]) return false;
    return true;
}
// The same face draws also run in passes that bind a 64x64 white DXT5 or no
// texture (shadow/depth, texdump 01-10); only the material pass binds the
// sheet itself, and only that one is swapped.
static const UINT AD_SHEET_W = 1024, AD_SHEET_H = 512;
static bool isAdSheet(IDirect3DBaseTexture9* bt) {
    if (!bt || bt->GetType() != D3DRTYPE_TEXTURE) return false;
    D3DSURFACE_DESC sd;
    if (FAILED(((IDirect3DTexture9*)bt)->GetLevelDesc(0, &sd))) return false;
    return sd.Width == AD_SHEET_W && sd.Height == AD_SHEET_H && sd.Format == D3DFMT_A8R8G8B8;
}
static const UINT BONE_REGS = 3;
static const UINT CU_SLOTS = 21;           // 19 main bones + 2 finger bones (kitmap.h)
// draws in a player's kit run from packet 5 to the run's end (24-09 dump:
// longest run seen (26-09 grabs: 16-19 draws, gloves and boots included);
// past this the run is over whatever the classes say
static const LONG MAX_RUN_DRAWS = 32;
// PoC deformation: inflate the swapped body sideways so it is unmistakable.
static const float SWAP_INFLATE_XZ = 1.0f;
static const float SWAP_STRETCH_Y = 2.0f;
static LONG g_swLo = -1, g_swHi = -1;    // swap body occurrences [lo, hi)
static unsigned g_bodyHide = 0;
static LONG g_drawIdx = 0;               // every DIP this frame
static LONG g_hrLo = -1, g_hrHi = -1;    // hide DIP indices [lo, hi)
static void readRange(const wchar_t* name, LONG* lo, LONG* hi) {
    wchar_t p[MAX_PATH]; lstrcpyW(p, FLAGDIR); lstrcatW(p, name);
    *lo = *hi = -1;
    HANDLE f = CreateFileW(p, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return;
    char b[64] = {0}; DWORD r = 0; ReadFile(f, b, 63, &r, NULL); CloseHandle(f);
    LONG v[2] = {0, 0}; int n = 0; bool in = false;
    for (DWORD i = 0; i <= r && n < 2; i++) {
        if (b[i] >= '0' && b[i] <= '9') { v[n] = v[n] * 10 + (b[i] - '0'); in = true; }
        else if (in) { n++; in = false; }
    }
    if (n == 2) { *lo = v[0]; *hi = v[1]; }
}          // bit k -> skip k-th body draw
static LONG g_bodyDraw = 0;              // body draws seen this frame
static IDirect3DVertexBuffer9* g_swapVB = NULL;
static IDirect3DVertexBuffer9* g_swapSrc = NULL;

static bool buildSwapVB(IDirect3DDevice9* d) {
    if (g_swapVB && g_swapSrc == g_vb) return true;
    if (g_swapVB) { g_swapVB->Release(); g_swapVB = NULL; }
    UINT len = BODY_NV * BODY_STRIDE;
    void* src = NULL;
    if (FAILED(g_vb->Lock(g_vbOff, len, &src, D3DLOCK_READONLY)) || !src) return false;
    BYTE* tmp = (BYTE*)HeapAlloc(GetProcessHeap(), 0, len);
    memcpy(tmp, src, len);
    g_vb->Unlock();
    for (UINT k = 0; k < BODY_NV; k++) {
        float* p = (float*)(tmp + k * BODY_STRIDE);
        p[0] *= SWAP_INFLATE_XZ; p[1] *= SWAP_STRETCH_Y; p[2] *= SWAP_INFLATE_XZ;
    }
    if (FAILED(d->CreateVertexBuffer(len, D3DUSAGE_WRITEONLY, 0, D3DPOOL_DEFAULT, &g_swapVB, NULL))) {
        HeapFree(GetProcessHeap(), 0, tmp); return false;
    }
    void* dst = NULL;
    if (SUCCEEDED(g_swapVB->Lock(0, len, &dst, 0)) && dst) { memcpy(dst, tmp, len); g_swapVB->Unlock(); }
    HeapFree(GetProcessHeap(), 0, tmp);
    g_swapSrc = g_vb;
    logline("swap VB built");
    return true;
}
static LONG g_grabSeq = 0;

struct Rec { DWORD nV, nP, stride, fvf, tex; };
static const int MAXR = 1 << 16;
static Rec g_recs[MAXR];
static volatile LONG g_n = 0;        // records this frame
static volatile LONG g_frame = 0;
static volatile LONG g_dumpLeft = 0; // frames left to dump
static volatile LONG g_wire = 0;

static bool flagExists(const wchar_t* name) {
    wchar_t p[MAX_PATH];
    lstrcpyW(p, FLAGDIR); lstrcatW(p, name);
    DWORD a = GetFileAttributesW(p);
    return a != INVALID_FILE_ATTRIBUTES;
}

// flag file content: decimal number (hide: bitmask of groups). Missing -> def.
static unsigned readFlagInt(const wchar_t* name, unsigned def) {
    wchar_t p[MAX_PATH];
    lstrcpyW(p, FLAGDIR); lstrcatW(p, name);
    HANDLE f = CreateFileW(p, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return def;
    char b[32] = {0}; DWORD r = 0;
    ReadFile(f, b, 31, &r, NULL);
    CloseHandle(f);
    unsigned v = 0; bool any = false;
    for (DWORD i = 0; i < r; i++) if (b[i] >= '0' && b[i] <= '9') { v = v * 10 + (b[i] - '0'); any = true; }
    return any ? v : def;
}

typedef HRESULT (STDMETHODCALLTYPE *DIP_FN)(IDirect3DDevice9*, D3DPRIMITIVETYPE, INT, UINT, UINT, UINT, UINT);
typedef HRESULT (STDMETHODCALLTYPE *PRESENT_FN)(IDirect3DDevice9*, const RECT*, const RECT*, HWND, const RGNDATA*);
typedef HRESULT (STDMETHODCALLTYPE *SSS_FN)(IDirect3DDevice9*, UINT, IDirect3DVertexBuffer9*, UINT, UINT);
typedef HRESULT (STDMETHODCALLTYPE *SVD_FN)(IDirect3DDevice9*, IDirect3DVertexDeclaration9*);
typedef HRESULT (STDMETHODCALLTYPE *SFVF_FN)(IDirect3DDevice9*, DWORD);
typedef HRESULT (STDMETHODCALLTYPE *SI_FN)(IDirect3DDevice9*, IDirect3DIndexBuffer9*);
typedef HRESULT (STDMETHODCALLTYPE *STEX_FN)(IDirect3DDevice9*, DWORD, IDirect3DBaseTexture9*);
typedef HRESULT (STDMETHODCALLTYPE *RS_FN)(IDirect3DDevice9*, D3DRENDERSTATETYPE, DWORD);
typedef HRESULT (STDMETHODCALLTYPE *CD_FN)(IDirect3D9*, UINT, D3DDEVTYPE, HWND, DWORD, D3DPRESENT_PARAMETERS*, IDirect3DDevice9**);
typedef IDirect3D9* (WINAPI *D3DC9_FN)(UINT);

static DIP_FN g_orgDIP = NULL;
static PRESENT_FN g_orgPresent = NULL;
static SSS_FN g_orgSSS = NULL;
static SVD_FN g_orgSVD = NULL;
static SFVF_FN g_orgSFVF = NULL;
static SI_FN g_orgSI = NULL;
static STEX_FN g_orgSTEX = NULL;
static RS_FN g_orgRS = NULL;
static CD_FN g_orgCD = NULL;
static D3DC9_FN g_realD3DC9 = NULL;
typedef HRESULT (STDMETHODCALLTYPE *SVSCF_FN)(IDirect3DDevice9*, UINT, const float*, UINT);
static SVSCF_FN g_orgSVSCF = NULL;
static SVSCF_FN g_orgSPSCF = NULL;   // SetPixelShaderConstantF, same signature
static const UINT PSC_N = 224;        // ps_3_0 float constant registers
static float g_psc[PSC_N][4];

// ---- custom body (tools/fmdl_to_pes12.py PGB1 / tools/pes15_to_pes12.py PGB2) ----
static const DWORD TEX_MAGIC = 0x31544750;      // 'PGT1'
static const DWORD CUSTOM_MAGIC = 0x31424750;   // 'PGB1': one texture, u16 indices
static const DWORD CUSTOM2_MAGIC = 0x32424750;  // 'PGB2': submeshes, u32 indices, flags
// The stock player, measured 26-09 from grabbed stock frames (tools/grab.py,
// bounds of each draw's vertices): one stride-76 skin draw (arms, legs, neck
// and bare hands; it samples the face atlas the marker rides on), then the
// LOD0 kit run opened by packet 5 (the shorts). The run's make-up varies per
// player - gloves add a draw, long sleeves merge two, boots differ by model -
// so its draws are classed by where their vertices sit, not by position:
//   kit pieces are in body space (bind pose, y up to 1.6 m), head pieces and
//   boots in their own bone's space (within 0.2 m of the origin).
enum Part { PART_SHORTS, PART_SHIRT, PART_SLEEVES, PART_SOCKS, PART_NECK, PART_GLOVES,
            PART_HEAD, PART_BOOTS, PART_OTHER, PART_COUNT };
static const unsigned PART_BIT = 1;
// The stock pieces a custom model keeps drawn, one bit each: the kit run's
// part classes, then the skin draw and the detail hands (bare hands; the
// keeper gloves follow PART_GLOVES, the detail boots PART_BOOTS). PGB2 header
// field 5 holds the mask (tools/pes15_to_pes12.py PIECES, same order);
// 4cc-players\custom\<name>\mode overrides it with the names of the pieces
// to keep, e.g. "shirt sleeves shorts socks" (an empty file keeps nothing).
enum { PIECE_SKIN = PART_COUNT, PIECE_HANDS, PIECE_COUNT };
static const char* PIECE_NAMES[PIECE_COUNT] = { "shorts", "shirt", "sleeves", "socks", "neck", "gloves",
                                                "head", "boots", "other", "skin", "hands" };
static bool keeps(DWORD keep, int piece) { return (keep >> piece) & 1; }
// class boundaries (metres, from the 26-09 bounds table)
static const float LOCAL_SPACE_MAX_Y = 0.3f;    // head pieces/boots top out at 0.17
static const float BOOT_MAX_Y = 0.03f;          // boots, and the bare hands (305/302 verts, hand-local), y -0.07..0.02; eyes/teeth sit at 0.04+
static const float BOOT_MIN_WIDTH = 0.1f;       // a boot is 0.15 wide on one side of x = 0; an eye 0.03
static const float SOCKS_MAX_Y = 0.55f;         // socks 0.06..0.51
static const float SHORTS_MAX_Y = 1.1f;         // shorts + shorts number 0.61..1.07
static const float TORSO_MAX_X = 0.25f;         // shirt, collar, back print within |x| 0.20
static const float GLOVES_MIN_X = 0.6f;         // gloves reach the hands at |x| 0.78; sleeves stop at 0.74
static const float NECK_MIN_Y = 1.6f;           // the neck (170 verts) rises to 1.62 above the collar's 1.58
static const float NONPLAYER_MIN_EXTENT = 5.0f; // stadium geometry: the run is over
// submesh flags (tools/pes15_to_pes12.py sub_flags, from the PES15 .mtl states)
static const DWORD SUB_ALPHATEST = 1, SUB_BLEND = 2, SUB_TWOSIDED = 4, SUB_NOZWRITE = 8, SUB_KIT = 16, SUB_OUTLINE = 32, SUB_FACE = 64;
// material shading (bits above the alpha ref): PES Shadeless/Constant draw
// the texture unlit, Pony cel-shaded - our pixel shaders (custom_ps.hlsl)
// replace the game's lit kit shader for those submeshes in the colour pass.
static const DWORD SUB_SHADELESS = 1u << 16, SUB_TOON = 1u << 17;
static const DWORD SUB_HAIR = 1u << 18;   // PES Hair shader: opaque, plus the alpha fringe pass
// One-hand parts (tools/pes15_to_pes12.py HAND_RIG_BONE): SUB_HAND_L/R on the
// rigid body copy, plus SUB_HAND_RIG on the hand-local copy weighted to the
// stock hand's 12-bone palette, drawn at the stock hand draw instead of it
// (its fingers then follow the game's hand animation); the body copy is
// skipped in a pass whose stock hand drew the rig copy.
static const DWORD SUB_HAND_L = 1u << 19, SUB_HAND_R = 1u << 20, SUB_HAND_RIG = 1u << 21;
static const DWORD SUB_HAND_SIDE[2] = { SUB_HAND_L, SUB_HAND_R };
// body.bin trailer after the indices (pes15_to_pes12 HRIG_MAGIC): per side
// the stock palette (slot -> rig bone), the rig parents and the model's own
// joints, hand-local (wrist at the origin, the hand bone binds unrotated)
static const DWORD HRIG_MAGIC = 0x47495248;   // 'HRIG'
static const UINT HAND_RIG_BONES = 12;        // dt0d #589's hand rig (pes12_rig.hand_rig)
static const UINT HRIG_SIDE_BYTES = HAND_RIG_BONES * (1 + 1 + 3 * sizeof(float));
// which submeshes a drawCustom call draws
enum { DRAW_BODY, DRAW_FACE, DRAW_HAND_L, DRAW_HAND_R };
static IDirect3DPixelShader9 *g_psShadeless = NULL, *g_psToon = NULL;
// Is vs the game's colour-pass kit shader (it outputs the UVs in TEXCOORD4;
// the depth and shadow passes' shaders do not)? Parsed from its dcl tokens.
static const DWORD D3DSIO_DCL_OP = 0x1F, USAGE_TEXCOORD = 5, UV_OUT_INDEX = 4, REG_OUTPUT = 6;   // vs_3_0 outputs are register type 6 (D3DSPR_TEXCRDOUT)
static bool isColourVS(IDirect3DVertexShader9* vs) {
    static void* known[16]; static bool val[16]; static int n = 0;
    if (!vs) return false;
    for (int i = 0; i < n; i++) if (known[i] == vs) return val[i];
    UINT sz = 0; bool colour = false;
    if (SUCCEEDED(vs->GetFunction(NULL, &sz)) && sz) {
        DWORD* t = (DWORD*)HeapAlloc(GetProcessHeap(), 0, sz); vs->GetFunction(t, &sz);
        for (UINT k = 1; k + 2 < sz / 4; k++) {
            if ((t[k] & 0xFFFF) != D3DSIO_DCL_OP) continue;
            DWORD usage = t[k + 1] & 0x1F, index = (t[k + 1] >> 16) & 0xF;
            DWORD reg = ((t[k + 2] >> 28) & 7) | ((t[k + 2] >> 8) & 0x18);
            if (usage == USAGE_TEXCOORD && index == UV_OUT_INDEX && reg == REG_OUTPUT) colour = true;
            k += 2;
        }
        HeapFree(GetProcessHeap(), 0, t);
    }
    if (n < 16) { known[n] = vs; val[n] = colour; n++; }
    return colour;
}
// the stock face mesh (dt0c face BIN, packet 3): drawn 669 or 670 verts, stride 88 (26-09 grabs)
static const UINT FACE_DRAW_NV_A = 669, FACE_DRAW_NV_B = 670, FACE_DRAW_STRIDE = 88;
// Outline shells sit a few mm outside the body; at match camera distance that
// is inside the depth buffer's resolution and the shell pokes through the
// body in jagged dark cracks (Kurisu, 26-09). Biased back, it only shows past
// the silhouette. PROVISIONAL values, tuned by eye on the 26-09 replay.
static const float OUTLINE_DEPTH_BIAS = 0.0002f;
static const float OUTLINE_SLOPE_BIAS = 2.0f;
static IDirect3DBaseTexture9* g_runKitTex = NULL;   // kit sheet bound for the current run (packet 5)
// kit sheet of the current player run, when it is one of ours (kitOfSheet)
static bool g_runKitOk = false; static int g_runTid = 0; static const wchar_t* g_runSlot = NULL;
static bool g_kitForceOff = false;                  // flags\\nokitforce: the game's own kit draws (A/B)
static bool g_kitPatchOff = false;                  // flags\\nokitpatch: no sheet patching at all (A/B; the
                                                   // patch writes into whichever sheet is bound, and
                                                   // that can be another team's - see tasks 01-10)
static const DWORD SUB_REF_SHIFT = 8, SUB_REF_MASK = 0xFF;
static LONG g_cuLo = -1, g_cuHi = -1;           // body occurrences drawn as custom
struct CustomSub { UINT first, count, tex, flags; };
static const int MAX_SUBS = 64, MAX_TEXS = 64;
struct CustomModel {
    char name[32];
    IDirect3DVertexBuffer9* vb; IDirect3DIndexBuffer9* ib;
    IDirect3DTexture9* tex[MAX_TEXS]; UINT ntex;
    CustomSub sub[MAX_SUBS]; UINT nsub;
    UINT nv, ni, stride; DWORD keep; bool tried;   // keep: stock pieces drawn (PIECE_NAMES bits)
    bool handRig[2];                             // has SUB_HAND_RIG subs and their HRIG joints, per side (L, R)
    BYTE hrigBone[2][HAND_RIG_BONES];            // HRIG trailer: palette slot -> rig bone
    signed char hrigParent[2][HAND_RIG_BONES];   // rig bone -> parent (-1: the wrist root)
    float hrigJoint[2][HAND_RIG_BONES][3];       // the model's joints, hand-local, by rig bone
    bool handCaught[2];                          // its stock hand drew this frame: handRel holds the pose
    float handRel[2][HAND_RIG_BONES * BONE_REGS][4];   // each palette slot relative to the rig root
    LONG pid, lastUsed;                          // resident-cache key, frame of last use
    float minY;                                  // lowest vertex (m): does it stand on its own
};
// Bodies are keyed by player id (the face marker IS the pid, tools/mark_face.py)
// and hot-loaded from custom\p<pid>\ on first draw; the cache holds this many
// and evicts the least recently drawn. Two teams' starters (22) plus menu and
// cutscene players fit with room to spare; any number can be installed.
static const int MAX_RESIDENT = 48;
static CustomModel g_models[MAX_RESIDENT]; static int g_nmodels = 0;
static LONG g_curModel = -1;                    // pid of the player whose run is open
static CustomModel* g_cuM = NULL;               // model drawCustom() draws
static bool g_cuKeepGameTex = false;
static bool g_cuGK = false;
static bool g_cuCullCW = false;
static LONG g_kitRun = 0;              // packet-5 occurrences this frame
static LONG g_cuDrawn = 0;             // custom LOD0 draws this frame
static LONG g_runPos = -1;             // draws since the current kit run opened (0 = packet 5), -1 outside
static bool g_runLog = false;           // flags\runlog: log every run draw of one frame
static IDirect3DSurface9* g_rtDump = NULL; static bool g_rtDumpArm = false;
static unsigned g_pMask = 0;           // flags\pmask: debug, hide these Part classes on every stock run
static float g_hipM[3][4];
static IDirect3DVertexDeclaration9* g_kitDecl = NULL;
static IDirect3DVertexShader9* g_kitVS = NULL;
static IDirect3DPixelShader9* g_kitPS = NULL;   // the kit draw's pixel shader (officials bind it: theirs expects other inputs)
// The last kit draw's shaders per pass kind ([1] colour, [0] depth/shadow;
// isColourVS). An official can come before any player in its pass, so he
// takes the set of his own pass, not simply the last one seen.
static IDirect3DVertexDeclaration9* g_passDecl[2]; static IDirect3DVertexShader9* g_passVS[2]; static IDirect3DPixelShader9* g_passPS[2];
// ... and its stages 1-7: the kit pixel shader reads lighting lookups there,
// which an official's own draw may bind to his textures (01-10: one
// official lit, the other black with stages 1/2 = his own textures).
static const int KIT_STAGES = 8;
static IDirect3DBaseTexture9* g_passStage[2][KIT_STAGES];
// ... and its VS constants: the kit VS reads lighting outside the bone block,
// where an official's draw holds his own shader's values (01-10: lit custom
// materials black on the referee, fine on the linesman; shadeless ones fine).
static float g_passVSC[2][256][4];
static float g_passPSC[2][PSC_N][4];   // ... and its PS constants (light colours)

static BYTE* readAll(const wchar_t* path, DWORD* size) {
    HANDLE f = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return NULL;
    *size = GetFileSize(f, NULL);
    BYTE* b = (BYTE*)HeapAlloc(GetProcessHeap(), 0, *size);
    DWORD r = 0; ReadFile(f, b, *size, &r, NULL); CloseHandle(f);
    return b;
}

// body_<k>.tex: either PGT1 (u32 magic, w, h, mips, then BGRA8 mips largest
// first; tools/fmdl_to_pes12.py) or a DXT1/3/5 DDS uploaded as is
// (tools/pes15_to_pes12.py: the packs' own compression, full size).
static const DWORD DDS_MAGIC = 0x20534444;             // 'DDS '
static const UINT DDS_HEADER_BYTES = 128, DDS_H_OFF = 12, DDS_W_OFF = 16, DDS_MIPS_OFF = 28, DDS_FOURCC_OFF = 84;
static const UINT DXT_BLOCK_PX = 4, DXT1_BLOCK_BYTES = 8, DXT35_BLOCK_BYTES = 16;
static IDirect3DTexture9* loadDDS(IDirect3DDevice9* d, BYTE* b, DWORD n) {
    UINT h = *(DWORD*)(b + DDS_H_OFF), w = *(DWORD*)(b + DDS_W_OFF), mips = *(DWORD*)(b + DDS_MIPS_OFF);
    DWORD four = *(DWORD*)(b + DDS_FOURCC_OFF);
    D3DFORMAT fmt = (D3DFORMAT)four;           // D3DFMT_DXTn are the fourcc codes
    UINT blk = four == MAKEFOURCC('D', 'X', 'T', '1') ? DXT1_BLOCK_BYTES : DXT35_BLOCK_BYTES;
    if (four != MAKEFOURCC('D', 'X', 'T', '1') && four != MAKEFOURCC('D', 'X', 'T', '3') && four != MAKEFOURCC('D', 'X', 'T', '5')) return NULL;
    if (!mips) mips = 1;
    IDirect3DTexture9* t = NULL;
    if (FAILED(d->CreateTexture(w, h, mips, 0, fmt, D3DPOOL_MANAGED, &t, NULL))) return NULL;
    BYTE* src = b + DDS_HEADER_BYTES;
    for (UINT m = 0; m < mips; m++) {
        UINT mw = w >> m, mh = h >> m; if (!mw) mw = 1; if (!mh) mh = 1;
        UINT rows = (mh + DXT_BLOCK_PX - 1) / DXT_BLOCK_PX, rowBytes = (mw + DXT_BLOCK_PX - 1) / DXT_BLOCK_PX * blk;
        if (src + rows * rowBytes > b + n) break;
        D3DLOCKED_RECT lr;
        if (SUCCEEDED(t->LockRect(m, &lr, NULL, 0))) {
            // one pitch step per ROW OF BLOCKS, not per pixel row
            for (UINT r = 0; r < rows; r++) memcpy((BYTE*)lr.pBits + r * lr.Pitch, src + r * rowBytes, rowBytes);
            t->UnlockRect(m);
        }
        src += rows * rowBytes;
    }
    return t;
}
static IDirect3DTexture9* loadTex(IDirect3DDevice9* d, const wchar_t* path) {
    DWORD n = 0; BYTE* b = readAll(path, &n);
    if (!b) return NULL;
    DWORD* hd = (DWORD*)b;
    IDirect3DTexture9* t = NULL;
    // PGT1 .tex: u32 magic, w, h, mips, then RGBA8 mips (pgb2.build_tex)
    const DWORD TEX_HEADER_BYTES = 16, TEX_PX_BYTES = 4;
    if (n >= DDS_HEADER_BYTES && hd[0] == DDS_MAGIC) t = loadDDS(d, b, n);
    else if (n >= TEX_HEADER_BYTES && hd[0] == TEX_MAGIC && SUCCEEDED(d->CreateTexture(hd[1], hd[2], hd[3], 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED, &t, NULL))) {
        BYTE* src = b + TEX_HEADER_BYTES;
        for (DWORD m = 0; m < hd[3]; m++) {
            UINT mw = hd[1] >> m, mh = hd[2] >> m; if (!mw) mw = 1; if (!mh) mh = 1;
            // a short file (e.g. RGB written where RGBA is read) stops here,
            // not past the heap buffer: that overrun crashed the game 01-10
            if (src + mw * mh * TEX_PX_BYTES > b + n) break;
            D3DLOCKED_RECT lr;
            if (SUCCEEDED(t->LockRect(m, &lr, NULL, 0))) {
                for (UINT y = 0; y < mh; y++) memcpy((BYTE*)lr.pBits + y * lr.Pitch, src + y * mw * TEX_PX_BYTES, mw * TEX_PX_BYTES);
                t->UnlockRect(m);
            }
            src += mw * mh * TEX_PX_BYTES;
        }
    }
    HeapFree(GetProcessHeap(), 0, b);
    return t;
}

// ---- player identity: fserv serves each custom player (GDB map.txt, keyed by
// player id) a face.bin stamped by tools/mark_face.py; the game builds it into
// the face atlas drawn right before his kit, where the PGB tag is searched.
static const UINT FACE_NV = 1478, FACE_STRIDE = 76;   // shared head mesh (24-09)
static const UINT FACE_MAX_NV = 4000;                  // head/hair draws are below this (25-09: 1478 face, 1780 hair)
static const UINT MARKER_SEARCH_LEVELS = 5;            // smallest atlas mips searched
static const int MAX_TEXCACHE = 256;
static DWORD g_tcTex[MAX_TEXCACHE]; static int g_tcMark[MAX_TEXCACHE]; static int g_ntc = 0;
static LONG g_pendingModel = -1;                        // pid set by a marked face draw
// how the pending / current model was decided (attribution log, 02-10)
enum { SRC_NONE, SRC_MARKER, SRC_KEY, SRC_CARRIED };
static const char* SRC_NAME[] = { "none", "marker", "key", "carried" };
static int g_pendingSrc = SRC_NONE, g_curSrc = SRC_NONE; static bool g_hipColour = false;
// marker unit (tools/mark_face.py): 'P' 'G' 'D' pid (u24 LE) sum ~sum, sum = byte sum of the pid
static const UINT MARKER_BYTES = 8;
static int readMarker(const BYTE* q) {
    if (q[0] != 'P' || q[1] != 'G' || q[2] != 'D') return -1;
    BYTE s = (BYTE)(q[3] + q[4] + q[5]);
    if (q[6] != s || (BYTE)(q[6] + q[7]) != 0xFF) return -1;
    return q[3] | q[4] << 8 | q[5] << 16;
}
static int findMarker(IDirect3DBaseTexture9* bt) {
    if (!bt) return -1;
    for (int i = 0; i < g_ntc; i++) if (g_tcTex[i] == (DWORD)bt) return g_tcMark[i];
    // keyed by texture pointer, so it only lives one frame (logic_present
    // clears it): the game frees a match's face atlases and the next match's
    // reuse their addresses - a cache that outlived the frame drew the
    // previous match's /g/ bodies on /u/ in the pre-match screen (29-09).
    if (g_ntc == MAX_TEXCACHE) g_ntc = 0;
    int mark = -1;
    if (bt->GetType() == D3DRTYPE_TEXTURE) {
        IDirect3DTexture9* t = (IDirect3DTexture9*)bt;
        DWORD lv = t->GetLevelCount();
        for (DWORD l = (lv > MARKER_SEARCH_LEVELS ? lv - MARKER_SEARCH_LEVELS : 0); l < lv && mark < 0; l++) {
            D3DSURFACE_DESC sd; t->GetLevelDesc(l, &sd);
            UINT bytes = ((sd.Width + 3) / 4) * ((sd.Height + 3) / 4) * 16;
            D3DLOCKED_RECT lr;
            if (FAILED(t->LockRect(l, &lr, NULL, D3DLOCK_READONLY))) continue;
            BYTE* b = (BYTE*)lr.pBits;
            UINT rows = (sd.Height + 3) / 4, rowBytes = ((sd.Width + 3) / 4) * 16;
            for (UINT r = 0; r < rows && mark < 0; r++)
                for (UINT i = 0; i + MARKER_BYTES <= rowBytes; i++) {
                    int v = readMarker(b + r * lr.Pitch + i);
                    if (v >= 0) { mark = v; break; }
                }
            t->UnlockRect(l);
            (void)bytes;
        }
    }
    g_tcTex[g_ntc] = (DWORD)bt; g_tcMark[g_ntc] = mark; g_ntc++;
    if (mark >= 0 && g_frame % 300 == 0) { char m[80]; wsprintfA(m, "marker %d on face tex %08x", mark, (DWORD)bt); logline(m); }
    return mark;
}
// ---- team kits: kserv cannot serve the 4cc DLC teams (ids 701+ are not in
// its team table and selecting one crashes, 26-09), so drawlogic writes the
// team's PES2012-layout kit sheet (tools/pes15_kits.py -> custom\kits\<tid>\
// <slot>.tex) straight into the kit texture the game bound for the run. The
// game loads the DLC's placeholder sheet for every 4cc team; which of its
// strips it loaded says which of the team's kits to write.
static const UINT KIT_W = 1024, KIT_H = 512;               // A8R8G8B8, 11 levels (26-09)
static const DWORD KIT_HASH_LEVEL_FROM_END = 3;            // 4x2 level: cheap, still distinct
struct KitSlot { DWORD hash; const wchar_t* slot; };
// placeholder strip -> team kit folder name. MEASURED: 1st player strip,
// both teams at kick-off 26-09. The 2nd and GK strips are not measured yet:
// their hashes log as "kit: unknown strip" and are left as the game drew them.
static const KitSlot KIT_SLOTS[] = { { 0x2552e187u, L"pa" } };
static DWORD kitHash(IDirect3DTexture9* t) {
    DWORD lv = t->GetLevelCount(), l = lv > KIT_HASH_LEVEL_FROM_END ? lv - KIT_HASH_LEVEL_FROM_END : 0, h = 2166136261u;
    D3DSURFACE_DESC sd; t->GetLevelDesc(l, &sd);
    D3DLOCKED_RECT lr;
    if (SUCCEEDED(t->LockRect(l, &lr, NULL, D3DLOCK_READONLY))) {
        for (UINT y = 0; y < sd.Height; y++)
            for (UINT i = 0; i < sd.Width * 4; i++) h = (h ^ ((BYTE*)lr.pBits)[y * lr.Pitch + i]) * 16777619u;
        t->UnlockRect(l);
    }
    return h;
}
static const int MAX_KITCACHE = 64;
static DWORD g_kcTex[MAX_KITCACHE], g_kcHash[MAX_KITCACHE]; static int g_nkc = 0;
static const wchar_t* g_kcSlot[MAX_KITCACHE];   // which team kit patchKit wrote into that texture
// the kit last identified for a team: replays and cutscenes bind a 512 x 512
// DXT5 kit sheet patchKit cannot identify (30-09), worn by the same team
static const int MAX_TEAM_SLOTS = 16;
static int g_tsTeam[MAX_TEAM_SLOTS]; static const wchar_t* g_tsSlot[MAX_TEAM_SLOTS]; static int g_nts = 0;
static void noteTeamSlot(int tid, const wchar_t* slot) {
    for (int i = 0; i < g_nts; i++) if (g_tsTeam[i] == tid) { g_tsSlot[i] = slot; return; }
    if (g_nts < MAX_TEAM_SLOTS) { g_tsTeam[g_nts] = tid; g_tsSlot[g_nts++] = slot; }
}
static const wchar_t* kitSlotOf(IDirect3DBaseTexture9* t, int tid) {
    for (int i = 0; i < g_nkc; i++) if (g_kcTex[i] == (DWORD)t) return g_kcSlot[i];
    for (int i = 0; i < g_nts; i++) if (g_tsTeam[i] == tid) return g_tsSlot[i];
    return NULL;
}
// patchKit writes the team's pack sheet into the sheet the game has bound on
// that team's custom player's hip. Only a colour-pass hip whose model came
// from the face marker may call it (see the call site): elsewhere the bound
// texture is not that player's sheet.
static void patchKit(int tid, IDirect3DBaseTexture9* bt) {
    if (tid <= 0 || !bt || bt->GetType() != D3DRTYPE_TEXTURE) return;
    IDirect3DTexture9* t = (IDirect3DTexture9*)bt;
    D3DSURFACE_DESC sd; t->GetLevelDesc(0, &sd);
    if (sd.Width != KIT_W || sd.Height != KIT_H || sd.Format != D3DFMT_A8R8G8B8) {
        static DWORD warned[16]; static int nw = 0; bool seen = false;
        for (int i = 0; i < nw; i++) seen |= warned[i] == (DWORD)t;
        if (!seen && nw < 16) { warned[nw++] = (DWORD)t; char m[128]; wsprintfA(m, "kit: team %d sheet %08x is %ux%u fmt %08x, not patchable", tid, (DWORD)t, sd.Width, sd.Height, (DWORD)sd.Format); logline(m); }
        return;
    }
    DWORD h = kitHash(t);
    for (int i = 0; i < g_nkc; i++) if (g_kcTex[i] == (DWORD)t && g_kcHash[i] == h) return;   // ours already
    const wchar_t* slot = NULL;
    for (UINT i = 0; i < sizeof(KIT_SLOTS) / sizeof(KIT_SLOTS[0]); i++) if (KIT_SLOTS[i].hash == h) slot = KIT_SLOTS[i].slot;
    if (!slot) {
        static DWORD warned[16]; static int nw = 0; bool seen = false;
        for (int i = 0; i < nw; i++) seen |= warned[i] == h;
        if (!seen && nw < 16) {
            warned[nw++] = h; char m[96]; wsprintfA(m, "kit: unknown strip hash %08x (team %d)", h, tid); logline(m);
            // the sheet itself, level 0 raw A8R8G8B8, to tell which kit it is:
            // custom\kits\unknown_<hash>.raw (1024 x 512)
            wchar_t p[MAX_PATH]; wsprintfW(p, L"%sunknown_%08x.raw", KIT_DIR, h);
            D3DLOCKED_RECT lr;
            if (SUCCEEDED(t->LockRect(0, &lr, NULL, D3DLOCK_READONLY))) {
                HANDLE f = CreateFileW(p, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
                DWORD w; for (UINT y = 0; y < KIT_H; y++) WriteFile(f, (BYTE*)lr.pBits + y * lr.Pitch, KIT_W * 4, &w, NULL);
                CloseHandle(f); t->UnlockRect(0);
            }
        }
        return;
    }
    wchar_t path[MAX_PATH]; wsprintfW(path, L"%s%d\\%s.tex", KIT_DIR, tid, slot);
    DWORD n = 0; BYTE* b = readAll(path, &n);
    if (!b) return;
    DWORD* hd = (DWORD*)b;
    if (hd[0] == TEX_MAGIC && hd[1] == KIT_W && hd[2] == KIT_H) {
        BYTE* src = b + 16;
        DWORD lv = t->GetLevelCount() < hd[3] ? t->GetLevelCount() : hd[3];
        for (DWORD m = 0; m < lv; m++) {
            UINT mw = KIT_W >> m, mh = KIT_H >> m; if (!mw) mw = 1; if (!mh) mh = 1;
            D3DLOCKED_RECT lr;
            if (SUCCEEDED(t->LockRect(m, &lr, NULL, 0))) {
                for (UINT y = 0; y < mh; y++) memcpy((BYTE*)lr.pBits + y * lr.Pitch, src + y * mw * 4, mw * 4);
                t->UnlockRect(m);
            }
            src += mw * mh * 4;
        }
        DWORD after = kitHash(t);
        int k = -1;
        for (int i = 0; i < g_nkc; i++) if (g_kcTex[i] == (DWORD)t) k = i;
        if (k < 0 && g_nkc < MAX_KITCACHE) k = g_nkc++;
        if (k >= 0) { g_kcTex[k] = (DWORD)t; g_kcHash[k] = after; g_kcSlot[k] = slot; }
        noteTeamSlot(tid, slot);
        char m[160]; wsprintfA(m, "kit: team %d %S -> tex %08x (pid %d by %s, %s pass)", tid, slot, (DWORD)t, (int)g_curModel, SRC_NAME[g_curSrc], g_hipColour ? "colour" : "other"); logline(m);
    }
    HeapFree(GetProcessHeap(), 0, b);
}
// custom model p<pid>, pid = (2000 + tid) * 100 + n (tools/pes12_import_team.py)
static const int PLACEHOLDER_TEAM_BASE = 2000, PLAYERS_PER_TEAM = 100;
static int modelTeam(const CustomModel& M) { return M.pid / PLAYERS_PER_TEAM - PLACEHOLDER_TEAM_BASE; }
// ---- full-resolution kits for custom models. The game's kit sheet is
// 1024 x 512 and a custom model's kit UVs reach it through a second remap
// (tools/pes15_kits.py), so its kit came out blurry (30-09). Its kit
// submeshes keep the pack's own UVs in TEXCOORD0 (the remap in TEXCOORD1)
// and are drawn with the pack's own sheet, custom\kits\<tid>\<slot>_hi.dds,
// for the kit patchKit wrote into the texture the game bound.
static const wchar_t* HI_KIT_SUFFIX = L"_hi.dds";        // tools/pes15_kits.py HI_KIT_SUFFIX
// the kit vertex shader's UV output is c176.x * TEXCOORD0 + c176.y * TEXCOORD1
static const UINT UV_SELECT_REG = 176;
static const float UV_SELECT_SET0[4] = { 1, 0, 0, 0 }, UV_SELECT_SET1[4] = { 0, 1, 0, 0 };
static const int MAX_HIKITS = 16;
struct HiKit { int tid; const wchar_t* slot; IDirect3DTexture9* tex; };
static HiKit g_hiKits[MAX_HIKITS]; static int g_nHiKits = 0;
// flags\kituv: the forced kit path draws a UV grid over the PES14+ sheet
// instead of the team's <slot>_hi.dds, to localise misregistration (02-10).
// Cells: UV_GRID_CELLS per axis, red = u cell, green = v cell, blue = checker
// parity; thin dark lines every 1/UV_GRID_FINE, white lines on cell borders.
static bool g_kitUV = false;
static IDirect3DTexture9* g_uvGrid = NULL;
static const UINT UV_GRID_PX = 1024;            // level 0 size; the sheet it stands in for is 2048
static const int UV_GRID_CELLS = 16, UV_GRID_FINE = 64;
static const float UV_GRID_FINE_W = 0.08f;      // line width as a fraction of a fine cell (~1.3 px at level 0)
static const float UV_GRID_CELL_W = 0.05f;      // line width as a fraction of a cell (~3 px at level 0)
static const BYTE UV_GRID_LO = 40, UV_GRID_STEP = 13, UV_GRID_PAR_HI = 220;   // channel ramp: 40 .. 235
static IDirect3DTexture9* uvGrid(IDirect3DDevice9* d) {
    if (g_uvGrid || FAILED(d->CreateTexture(UV_GRID_PX, UV_GRID_PX, 0, 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED, &g_uvGrid, NULL))) return g_uvGrid;
    for (DWORD lv = 0; lv < g_uvGrid->GetLevelCount(); lv++) {
        D3DSURFACE_DESC sd; g_uvGrid->GetLevelDesc(lv, &sd);
        D3DLOCKED_RECT lr; if (FAILED(g_uvGrid->LockRect(lv, &lr, NULL, 0))) continue;
        for (UINT y = 0; y < sd.Height; y++) {
            DWORD* row = (DWORD*)((BYTE*)lr.pBits + y * lr.Pitch);
            float v = (y + 0.5f) / sd.Height;
            for (UINT x = 0; x < sd.Width; x++) {
                float u = (x + 0.5f) / sd.Width;
                int cu = (int)(u * UV_GRID_CELLS), cv = (int)(v * UV_GRID_CELLS);
                float fu = u * UV_GRID_CELLS - cu, fv = v * UV_GRID_CELLS - cv;
                float gu = u * UV_GRID_FINE - (int)(u * UV_GRID_FINE), gv = v * UV_GRID_FINE - (int)(v * UV_GRID_FINE);
                DWORD c = D3DCOLOR_ARGB(255, UV_GRID_LO + cu * UV_GRID_STEP, UV_GRID_LO + cv * UV_GRID_STEP, ((cu + cv) & 1) ? UV_GRID_PAR_HI : UV_GRID_LO);
                if (gu < UV_GRID_FINE_W || gv < UV_GRID_FINE_W) c = D3DCOLOR_ARGB(255, 0, 0, 0);
                if (fu < UV_GRID_CELL_W || fv < UV_GRID_CELL_W) c = D3DCOLOR_ARGB(255, 255, 255, 255);
                row[x] = c;
            }
        }
        g_uvGrid->UnlockRect(lv);
    }
    return g_uvGrid;
}
static IDirect3DTexture9* hiKit(IDirect3DDevice9* d, int tid, const wchar_t* slot) {
    if (!slot) return NULL;
    if (g_kitUV) return uvGrid(d);
    for (int i = 0; i < g_nHiKits; i++) if (g_hiKits[i].tid == tid && g_hiKits[i].slot == slot) return g_hiKits[i].tex;
    wchar_t path[MAX_PATH]; wsprintfW(path, L"%s%d\\%s%s", KIT_DIR, tid, slot, HI_KIT_SUFFIX);
    IDirect3DTexture9* t = loadTex(d, path);
    if (g_nHiKits < MAX_HIKITS) { g_hiKits[g_nHiKits].tid = tid; g_hiKits[g_nHiKits].slot = slot; g_hiKits[g_nHiKits++].tex = t; }
    char m[128]; wsprintfA(m, "kit: team %d %S full-res sheet %s", tid, slot, t ? "loaded" : "missing"); logline(m);
    return t;
}

// ---- identity outside the colour pass. A frame draws every player four
// times (26-09 grab): depth pre-pass (cutscene depth of field reads it),
// two shadow passes, colour pass last. Only the colour pass's skin draw
// samples the per-player face atlas carrying the marker, so without this the
// depth pass drew every custom player as the stock body and the DoF focused
// on the stock head (box heads blurred round a sharp head-sized disk). The
// depth and colour passes upload identical bone matrices, so a player's key
// is his palette slot 0 translation at the skin draw: the colour pass records
// key -> model, the next frame's depth pass looks it up.
static const int MAX_KEYS = 64;
static const float KEY_MATCH_DIST = 0.25f;      // metres moved per frame, well above a sprint (~0.15)
struct PlayerKey { float p[3]; LONG model; };   // model = pid
static PlayerKey g_keyCur[MAX_KEYS], g_keyPrev[MAX_KEYS]; static int g_nKeyCur = 0, g_nKeyPrev = 0;
static void runKey(float out[3]) { for (int c = 0; c < 3; c++) out[c] = g_vsc[BONE_REG0 + c][3]; }
static LONG modelForKey(const float k[3]) {
    int best = -1; float bd = KEY_MATCH_DIST * KEY_MATCH_DIST;
    for (int i = 0; i < g_nKeyPrev; i++) {
        float d = 0; for (int c = 0; c < 3; c++) { float e = g_keyPrev[i].p[c] - k[c]; d += e * e; }
        if (d < bd) { bd = d; best = g_keyPrev[i].model; }
    }
    return best;
}

static void releaseModel(CustomModel& M) {
    if (M.vb) M.vb->Release(); if (M.ib) M.ib->Release();
    for (UINT i = 0; i < M.ntex; i++) if (M.tex[i]) M.tex[i]->Release();
    M.vb = NULL; M.ib = NULL; M.ntex = 0;
}
// 4cc-players\custom\<name>\mode: the stock pieces to keep (PIECE_NAMES,
// separated by anything that is not a letter) -> mask; no file -> false
static bool readModeFile(const wchar_t* dir, DWORD* keep) {
    wchar_t p[MAX_PATH]; lstrcpyW(p, dir); lstrcatW(p, L"mode");
    HANDLE f = CreateFileW(p, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return false;
    char b[256] = {0}; DWORD r = 0; ReadFile(f, b, sizeof(b) - 1, &r, NULL); CloseHandle(f);
    *keep = 0;
    for (DWORD i = 0; i < r; ) {
        while (i < r && !((b[i] | 0x20) >= 'a' && (b[i] | 0x20) <= 'z')) i++;
        DWORD j = i; while (j < r && (b[j] | 0x20) >= 'a' && (b[j] | 0x20) <= 'z') { b[j] |= 0x20; j++; }
        if (j == i) break;
        int k = 0; while (k < PIECE_COUNT && !(lstrlenA(PIECE_NAMES[k]) == (int)(j - i) && !memcmp(b + i, PIECE_NAMES[k], j - i))) k++;
        if (k < PIECE_COUNT) *keep |= PART_BIT << k;
        else { char m[96]; b[j] = 0; wsprintfA(m, "custom: unknown stock piece '%s' in mode", b + i); logline(m); }
        i = j;
    }
    return true;
}
// dir: folder holding body.bin (+ body.tex | body_<k>.tex)
static bool loadModel(IDirect3DDevice9* d, CustomModel& M, const wchar_t* dir) {
    wchar_t path[MAX_PATH]; lstrcpyW(path, dir); lstrcatW(path, L"body.bin");
    DWORD n = 0; BYTE* b = readAll(path, &n);
    if (!b) { logline("custom: no body.bin"); return false; }
    DWORD* hd = (DWORD*)b;
    bool v2 = hd[0] == CUSTOM2_MAGIC;
    if (hd[0] != CUSTOM_MAGIC && !v2) { logline("custom: bad header"); HeapFree(GetProcessHeap(), 0, b); return false; }
    M.nv = hd[1]; M.ni = hd[2]; M.stride = hd[3];
    BYTE* at = b + 16;
    if (v2) {
        M.nsub = hd[4] < (DWORD)MAX_SUBS ? hd[4] : MAX_SUBS; M.keep = hd[5];
        memcpy(M.sub, b + 24, M.nsub * sizeof(CustomSub));
        at = b + 24 + hd[4] * sizeof(CustomSub);
    } else {
        M.nsub = 1; M.keep = 0; M.sub[0].first = 0; M.sub[0].count = M.ni; M.sub[0].tex = 0; M.sub[0].flags = 0;
    }
    { DWORD k; if (readModeFile(dir, &k)) M.keep = k; }
    M.keep &= (PART_BIT << PIECE_COUNT) - 1;
    UINT isz = v2 ? 4 : 2, vlen = M.nv * M.stride, ilen = M.ni * isz;
    void* p = NULL;
    bool ok = SUCCEEDED(d->CreateVertexBuffer(vlen, D3DUSAGE_WRITEONLY, 0, D3DPOOL_MANAGED, &M.vb, NULL))
           && SUCCEEDED(d->CreateIndexBuffer(ilen, D3DUSAGE_WRITEONLY, v2 ? D3DFMT_INDEX32 : D3DFMT_INDEX16, D3DPOOL_MANAGED, &M.ib, NULL));
    if (ok && SUCCEEDED(M.vb->Lock(0, vlen, &p, 0))) { memcpy(p, at, vlen); M.vb->Unlock(); }
    M.minY = 1e9f;   // POSITION is the vertex's first 3 floats (PGB2, pgb2.VERT_FMT)
    if (ok) for (UINT i = 0; i < M.nv; i++) { float y = *(const float*)(at + i * M.stride + sizeof(float)); if (y < M.minY) M.minY = y; }
    if (ok && SUCCEEDED(M.ib->Lock(0, ilen, &p, 0))) { memcpy(p, at + vlen, ilen); M.ib->Unlock(); }
    const BYTE* tr = at + vlen + ilen;
    bool hrig = v2 && tr + sizeof(DWORD) + 2 * HRIG_SIDE_BYTES <= b + n && *(const DWORD*)tr == HRIG_MAGIC;
    for (int h = 0; h < 2; h++) {
        M.handRig[h] = M.handCaught[h] = false;
        if (!hrig) continue;
        const BYTE* q = tr + sizeof(DWORD) + h * HRIG_SIDE_BYTES;
        memcpy(M.hrigBone[h], q, HAND_RIG_BONES);
        memcpy(M.hrigParent[h], q + HAND_RIG_BONES, HAND_RIG_BONES);
        memcpy(M.hrigJoint[h], q + 2 * HAND_RIG_BONES, sizeof(M.hrigJoint[h]));
        for (UINT i = 0; i < M.nsub; i++) if ((M.sub[i].flags & SUB_HAND_RIG) && (M.sub[i].flags & SUB_HAND_SIDE[h])) M.handRig[h] = true;
    }
    HeapFree(GetProcessHeap(), 0, b);
    M.ntex = 0;
    for (UINT i = 0; i < M.nsub; i++) if (M.sub[i].tex + 1 > M.ntex) M.ntex = M.sub[i].tex + 1;
    if (M.ntex > (UINT)MAX_TEXS) M.ntex = MAX_TEXS;
    for (UINT k = 0; k < M.ntex; k++) {
        lstrcpyW(path, dir);
        if (v2) { wchar_t f[32]; wsprintfW(f, L"body_%u.tex", k); lstrcatW(path, f); } else lstrcatW(path, L"body.tex");
        M.tex[k] = loadTex(d, path);
        if (!M.tex[k]) { char e[MAX_PATH + 48]; wsprintfA(e, "custom %s: texture %u failed to load (%S)", M.name, k, path); logline(e); }
    }
    char m[160]; wsprintfA(m, "custom %s: ok=%d nv=%u ni=%u subs=%u texs=%u keep=%03x", M.name, (int)ok, M.nv, M.ni, M.nsub, M.ntex, M.keep); logline(m);
    if (!ok) releaseModel(M);
    return ok;
}
// select the body of player pid: resident, else hot-loaded into a free or
// the least recently drawn slot
static bool useModel(IDirect3DDevice9* d, LONG pid) {
    if (pid < 0) return false;
    int m = -1, lru = -1;
    for (int i = 0; i < g_nmodels && m < 0; i++) {
        if (g_models[i].pid == pid) m = i;
        else if (lru < 0 || g_models[i].lastUsed < g_models[lru].lastUsed) lru = i;
    }
    if (m < 0) {
        m = g_nmodels < MAX_RESIDENT ? g_nmodels++ : lru;
        CustomModel& E = g_models[m];
        if (E.vb) { char l[64]; wsprintfA(l, "custom %s evicted", E.name); logline(l); }
        releaseModel(E); memset(&E, 0, sizeof(E));
        E.pid = pid; wsprintfA(E.name, "p%ld", pid);
    }
    CustomModel& M = g_models[m];
    M.lastUsed = g_frame;
    if (!M.vb && !M.tried) {
        M.tried = true;
        wchar_t dir[MAX_PATH]; wsprintfW(dir, L"%s%S\\", CUSTOM_DIR, M.name);
        loadModel(d, M, dir);
    }
    if (!M.vb) return false;
    g_cuM = &M;
    return true;
}

static CustomModel g_default;
static bool loadCustom(IDirect3DDevice9* d) {
    if (!g_default.vb && !g_default.tried) { g_default.tried = true; lstrcpyA(g_default.name, "default"); loadModel(d, g_default, CUSTOM_DIR); }
    if (!g_default.vb) return false;
    g_cuM = &g_default;
    return true;
}

// The adboard sheet: custom\boards\board_0.tex (DDS or .tex) if present,
// built once per session. No file = stock boards, like every other
// drawlogic path with nothing installed.
static bool adboardTex(IDirect3DDevice9* d) {
    if (!g_adTex && !g_adTried) {
        g_adTried = true;
        wchar_t path[MAX_PATH]; lstrcpyW(path, CUSTOM_DIR); lstrcatW(path, L"boards\\board_0.tex");
        g_adTex = loadTex(d, path);
        char m[80]; wsprintfA(m, "adboards: %s", g_adTex ? "custom sheet loaded" : "no custom sheet, stock");
        logline(m);
    }
    return g_adTex != NULL;
}

static HRESULT drawCustom(IDirect3DDevice9* d, int part = DRAW_BODY);

static const UINT FACE_PRINT_BYTES = 32;        // vertex bytes fingerprinting a face draw
static IDirect3DBaseTexture9* g_prevTex = NULL;
static IDirect3DVertexBuffer9* g_prevVB = NULL; static UINT g_prevOff = 0, g_prevStride = 0, g_prevFirst = 0, g_prevNV = 0;
static int g_facelog = 0;

// ---- stock detail hands and boots. Close to the camera PES2012 draws each
// player's hands (466 v, both hands) and boots (930 v, both feet; 228 v per
// foot) as separate meshes, batched in their own world space (shared camera in
// c16-c19) rather than inside the player's kit run (30-09 grab: stock hands
// and boots poking out of Kurisu's mode-body model). They are matched to their
// player on screen: each hidden custom player's feet and hands are projected
// with exactly what its custom draw uses, each detail draw by its slot-0 bone.
struct Extremity { float x, y, w; int model; DWORD hidden; };   // hidden: PIECE bits of this player's stock pieces it stands for
static const int MAX_EXTREMITIES = 64;
static Extremity g_feet[2][MAX_EXTREMITIES], g_hands[2][MAX_EXTREMITIES];
static int g_nFeet[2], g_nHands[2], g_extCur = 0;
static const float FOOT_BIND_L[3] = { 0.09f, 0.11f, -0.059f };   // probe/body349b2_bones.json bone 7 (sk_foot_l)
static const float FOOT_BIND_R[3] = { -0.09f, 0.11f, -0.059f };  // bone 8 (sk_foot_r)
static const float HAND_BIND_L[3] = { 0.704f, 1.455f, -0.016f }; // bone 17 (sk_hand_l)
static const float HAND_BIND_R[3] = { -0.704f, 1.455f, -0.016f };// bone 18 (sk_hand_r)
static const UINT SLOT_FOOT_L = 5, SLOT_FOOT_R = 6, SLOT_HAND_R = 7, SLOT_HAND_L = 11;   // pgb2.SLOT_BONES
static Extremity projectBone(const float (*M)[4], const float* p) {
    float w[4] = { 0, 0, 0, 1 };
    for (int r = 0; r < 3; r++) w[r] = M[r][0] * p[0] + M[r][1] * p[1] + M[r][2] * p[2] + M[r][3];
    float c[4];
    for (int r = 0; r < 4; r++) c[r] = g_vsc[16 + r][0] * w[0] + g_vsc[16 + r][1] * w[1] + g_vsc[16 + r][2] * w[2] + g_vsc[16 + r][3] * w[3];
    Extremity e = { c[3] != 0 ? c[0] / c[3] : 0, c[3] != 0 ? c[1] / c[3] : 0, c[3], -1, 0 };
    return e;
}
static void noteExtremities(const float (*c)[4], DWORD keep, int model) {
    int k = g_extCur;
    if (!keeps(keep, PART_BOOTS)) {
        const UINT sl[2] = { SLOT_FOOT_L, SLOT_FOOT_R }; const float* bp[2] = { FOOT_BIND_L, FOOT_BIND_R };
        for (int i = 0; i < 2 && g_nFeet[k] < MAX_EXTREMITIES; i++) { Extremity e = projectBone(c + sl[i] * BONE_REGS, bp[i]); e.model = model; e.hidden = PART_BIT << PART_BOOTS; g_feet[k][g_nFeet[k]++] = e; }
    }
    DWORD hands = ((keeps(keep, PIECE_HANDS) ? 0 : PART_BIT << PIECE_HANDS) | (keeps(keep, PART_GLOVES) ? 0 : PART_BIT << PART_GLOVES));
    if (hands) {
        const UINT sl[2] = { SLOT_HAND_L, SLOT_HAND_R }; const float* bp[2] = { HAND_BIND_L, HAND_BIND_R };
        for (int i = 0; i < 2 && g_nHands[k] < MAX_EXTREMITIES; i++) { Extremity e = projectBone(c + sl[i] * BONE_REGS, bp[i]); e.model = model; e.hidden = hands; g_hands[k][g_nHands[k]++] = e; }
    }
}
// nearest recorded extremity (this frame and the last) to this draw's rig
// root, in metres at its depth: the nearest of its first DETAIL_RIG_BONES
// palette bones, each at the mesh origin (#589's pieces are modelled about
// their wrist / ankle) and at the two bind joints (the 930-vertex boots are
// both feet in body bind, 02-10 grab: at the origin their bones sat 11 cm off)
static float nearestExtremity(int piece, int* model) {
    bool feet = piece == PART_BOOTS;
    static const float ORIGIN[3] = { 0, 0, 0 };
    const float* at[3] = { ORIGIN, feet ? FOOT_BIND_L : HAND_BIND_L, feet ? FOOT_BIND_R : HAND_BIND_R };
    float best = 1e9f; *model = -1;
    const UINT NAT = sizeof(at) / sizeof(at[0]);
    for (UINT b = 0; b < DETAIL_RIG_BONES * NAT; b++) {
        Extremity me = projectBone(&g_vsc[BONE_REG0 + (b / NAT) * BONE_REGS], at[b % NAT]);
        for (int f = 0; f < 2; f++) {
            int n = feet ? g_nFeet[f] : g_nHands[f]; Extremity* a = feet ? g_feet[f] : g_hands[f];
            for (int i = 0; i < n; i++) {
                if (!keeps(a[i].hidden, piece)) continue;
                float dx = (a[i].x - me.x) * me.w, dy = (a[i].y - me.y) * me.w, dw = a[i].w - me.w;
                float dd = sqrtf(dx * dx + dy * dy + dw * dw);
                if (dd < best) { best = dd; *model = a[i].model; }
            }
        }
    }
    return best;
}

// 3x4 affine bone matrices (rows = x, y, z; column 3 = translation)
static void affMul(const float (*A)[4], const float (*B)[4], float (*O)[4]) {
    for (int r = 0; r < 3; r++)
        for (int c = 0; c < 4; c++)
            O[r][c] = A[r][0] * B[0][c] + A[r][1] * B[1][c] + A[r][2] * B[2][c] + (c == 3 ? A[r][3] : 0.0f);
}
static void affInv(const float (*M)[4], float (*O)[4]) {   // general (the game's bones carry scale)
    float a = M[0][0], b = M[0][1], c = M[0][2], d = M[1][0], e = M[1][1], f = M[1][2], g = M[2][0], h = M[2][1], i = M[2][2];
    float A = e * i - f * h, B = f * g - d * i, C = d * h - e * g, det = a * A + b * B + c * C;
    float k = det != 0 ? 1.0f / det : 0.0f;
    float R[3][3] = { { A * k, (c * h - b * i) * k, (b * f - c * e) * k },
                      { B * k, (a * i - c * g) * k, (c * d - a * f) * k },
                      { C * k, (b * g - a * h) * k, (a * e - b * d) * k } };
    for (int r = 0; r < 3; r++) {
        for (int q = 0; q < 3; q++) O[r][q] = R[r][q];
        O[r][3] = -(R[r][0] * M[0][3] + R[r][1] * M[1][3] + R[r][2] * M[2][3]);
    }
}
// The stock hand draw's palette, each slot relative to the rig root: the
// finger pose without the game's own placement of the hand rig, which sits
// about 6 deg and 3 % off the body's hand bone (02-10 grab) - drawn where the
// game puts it, the hand broke away from the wrist (owner, Green Is My Pepper).
static void captureHand(CustomModel& M, int side) {
    UINT root = 0;
    while (root < HAND_RIG_BONES && M.hrigBone[side][root] != 0) root++;
    if (root == HAND_RIG_BONES) return;
    float inv[3][4]; affInv(&g_vsc[BONE_REG0 + root * BONE_REGS], inv);
    for (UINT k = 0; k < HAND_RIG_BONES; k++) affMul(inv, &g_vsc[BONE_REG0 + k * BONE_REGS], &M.handRel[side][k * BONE_REGS]);
    M.handCaught[side] = true;
}
// That pose on this draw's hand bone, each rig bone turned about the model's
// own joint (its rotation from the game, its pivot from HRIG: the stock palm
// is cupped, a 4cc hand's joints sit up to 4 cm elsewhere) -> the 12-slot
// palette its SUB_HAND_RIG vertices are weighted to.
static void handPalette(const CustomModel& M, int side, const float (*handBone)[4], float (*pal)[4]) {
    const float* wrist = side ? HAND_BIND_R : HAND_BIND_L;
    float anchor[3][4]; memcpy(anchor, handBone, sizeof(anchor));
    for (int r = 0; r < 3; r++) anchor[r][3] += anchor[r][0] * wrist[0] + anchor[r][1] * wrist[1] + anchor[r][2] * wrist[2];
    UINT slot[HAND_RIG_BONES];
    for (UINT k = 0; k < HAND_RIG_BONES; k++) if (M.hrigBone[side][k] < HAND_RIG_BONES) slot[M.hrigBone[side][k]] = k;
    float bone[HAND_RIG_BONES][3][4];
    for (UINT b = 0; b < HAND_RIG_BONES; b++) {           // parents come first (#589: 0; 1-5 <- 0; 6-10 <- 1-5; 11 <- 6)
        const float (*R)[4] = &M.handRel[side][slot[b] * BONE_REGS];
        int par = M.hrigParent[side][b];
        const float (*P)[4] = par >= 0 && (UINT)par < b ? bone[par] : R;
        const float* c = M.hrigJoint[side][b];
        for (int r = 0; r < 3; r++) {
            float moved = P[r][0] * c[0] + P[r][1] * c[1] + P[r][2] * c[2] + P[r][3];   // the joint, carried by its parent
            for (int q = 0; q < 3; q++) bone[b][r][q] = R[r][q];
            bone[b][r][3] = moved - (R[r][0] * c[0] + R[r][1] * c[1] + R[r][2] * c[2]);
        }
    }
    for (UINT k = 0; k < HAND_RIG_BONES; k++) affMul(anchor, bone[M.hrigBone[side][k] < HAND_RIG_BONES ? M.hrigBone[side][k] : 0], &pal[k * BONE_REGS]);
}

static HRESULT drawWithBones(IDirect3DDevice9* d, const float (*c)[4], const char* who);
static HRESULT drawCustomLod0(IDirect3DDevice9* d) {
    float c[CU_SLOTS * BONE_REGS][4];
    for (UINT k = 0; k < CU_SLOTS; k++)
        for (UINT r = 0; r < BONE_REGS; r++)
            memcpy(c[k * BONE_REGS + r], CU_SRC_PKT[k] == 0 ? g_hipM[r] : g_vsc[BONE_REG0 + CU_SRC_SLOT[k] * BONE_REGS + r], 16);
    noteExtremities(c, g_cuM->keep, g_curModel);
    return drawWithBones(d, c, "player");
}

// The custom model with the given 21-slot bone block, through the kit run's
// declaration and vertex shader (captured at the last custom player's hip).
static HRESULT drawWithBones(IDirect3DDevice9* d, const float (*c)[4], const char* who) {
    // flags\dumpc = "player" | "official": the next such custom draw writes its
    // bone block and the game's VS constants to 4cc-players\uploaded_<who>.bin
    if (flagExists(L"dumpc")) {
        wchar_t p[MAX_PATH]; lstrcpyW(p, FLAGDIR); lstrcatW(p, L"dumpc");
        char nm[32] = {0}; DWORD r = 0; HANDLE h = CreateFileW(p, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
        if (h != INVALID_HANDLE_VALUE) { ReadFile(h, nm, 31, &r, NULL); CloseHandle(h); }
        for (DWORD i = 0; i < r; i++) if (nm[i] < 'a' || nm[i] > 'z') { nm[i] = 0; break; }
        if (lstrcmpA(nm, who) != 0) goto draw;
        DeleteFileW(p);
        wchar_t up[MAX_PATH]; wsprintfW(up, L"%suploaded_%S.bin", g_root, nm);
        HANDLE f = CreateFileW(up, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
        DWORD w; WriteFile(f, c, CU_SLOTS * BONE_REGS * 16, &w, NULL); WriteFile(f, g_vsc, sizeof(g_vsc), &w, NULL); CloseHandle(f);
    }
draw:
    float keep[CU_SLOTS * BONE_REGS][4];
    memcpy(keep, g_vsc[BONE_REG0], sizeof(keep));
    IDirect3DVertexDeclaration9* keepDecl = NULL; IDirect3DVertexShader9* keepVS = NULL;
    d->GetVertexDeclaration(&keepDecl); d->GetVertexShader(&keepVS);
    d->SetVertexDeclaration(g_kitDecl); d->SetVertexShader(g_kitVS);
    g_orgSVSCF(d, BONE_REG0, &c[0][0], CU_SLOTS * BONE_REGS);
    HRESULT hr = drawCustom(d);
    for (int s = 0; s < 2; s++) {                // his hands on the stock hand's pose (captureHand)
        if (!g_cuM->handCaught[s]) continue;
        g_cuM->handCaught[s] = false;
        float pal[HAND_RIG_BONES * BONE_REGS][4];
        handPalette(*g_cuM, s, &c[(s ? SLOT_HAND_R : SLOT_HAND_L) * BONE_REGS], pal);
        g_orgSVSCF(d, BONE_REG0, &pal[0][0], HAND_RIG_BONES * BONE_REGS);
        drawCustom(d, DRAW_HAND_L + s);
    }
    g_orgSVSCF(d, BONE_REG0, &keep[0][0], CU_SLOTS * BONE_REGS);
    d->SetVertexDeclaration(keepDecl); d->SetVertexShader(keepVS);
    if (keepDecl) keepDecl->Release(); if (keepVS) keepVS->Release();
    return hr;
}

// An official's draw carries all 19 body bones in the block-1 palette order;
// OFFICIAL_SLOT (tools/pes12_rig.py) puts them in the custom rig's order. The
// rig has no finger bones, so the two finger slots follow the hands. The kit
// pieces wear the model's own team's sheet (hiKit, full resolution).
static const UINT CU_SLOT_FINGER_L = 19, CU_SLOT_FINGER_R = 20;   // kitmap.h: children of hand_l / hand_r
// Officials pool: custom\p<pid> for pid = (OFFICIALS_TID + 2000) * 100 + n,
// written by tools/pes12_import_referees.py (OFFICIALS_TID there too); kits
// custom\kits\<OFFICIALS_TID>\r<n>_hi.dds. Per match each official (keyed by
// his head vertex buffer) gets a random referee and all of them one random
// kit. A new match = the game creating an officials' vertex buffer
// (match load builds the officials' models; replays and camera cuts reuse
// them). The earlier frame-gap rule re-rolled the referees mid-replay (01-10).
static const int MAX_POOL = 99, MAX_OFFICIALS = 8, MAX_REF_KITS = 9, MAX_OFFICIALS_RUNS = 8;
static const int OFFICIALS_TID = 999;
// A model reaching below the knee is a whole figure even when the converter
// calls it "head" (PES15 "parts" referees like Tesla, y -0.17..2.62): the
// stock official body under it is PES12's, bulkier than the PES15 body the
// model was built on, and shows through (seen 01-10). Face packs (referee025,
// y 1.30..1.73) keep the stock body. ponytail: knee-height heuristic, a
// per-referee flag from the pack if one turns up.
static const float OFFICIAL_FULL_FIGURE_FOOT_M = 0.5f;
// flags\officialpid: "<pid>" puts that model on every official (test override).
static UINT g_officialPid = 0;
// run k of this frame -> the pid it resolved to; the body draws (before the
// head names the official) use last frame's to keep or hide the stock body.
static LONG g_runPidPrev[MAX_OFFICIALS_RUNS], g_runPidCur[MAX_OFFICIALS_RUNS]; static int g_offRun = 0;
static volatile LONG g_offNewMatch = 1;          // set by myCVB at match load; 1: first use
static const wchar_t* REF_KIT_SLOTS[MAX_REF_KITS] = { L"r1", L"r2", L"r3", L"r4", L"r5", L"r6", L"r7", L"r8", L"r9" };
static LONG g_pool[MAX_POOL]; static int g_nPool = -1;      // -1: not probed yet
static int g_nRefKits = 0;
struct OfficialSlot { IDirect3DVertexBuffer9* head; LONG pid; };
static OfficialSlot g_offTab[MAX_OFFICIALS]; static int g_nOff = 0;
static const wchar_t* g_refKit = NULL; static DWORD g_rng = 1;
static DWORD nextRand() { g_rng = g_rng * 1103515245u + 12345u; return g_rng >> 16; }
static void probePool() {
    g_nPool = 0;
    wchar_t p[MAX_PATH];
    for (int n = 1; n <= MAX_POOL && g_nPool < MAX_POOL; n++) {
        LONG pid = (OFFICIALS_TID + PLACEHOLDER_TEAM_BASE) * PLAYERS_PER_TEAM + n;
        wsprintfW(p, L"%sp%d", CUSTOM_DIR, (int)pid);
        if (GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES) g_pool[g_nPool++] = pid;
    }
    for (g_nRefKits = 0; g_nRefKits < MAX_REF_KITS; g_nRefKits++) {
        wsprintfW(p, L"%s%d\\%s%s", KIT_DIR, OFFICIALS_TID, REF_KIT_SLOTS[g_nRefKits], HI_KIT_SUFFIX);
        if (GetFileAttributesW(p) == INVALID_FILE_ATTRIBUTES) break;
    }
    char m[96]; wsprintfA(m, "officials: pool %d referees, %d kits", g_nPool, g_nRefKits); logline(m);
}
static LONG officialPid(IDirect3DVertexBuffer9* head) {
    if (g_officialPid > 0) return g_officialPid;               // flags\officialpid test override
    if (g_nPool < 0) probePool();
    if (g_nPool == 0) return -1;
    if (InterlockedExchange(&g_offNewMatch, 0)) {   // a new match
        g_nOff = 0; g_rng = GetTickCount() | 1;
        g_refKit = g_nRefKits ? REF_KIT_SLOTS[nextRand() % g_nRefKits] : NULL;
    }
    for (int i = 0; i < g_nOff; i++) if (g_offTab[i].head == head) return g_offTab[i].pid;
    if (g_nOff == MAX_OFFICIALS) return -1;
    LONG pid;
    for (int tries = 0;; tries++) {      // distinct while the pool allows
        pid = g_pool[nextRand() % g_nPool];
        bool used = false;
        for (int i = 0; i < g_nOff; i++) if (g_offTab[i].pid == pid) used = true;
        if (!used || tries >= g_nPool * 4) break;
    }
    g_offTab[g_nOff].head = head; g_offTab[g_nOff++].pid = pid;
    char m[96]; wsprintfA(m, "official %d (head vb %08x) -> p%d, kit %S", g_nOff, (DWORD)head, (int)pid, g_refKit ? g_refKit : L"-"); logline(m);
    return pid;
}
// A custom model's face parts are head-local on the face palette and drawn
// at the game's face draw (players). Officials have none: every face slot
// takes the head joint's frame (head skin matrix through the head's bind
// position, officialmap.h), so the face rides the head without animating.
static HRESULT drawFaceRigid(IDirect3DDevice9* d, const float (*c)[4]) {
    float f[FACE_SLOTS * BONE_REGS][4];
    const float (*h)[4] = c + HEAD_SLOT * BONE_REGS;
    for (UINT r = 0; r < BONE_REGS; r++) {
        float row[4] = { h[r][0], h[r][1], h[r][2],
                         h[r][0] * HEAD_BIND[0] + h[r][1] * HEAD_BIND[1] + h[r][2] * HEAD_BIND[2] + h[r][3] };
        for (UINT k = 0; k < FACE_SLOTS; k++) memcpy(f[k * BONE_REGS + r], row, 16);
    }
    float keep[FACE_SLOTS * BONE_REGS][4];
    memcpy(keep, g_vsc[BONE_REG0], sizeof(keep));
    IDirect3DVertexDeclaration9* keepDecl = NULL; IDirect3DVertexShader9* keepVS = NULL;
    d->GetVertexDeclaration(&keepDecl); d->GetVertexShader(&keepVS);
    d->SetVertexDeclaration(g_kitDecl); d->SetVertexShader(g_kitVS);
    g_orgSVSCF(d, BONE_REG0, &f[0][0], FACE_SLOTS * BONE_REGS);
    HRESULT hr = drawCustom(d, DRAW_FACE);
    g_orgSVSCF(d, BONE_REG0, &keep[0][0], FACE_SLOTS * BONE_REGS);
    d->SetVertexDeclaration(keepDecl); d->SetVertexShader(keepVS);
    if (keepDecl) keepDecl->Release(); if (keepVS) keepVS->Release();
    return hr;
}
// The close-up model's bones of palette group g, from a draw of that group.
static void closeBones(float (*c)[4], int g) {
    for (UINT k = 0; k < CU_SLOTS; k++) {
        UINT s = k < sizeof(CLOSE_SRC_SLOT) / sizeof(CLOSE_SRC_SLOT[0]) ? k : (k == CU_SLOT_FINGER_L ? SLOT_HAND_L : SLOT_HAND_R);
        if (CLOSE_SRC_GROUP[s] != g) continue;
        for (UINT r = 0; r < BONE_REGS; r++) memcpy(c[k * BONE_REGS + r], g_vsc[BONE_REG0 + CLOSE_SRC_SLOT[s] * BONE_REGS + r], 16);
    }
}
// The official's bone block from his body draw's constants (OFFICIAL_SLOT).
static void officialBones(float (*c)[4]) {
    for (UINT k = 0; k < CU_SLOTS; k++) {
        UINT src = k < sizeof(OFFICIAL_SLOT) / sizeof(OFFICIAL_SLOT[0]) ? OFFICIAL_SLOT[k]
                 : OFFICIAL_SLOT[k == CU_SLOT_FINGER_L ? SLOT_HAND_L : SLOT_HAND_R];
        for (UINT r = 0; r < BONE_REGS; r++) memcpy(c[k * BONE_REGS + r], g_vsc[BONE_REG0 + src * BONE_REGS + r], 16);
    }
}
static bool drawOfficial(IDirect3DDevice9* d, const float (*c)[4]) {
    bool ok = g_runKitOk; int tid = g_runTid; const wchar_t* slot = g_runSlot; IDirect3DBaseTexture9* kt = g_runKitTex;
    g_runKitOk = g_refKit != NULL; g_runTid = OFFICIALS_TID; g_runSlot = g_refKit; g_runKitTex = g_tex0;
    IDirect3DVertexShader9* ownVS = NULL; d->GetVertexShader(&ownVS);
    int pass = isColourVS(ownVS) ? 1 : 0;
    if (ownVS) ownVS->Release();
    if (!g_passDecl[pass] || !g_passVS[pass]) { g_runKitOk = ok; g_runTid = tid; g_runSlot = slot; g_runKitTex = kt; return false; }
    IDirect3DVertexDeclaration9* kd = g_kitDecl; IDirect3DVertexShader9* kv = g_kitVS;
    g_kitDecl = g_passDecl[pass]; g_kitVS = g_passVS[pass];
    IDirect3DPixelShader9* keepPS = NULL; d->GetPixelShader(&keepPS);
    if (g_passPS[pass]) d->SetPixelShader(g_passPS[pass]);
    IDirect3DBaseTexture9* keepStage[KIT_STAGES];
    for (int st = 1; st < KIT_STAGES; st++) { keepStage[st] = g_texStage[st]; g_orgSTEX(d, st, g_passStage[pass][st]); }
    const UINT BONE_END = BONE_REG0 + CU_SLOTS * BONE_REGS, VSC_N = 256;
    g_orgSVSCF(d, 0, &g_passVSC[pass][0][0], BONE_REG0);
    g_orgSVSCF(d, BONE_END, &g_passVSC[pass][BONE_END][0], VSC_N - BONE_END);
    float keepPSC[PSC_N][4]; memcpy(keepPSC, g_psc, sizeof(g_psc));
    g_orgSPSCF(d, 0, &g_passPSC[pass][0][0], PSC_N);
    // drawCustom reads the UV selector (c176) from the tracked constants; give it
    // the kit draw's, as players get. The official's own differs between his
    // two models (01-10: Eustace wrong zoomed out, right zoomed in).
    float keepUV[4]; memcpy(keepUV, g_vsc[UV_SELECT_REG], sizeof(keepUV));
    memcpy(g_vsc[UV_SELECT_REG], g_passVSC[pass][UV_SELECT_REG], sizeof(keepUV));
    HRESULT hr = drawWithBones(d, c, "official");
    if (SUCCEEDED(hr)) drawFaceRigid(d, c);
    memcpy(g_vsc[UV_SELECT_REG], keepUV, sizeof(keepUV));
    g_orgSPSCF(d, 0, &keepPSC[0][0], PSC_N); memcpy(g_psc, keepPSC, sizeof(g_psc));
    g_orgSVSCF(d, 0, &g_vsc[0][0], BONE_REG0);
    g_orgSVSCF(d, BONE_END, &g_vsc[BONE_END][0], VSC_N - BONE_END);
    for (int st = 1; st < KIT_STAGES; st++) g_orgSTEX(d, st, keepStage[st]);
    d->SetPixelShader(keepPS); if (keepPS) keepPS->Release();
    g_kitDecl = kd; g_kitVS = kv;
    g_runKitOk = ok; g_runTid = tid; g_runSlot = slot; g_runKitTex = kt;
    return SUCCEEDED(hr);
}

// flags\shaderdump: write every distinct vertex/pixel shader pair bound at
// custom draws (depth, shadow and colour passes differ) to
// 4cc-players\shaders\vs_<n>.bin / ps_<n>.bin (disassemble with vkd3d-compiler
// -x d3dbc -b d3d-asm); the flag is removed after SHADER_DUMP_MAX pairs.
static const int SHADER_DUMP_MAX = 8;
static void dumpShaders(IDirect3DDevice9* d) {
    static void* seen[SHADER_DUMP_MAX][2]; static int nseen = 0;
    wchar_t p[MAX_PATH], dir[MAX_PATH]; CreateDirectoryW(rootFile(dir, L"shaders"), NULL);
    IDirect3DVertexShader9* vs = NULL; IDirect3DPixelShader9* ps = NULL;
    d->GetVertexShader(&vs); d->GetPixelShader(&ps);
    bool known = false;
    for (int i = 0; i < nseen; i++) known |= seen[i][0] == vs && seen[i][1] == ps;
    if (!known && nseen < SHADER_DUMP_MAX) {
        seen[nseen][0] = vs; seen[nseen][1] = ps;
        for (int k = 0; k < 2; k++) {
            UINT n = 0; HRESULT hr = k ? (ps ? ps->GetFunction(NULL, &n) : E_FAIL) : (vs ? vs->GetFunction(NULL, &n) : E_FAIL);
            if (FAILED(hr) || !n) continue;
            BYTE* b = (BYTE*)HeapAlloc(GetProcessHeap(), 0, n);
            if (k) ps->GetFunction(b, &n); else vs->GetFunction(b, &n);
            wchar_t rel[64]; wsprintfW(rel, k ? L"shaders\\ps_%d.bin" : L"shaders\\vs_%d.bin", nseen);
            HANDLE f = CreateFileW(rootFile(p, rel), GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
            DWORD w; WriteFile(f, b, n, &w, NULL); CloseHandle(f); HeapFree(GetProcessHeap(), 0, b);
        }
        char m[64]; wsprintfA(m, "shaders dumped: pair %d", nseen); logline(m);
        if (++nseen == SHADER_DUMP_MAX) { lstrcpyW(p, FLAGDIR); lstrcatW(p, L"shaderdump"); DeleteFileW(p); }
    }
    if (vs) vs->Release(); if (ps) ps->Release();
}
static bool g_shaderDump = false;              // flags\shaderdump, polled every 60 frames
// The game's lit kit pixel shader (ps_1, dumped 30-09) perturbs the normal by
// two normal maps, samplers s4 and s9 (DXT5nm: xy = .wy * 2 - 1, blended by
// c27.x), through the kit UV set. A custom model drawn with it inherited the
// kit draw's cloth-wrinkle maps on its own UVs: fold ridges across Feynman's
// neck and many other players' skin. Custom subs get a flat normal instead
// (the pack models' own normal maps are not converted).
static const DWORD NORMAL_MAP_STAGES[] = { 4, 9 };
static const D3DCOLOR FLAT_NORMAL_ARGB = 0x808080FF;   // .w = .y = 128: x = y = 0 -> the vertex normal
static IDirect3DTexture9* g_flatNormal = NULL;
static bool g_keepNormalMaps = false;
// Hair: PES hair-tube materials alpha-test at ref 254, keeping only the fully
// opaque core, and draw the filtered strand edges in a second, blended pass.
// Drawn once, the cut left 4cc hair matte and chunky (Feynman, 30-09). A sub
// alpha-tested at or above HAIR_CORE_MIN_REF gets that fringe pass: alpha in
// (FRINGE_MIN_ALPHA, ref], blended over, depth tested, no depth write.
static const DWORD HAIR_CORE_MIN_REF = 200;   // PES hair tubes use 254; body/face cut-outs 0-128
static const DWORD FRINGE_MIN_ALPHA = 8;      // below this the fringe is noise
static const DWORD HAIR_CORE_REF = 254;       // the PES hair-tube core cut (materials' own alpharef)
static bool g_noHairFringe = false;           // flags\\nohairfringe: single pass (A/B)   // flags\\gamenormals: the kit draw's normal maps (A/B)
static IDirect3DTexture9* flatNormal(IDirect3DDevice9* d) {
    if (!g_flatNormal && SUCCEEDED(d->CreateTexture(1, 1, 1, 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED, &g_flatNormal, NULL))) {
        D3DLOCKED_RECT lr;
        if (SUCCEEDED(g_flatNormal->LockRect(0, &lr, NULL, 0))) { *(D3DCOLOR*)lr.pBits = FLAT_NORMAL_ARGB; g_flatNormal->UnlockRect(0); }
    }
    return g_flatNormal;
}

static HRESULT drawCustom(IDirect3DDevice9* d, int part) {
    CustomModel& M = *g_cuM;
    if (g_shaderDump) dumpShaders(d);
    if (g_rtDumpArm && !g_rtDump && part == DRAW_BODY) d->GetRenderTarget(0, &g_rtDump);
    IDirect3DVertexBuffer9* keepVB = g_vb; UINT keepOff = g_vbOff, keepSt = g_stride;
    IDirect3DIndexBuffer9* keepIB = g_ib;
    IDirect3DBaseTexture9* keepTex = g_tex0;
    DWORD cull = 0, at = 0, aref = 0, afn = 0, ab = 0, sb = 0, db = 0, zw = 0;
    d->GetRenderState(D3DRS_CULLMODE, &cull);
    d->GetRenderState(D3DRS_ALPHATESTENABLE, &at); d->GetRenderState(D3DRS_ALPHAREF, &aref); d->GetRenderState(D3DRS_ALPHAFUNC, &afn);
    d->GetRenderState(D3DRS_ALPHABLENDENABLE, &ab); d->GetRenderState(D3DRS_SRCBLEND, &sb); d->GetRenderState(D3DRS_DESTBLEND, &db);
    d->GetRenderState(D3DRS_ZWRITEENABLE, &zw);
    DWORD dbias = 0, sbias = 0;
    d->GetRenderState(D3DRS_DEPTHBIAS, &dbias); d->GetRenderState(D3DRS_SLOPESCALEDEPTHBIAS, &sbias);
    g_orgSSS(d, 0, M.vb, 0, M.stride);
    g_orgSI(d, M.ib);
    // 4cc toon models carry an inverted-hull outline shell (black, inflated);
    // it only reads as an outline with back faces culled. Fox winds
    // clockwise-front = D3D's native, so cull CCW (flags\\cullcw flips it).
    DWORD culled = g_cuCullCW ? D3DCULL_CW : D3DCULL_CCW;
    // fmdl UVs run past 0..1 (Caulifla: u -3..2.5): PES21 samples them wrapped.
    DWORD au = 0, av = 0;
    d->GetSamplerState(0, D3DSAMP_ADDRESSU, &au); d->GetSamplerState(0, D3DSAMP_ADDRESSV, &av);
    if (!g_cuKeepGameTex) { d->SetSamplerState(0, D3DSAMP_ADDRESSU, D3DTADDRESS_WRAP); d->SetSamplerState(0, D3DSAMP_ADDRESSV, D3DTADDRESS_WRAP); }
    HRESULT hr = D3D_OK;
    if (!g_psShadeless) d->CreatePixelShader(PS_SHADELESS, &g_psShadeless);
    if (!g_psToon) d->CreatePixelShader(PS_TOON, &g_psToon);
    IDirect3DVertexShader9* curVS = NULL; d->GetVertexShader(&curVS);
    bool colour = isColourVS(curVS); if (curVS) curVS->Release();
    IDirect3DPixelShader9* gamePS = NULL; d->GetPixelShader(&gamePS);
    float uvSel[4]; memcpy(uvSel, g_vsc[UV_SELECT_REG], sizeof(uvSel));   // the game's, for non-kit subs
    const int NNS = sizeof(NORMAL_MAP_STAGES) / sizeof(NORMAL_MAP_STAGES[0]);
    IDirect3DBaseTexture9* keepNrm[NNS];
    for (int k = 0; k < NNS; k++) { keepNrm[k] = NULL; d->GetTexture(NORMAL_MAP_STAGES[k], &keepNrm[k]); if (!g_keepNormalMaps && flatNormal(d)) g_orgSTEX(d, NORMAL_MAP_STAGES[k], g_flatNormal); }
    for (UINT i = 0; i < M.nsub; i++) {
        const CustomSub& S = M.sub[i];
        // face subs are head-local on the face palette: only at the face draw;
        // hand-rig subs hand-local on the stock hand palette: only at its draw
        bool face = (S.flags & SUB_FACE) != 0, rig = (S.flags & SUB_HAND_RIG) != 0;
        if (part == DRAW_FACE ? !face : part == DRAW_BODY ? face || rig
            : !rig || !(S.flags & SUB_HAND_SIDE[part - DRAW_HAND_L])) continue;
        if (part == DRAW_BODY && ((S.flags & SUB_HAND_L && M.handCaught[0]) || (S.flags & SUB_HAND_R && M.handCaught[1]))) continue;
        // a texture that failed to load binds nothing, never the previous sub's
        // texture (01-10: Eustace's chair drew his newspaper)
        if (!g_cuKeepGameTex) g_orgSTEX(d, 0, S.tex < M.ntex ? M.tex[S.tex] : NULL);
        // kit slot: the pack's own sheet for the kit this player is wearing, at
        // full resolution through TEXCOORD0; else the game's bound sheet
        // through TEXCOORD1 (the remapped UVs)
        // c176 is the UV selector of the colour pass's kit VS only; the depth
        // and shadow passes' VS read c176 as something else, and the kit subs
        // wrote far-plane depth into the depth pre-pass (02-10 rtdump: red and
        // white noise on the shirt and shorts; DoF blurred them, the sun flare
        // shone through them), so other passes keep the game's c176.
        if ((S.flags & SUB_KIT) && g_runKitTex) {
            IDirect3DTexture9* hi = g_runKitOk && !g_kitForceOff ? hiKit(d, g_runTid, g_runSlot) : NULL;
            g_orgSTEX(d, 0, hi ? (IDirect3DBaseTexture9*)hi : g_runKitTex);
            g_orgSVSCF(d, UV_SELECT_REG, !colour ? uvSel : hi ? UV_SELECT_SET0 : UV_SELECT_SET1, 1);
        } else g_orgSVSCF(d, UV_SELECT_REG, uvSel, 1);
        // the material's own states (PES15 .mtl), never the kit draw's leftovers
        bool atest = (S.flags & SUB_ALPHATEST) != 0, blend = (S.flags & SUB_BLEND) != 0;
        bool hairCore = (S.flags & SUB_HAIR) && !g_noHairFringe;   // opaque core only, the fringe follows
        g_orgRS(d, D3DRS_ALPHATESTENABLE, atest || hairCore);
        if (atest) { g_orgRS(d, D3DRS_ALPHAREF, (S.flags >> SUB_REF_SHIFT) & SUB_REF_MASK); g_orgRS(d, D3DRS_ALPHAFUNC, D3DCMP_GREATER); }
        else if (hairCore) { g_orgRS(d, D3DRS_ALPHAREF, HAIR_CORE_REF); g_orgRS(d, D3DRS_ALPHAFUNC, D3DCMP_GREATER); }
        g_orgRS(d, D3DRS_ALPHABLENDENABLE, blend);
        if (blend) { g_orgRS(d, D3DRS_SRCBLEND, D3DBLEND_SRCALPHA); g_orgRS(d, D3DRS_DESTBLEND, D3DBLEND_INVSRCALPHA); }
        g_orgRS(d, D3DRS_ZWRITEENABLE, (S.flags & SUB_NOZWRITE) ? FALSE : TRUE);
        g_orgRS(d, D3DRS_CULLMODE, (S.flags & SUB_TWOSIDED) ? D3DCULL_NONE : culled);
        bool outline = (S.flags & SUB_OUTLINE) != 0;
        float bias = outline ? OUTLINE_DEPTH_BIAS : 0.0f, slope = outline ? OUTLINE_SLOPE_BIAS : 0.0f;
        g_orgRS(d, D3DRS_DEPTHBIAS, *(DWORD*)&bias); g_orgRS(d, D3DRS_SLOPESCALEDEPTHBIAS, *(DWORD*)&slope);
        IDirect3DPixelShader9* ps = !colour ? gamePS : (S.flags & SUB_SHADELESS) && g_psShadeless ? g_psShadeless
                                  : (S.flags & SUB_TOON) && g_psToon ? g_psToon : gamePS;
        d->SetPixelShader(ps);
        hr = g_orgDIP(d, D3DPT_TRIANGLELIST, 0, 0, M.nv, S.first, S.count / 3);
        DWORD ref = (S.flags >> SUB_REF_SHIFT) & SUB_REF_MASK;
        bool hairSub = (S.flags & SUB_HAIR) != 0;
        if (((atest && !blend && ref >= HAIR_CORE_MIN_REF) || hairSub) && !g_noHairFringe) {
            g_orgRS(d, D3DRS_ALPHATESTENABLE, TRUE); g_orgRS(d, D3DRS_ALPHAFUNC, D3DCMP_GREATER);
            g_orgRS(d, D3DRS_ALPHAREF, FRINGE_MIN_ALPHA);
            g_orgRS(d, D3DRS_ALPHABLENDENABLE, TRUE);
            g_orgRS(d, D3DRS_SRCBLEND, D3DBLEND_SRCALPHA); g_orgRS(d, D3DRS_DESTBLEND, D3DBLEND_INVSRCALPHA);
            g_orgRS(d, D3DRS_ZWRITEENABLE, FALSE);
            hr = g_orgDIP(d, D3DPT_TRIANGLELIST, 0, 0, M.nv, S.first, S.count / 3);
        }
    }
    d->SetPixelShader(gamePS); if (gamePS) gamePS->Release();
    for (int k = 0; k < NNS; k++) { g_orgSTEX(d, NORMAL_MAP_STAGES[k], keepNrm[k]); if (keepNrm[k]) keepNrm[k]->Release(); }
    g_orgSVSCF(d, UV_SELECT_REG, uvSel, 1);
    g_orgRS(d, D3DRS_ALPHABLENDENABLE, ab); g_orgRS(d, D3DRS_SRCBLEND, sb); g_orgRS(d, D3DRS_DESTBLEND, db);
    g_orgRS(d, D3DRS_ZWRITEENABLE, zw);
    g_orgRS(d, D3DRS_DEPTHBIAS, dbias); g_orgRS(d, D3DRS_SLOPESCALEDEPTHBIAS, sbias);
    g_orgRS(d, D3DRS_ALPHATESTENABLE, at); g_orgRS(d, D3DRS_ALPHAREF, aref); g_orgRS(d, D3DRS_ALPHAFUNC, afn);
    d->SetSamplerState(0, D3DSAMP_ADDRESSU, au); d->SetSamplerState(0, D3DSAMP_ADDRESSV, av);
    g_orgRS(d, D3DRS_CULLMODE, cull);
    g_orgSTEX(d, 0, keepTex);
    g_orgSI(d, keepIB);
    g_orgSSS(d, 0, keepVB, keepOff, keepSt);
    return hr;
}


// Part of a run draw, from its vertex bounds; cached per vertex range (the
// kit model's buffers are static, so one lock per piece per session).
static const int MAX_PARTCACHE = 1024;
struct PartKey { void* vb; UINT off, first, nv, stride; };
static PartKey g_pcKey[MAX_PARTCACHE]; static BYTE g_pcPart[MAX_PARTCACHE]; static int g_npc = 0;
static int classifyPart(UINT first, UINT nV) {
    for (int i = 0; i < g_npc; i++) {
        const PartKey& k = g_pcKey[i];
        if (k.vb == g_vb && k.off == g_vbOff && k.first == first && k.nv == nV && k.stride == g_stride) return g_pcPart[i];
    }
    int part = PART_OTHER;
    void* p = NULL;
    if (g_vb && g_stride >= 12 && SUCCEEDED(g_vb->Lock(g_vbOff + first * g_stride, nV * g_stride, &p, D3DLOCK_READONLY)) && p) {
        float lo[3] = {1e9f, 1e9f, 1e9f}, hi[3] = {-1e9f, -1e9f, -1e9f};
        for (UINT v = 0; v < nV; v++) {
            const float* q = (const float*)((BYTE*)p + v * g_stride);
            for (int c = 0; c < 3; c++) { if (q[c] < lo[c]) lo[c] = q[c]; if (q[c] > hi[c]) hi[c] = q[c]; }
        }
        g_vb->Unlock();
        float ax = hi[0] > -lo[0] ? hi[0] : -lo[0];
        if (hi[0] - lo[0] > NONPLAYER_MIN_EXTENT || hi[1] - lo[1] > NONPLAYER_MIN_EXTENT) part = PART_OTHER;
        else if (hi[1] < LOCAL_SPACE_MAX_Y)
            part = (hi[1] < BOOT_MAX_Y && hi[0] - lo[0] > BOOT_MIN_WIDTH && lo[0] * hi[0] >= 0) ? PART_BOOTS : PART_HEAD;
        else if (hi[1] < SOCKS_MAX_Y) part = PART_SOCKS;
        else if (hi[1] < SHORTS_MAX_Y) part = PART_SHORTS;
        else if (ax > GLOVES_MIN_X && g_stride != 80) part = PART_GLOVES;
        else if (ax > TORSO_MAX_X) part = PART_SLEEVES;
        else if (hi[1] > NECK_MIN_Y) part = PART_NECK;
        else part = PART_SHIRT;
    }
    if (g_npc < MAX_PARTCACHE) {
        PartKey k = { g_vb, g_vbOff, first, nV, g_stride };
        g_pcKey[g_npc] = k; g_pcPart[g_npc] = (BYTE)part; g_npc++;
        char m[96]; wsprintfA(m, "part %u/%u -> %d", nV, g_stride, part); logline(m);
    }
    return part;
}

// A custom model's face part at the stock face draw: the game has just
// uploaded the face palette (head-local bind -> world), which the part's
// vertices are weighted to (tools/pes15_to_pes12.py SUB_FACE).
static HRESULT drawCustomFace(IDirect3DDevice9* d) {
    IDirect3DVertexDeclaration9* keepDecl = NULL; IDirect3DVertexShader9* keepVS = NULL;
    d->GetVertexDeclaration(&keepDecl); d->GetVertexShader(&keepVS);
    d->SetVertexDeclaration(g_kitDecl); d->SetVertexShader(g_kitVS);
    HRESULT hr = drawCustom(d, DRAW_FACE);
    d->SetVertexDeclaration(keepDecl); d->SetVertexShader(keepVS);
    if (keepDecl) keepDecl->Release(); if (keepVS) keepVS->Release();
    return hr;
}

static void hookV(void** vt, int idx, void* fn, void** org) {
    DWORD prot;
    if (VirtualProtect(&vt[idx], 4, PAGE_EXECUTE_READWRITE, &prot)) {
        if (org && !*org) *org = (void*)vt[idx];
        vt[idx] = fn;
        VirtualProtect(&vt[idx], 4, prot, &prot);
    }
}

static HRESULT STDMETHODCALLTYPE mySSS(IDirect3DDevice9* d, UINT s,
        IDirect3DVertexBuffer9* vb, UINT off, UINT st) {
    if (s == 0) { g_vb = vb; g_stride = st; g_vbOff = off; }
    return g_orgSSS(d, s, vb, off, st);
}
static HRESULT STDMETHODCALLTYPE mySVD(IDirect3DDevice9* d, IDirect3DVertexDeclaration9* p) {
    g_decl = p; g_declBoard = isBoardDecl(p); g_declSkinned = declHasUsage(p, D3DDECLUSAGE_BLENDINDICES);
    return g_orgSVD(d, p);
}
static HRESULT STDMETHODCALLTYPE mySFVF(IDirect3DDevice9* d, DWORD f) {
    g_fvf = f;
    return g_orgSFVF(d, f);
}
static HRESULT STDMETHODCALLTYPE mySI(IDirect3DDevice9* d, IDirect3DIndexBuffer9* ib) {
    g_ib = ib;
    return g_orgSI(d, ib);
}
static HRESULT STDMETHODCALLTYPE mySTEX(IDirect3DDevice9* d, DWORD s, IDirect3DBaseTexture9* t) {
    if (s == 0) g_tex0 = t;
    if (s < 8) g_texStage[s] = t;
    return g_orgSTEX(d, s, t);
}
static HRESULT STDMETHODCALLTYPE mySPSCF(IDirect3DDevice9* d, UINT r, const float* v, UINT n) {
    if (r < PSC_N) memcpy(g_psc[r], v, ((r + n > PSC_N) ? PSC_N - r : n) * 16);
    return g_orgSPSCF(d, r, v, n);
}
static HRESULT STDMETHODCALLTYPE mySVSCF(IDirect3DDevice9* d, UINT r, const float* v, UINT n) {
    if (r < 256) {
        UINT m = (r + n > 256) ? 256 - r : n;
        memcpy(g_vsc[r], v, m * 16);
    }
    return g_orgSVSCF(d, r, v, n);
}

#include "kitforce.h"
// The skin draw carries bare hands of its own (stride 76, body bind, the hand
// from the wrist joint out: palette slots 9/10 and 17/18, the forearm 8/16,
// 02-10 grab). A model that keeps the skin but not the stock hands (its own
// hands, e.g. a gloveL/gloveR part) gets the skin minus every triangle past
// the wrist (Green Is My Pepper: stock palms over his green hands, owner).
// Per distinct draw, an index list of the other triangles.
static const int MAX_SKIN_CUTS = 64;
struct SkinCut { IDirect3DVertexBuffer9* vb; UINT off, first, nv; IDirect3DIndexBuffer9* ib; UINT si, np; D3DPRIMITIVETYPE t; IDirect3DIndexBuffer9* cut; UINT ntris; };
static SkinCut g_skinCut[MAX_SKIN_CUTS]; static int g_nSkinCut = 0;
static IDirect3DIndexBuffer9* skinWithoutHands(D3DPRIMITIVETYPE t, INT bV, UINT mV, UINT nV, UINT sI, UINT nP) {
    UINT first = (UINT)(bV + (INT)mV);
    for (int i = 0; i < g_nSkinCut; i++) {
        SkinCut& c = g_skinCut[i];
        if (c.vb == g_vb && c.off == g_vbOff && c.first == first && c.nv == nV && c.ib == g_ib && c.si == sI && c.np == nP && c.t == t)
            return c.cut;
    }
    if (g_nSkinCut >= MAX_SKIN_CUTS || !g_vb || !g_ib || (t != D3DPT_TRIANGLESTRIP && t != D3DPT_TRIANGLELIST)) return NULL;
    SkinCut& c = g_skinCut[g_nSkinCut++]; memset(&c, 0, sizeof(c));
    c.vb = g_vb; c.off = g_vbOff; c.first = first; c.nv = nV; c.ib = g_ib; c.si = sI; c.np = nP; c.t = t;
    D3DINDEXBUFFER_DESC id; if (FAILED(g_ib->GetDesc(&id))) return NULL;
    UINT isz = id.Format == D3DFMT_INDEX32 ? 4 : 2, nIdx = t == D3DPT_TRIANGLESTRIP ? nP + 2 : nP * 3;
    void* vp = NULL; void* ip = NULL;
    BYTE* hand = (BYTE*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, nV);
    if (SUCCEEDED(g_vb->Lock(g_vbOff + first * g_stride, nV * g_stride, &vp, D3DLOCK_READONLY)) && vp) {
        for (UINT v = 0; v < nV; v++) hand[v] = fabsf(*(const float*)((const BYTE*)vp + v * g_stride)) >= HAND_BIND_L[0];   // the wrist joint's |x|
        g_vb->Unlock();
    }
    UINT* out = (UINT*)HeapAlloc(GetProcessHeap(), 0, nIdx * 3 * sizeof(UINT) + sizeof(UINT));
    UINT n = 0;
    if (SUCCEEDED(g_ib->Lock(sI * isz, nIdx * isz, &ip, D3DLOCK_READONLY)) && ip) {
        for (UINT k = 0; k + 2 < nIdx || (t == D3DPT_TRIANGLELIST && k < nIdx); ) {
            UINT a, b, d3;
            UINT i0 = isz == 4 ? ((UINT*)ip)[k] : ((WORD*)ip)[k], i1 = isz == 4 ? ((UINT*)ip)[k + 1] : ((WORD*)ip)[k + 1], i2 = isz == 4 ? ((UINT*)ip)[k + 2] : ((WORD*)ip)[k + 2];
            if (t == D3DPT_TRIANGLESTRIP) { a = i0; b = i1; d3 = i2; if (k & 1) { UINT x = a; a = b; b = x; } k++; }
            else { a = i0; b = i1; d3 = i2; k += 3; }
            if (a == b || b == d3 || a == d3) continue;
            bool in = a >= mV && b >= mV && d3 >= mV && a < mV + nV && b < mV + nV && d3 < mV + nV;
            if (in && hand[a - mV] && hand[b - mV] && hand[d3 - mV]) continue;   // past the wrist
            out[n++] = a; out[n++] = b; out[n++] = d3;
        }
        g_ib->Unlock();
    }
    HeapFree(GetProcessHeap(), 0, hand);
    c.ntris = n / 3;
    if (n && SUCCEEDED(g_dev->CreateIndexBuffer(n * 4, D3DUSAGE_WRITEONLY, D3DFMT_INDEX32, D3DPOOL_MANAGED, &c.cut, NULL)) && SUCCEEDED(c.cut->Lock(0, n * 4, &ip, 0))) {
        memcpy(ip, out, n * 4); c.cut->Unlock();
    }
    HeapFree(GetProcessHeap(), 0, out);
    char m[128]; wsprintfA(m, "skin: %u/%u without its hands -> %u tris", nV, nP, c.ntris); logline(m);
    return c.cut;
}
static HRESULT drawSkinWithout(IDirect3DDevice9* d, IDirect3DIndexBuffer9* cut, INT bV, UINT mV, UINT nV) {
    SkinCut* c = NULL;
    for (int i = 0; i < g_nSkinCut; i++) if (g_skinCut[i].cut == cut) c = &g_skinCut[i];
    IDirect3DIndexBuffer9* keepIB = g_ib;
    g_orgSI(d, cut);
    HRESULT hr = g_orgDIP(d, D3DPT_TRIANGLELIST, bV, mV, nV, 0, c ? c->ntris : 0);
    g_orgSI(d, keepIB);
    return hr;
}
// the stock kit draw, with forced PES14+ UVs and the pack's own sheet when
// the run wears one of our kits; else the game's draw
static HRESULT kitDraw(IDirect3DDevice9* d, D3DPRIMITIVETYPE t, INT bV, UINT mV, UINT nV, UINT sI, UINT nP) {
    IDirect3DTexture9* hi = NULL; Forced* F = NULL;
    if (g_runKitOk && g_tex0 == g_runKitTex && !g_kitForceOff && (hi = hiKit(d, g_runTid, g_runSlot)) != NULL)
        F = forcedFor(d, t, (UINT)(bV + (INT)mV), mV, nV, sI, nP);
    if (!F) return g_orgDIP(d, t, bV, mV, nV, sI, nP);
    IDirect3DVertexBuffer9* keepVB = g_vb; UINT keepOff = g_vbOff, keepSt = g_stride;
    IDirect3DIndexBuffer9* keepIB = g_ib; IDirect3DBaseTexture9* keepTex = g_tex0;
    g_orgSSS(d, 0, F->vb, 0, F->stride);
    g_orgSI(d, F->ib);
    g_orgSTEX(d, 0, hi);
    HRESULT hr = g_orgDIP(d, D3DPT_TRIANGLELIST, 0, 0, F->nverts, 0, F->ntris);
    g_orgSSS(d, 0, keepVB, keepOff, keepSt);
    g_orgSI(d, keepIB);
    g_orgSTEX(d, 0, keepTex);
    return hr;
}

static void grabDraw(IDirect3DDevice9* d, D3DPRIMITIVETYPE t, INT bV, UINT mV,
                     UINT nV, UINT sI, UINT nP) {
    wchar_t path[MAX_PATH];
    wsprintfW(path, L"%sgrab\\g%04d.bin", g_root, (int)g_grabSeq++);
    HANDLE f = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return;
    DWORD w;
    D3DVERTEXELEMENT9 el[64]; UINT ne = 0;
    if (g_decl) g_decl->GetDeclaration(el, &ne); // ne includes D3DDECL_END
    D3DINDEXBUFFER_DESC idesc; memset(&idesc, 0, sizeof(idesc));
    if (g_ib) g_ib->GetDesc(&idesc);
    UINT isz = (idesc.Format == D3DFMT_INDEX32) ? 4 : 2;
    UINT nIdx = (t == D3DPT_TRIANGLESTRIP) ? nP + 2 : nP * 3;
    DWORD hdr[16] = { 0x42415247, (DWORD)t, (DWORD)bV, mV, nV, sI, nP, g_stride,
                      g_vbOff, isz, nIdx, ne, (DWORD)g_tex0, (DWORD)g_vb, (DWORD)g_ib, 0 };
    WriteFile(f, hdr, sizeof(hdr), &w, NULL);
    WriteFile(f, el, ne * sizeof(D3DVERTEXELEMENT9), &w, NULL);
    WriteFile(f, g_vsc, sizeof(g_vsc), &w, NULL);
    // vertices: the draw reads [bV+mV, bV+mV+nV) of stream 0
    DWORD vstatus = 0;
    if (g_vb) {
        void* p = NULL;
        UINT off = g_vbOff + (bV + mV) * g_stride, len = nV * g_stride;
        if (SUCCEEDED(g_vb->Lock(off, len, &p, D3DLOCK_READONLY)) && p) {
            WriteFile(f, p, len, &w, NULL); vstatus = 1;
            g_vb->Unlock();
        }
    }
    DWORD istatus = 0;
    if (g_ib) {
        void* p = NULL;
        if (SUCCEEDED(g_ib->Lock(sI * isz, nIdx * isz, &p, D3DLOCK_READONLY)) && p) {
            WriteFile(f, p, nIdx * isz, &w, NULL); istatus = 1;
            g_ib->Unlock();
        }
    }
    DWORD tail[2] = { vstatus, istatus };
    WriteFile(f, tail, 8, &w, NULL);
    CloseHandle(f);
}

// flags\texdump: "<nV>" -> for one full frame, every draw with that vertex
// count (TEXDUMP_ALL: every draw) writes each bound stage's top mip once to
// 4cc-players\grab\tex_<ptr>.dds; drawhook.log maps draw -> stage -> ptr.
// Read-only lock; DEFAULT-pool textures fail it and are only logged.
static const UINT TEXDUMP_ALL = 1;      // no real draw has a single vertex
static const int MAX_TEXDUMPS = 1024;   // distinct textures per dump frame
static UINT g_texDumpNV = 0;
// flags\hidenv: "<nV>" skips every draw with that vertex count; draw indices
// shift between frames, a signature does not (debug, finding characters).
static UINT g_hideNV = 0;
static const int OFFICIAL_HEAD_DRAWS = 2;   // head + hair after each official (01-10 kickoff log)
static const UINT OFFICIAL_HEAD_STRIDE = 64;  // the head's first piece at both LODs (82/171/64, 145/369/64; 01-10 grabs)
static const int OFFICIAL_FACE_DRAWS_MAX = 5; // close LOD face set before the head (8/7/60 30/57/88 8/9/60 671/1599/88 28/39/80)
static bool g_texDumpArmed = false;     // dumps run for exactly one full frame
static IDirect3DBaseTexture9* g_texDumped[MAX_TEXDUMPS]; static int g_nTexDumped = 0;
static void dumpStageTextures(UINT nV, UINT nP, LONG di) {
    const DWORD DDS_HDR_SIZE = 124, DDS_PF_SIZE = 32, DDSD_CAPS_HEIGHT_WIDTH_PF = 0x1007,
                DDSD_LINEARSIZE = 0x80000, DDPF_FOURCC = 0x4, DDPF_RGB_ALPHA = 0x41,
                DDSCAPS_TEXTURE = 0x1000, RGBA_BITS = 32;
    for (int s = 0; s < 8; s++) {
        IDirect3DBaseTexture9* bt = g_texStage[s];
        if (!bt || bt->GetType() != D3DRTYPE_TEXTURE) continue;
        char m[128];
        wsprintfA(m, "texdump draw %d nV=%u nP=%u s%d tex=%08x", (int)di, nV, nP, s, (DWORD)bt);
        logline(m);
        bool seen = false;
        for (int k = 0; k < g_nTexDumped; k++) if (g_texDumped[k] == bt) seen = true;
        if (seen || g_nTexDumped >= MAX_TEXDUMPS) continue;
        g_texDumped[g_nTexDumped++] = bt;
        IDirect3DTexture9* tx = (IDirect3DTexture9*)bt;
        D3DSURFACE_DESC sd; tx->GetLevelDesc(0, &sd);
        bool dxt = sd.Format == D3DFMT_DXT1 || sd.Format == D3DFMT_DXT3 || sd.Format == D3DFMT_DXT5;
        bool argb = sd.Format == D3DFMT_A8R8G8B8 || sd.Format == D3DFMT_X8R8G8B8;
        D3DLOCKED_RECT lr;
        if ((!dxt && !argb) || FAILED(tx->LockRect(0, &lr, NULL, D3DLOCK_READONLY))) {
            wsprintfA(m, "texdump tex=%08x %ux%u fmt=%08x: not dumped", (DWORD)bt, sd.Width, sd.Height, (DWORD)sd.Format);
            logline(m); continue;
        }
        UINT rows = dxt ? (sd.Height + DXT_BLOCK_PX - 1) / DXT_BLOCK_PX : sd.Height;
        UINT rowBytes = dxt ? (sd.Width + DXT_BLOCK_PX - 1) / DXT_BLOCK_PX * (sd.Format == D3DFMT_DXT1 ? DXT1_BLOCK_BYTES : DXT35_BLOCK_BYTES)
                            : sd.Width * RGBA_BITS / 8;
        DWORD h[32] = {0};
        h[0] = DDS_MAGIC; h[1] = DDS_HDR_SIZE; h[2] = DDSD_CAPS_HEIGHT_WIDTH_PF | DDSD_LINEARSIZE;
        h[3] = sd.Height; h[4] = sd.Width; h[5] = rows * rowBytes; h[7] = 1;
        h[19] = DDS_PF_SIZE;
        if (dxt) { h[20] = DDPF_FOURCC; h[21] = (DWORD)sd.Format; }
        else { h[20] = DDPF_RGB_ALPHA; h[22] = RGBA_BITS; h[23] = 0x00FF0000; h[24] = 0x0000FF00; h[25] = 0x000000FF; h[26] = 0xFF000000; }
        h[27] = DDSCAPS_TEXTURE;
        wchar_t path[MAX_PATH]; wsprintfW(path, L"%sgrab\\tex_%08x.dds", g_root, (DWORD)bt);
        HANDLE f = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
        DWORD w = 0;
        if (f != INVALID_HANDLE_VALUE) {
            WriteFile(f, h, sizeof(h), &w, NULL);
            for (UINT r = 0; r < rows; r++) WriteFile(f, (BYTE*)lr.pBits + r * lr.Pitch, rowBytes, &w, NULL);
            CloseHandle(f);
        }
        tx->UnlockRect(0);
        wsprintfA(m, "texdump tex=%08x %ux%u fmt=%08x -> %s", (DWORD)bt, sd.Width, sd.Height, (DWORD)sd.Format, f != INVALID_HANDLE_VALUE ? "written" : "open failed");
        logline(m);
    }
}

static HRESULT STDMETHODCALLTYPE myDIP(IDirect3DDevice9* d, D3DPRIMITIVETYPE t,
        INT bV, UINT mV, UINT nV, UINT sI, UINT nP) {
    if (nV == GROUP_START_NV && nP == GROUP_START_NP && g_stride == GROUP_START_STRIDE) {
        g_group++; g_groupDraw = 0;
    } else if (g_group >= 0) {
        g_groupDraw++;
        if (g_stride == NONPLAYER_STRIDE || nV >= NONPLAYER_MIN_NV || g_groupDraw >= MAX_GROUP_DRAWS)
            g_group = -1000; // left the player run until the next start
    }
    bool inGroup = g_group >= 0 && g_group < 32;
    LONG di = g_drawIdx++;
    if (g_texDumpArmed && (nV == g_texDumpNV || g_texDumpNV == TEXDUMP_ALL)) dumpStageTextures(nV, nP, di);
    if (di >= g_hrLo && di < g_hrHi) return D3D_OK;
    if (g_hideNV && nV == g_hideNV) return D3D_OK;   // flags\hidenv (debug)
    {   // Officials: the dt09 #349 block 1 model, recognised by its vertex buffer.
        // One official = consecutive draws from that buffer, then his head and
        // hair (2 skinned draws from his own head buffer; 01-10 kickoff log).
        // Who he is shows only at the head: the body run captures his bones,
        // the first head draw picks the model and draws it.
        // Two models: block 1 (one palette, all 19 bones) and, at close cameras,
        // block 0 (two palette groups; each bone taken from its group's draw).
        static bool open = false, hideBody = false, resolved = true; static int headLeft = 0, tail = 0;
        static float bones[CU_SLOTS * BONE_REGS][4];
        D3DVERTEXBUFFER_DESC vd;
        bool haveDesc = g_vb && SUCCEEDED(g_vb->GetDesc(&vd));
        bool farModel = haveDesc && vd.Size >= OFFICIAL_VB_LO && vd.Size < OFFICIAL_VB_HI;
        bool closeModel = haveDesc && vd.Size >= CLOSE_VB_LO && vd.Size < CLOSE_VB_HI;
        bool off = farModel || closeModel;
        if (closeModel) closeBones(bones, g_vbOff >= CLOSE_GROUP_START[CLOSE_GROUPS - 1] ? CLOSE_GROUPS - 1 : 0);
        if (off) {
            if (!open) {
                open = true; if (farModel) officialBones(bones);
                if (g_nPool < 0) probePool();
                // outfield rule: a body model replaces the whole figure, any other
                // mode (head, kit, boots) is worn over the stock body
                LONG guess = g_offRun < MAX_OFFICIALS_RUNS ? g_runPidPrev[g_offRun] : -1;
                if (g_officialPid > 0) guess = g_officialPid;
                hideBody = (g_officialPid > 0 || g_nPool > 0) && g_passVS[1] &&
                           (guess <= 0 || (useModel(d, guess) && (g_cuM->keep == 0 || g_cuM->minY < OFFICIAL_FULL_FIGURE_FOOT_M)));
            }
            if (hideBody) return D3D_OK;
        } else if (open) {
            open = false; headLeft = 0; tail = OFFICIAL_FACE_DRAWS_MAX + 1; resolved = false;
        }
        // after the body: at close cameras his face set, then (both LODs) his head,
        // whose first piece is the first skinned stride-OFFICIAL_HEAD_STRIDE draw.
        // The head VB names him for the match at either LOD.
        if (tail > 0 && !off) {
            bool head = g_declSkinned && g_stride == OFFICIAL_HEAD_STRIDE;
            if (head) {
                tail = 0; resolved = true;
                if (g_officialPid > 0 || g_nPool > 0) {
                    LONG pid = officialPid(g_vb);
                    if (g_offRun < MAX_OFFICIALS_RUNS) g_runPidCur[g_offRun] = pid;
                    if (pid > 0 && useModel(d, pid) && drawOfficial(d, bones)) headLeft = OFFICIAL_HEAD_DRAWS;
                }
                g_offRun++;
            } else if (!g_declSkinned || --tail == 0) {
                tail = 0; if (!resolved) { resolved = true; g_offRun++; }
            } else if (hideBody) return D3D_OK;   // the face set of a replaced official
        }
        if (headLeft > 0) { headLeft--; if (g_declSkinned) return D3D_OK; headLeft = 0; }
    }
    // previous draw = the face when this one is a kit packet 5
    static IDirect3DVertexBuffer9* lastVB = NULL; static UINT lastOff = 0, lastSt = 0, lastFirst = 0, lastNV = 0;
    static IDirect3DBaseTexture9* lastTex = NULL; g_prevTex = lastTex; lastTex = g_tex0;
    static IDirect3DBaseTexture9* lastStage[8]; memcpy(g_prevStage, lastStage, sizeof(lastStage)); memcpy(lastStage, g_texStage, sizeof(lastStage));
    g_prevVB = lastVB; g_prevOff = lastOff; g_prevStride = lastSt; g_prevFirst = lastFirst; g_prevNV = lastNV;
    lastVB = g_vb; lastOff = g_vbOff; lastSt = g_stride; lastFirst = (UINT)(bV + (INT)mV); lastNV = nV;
    // The skin draw (stride 76: arms, legs, neck; 1478 verts on the stock body,
    // other counts on edited players) precedes the player's kit run and samples
    // the face atlas. The marked atlas names the model; its mode says whether
    // the stock skin stays.
    if (g_stride == FACE_STRIDE && nV < FACE_MAX_NV) {
        g_runPos = -1;                  // a skin draw opens the next player
        int mk = findMarker(g_tex0);
        float key[3]; runKey(key);
        if (mk >= 0) {
            g_pendingModel = mk; g_pendingSrc = SRC_MARKER;
            if (g_pendingModel >= 0 && g_nKeyCur < MAX_KEYS) {
                for (int c = 0; c < 3; c++) g_keyCur[g_nKeyCur].p[c] = key[c];
                g_keyCur[g_nKeyCur++].model = g_pendingModel;
            }
        } else if (g_pendingModel < 0) { g_pendingModel = modelForKey(key); g_pendingSrc = g_pendingModel >= 0 ? SRC_KEY : SRC_NONE; }
        else g_pendingSrc = SRC_CARRIED;
        if (g_runLog) {
            IDirect3DVertexShader9* vs = NULL; d->GetVertexShader(&vs); bool col = isColourVS(vs); if (vs) vs->Release();
            char m[128]; wsprintfA(m, "run SKIN %u/%u/%u tex=%08x mk=%d pend=%d src=%s %s", nV, nP, g_stride, (DWORD)g_tex0, mk, g_pendingModel, SRC_NAME[g_pendingSrc], col ? "colour" : "other"); logline(m);
        }
        if (g_pendingModel >= 0 && useModel(d, g_pendingModel) && !keeps(g_cuM->keep, PIECE_SKIN)) return D3D_OK;
        if (g_pendingModel >= 0 && useModel(d, g_pendingModel) && !keeps(g_cuM->keep, PIECE_HANDS)) {
            IDirect3DIndexBuffer9* noHands = skinWithoutHands(t, bV, mV, nV, sI, nP);
            if (noHands) return drawSkinWithout(d, noHands, bV, mV, nV);
        }
    }
    bool hip = nV == KIT_HIP_NV && nP == KIT_HIP_NP && g_stride == KIT_HIP_STRIDE;
    if (hip) { g_runPos = 0; g_runKitTex = g_tex0; }

    else if (g_runPos >= 0 && ++g_runPos >= MAX_RUN_DRAWS) g_runPos = -1;
    if (hip) {
        LONG k = g_kitRun++;
        if (g_facelog > 0 && g_prevVB) {
            g_facelog--;
            void* p = NULL; char m[200]; int n = wsprintfA(m, "face run=%d nV=%u st=%u ", (int)k, g_prevNV, g_prevStride);
            if (SUCCEEDED(g_prevVB->Lock(g_prevOff + g_prevFirst * g_prevStride, FACE_PRINT_BYTES, &p, D3DLOCK_READONLY)) && p) {
                for (UINT i = 0; i < FACE_PRINT_BYTES; i++) n += wsprintfA(m + n, "%02x", ((BYTE*)p)[i]);
                g_prevVB->Unlock();
            }
            logline(m);
            if (g_prevTex && g_prevTex->GetType() == D3DRTYPE_TEXTURE) {
                IDirect3DTexture9* t = (IDirect3DTexture9*)g_prevTex;
                DWORD lv = t->GetLevelCount(); D3DSURFACE_DESC sd; t->GetLevelDesc(lv - 1, &sd);
                D3DSURFACE_DESC s0; t->GetLevelDesc(0, &s0);
                D3DLOCKED_RECT lr; HRESULT hr = t->LockRect(lv - 1, &lr, NULL, D3DLOCK_READONLY);
                n = wsprintfA(m, "  tex=%08x %ux%u fmt=%08x pool=%u levels=%u lock=%08x ", (DWORD)t, s0.Width, s0.Height, (DWORD)s0.Format, (UINT)s0.Pool, lv, (DWORD)hr);
                if (SUCCEEDED(hr)) { for (int i = 0; i < 16; i++) n += wsprintfA(m + n, "%02x", ((BYTE*)lr.pBits)[i]); t->UnlockRect(lv - 1); }
                logline(m);
            }
            for (int st = 1; st < 8; st++) {
                IDirect3DBaseTexture9* bt = g_prevStage[st];
                if (!bt || bt->GetType() != D3DRTYPE_TEXTURE) continue;
                IDirect3DTexture9* t = (IDirect3DTexture9*)bt; D3DSURFACE_DESC s0; t->GetLevelDesc(0, &s0);
                n = wsprintfA(m, "  stage%d tex=%08x %ux%u fmt=%08x levels=%u", st, (DWORD)t, s0.Width, s0.Height, (DWORD)s0.Format, t->GetLevelCount());
                logline(m);
            }
        }
        g_curModel = g_pendingModel; g_pendingModel = -1; g_curSrc = g_pendingSrc; g_pendingSrc = SRC_NONE;
        {   // every kit hip (stock players too) refreshes its pass's kit context:
            // officials need one even when no custom player is on screen (01-10:
            // a close camera with only the linesman drew him stock)
            IDirect3DVertexDeclaration9* kd = NULL; IDirect3DVertexShader9* kv = NULL; IDirect3DPixelShader9* kp = NULL;
            d->GetVertexDeclaration(&kd); d->GetVertexShader(&kv); d->GetPixelShader(&kp);
            int k = isColourVS(kv) ? 1 : 0; g_hipColour = k == 1;
            if (g_passDecl[k]) g_passDecl[k]->Release(); if (g_passVS[k]) g_passVS[k]->Release(); if (g_passPS[k]) g_passPS[k]->Release();
            g_passDecl[k] = kd; g_passVS[k] = kv; g_passPS[k] = kp;   // Get* AddRef'd: owned here
            memcpy(g_passVSC[k], g_vsc, sizeof(g_vsc)); memcpy(g_passPSC[k], g_psc, sizeof(g_psc));
            for (int st = 1; st < KIT_STAGES; st++) {
                if (g_passStage[k][st]) g_passStage[k][st]->Release();
                g_passStage[k][st] = g_texStage[st]; if (g_texStage[st]) g_texStage[st]->AddRef();
            }
        }
        if (g_curModel >= 0) {
            memcpy(g_hipM, g_vsc[BONE_REG0 + CU_SRC_SLOT[2] * BONE_REGS], sizeof(g_hipM));
            if (g_kitDecl) g_kitDecl->Release(); if (g_kitVS) g_kitVS->Release(); if (g_kitPS) g_kitPS->Release();
            d->GetVertexDeclaration(&g_kitDecl); d->GetVertexShader(&g_kitVS); d->GetPixelShader(&g_kitPS);
            // Attribution: in the depth and shadow passes the hip binds no kit
            // sheet - g_tex0 is whatever was bound last, often the previous
            // frame's last colour draw, i.e. another team's sheet - and the
            // model there comes from the bone key. Patching from those hips
            // put /g/'s art on /hm/'s sheet (01-10 and 02-10, runlog: /g/'s
            // colour hips bind 22d309d0, /hm/'s 22d73aa8, and the bad patch
            // went 721 -> 22d73aa8). The colour pass's marked hips are exact.
            if (useModel(d, g_curModel) && !g_kitPatchOff && g_hipColour && g_curSrc == SRC_MARKER)
                patchKit(modelTeam(*g_cuM), g_tex0);
        }
        g_runKitOk = kitOfSheet(g_tex0, g_runTid, g_runSlot);
    }
    int part = PART_OTHER;
    if (g_runPos >= 0 && (g_curModel >= 0 || g_pMask)) {
        part = classifyPart((UINT)(bV + (INT)mV), nV);
        if (part == PART_OTHER && !hip) g_runPos = -1;   // left the player
    }
    if (g_runLog) { char m[160]; wsprintfA(m, "run%s m=%d pos=%d %u/%u/%u part=%d tex=%08x%s%s", hip ? " HIP" : "", g_curModel, (int)g_runPos, nV, nP, g_stride, part, (DWORD)g_tex0, hip ? " src=" : "", hip ? SRC_NAME[g_curSrc] : ""); if (hip) lstrcatA(m, g_hipColour ? " colour" : " other"); logline(m); }
    {
        bool detailBoots = (nV == DETAIL_BOOTS_NV && nP == DETAIL_BOOTS_NP) || (nV == DETAIL_BOOT_NV && (nP == DETAIL_BOOT_L_NP || nP == DETAIL_BOOT_R_NP));
        bool detailHands = (nV == DETAIL_HANDS_NV && nP == DETAIL_HANDS_NP) ||
            (g_stride == DETAIL_HAND_STRIDE && ((nV == DETAIL_HAND_L_NV && nP == DETAIL_HAND_L_NP) || (nV == DETAIL_HAND_R_NV && nP == DETAIL_HAND_R_NP)));
        bool detailGloves = g_stride == DETAIL_GLOVE_STRIDE && nV == DETAIL_GLOVE_NV && (nP == DETAIL_GLOVE_L_NP || nP == DETAIL_GLOVE_R_NP);
        if (detailBoots || detailHands || detailGloves) {
            int piece = detailBoots ? (int)PART_BOOTS : detailHands ? (int)PIECE_HANDS : (int)PART_GLOVES;
            int who; float dm = nearestExtremity(piece, &who);
            if (g_runLog) { char m[128]; wsprintfA(m, "  detail %s %u/%u/%u: nearest hiding model %d at %d mm", PIECE_NAMES[piece], nV, nP, g_stride, who, (int)(dm * 1000)); logline(m); }
            if (dm < DETAIL_OWNER_MAX_M) {               // a custom player's, who does not keep it
                // his own hand on this rig, if he has one: the game has just
                // uploaded this hand's palette; its pose relative to the rig
                // root is kept for his body draw (drawWithBones), which
                // anchors it on his own hand bone
                int side = nV == DETAIL_HAND_L_NV ? 0 : nV == DETAIL_HAND_R_NV ? 1 : -1;
                if (detailHands && g_stride == DETAIL_HAND_STRIDE && side >= 0 && useModel(d, who) && g_cuM->handRig[side])
                    captureHand(*g_cuM, side);
                return D3D_OK;
            }
        }
    }
    if (g_runPos >= 0 && g_curModel >= 0 && useModel(d, g_curModel)) {
        bool main = nV == KIT_MAIN_NV && nP == KIT_MAIN_NP && g_stride == KIT_MAIN_STRIDE;
        bool hidden = !keeps(g_cuM->keep, part);
        bool faceDraw = (nV == FACE_DRAW_NV_A || nV == FACE_DRAW_NV_B) && g_stride == FACE_DRAW_STRIDE;
        if (faceDraw && g_kitVS) {
            if (!hidden) g_orgDIP(d, t, bV, mV, nV, sI, nP);
            return drawCustomFace(d);
        }
        if (main) {
            g_cuDrawn++;
            if (!hidden) kitDraw(d, t, bV, mV, nV, sI, nP);
            return drawCustomLod0(d);
        }
        return hidden ? D3D_OK : kitDraw(d, t, bV, mV, nV, sI, nP);
    }
    if (g_runPos >= 0 && ((g_pMask >> part) & 1)) return D3D_OK;
    if (g_runPos >= 0 && g_runKitOk && g_tex0 == g_runKitTex) return kitDraw(d, t, bV, mV, nV, sI, nP);
    if (g_declBoard && g_stride == ADBOARD_STRIDE && isAdSheet(g_tex0) && adboardTex(d)) {
        IDirect3DBaseTexture9* keepTex = g_tex0;
        g_orgSTEX(d, 0, g_adTex);
        HRESULT hr = g_orgDIP(d, t, bV, mV, nV, sI, nP);
        g_orgSTEX(d, 0, keepTex);
        return hr;
    }
    if (nV == BODY_NV && nP == BODY_NP && g_stride == BODY_STRIDE) {
        LONG k = g_bodyDraw++;
        if (k < 32 && ((g_bodyHide >> k) & 1)) return D3D_OK;
        if (k >= g_cuLo && k < g_cuHi && loadCustom(d)) return drawCustom(d);
        if (k >= g_swLo && k < g_swHi && g_vb && buildSwapVB(d)) {
            IDirect3DVertexBuffer9* keepVB = g_vb; UINT keepOff = g_vbOff, keepSt = g_stride;
            g_orgSSS(d, 0, g_swapVB, 0, BODY_STRIDE);
            HRESULT hr = g_orgDIP(d, t, bV, mV, nV, sI, nP);
            g_orgSSS(d, 0, keepVB, keepOff, keepSt);
            return hr;
        }
    }
    if (nV == GK_BODY_NV && nP == GK_BODY_NP && g_stride == BODY_STRIDE) {
        if (g_cuGK && loadCustom(d)) return drawCustom(d);
    }
    if ((inGroup && g_grabGroup == g_group) || g_grabAll == 2) grabDraw(d, t, bV, mV, nV, sI, nP);
    if (inGroup && (g_hideMask >> g_group) & 1) return D3D_OK;
    if (g_dumpLeft > 0) {
        LONG i = InterlockedIncrement(&g_n) - 1;
        if (i < MAXR) {
            g_recs[i].nV = nV; g_recs[i].nP = nP;
            g_recs[i].stride = g_stride; g_recs[i].fvf = g_fvf;
            g_recs[i].tex = (DWORD)g_tex0;
        }
    }
    if (g_wire && nV > 2000 && nV < 60000 && g_stride >= 32) {
        g_orgRS(d, D3DRS_FILLMODE, D3DFILL_WIREFRAME);
        HRESULT hr = g_orgDIP(d, t, bV, mV, nV, sI, nP);
        g_orgRS(d, D3DRS_FILLMODE, D3DFILL_SOLID);
        return hr;
    }
    return g_orgDIP(d, t, bV, mV, nV, sI, nP);
}

// flags\shot: write the frame about to be presented to 4cc-players\shots\frame.bmp
// (XWayland grabs of an occluded window come back black, 27-09).
// flags\rtdump: the render target the next custom model draw goes into (the
// first of a frame is the depth pre-pass), alpha included, as it stands at
// Present -> shots\rt.bmp. The scene is not drawn into the back buffer (its
// alpha is 255 everywhere at Present, 02-10).
static void dumpSurface(IDirect3DDevice9* d, IDirect3DSurface9* bb, const wchar_t* path, const char* what) {
    IDirect3DSurface9* sys = NULL;
    D3DSURFACE_DESC sd; bb->GetDesc(&sd);
    { char m[128]; wsprintfA(m, "%s: surface %08x %ux%u fmt %u ms %u", what, (DWORD)bb, sd.Width, sd.Height, (UINT)sd.Format, (UINT)sd.MultiSampleType); if (lstrcmpA(what, "shot")) logline(m); }
    if (sd.Format != D3DFMT_A8R8G8B8 && sd.Format != D3DFMT_X8R8G8B8) return;
    if (SUCCEEDED(d->CreateOffscreenPlainSurface(sd.Width, sd.Height, sd.Format, D3DPOOL_SYSTEMMEM, &sys, NULL))
        && SUCCEEDED(d->GetRenderTargetData(bb, sys))) {
        D3DLOCKED_RECT lr;
        if (SUCCEEDED(sys->LockRect(&lr, NULL, D3DLOCK_READONLY))) {
            wchar_t dir[MAX_PATH]; CreateDirectoryW(rootFile(dir, L"shots"), NULL);
            HANDLE f = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
            if (f != INVALID_HANDLE_VALUE) {
                const DWORD BMP_BPP = 4, BMP_HDR = 54, BMP_INFO = 40, BMP_PLANES = 1, BMP_BITS = 32;
                DWORD img = sd.Width * sd.Height * BMP_BPP, w;
                BYTE h[54] = {0};
                h[0] = 'B'; h[1] = 'M'; *(DWORD*)(h + 2) = BMP_HDR + img; *(DWORD*)(h + 10) = BMP_HDR;
                *(DWORD*)(h + 14) = BMP_INFO; *(LONG*)(h + 18) = sd.Width; *(LONG*)(h + 22) = -(LONG)sd.Height;
                *(WORD*)(h + 26) = BMP_PLANES; *(WORD*)(h + 28) = BMP_BITS; *(DWORD*)(h + 34) = img;
                WriteFile(f, h, BMP_HDR, &w, NULL);
                for (UINT y = 0; y < sd.Height; y++) WriteFile(f, (BYTE*)lr.pBits + y * lr.Pitch, sd.Width * BMP_BPP, &w, NULL);
                CloseHandle(f);
            }
            sys->UnlockRect();
        }
    } else if (lstrcmpA(what, "shot")) logline("rtdump: GetRenderTargetData failed");
    if (sys) sys->Release();
}
static void captureFrame(IDirect3DDevice9* d) {
    IDirect3DSurface9* bb = NULL;
    if (FAILED(d->GetBackBuffer(0, 0, D3DBACKBUFFER_TYPE_MONO, &bb)) || !bb) return;
    dumpSurface(d, bb, SHOT_PATH, "shot");
    bb->Release();
}

extern "C" __declspec(dllexport) void logic_present(IDirect3DDevice9* d) {
    if (flagExists(L"shot")) { wchar_t p[MAX_PATH]; lstrcpyW(p, FLAGDIR); lstrcatW(p, L"shot"); DeleteFileW(p); captureFrame(d); }
    if (g_rtDump) {
        wchar_t p[MAX_PATH]; dumpSurface(d, g_rtDump, rootFile(p, L"shots\\rt.bmp"), "rtdump");
        g_rtDump->Release(); g_rtDump = NULL; g_rtDumpArm = false;
    } else if (flagExists(L"rtdump")) {
        wchar_t p[MAX_PATH]; lstrcpyW(p, FLAGDIR); lstrcatW(p, L"rtdump"); DeleteFileW(p); g_rtDumpArm = true;
    }
    LONG f = InterlockedIncrement(&g_frame);
    static LONG dumpStart = 0;
    if (g_grabAll == 2) { g_grabAll = 0; char m[64]; wsprintfA(m, "frame %d grabbed all: %d draws", (int)f, (int)g_grabSeq); logline(m); }
    if (g_grabAll == 1) g_grabAll = 2;  // arm for the next full frame
    memcpy(g_runPidPrev, g_runPidCur, sizeof(g_runPidCur));
    for (int k = 0; k < MAX_OFFICIALS_RUNS; k++) g_runPidCur[k] = -1;
    g_offRun = 0;
    if (g_texDumpArmed) { g_texDumpArmed = false; g_texDumpNV = 0; }
    else if (g_texDumpNV) { g_texDumpArmed = true; g_nTexDumped = 0; }
    if (g_grabGroup >= 0) {  // grab covers exactly one frame
        char m[64]; wsprintfA(m, "frame %d grabbed group %d: %d draws", (int)f, (int)g_grabGroup, (int)g_grabSeq);
        logline(m);
        g_grabGroup = -1;
    }
    g_group = -1;
    LONG bodiesLastFrame = g_bodyDraw;
    g_bodyDraw = 0;
    g_drawIdx = 0;
    { static LONG lr = -1, lc = -1; if (g_kitRun != lr || g_cuDrawn != lc) { char m[96]; wsprintfA(m, "kit runs/frame=%d custom drawn=%d ", (int)g_kitRun, (int)g_cuDrawn); logline(m); lr = g_kitRun; lc = g_cuDrawn; } }
    if (g_runLog && f % 60 != 0) g_runLog = false;
    g_kitRun = 0; g_cuDrawn = 0; g_runPos = -1; g_ntc = 0;
    // a marked face sets g_pendingModel and the next hip consumes it (1471); if the
    // consuming hip is in another frame the pending model outlives its frame and
    // gets consumed by an unrelated player's run - patchKit then writes that
    // team's art into whichever sheet that player had bound (the away team wore
    // /g/'s kit, 01-10). Anything still pending when the frame rolls over is stale.
    g_pendingModel = -1; g_pendingSrc = SRC_NONE;
    memcpy(g_keyPrev, g_keyCur, sizeof(g_keyCur)); g_nKeyPrev = g_nKeyCur; g_nKeyCur = 0;
    g_extCur ^= 1; g_nFeet[g_extCur] = g_nHands[g_extCur] = 0;
    if (f % 60 == 0) {
        g_hideMask = readFlagInt(L"hide", 0);
        g_bodyHide = readFlagInt(L"bodyhide", 0);
        g_pMask = readFlagInt(L"pmask", 0);
        g_hideNV = readFlagInt(L"hidenv", 0);
        g_officialPid = readFlagInt(L"officialpid", 0);
        g_runLog = flagExists(L"runlog");
        if (g_runLog) { wchar_t p[MAX_PATH]; lstrcpyW(p, FLAGDIR); lstrcatW(p, L"runlog"); DeleteFileW(p); }
        readRange(L"hr", &g_hrLo, &g_hrHi);
        readRange(L"swap", &g_swLo, &g_swHi);
        readRange(L"custom", &g_cuLo, &g_cuHi);
        g_cuKeepGameTex = flagExists(L"gametex");
        g_cuGK = flagExists(L"customgk");
        g_cuCullCW = flagExists(L"cullcw");
        g_kitForceOff = flagExists(L"nokitforce");
        g_kitUV = flagExists(L"kituv");
        g_kitPatchOff = flagExists(L"nokitpatch");
        g_keepNormalMaps = flagExists(L"gamenormals");
        g_noHairFringe = flagExists(L"nohairfringe");
        g_shaderDump = flagExists(L"shaderdump");
        if (flagExists(L"codedump")) {   // flags\codedump: "hexaddr hexlen" -> 4cc-players\codedump.bin
            wchar_t p[MAX_PATH]; lstrcpyW(p, FLAGDIR); lstrcatW(p, L"codedump");
            HANDLE f = CreateFileW(p, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
            char b[64] = {0}; DWORD r = 0; if (f != INVALID_HANDLE_VALUE) { ReadFile(f, b, 63, &r, NULL); CloseHandle(f); }
            DeleteFileW(p);
            DWORD addr = 0, len = 0; char* q = b;
            while (*q && *q != ' ') { char c = *q++; addr = addr * 16 + (c <= '9' ? c - '0' : (c | 32) - 'a' + 10); }
            while (*q == ' ') q++;
            while (*q && *q != '\n' && *q != '\r') { char c = *q++; len = len * 16 + (c <= '9' ? c - '0' : (c | 32) - 'a' + 10); }
            wchar_t cd[MAX_PATH]; HANDLE o = CreateFileW(rootFile(cd, L"codedump.bin"), GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
            MEMORY_BASIC_INFORMATION mbi; DWORD w = 0;
            if (o != INVALID_HANDLE_VALUE && VirtualQuery((void*)addr, &mbi, sizeof(mbi)) && mbi.State == MEM_COMMIT) WriteFile(o, (void*)addr, len, &w, NULL);
            if (o != INVALID_HANDLE_VALUE) CloseHandle(o);
            char m[80]; wsprintfA(m, "codedump %08x +%x -> %u bytes", addr, len, w); logline(m);
        }
        if (flagExists(L"facelog")) { wchar_t p[MAX_PATH]; lstrcpyW(p, FLAGDIR); lstrcatW(p, L"facelog"); DeleteFileW(p); g_facelog = 70; }
        { static LONG lg = 0; if (!lg && g_cuLo >= 0) { lg = 1; } }
        { static LONG last = -1; if (bodiesLastFrame != last) { char m[64]; wsprintfA(m, "body draws/frame=%d", (int)bodiesLastFrame); logline(m); last = bodiesLastFrame; } }
        if (flagExists(L"texdump")) {
            g_texDumpNV = readFlagInt(L"texdump", 0);
            wchar_t p[MAX_PATH]; lstrcpyW(p, FLAGDIR); lstrcatW(p, L"texdump"); DeleteFileW(p);
            wchar_t gd[MAX_PATH]; CreateDirectoryW(rootFile(gd, L"grab"), NULL);
        }
        if (flagExists(L"grab")) {
            LONG gv = readFlagInt(L"grab", 0); g_grabSeq = 0;
            if (gv == GRAB_ALL) g_grabAll = 1; else g_grabGroup = gv & 31;
            wchar_t p[MAX_PATH]; lstrcpyW(p, FLAGDIR); lstrcatW(p, L"grab"); DeleteFileW(p);
            wchar_t gd[MAX_PATH]; CreateDirectoryW(rootFile(gd, L"grab"), NULL);
        }
    }
    if (f % 60 == 0) {
        LONG w = flagExists(L"wire") ? 1 : 0;
        if (w != g_wire) { g_wire = w; char m[64]; wsprintfA(m, "frame %d wire=%d", (int)f, (int)w); logline(m); }
        if (g_dumpLeft == 0 && flagExists(L"dump")) {
            g_dumpLeft = 3; dumpStart = f + 1; g_n = 0;
            char m[64]; wsprintfA(m, "frame %d dump armed", (int)f); logline(m);
            return;
        }
    }
    if (g_dumpLeft > 0) {
        LONG n = g_n;
        char m[160];
        wsprintfA(m, "frame %d draws=%d", (int)f, (int)n);
        logline(m);
        for (LONG i = 0; i < n && i < MAXR; i++) {
            char r[200];
            wsprintfA(r, "d %u %u st=%u fvf=%08x tex=%08x",
                      (unsigned)g_recs[i].nV, (unsigned)g_recs[i].nP,
                      (unsigned)g_recs[i].stride, (unsigned)g_recs[i].fvf,
                      (unsigned)g_recs[i].tex);
            logline(r);
        }
        g_n = 0;
        if (InterlockedDecrement(&g_dumpLeft) == 0) {
            wchar_t p[MAX_PATH];
            lstrcpyW(p, FLAGDIR); lstrcatW(p, L"dump");
            DeleteFileW(p);
            logline("dump done");
        }
    }
}

struct Saved { int idx; void* org; };
static Saved g_saved[16]; static int g_nsaved = 0;
typedef HRESULT (STDMETHODCALLTYPE *CVB_FN)(IDirect3DDevice9*, UINT, DWORD, DWORD, D3DPOOL, IDirect3DVertexBuffer9**, HANDLE*);
static CVB_FN g_orgCVB = NULL;
static HRESULT STDMETHODCALLTYPE myCVB(IDirect3DDevice9* d, UINT len, DWORD usage, DWORD fvf, D3DPOOL pool,
                                       IDirect3DVertexBuffer9** vb, HANDLE* sh) {
    bool farVB = len >= OFFICIAL_VB_LO && len < OFFICIAL_VB_HI, closeVB = len >= CLOSE_VB_LO && len < CLOSE_VB_HI;
    if (farVB || closeVB) {
        InterlockedExchange(&g_offNewMatch, 1);
        char m[96]; wsprintfA(m, "officials: match load (%s model VB, %u bytes, frame %d)", farVB ? "far" : "close", len, (int)g_frame);
        logline(m);
    }
    return g_orgCVB(d, len, usage, fvf, pool, vb, sh);
}
static void hookS(void** vt, int idx, void* fn, void** org) {
    g_saved[g_nsaved].idx = idx; g_saved[g_nsaved].org = vt[idx]; g_nsaved++;
    *org = NULL; hookV(vt, idx, fn, org);
}
extern "C" __declspec(dllexport) void logic_uninstall() {
    void** vt = *(void***)g_dev;
    for (int i = g_nsaved - 1; i >= 0; i--) { void* dummy = NULL; hookV(vt, g_saved[i].idx, g_saved[i].org, &dummy); }
    g_nsaved = 0;
    if (g_swapVB) { g_swapVB->Release(); g_swapVB = NULL; }
    if (g_kitDecl) g_kitDecl->Release(); if (g_kitVS) g_kitVS->Release(); if (g_kitPS) g_kitPS->Release(); g_kitDecl = NULL; g_kitVS = NULL; g_kitPS = NULL;
    for (int k = 0; k < 2; k++) {
        if (g_passDecl[k]) g_passDecl[k]->Release(); if (g_passVS[k]) g_passVS[k]->Release(); if (g_passPS[k]) g_passPS[k]->Release(); g_passDecl[k] = NULL; g_passVS[k] = NULL; g_passPS[k] = NULL;
        for (int st = 1; st < KIT_STAGES; st++) { if (g_passStage[k][st]) g_passStage[k][st]->Release(); g_passStage[k][st] = NULL; }
    }
    for (int i = 0; i < g_nmodels; i++) releaseModel(g_models[i]);
    g_nmodels = 0;
    releaseModel(g_default); memset(&g_default, 0, sizeof(g_default)); g_cuM = NULL;
    if (g_flatNormal) { g_flatNormal->Release(); g_flatNormal = NULL; }
    if (g_uvGrid) { g_uvGrid->Release(); g_uvGrid = NULL; }
    freeFwd();   // 25 MB (kitforce.h): reloads must not stack copies
    for (int i = 0; i < g_nSkinCut; i++) if (g_skinCut[i].cut) g_skinCut[i].cut->Release();
    g_nSkinCut = 0;
    if (g_psShadeless) g_psShadeless->Release(); if (g_psToon) g_psToon->Release(); g_psShadeless = g_psToon = NULL;
    logline("logic uninstalled");
    if (g_log != INVALID_HANDLE_VALUE) { CloseHandle(g_log); g_log = INVALID_HANDLE_VALUE; }
}

extern "C" __declspec(dllexport) void logic_install(IDirect3DDevice9* dev) {
    initPaths();
    g_log = CreateFileW(LOGPATH, FILE_APPEND_DATA, FILE_SHARE_READ, NULL,
                        OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    g_dev = dev;
    void** vt = *(void***)g_dev;
    hookS(vt, 100, (void*)mySSS, (void**)&g_orgSSS);   // SetStreamSource
    hookS(vt, 87, (void*)mySVD, (void**)&g_orgSVD);   // SetVertexDeclaration
    hookS(vt, 89, (void*)mySFVF, (void**)&g_orgSFVF); // SetFVF
    hookS(vt, 104, (void*)mySI, (void**)&g_orgSI);     // SetIndices
    hookS(vt, 65, (void*)mySTEX, (void**)&g_orgSTEX);  // SetTexture
    g_orgRS = (RS_FN)vt[57]; // SetRenderState: read only, NEVER write
    hookS(vt, 94, (void*)mySVSCF, (void**)&g_orgSVSCF); // SetVertexShaderConstantF
    hookS(vt, 109, (void*)mySPSCF, (void**)&g_orgSPSCF); // SetPixelShaderConstantF
    hookS(vt, 82, (void*)myDIP, (void**)&g_orgDIP);    // DrawIndexedPrimitive
    hookS(vt, 26, (void*)myCVB, (void**)&g_orgCVB);    // CreateVertexBuffer
    logline("logic installed");
}

