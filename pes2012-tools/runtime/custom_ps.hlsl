// Pixel shaders for PES materials the game's kit shader cannot reproduce.
// They run after PES2012's own colour-pass kit vertex shader (dumped with
// flags\shaderdump, 29-09), so the inputs are its outputs:
//   TEXCOORD0 world normal, TEXCOORD3 world position, TEXCOORD4.xy UV set 0
//   (main UVs), TEXCOORD4.zw UV set 1 (the timing UVs: drawlogic sets
//   c176 = (1, 0) and c179 = (0, 1) for UVStep subs, so .xy = TEXCOORD0 and
//   .zw = TEXCOORD1), TEXCOORD6 = (0, 0, 0, 1).
// Constants are the ones the game's kit pixel shader reads (its lighting,
// decoded 30-09 from the dumped ps_3_0 in the pre-match preview and a match):
//   c0 material diffuse scale, c6 fog (scale, offset, -min height), c7 fog
//   colour, c10 / c11 main and second light direction, c12 / c13 their
//   colours, c20 hemisphere axis, c21 / c22 the ambients at the axis' far and
//   near side: ambient = lerp(c22, c21, 0.5 - 0.5 * dot(n, c20)).
// c16..c19 are NOT lighting: the game multiplies world position by them (the
// shadow-map projection). This shader used to read them as hemisphere axis
// and sky / ground ambient, which lit Pony models with matrix rows - bright
// green in the pre-match preview, red in JPEG Arena's sun (30-09).
// Build: tools/build_shaders.sh (vkd3d-compiler -> custom_ps.h).

sampler2D diffuseMap : register(s0);
float4 materialScale : register(c0);
float4 fogParams : register(c6);
float4 fogColour : register(c7);
float4 lightDir : register(c10);
float4 light2Dir : register(c11);
float4 lightColour : register(c12);
float4 light2Colour : register(c13);
float4 hemiAxis : register(c20);
float4 ambientFar : register(c21);
float4 ambientNear : register(c22);

static const float LOG2_E = 1.44269502;   // the game's exp2 fog: exp(x) = exp2(x * log2 e)
// Pony: lit where N.L exceeds this, else ambient only (a hard cel terminator).
// Provisional: PES's Pony shader is not available; 0.1 keeps the band off
// grazing angles.
static const float TOON_STEP = 0.1;

struct In {
    float4 fog : TEXCOORD6;
    float3 normal : TEXCOORD0;
    float3 pos : TEXCOORD3;
    float4 uv : TEXCOORD4;
};

float3 fogged(float3 c, float3 pos) {
    // exactly the game's kit shader: f = sat(exp2((max(pos.z, -c6.z) + c6.y) * c6.x * log2 e))
    float f = saturate(exp2((max(pos.z, -fogParams.z) + fogParams.y) * fogParams.x * LOG2_E));
    return lerp(fogColour.rgb, c, f);
}

// Shadeless / Constant: the texture as painted, no lighting.
float4 shadeless(In i) : COLOR {
    float4 c = tex2D(diffuseMap, i.uv.xy);
    return float4(fogged(c.rgb, i.pos), c.a * i.fog.w);
}

// Pony: flat cel shading - the game's own hemisphere ambient everywhere, each
// of its two lights on the side it faces.
float4 toon(In i) : COLOR {
    float4 c = tex2D(diffuseMap, i.uv.xy);
    float3 n = normalize(i.normal);
    float3 amb = lerp(ambientNear.rgb, ambientFar.rgb, 0.5 - 0.5 * dot(n, hemiAxis.xyz));
    float lit = step(TOON_STEP, dot(n, -lightDir.xyz));
    float lit2 = step(TOON_STEP, dot(n, -light2Dir.xyz));
    float3 light = (amb + lit * lightColour.rgb + lit2 * light2Colour.rgb) * materialScale.rgb;
    return float4(fogged(c.rgb * light, i.pos), c.a * i.fog.w);
}

