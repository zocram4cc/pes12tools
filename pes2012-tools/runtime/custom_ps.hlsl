// Pixel shaders for PES materials the game's kit shader cannot reproduce.
// They run after PES2012's own colour-pass kit vertex shader (dumped with
// flags\shaderdump, 29-09), so the inputs are its outputs:
//   TEXCOORD0 world normal, TEXCOORD3 world position, TEXCOORD4.xy UV set 0,
//   TEXCOORD6 = (0, 0, 0, 1).
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
