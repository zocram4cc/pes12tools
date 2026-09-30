// Forced PES14+ kit UVs on the stock kit (included by drawlogic.cpp).
//
// The 4cc packs ship PES14-21 kit sheets. Instead of re-painting them into
// PES2012's 1024 x 512 layout (tools/pes15_kits.py: blurry, one sample of the
// pack per texel), every stock kit draw of a 4cc team is replaced by a copy of
// the same mesh whose kit UV (TEXCOORD1) points into the PES14+ sheet, drawn
// with the pack's own 2048 px sheet.
//
// The UVs come from the per-texel remap tools/pes15_kits.py builds (fwd:
// PES2012 kit uv -> PES14+ uv, 4cc-players\kitmap\fwd.bin). One UV per vertex
// cannot follow the remap across a border between two charts of the PES14+
// sheet (16% of the stock kit's triangles straddle one, 30-09), so a triangle
// whose corners disagree with the remap inside it is cut in four at its edge
// midpoints, down to FORCE_MAX_DEPTH; every vertex attribute is interpolated
// (blend weights merged per bone). Built once per distinct draw, on first sight.

#include <math.h>

static const DWORD FWD_MAGIC = 0x31445746;          // tools/pes15_kits.py FWD_MAGIC
static const float FORCE_AGREE_TOL = 0.004f;        // PES14+ uv units, ~8 px of the 2048 sheet (prototype 30-09)
static const int FORCE_MAX_DEPTH = 4;               // 4 levels: torso 1174 -> 28240 tris in the prototype
static const int MAX_FORCED = 64;                   // distinct kit draws kept
static const UINT FORCE_MAX_VERTS = 1u << 20;       // safety cap per draw
static const float FORCE_JACOBIAN_STEP_TEXELS = 0.5f; // finite-difference step of the remap, in fwd texels (half a PES2012 kit texel)
static const float PROBES[4][2] = { { 1.f / 3, 1.f / 3 }, { .5f, .25f }, { .25f, .5f }, { .25f, .25f } };

static float* g_fwd = NULL; static UINT g_fwdW = 0, g_fwdH = 0; static bool g_fwdTried = false;
static bool loadFwd() {
    if (g_fwdTried) return g_fwd != NULL;
    g_fwdTried = true;
    wchar_t p[MAX_PATH]; rootFile(p, L"kitmap\\fwd.bin");
    DWORD n = 0; BYTE* b = readAll(p, &n);
    if (!b) { logline("kitforce: no kitmap\\fwd.bin"); return false; }
    DWORD* h = (DWORD*)b;
    if (h[0] != FWD_MAGIC || n < 12 + h[1] * h[2] * 8) { HeapFree(GetProcessHeap(), 0, b); logline("kitforce: bad fwd.bin"); return false; }
    g_fwdW = h[1]; g_fwdH = h[2];
    g_fwd = (float*)HeapAlloc(GetProcessHeap(), 0, g_fwdW * g_fwdH * 8);
    memcpy(g_fwd, b + 12, g_fwdW * g_fwdH * 8);
    HeapFree(GetProcessHeap(), 0, b);
    return true;
}
// bilinear, texel centres at (i + 0.5) / w, clamped (tools/pes15_kits.py sample)
static void fwdLook(float u, float v, float out[2]) {
    float x = u * g_fwdW - 0.5f, y = v * g_fwdH - 0.5f;
    if (x < 0) x = 0; if (y < 0) y = 0;
    if (x > g_fwdW - 1) x = (float)(g_fwdW - 1); if (y > g_fwdH - 1) y = (float)(g_fwdH - 1);
    int x0 = (int)x, y0 = (int)y, x1 = x0 + 1 < (int)g_fwdW ? x0 + 1 : x0, y1 = y0 + 1 < (int)g_fwdH ? y0 + 1 : y0;
    float fx = x - x0, fy = y - y0;
    for (int c = 0; c < 2; c++) {
        float a = g_fwd[(y0 * g_fwdW + x0) * 2 + c], b = g_fwd[(y0 * g_fwdW + x1) * 2 + c];
        float e = g_fwd[(y1 * g_fwdW + x0) * 2 + c], f = g_fwd[(y1 * g_fwdW + x1) * 2 + c];
        out[c] = (a * (1 - fx) + b * fx) * (1 - fy) + (e * (1 - fx) + f * fx) * fy;
    }
}