// UVStep frame grids (PES2019+ fox3dfw_constant_srgb_ndr_uvstep / fox3ddf_blin_uvstep):
// the base texture holds the animation frames as a grid, left-to-right,
// top-to-bottom. Per-sub params ride the first free float registers past the
// lighting block (drawlogic.cpp USTEP_PS_*; the game's kit shader reads c23..c28,
// so these start at c29):
//   c29 = (tileU, tileV, tilesUsed, scaleUVsToTiles)
//   c30 = (useTiming, secCycle, secTimingU, secTimingV)
//   c31 = (time mod secCycle, time mod secTimingU, time mod secTimingV, 0):
//         drawlogic reduces the real time per period in double precision; the
//         raw seconds since boot (1e5+) left float32 too coarse to step frames
// Timing mode samples the timing texture (s2) at the timing UVs plus the time
// scroll; its R picks the frame (R * tilesUsed / 256 bands: 0 the first, 255
// the last), a transparent texel draws nothing (per-part visibility). Uniform
// mode steps through the frames over secCycle (0 stays on frame 0). Scale 0:
// the main UVs already sit inside the top-left tile; 1: they span the whole
// texture and shrink into one tile.
sampler2D timingMap : register(s2);
float4 uvTiles : register(c29);
float4 uvTiming : register(c30);
float4 uvTime : register(c31);

float2 uvstepUV(float2 mainUV, float2 timingUV, out bool hide) {
    float frame;
    hide = false;
    if (uvTiming.x > 0.5) {
        // time scroll per axis; a zero cycle means no motion on that axis
        float2 scrolled = timingUV;
        if (uvTiming.z > 0.0) scrolled.x += uvTime.y / uvTiming.z;
        if (uvTiming.w > 0.0) scrolled.y += uvTime.z / uvTiming.w;
        float4 t = tex2D(timingMap, scrolled);
        if (t.a == 0.0) { hide = true; return 0.0; }
        // floor keeps R = 255 on the last frame (band min(255 * used / 256) = used - 1)
        frame = floor(t.r * 255.0 * uvTiles.z / 256.0);
    } else if (uvTiming.y > 0.0) {
        frame = floor(frac(uvTime.x / uvTiming.y) * uvTiles.z);   // uvTime.x < secCycle
    } else {
        frame = 0.0;   // a zero cycle stays on frame 0
    }
    frame = clamp(frame, 0.0, uvTiles.z - 1.0);
    float2 local = lerp(mainUV, mainUV / uvTiles.xy, step(0.5, uvTiles.w));
    float col = fmod(frame, uvTiles.x), row = floor(frame / uvTiles.x);
    return (float2(col, row) + local) / uvTiles.xy;
}

// UVStep constant: the grid frame as painted, no lighting (like shadeless).
float4 uvstep(In i) : COLOR {
    bool hide;
    float2 guv = uvstepUV(i.uv.xy, i.uv.zw, hide);
    if (hide) clip(-1.0);
    float4 c = tex2D(diffuseMap, guv);
    return float4(fogged(c.rgb, i.pos), c.a * i.fog.w);
}

// UVStep lit: the grid frame under the toon lighting (like toon).
float4 uvstep_lit(In i) : COLOR {
    bool hide;
    float2 guv = uvstepUV(i.uv.xy, i.uv.zw, hide);
    if (hide) clip(-1.0);
    float4 c = tex2D(diffuseMap, guv);
    float3 n = normalize(i.normal);
    float3 amb = lerp(ambientNear.rgb, ambientFar.rgb, 0.5 - 0.5 * dot(n, hemiAxis.xyz));
    float lit = step(TOON_STEP, dot(n, -lightDir.xyz));
    float lit2 = step(TOON_STEP, dot(n, -light2Dir.xyz));
    float3 light = (amb + lit * lightColour.rgb + lit2 * light2Colour.rgb) * materialScale.rgb;
    return float4(fogged(c.rgb * light, i.pos), c.a * i.fog.w);
}