// ---- vertex layout of a draw, from its declaration ----
struct KitLayout { UINT stride; int uvOff; int bwOff, bwN; int biOff; D3DVERTEXELEMENT9 el[32]; UINT ne; };
static bool kitLayout(IDirect3DVertexDeclaration9* decl, UINT stride, KitLayout& L) {
    if (!decl) return false;
    L.ne = 0; decl->GetDeclaration(NULL, &L.ne);
    if (L.ne == 0 || L.ne > 32) return false;
    decl->GetDeclaration(L.el, &L.ne);
    L.stride = stride; L.uvOff = -1; L.bwOff = -1; L.bwN = 0; L.biOff = -1;
    for (UINT i = 0; i < L.ne; i++) {
        const D3DVERTEXELEMENT9& e = L.el[i];
        if (e.Stream != 0) continue;
        if (e.Usage == D3DDECLUSAGE_TEXCOORD && e.UsageIndex == 1 && e.Type == D3DDECLTYPE_FLOAT2) L.uvOff = e.Offset;
        if (e.Usage == D3DDECLUSAGE_BLENDWEIGHT) { L.bwOff = e.Offset; L.bwN = e.Type - D3DDECLTYPE_FLOAT1 + 1; }
        if (e.Usage == D3DDECLUSAGE_BLENDINDICES && e.Type == D3DDECLTYPE_UBYTE4) L.biOff = e.Offset;
    }
    return L.uvOff >= 0;
}
static int typeFloats(BYTE t) { return t <= D3DDECLTYPE_FLOAT4 ? t - D3DDECLTYPE_FLOAT1 + 1 : 0; }
// midpoint of two vertex rows: floats averaged, bone influences merged per bone
static void midRow(const KitLayout& L, const BYTE* a, const BYTE* b, BYTE* out) {
    memcpy(out, a, L.stride);
    for (UINT i = 0; i < L.ne; i++) {
        const D3DVERTEXELEMENT9& e = L.el[i];
        if (e.Stream != 0 || e.Usage == D3DDECLUSAGE_BLENDWEIGHT) continue;
        int nf = typeFloats(e.Type);
        for (int k = 0; k < nf; k++) {
            float x = ((const float*)(a + e.Offset))[k], y = ((const float*)(b + e.Offset))[k];
            ((float*)(out + e.Offset))[k] = (x + y) * 0.5f;
        }
        if (e.Type == D3DDECLTYPE_D3DCOLOR)
            for (int k = 0; k < 4; k++) out[e.Offset + k] = (BYTE)((a[e.Offset + k] + b[e.Offset + k] + 1) / 2);
    }
    if (L.biOff < 0) return;
    // kit VS (vs_0): index 0 takes the implicit weight 1 - sum(w), index k + 1 takes w[k]
    int nb = L.bwOff >= 0 ? L.bwN + 1 : 1;
    int bone[8]; float w[8]; int n = 0;
    for (int s = 0; s < 2; s++) {
        const BYTE* r = s ? b : a; float rest = 1;
        for (int k = 1; k < nb; k++) rest -= ((const float*)(r + L.bwOff))[k - 1];
        for (int k = 0; k < nb; k++) {
            float wk = k ? ((const float*)(r + L.bwOff))[k - 1] : rest;
            int bi = r[L.biOff + k], j = 0;
            while (j < n && bone[j] != bi) j++;
            if (j == n) { bone[n] = bi; w[n++] = 0; }
            w[j] += wk * 0.5f;
        }
    }
    for (int i = 0; i < n; i++) for (int j = i + 1; j < n; j++) if (w[j] > w[i]) { float t = w[i]; w[i] = w[j]; w[j] = t; int u = bone[i]; bone[i] = bone[j]; bone[j] = u; }
    if (n > nb) n = nb;
    float sum = 0; for (int i = 0; i < n; i++) sum += w[i]; if (sum <= 0) sum = 1;
    for (int k = 0; k < 4; k++) out[L.biOff + k] = (BYTE)bone[k < n ? k : 0];
    for (int k = 1; k < nb; k++) ((float*)(out + L.bwOff))[k - 1] = k < n ? w[k] / sum : 0;
}

// ---- growable arrays (no CRT containers in this DLL) ----
struct Bytes { BYTE* p; UINT n, cap; };
static void grow(Bytes& b, UINT add) {
    if (b.n + add <= b.cap) return;
    UINT c = b.cap ? b.cap * 2 : 1 << 16; while (c < b.n + add) c *= 2;
    BYTE* q = (BYTE*)HeapAlloc(GetProcessHeap(), 0, c); if (b.p) { memcpy(q, b.p, b.n); HeapFree(GetProcessHeap(), 0, b.p); }
    b.p = q; b.cap = c;
}
static void bfree(Bytes& b) { if (b.p) HeapFree(GetProcessHeap(), 0, b.p); b.p = NULL; b.n = b.cap = 0; }

// midpoint cache: open addressing over (min, max) vertex pairs
struct MidMap { UINT64* key; UINT* val; UINT cap; };
static UINT midFind(MidMap& m, UINT i, UINT j, bool& isNew, UINT fresh) {
    UINT64 k = i < j ? ((UINT64)i << 32) | j : ((UINT64)j << 32) | i;
    UINT h = (UINT)((k * 0x9E3779B97F4A7C15ull) >> 40) & (m.cap - 1);
    while (m.key[h] != ~0ull && m.key[h] != k) h = (h + 1) & (m.cap - 1);
    if (m.key[h] == k) { isNew = false; return m.val[h]; }
    m.key[h] = k; m.val[h] = fresh; isNew = true; return fresh;
}

struct Forced {
    IDirect3DVertexBuffer9* vb0; UINT off, first, mv, nv, stride, si, np; D3DPRIMITIVETYPE t;   // key (first = bV + mV)
    IDirect3DVertexBuffer9* vb; IDirect3DIndexBuffer9* ib; UINT nverts, ntris; bool ok;
};
static Forced g_forced[MAX_FORCED]; static int g_nforced = 0;

static float* rowUV(Bytes& rows, UINT stride, UINT v, int off) { return (float*)(rows.p + v * stride + off); }

static bool buildForced(IDirect3DDevice9* d, const KitLayout& L, Forced& F, IDirect3DIndexBuffer9* ib) {
    // source vertices [first, first + nv) and indices
    D3DINDEXBUFFER_DESC id; if (!ib || FAILED(ib->GetDesc(&id))) return false;
    UINT isz = id.Format == D3DFMT_INDEX32 ? 4 : 2;
    UINT nIdx = F.t == D3DPT_TRIANGLESTRIP ? F.np + 2 : F.np * 3;
    Bytes rows = { 0 }, tris = { 0 }, stuck = { 0 };   // stuck: leaves still straddling a border
    grow(rows, F.nv * F.stride);
    void* p = NULL;
    if (FAILED(F.vb0->Lock(F.off + F.first * F.stride, F.nv * F.stride, &p, D3DLOCK_READONLY)) || !p) { bfree(rows); return false; }
    memcpy(rows.p, p, F.nv * F.stride); rows.n = F.nv * F.stride; F.vb0->Unlock();
    UINT* idx = (UINT*)HeapAlloc(GetProcessHeap(), 0, nIdx * 4);
    if (FAILED(ib->Lock(F.si * isz, nIdx * isz, &p, D3DLOCK_READONLY)) || !p) { bfree(rows); HeapFree(GetProcessHeap(), 0, idx); return false; }
    for (UINT i = 0; i < nIdx; i++) idx[i] = isz == 4 ? ((UINT*)p)[i] : ((WORD*)p)[i];
    ib->Unlock();
    // triangles of the draw (strip winding alternates), relative to first
    UINT ntri = 0;
    UINT (*tri)[3] = (UINT(*)[3])HeapAlloc(GetProcessHeap(), 0, (nIdx + 2) * 12);
    for (UINT i = 0; i + 2 < nIdx || (F.t != D3DPT_TRIANGLESTRIP && i < nIdx); ) {
        UINT a, b, c;
        if (F.t == D3DPT_TRIANGLESTRIP) { a = idx[i]; b = idx[i + 1]; c = idx[i + 2]; if (i & 1) { UINT s = a; a = b; b = s; } i++; }
        else { a = idx[i]; b = idx[i + 1]; c = idx[i + 2]; i += 3; }
        if (a == b || b == c || a == c) continue;
        // indices are relative to BaseVertexIndex: the draw reads [mV, mV + nV) of them
        if (a < F.mv || b < F.mv || c < F.mv) continue;
        a -= F.mv; b -= F.mv; c -= F.mv;
        if (a >= F.nv || b >= F.nv || c >= F.nv) continue;
        tri[ntri][0] = a; tri[ntri][1] = b; tri[ntri][2] = c; ntri++;
    }
    HeapFree(GetProcessHeap(), 0, idx);
    // subdivide against the remap
    MidMap mm; mm.cap = 1 << 20; mm.key = (UINT64*)HeapAlloc(GetProcessHeap(), 0, mm.cap * 8); mm.val = (UINT*)HeapAlloc(GetProcessHeap(), 0, mm.cap * 4);
    memset(mm.key, 0xFF, mm.cap * 8);
    struct Item { UINT a, b, c; int depth; };
    UINT stackCap = 4096, sn = 0;
    Item* st = (Item*)HeapAlloc(GetProcessHeap(), 0, stackCap * sizeof(Item));
    for (UINT i = 0; i < ntri; i++) {
        if (sn == stackCap) { stackCap *= 2; Item* q = (Item*)HeapAlloc(GetProcessHeap(), 0, stackCap * sizeof(Item)); memcpy(q, st, sn * sizeof(Item)); HeapFree(GetProcessHeap(), 0, st); st = q; }
        Item it = { tri[i][0], tri[i][1], tri[i][2], 0 }; st[sn++] = it;
    }
    HeapFree(GetProcessHeap(), 0, tri);
    UINT nverts = F.nv;
    BYTE* tmp = (BYTE*)HeapAlloc(GetProcessHeap(), 0, F.stride);
    while (sn) {
        Item it = st[--sn];
        float* ua = rowUV(rows, F.stride, it.a, L.uvOff), *ub = rowUV(rows, F.stride, it.b, L.uvOff), *uc = rowUV(rows, F.stride, it.c, L.uvOff);
        float ca[2], cb[2], cc[2]; fwdLook(ua[0], ua[1], ca); fwdLook(ub[0], ub[1], cb); fwdLook(uc[0], uc[1], cc);
        bool ok = true;
        for (int k = 0; k < 4 && ok; k++) {
            float s = PROBES[k][0], t = PROBES[k][1], want[2];
            fwdLook(ua[0] + (ub[0] - ua[0]) * s + (uc[0] - ua[0]) * t, ua[1] + (ub[1] - ua[1]) * s + (uc[1] - ua[1]) * t, want);
            for (int c = 0; c < 2; c++) if (fabsf(want[c] - (ca[c] + (cb[c] - ca[c]) * s + (cc[c] - ca[c]) * t)) > FORCE_AGREE_TOL) ok = false;
        }
        if (ok || it.depth >= FORCE_MAX_DEPTH || nverts + 3 > FORCE_MAX_VERTS) {
            if (!ok) { grow(stuck, 4); *(UINT*)(stuck.p + stuck.n) = tris.n / 12; stuck.n += 4; }
            grow(tris, 12); UINT* o = (UINT*)(tris.p + tris.n); o[0] = it.a; o[1] = it.b; o[2] = it.c; tris.n += 12;
            continue;
        }
        UINT m[3], e[3][2] = { { it.a, it.b }, { it.b, it.c }, { it.c, it.a } };
        for (int k = 0; k < 3; k++) {
            bool isNew; m[k] = midFind(mm, e[k][0], e[k][1], isNew, nverts);
            if (isNew) {
                midRow(L, rows.p + e[k][0] * F.stride, rows.p + e[k][1] * F.stride, tmp);
                grow(rows, F.stride); memcpy(rows.p + rows.n, tmp, F.stride); rows.n += F.stride; nverts++;
            }
        }
        if (sn + 4 > stackCap) { stackCap *= 2; Item* q = (Item*)HeapAlloc(GetProcessHeap(), 0, stackCap * sizeof(Item)); memcpy(q, st, sn * sizeof(Item)); HeapFree(GetProcessHeap(), 0, st); st = q; }
        int nd = it.depth + 1;
        Item k0 = { it.a, m[0], m[2], nd }, k1 = { m[0], it.b, m[1], nd }, k2 = { m[2], m[1], it.c, nd }, k3 = { m[0], m[1], m[2], nd };
        st[sn++] = k0; st[sn++] = k1; st[sn++] = k2; st[sn++] = k3;
    }
    HeapFree(GetProcessHeap(), 0, st); HeapFree(GetProcessHeap(), 0, tmp);
    HeapFree(GetProcessHeap(), 0, mm.key); HeapFree(GetProcessHeap(), 0, mm.val);
    // a leaf still straddling a chart border would interpolate across the gap
    // between the charts (a thin grey line): give it its own corners, mapped
    // through the one chart at its centroid (local affine map of the remap)
    UINT nStuck = stuck.n / 4;
    float* snap = (float*)HeapAlloc(GetProcessHeap(), 0, nStuck * 6 * sizeof(float) + 1);
    const float h = FORCE_JACOBIAN_STEP_TEXELS / g_fwdW;
    for (UINT s = 0; s < nStuck; s++) {
        UINT* t = (UINT*)(tris.p + ((UINT*)stuck.p)[s] * 12);
        float* u[3]; for (int k = 0; k < 3; k++) u[k] = rowUV(rows, F.stride, t[k], L.uvOff);
        float c0 = (u[0][0] + u[1][0] + u[2][0]) / 3, c1 = (u[0][1] + u[1][1] + u[2][1]) / 3;
        float C[2], J[2][2];                      // J[axis][out]
        fwdLook(c0, c1, C);
        for (int ax = 0; ax < 2; ax++) {
            float p[2], m[2];
            fwdLook(c0 + (ax ? 0 : h), c1 + (ax ? h : 0), p); fwdLook(c0 - (ax ? 0 : h), c1 - (ax ? h : 0), m);
            // one-sided: the side that jumps a border has the larger step
            bool fwdSide = fabsf(p[0] - C[0]) + fabsf(p[1] - C[1]) <= fabsf(C[0] - m[0]) + fabsf(C[1] - m[1]);
            for (int o = 0; o < 2; o++) J[ax][o] = (fwdSide ? p[o] - C[o] : C[o] - m[o]) / h;
        }
        for (int k = 0; k < 3; k++) for (int o = 0; o < 2; o++)
            snap[s * 6 + k * 2 + o] = C[o] + J[0][o] * (u[k][0] - c0) + J[1][o] * (u[k][1] - c1);
    }
    // each vertex's kit uv -> the PES14+ sheet
    for (UINT v = 0; v < nverts; v++) { float* u = rowUV(rows, F.stride, v, L.uvOff); float o[2]; fwdLook(u[0], u[1], o); u[0] = o[0]; u[1] = o[1]; }
    for (UINT s = 0; s < nStuck; s++) {
        UINT* t = (UINT*)(tris.p + ((UINT*)stuck.p)[s] * 12);
        for (int k = 0; k < 3; k++) {
            grow(rows, F.stride); memcpy(rows.p + rows.n, rows.p + t[k] * F.stride, F.stride);
            float* u = (float*)(rows.p + rows.n + L.uvOff); u[0] = snap[s * 6 + k * 2]; u[1] = snap[s * 6 + k * 2 + 1];
            rows.n += F.stride; t[k] = nverts++;
        }
    }
    HeapFree(GetProcessHeap(), 0, snap); bfree(stuck);
    F.nverts = nverts; F.ntris = tris.n / 12;
    bool ok = SUCCEEDED(d->CreateVertexBuffer(rows.n, D3DUSAGE_WRITEONLY, 0, D3DPOOL_MANAGED, &F.vb, NULL))
           && SUCCEEDED(d->CreateIndexBuffer(tris.n, D3DUSAGE_WRITEONLY, D3DFMT_INDEX32, D3DPOOL_MANAGED, &F.ib, NULL));
    if (ok && SUCCEEDED(F.vb->Lock(0, rows.n, &p, 0))) { memcpy(p, rows.p, rows.n); F.vb->Unlock(); }
    if (ok && SUCCEEDED(F.ib->Lock(0, tris.n, &p, 0))) { memcpy(p, tris.p, tris.n); F.ib->Unlock(); }
    bfree(rows); bfree(tris);
    char msg[160]; wsprintfA(msg, "kitforce: %u/%u/%u -> %u verts %u tris (%u snapped) %s", F.nv, F.np, F.stride, F.nverts, F.ntris, nStuck, ok ? "ok" : "FAILED");
    logline(msg);
    return ok;
}

static Forced* forcedFor(IDirect3DDevice9* d, D3DPRIMITIVETYPE t, UINT first, UINT mv, UINT nv, UINT si, UINT np) {
    for (int i = 0; i < g_nforced; i++) {
        Forced& F = g_forced[i];
        if (F.vb0 == g_vb && F.off == g_vbOff && F.first == first && F.nv == nv && F.stride == g_stride && F.si == si && F.np == np && F.t == t)
            return F.ok ? &F : NULL;
    }
    if (g_nforced >= MAX_FORCED || !loadFwd()) return NULL;
    KitLayout L;
    if (!kitLayout(g_decl, g_stride, L)) return NULL;
    Forced& F = g_forced[g_nforced++]; memset(&F, 0, sizeof(F));
    F.vb0 = g_vb; F.off = g_vbOff; F.first = first; F.mv = mv; F.nv = nv; F.stride = g_stride; F.si = si; F.np = np; F.t = t;
    F.ok = buildForced(d, L, F, g_ib);
    return F.ok ? &F : NULL;
}

// ---- which 4cc kit a sheet is: our own .tex files, hashed at the level
// kitHash reads (survives drawlogic reloads, unlike the patch cache) ----
static const int MAX_KITFILES = 128;
struct KitFile { DWORD hash; int tid; const wchar_t* slot; };
static KitFile g_kitFiles[MAX_KITFILES]; static int g_nKitFiles = -1;
static const wchar_t* KIT_SLOT_NAMES[] = { L"pa", L"pb", L"ga", L"gb" };
static void scanKitFiles() {
    g_nKitFiles = 0;
    wchar_t pat[MAX_PATH]; wsprintfW(pat, L"%s*", KIT_DIR);
    WIN32_FIND_DATAW fd; HANDLE h = FindFirstFileW(pat, &fd);
    if (h == INVALID_HANDLE_VALUE) return;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
        int tid = _wtoi(fd.cFileName); if (tid <= 0) continue;
        for (int s = 0; s < 4 && g_nKitFiles < MAX_KITFILES; s++) {
            wchar_t p[MAX_PATH]; wsprintfW(p, L"%s%d\\%s.tex", KIT_DIR, tid, KIT_SLOT_NAMES[s]);
            DWORD n = 0; BYTE* b = readAll(p, &n); if (!b) continue;
            DWORD* hd = (DWORD*)b;
            if (hd[0] == TEX_MAGIC && hd[1] == KIT_W && hd[2] == KIT_H && hd[3] > KIT_HASH_LEVEL_FROM_END) {
                DWORD lv = hd[3] - KIT_HASH_LEVEL_FROM_END, off = 16;
                for (DWORD m = 0; m < lv; m++) { UINT mw = KIT_W >> m, mh = KIT_H >> m; if (!mw) mw = 1; if (!mh) mh = 1; off += mw * mh * 4; }
                UINT mw = KIT_W >> lv, mh = KIT_H >> lv; if (!mw) mw = 1; if (!mh) mh = 1;
                DWORD fh = 2166136261u; for (UINT i = 0; i < mw * mh * 4; i++) fh = (fh ^ b[off + i]) * 16777619u;
                g_kitFiles[g_nKitFiles].hash = fh; g_kitFiles[g_nKitFiles].tid = tid; g_kitFiles[g_nKitFiles++].slot = KIT_SLOT_NAMES[s];
            }
            HeapFree(GetProcessHeap(), 0, b);
        }
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    char m[64]; wsprintfA(m, "kitforce: %d team kit files", g_nKitFiles); logline(m);
}
static bool kitOfSheet(IDirect3DBaseTexture9* bt, int& tid, const wchar_t*& slot) {
    if (!bt || bt->GetType() != D3DRTYPE_TEXTURE) return false;
    IDirect3DTexture9* t = (IDirect3DTexture9*)bt;
    D3DSURFACE_DESC sd; t->GetLevelDesc(0, &sd);
    if (sd.Width != KIT_W || sd.Height != KIT_H || sd.Format != D3DFMT_A8R8G8B8) return false;
    if (g_nKitFiles < 0) scanKitFiles();
    DWORD h = kitHash(t);
    for (int i = 0; i < g_nKitFiles; i++) if (g_kitFiles[i].hash == h) { tid = g_kitFiles[i].tid; slot = g_kitFiles[i].slot; return true; }
    return false;
}
