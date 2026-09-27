"""PES <-> GameplayFootball skeleton bridge.

The engine's skeleton IS the Fox Engine ANIMATION skeleton: GF's
player.object carries PES's twenty body bones at the bind positions of
common/anime/FoxAnim/Body/CharacterAssets/body_anim_skel.ask (dt13) -- a
strict T-pose, arms straight along +-X, legs straight down, world-aligned
identity bind rotations -- plus PES's own hand rig (nineteen skh_* bones a
hand). Every .gani local quaternion applies to this rig VERBATIM after the
Fox->GF change of basis; that is what makes conversion 1:1.

Fox ships a SECOND skeleton for the same character: the render/skin bind
(common_package body.skl + hand_[lr].skl, dt00), a relaxed pose with the
arms ~45 degrees down and the legs splayed ~7 degrees. Meshes (fmdl), skin
weights and the hand-pose rig are authored in THAT pose. The two are the
same bones in different rest poses; PES_ALIGN below is the fixed per-bone
world rotation W taking the render pose onto the anim pose
(W(b) . offset_render(child) == offset_anim(child), minimal arc). It is
what re-expresses everything A-pose-authored onto the rig:

  - meshes: the engine's authoring->bind bake (humanoidbase.cpp,
    base.anim.util = BASE_POSE below) re-poses every vertex at load;
  - hand poses: finger locals conjugate by W(hand) (hand_poses.py);
  - legacy GF animations: locals sandwich W(parent) . q . W(bone)^-1
    (migrate_anims_tpose.py).

History: the rig used to BE the render bind, and ganis were FK'd over it --
which over-rotated every limb by the pose difference (the y08 foul's wrists
collapsed to 0.03 m behind the chest; entrance arms splayed). Decoded knee
tracks are pure-X hinges and elbows pure-Y across every key, which proves
the gani stores parent-local chained rotations; on the anim skeleton the
same clip is a natural referee. Do not "calibrate" bind offsets to fix a
pose again: the two-skeleton split is the mechanism.

The sixteen legacy node names are kept -- engine code refers to them.
Joint IDs are the twenty body joints first, in the order they have always
had, then the thirty-eight finger joints (jointorder.cpp builds the same).

What PES has and this does not: skf_* face muscles (the FaceRig deforms
those instead), dsk_* skin helpers and cloth bones. Those are never
independently animated by the body ganis, and under the engine's
inverse-bind skinning collapsing such a bone onto the animated bone it
rigidly follows is mathematically lossless, so their skin weights resolve
through HELPER_TO_GF/resolve_bone below. (What IS lost: Fox's runtime
constraints -- twist distribution on dsk_forearm/dsk_thigh, clavicle aim --
which GF does not emulate. Documented, not accidental.)
"""

import math

# ---------------------------------------------------------------------------
# Animation-track data (decoded from dt13 body_skel.frig + .gani files).
#
# A PES body gani has 15 track units / 27 segments. Units follow the frig's
# order (unit 0's name hash is StrCode32("RIG_ROOT")); the map below assigns
# each rotation segment to its bone. The vec3 segments inside the chain units
# (legs seg0, arms seg1) are auxiliary IK channels, often 0xFF-filled - skip.
PES_TRACK_MAP = {
    (2, 0): "dsk_hip",
    (3, 1): "sk_thigh_l", (3, 2): "sk_leg_l", (4, 0): "sk_foot_l",
    (5, 1): "sk_thigh_r", (5, 2): "sk_leg_r", (6, 0): "sk_foot_r",
    (7, 0): "sk_belly", (8, 0): "sk_chest",
    (9, 0): "sk_neck", (10, 0): "sk_head",
    (11, 0): "sk_shoulder_l", (11, 2): "sk_upperarm_l", (11, 3): "sk_forearm_l",
    (12, 0): "sk_hand_l",
    (13, 0): "sk_shoulder_r", (13, 2): "sk_upperarm_r", (13, 3): "sk_forearm_r",
    (14, 0): "sk_hand_r",
}
# root motion: unit 0 = RIG_ROOT (quat + XZ position), unit 1 = the mover
# ("motion" node == sk_root_hip, StrCode32 3ca4c491): quat + Y position,
# bind-relative.
PES_ROOT_UNIT = 0
PES_MOTION_UNIT = 1

# Position tracks store IEEE half-floats with the exponent rebased +7 (x128).
# Reading the result as millimetres (metres = raw / 128 / 1000) is what the
# entrance/cutscene export was built on, and it is kept here so that content
# does not move under anyone's feet.
PES_POS_TO_M = 1.0 / 128000.0

# For match animation that value is demonstrably too small by ~6x: at
# 1/128000 a sprinter's ankles never come within 15 cm of the pitch and a
# sliding tackle keeps its pelvis at standing height, swimming through the
# air. GameplayFootball reads velocity and every ball contact off the root
# track, so the scale was measured rather than assumed --
# calibrate_pos_scale.py sweeps it against three properties any human
# animation has (stance feet do not slide, ankles reach the grass and no
# further, a player lying down has his pelvis ~0.14 m up). Those three agree
# on a shallow optimum around 1/20000; 1/20480 = 2^11 * 10 sits inside it and
# is the kind of constant an engine actually stores.
PES_POS_TO_M_GAMEPLAY = 1.0 / 20480.0

# ---------------------------------------------------------------------------
# THE RIG: Fox anim-skeleton bind (Fox coords: Y up, +Z forward, metres),
# transcribed exactly from body_anim_skel.ask. "motion" is sk_root_hip, the
# mover unit 1 drives (quat + Y position, bind-relative); the tiny L/R float
# asymmetries are Konami's own data. The finger half is DERIVED below:
# render-bind finger offsets rotated by PES_ALIGN of the hand.
PES_BIND = {
    # bone: (global bind position, parent)
    "motion":        ((0.000000, 1.096120, 0.000000), None),
    "dsk_hip":       ((0.000000, 0.992081, -0.018350), "motion"),
    "sk_thigh_l":    ((0.090000, 0.959973, 0.052909), "dsk_hip"),
    "sk_leg_l":      ((0.089916, 0.540183, 0.088034), "sk_thigh_l"),
    "sk_foot_l":     ((0.089829, 0.100869, 0.041371), "sk_leg_l"),
    "sk_thigh_r":    ((-0.090000, 0.959973, 0.052909), "dsk_hip"),
    "sk_leg_r":      ((-0.089916, 0.540183, 0.088034), "sk_thigh_r"),
    "sk_foot_r":     ((-0.089829, 0.100869, 0.041371), "sk_leg_r"),
    "sk_belly":      ((0.000000, 1.096120, 0.000000), "motion"),
    "sk_chest":      ((0.000000, 1.262772, 0.013794), "sk_belly"),
    "sk_neck":       ((0.000000, 1.532053, 0.033468), "sk_chest"),
    "sk_head":       ((0.000000, 1.640056, 0.054333), "sk_neck"),
    "sk_shoulder_l": ((0.105080, 1.467052, 0.033469), "sk_chest"),
    "sk_upperarm_l": ((0.195000, 1.467052, 0.033468), "sk_shoulder_l"),
    "sk_forearm_l":  ((0.483665, 1.467049, 0.005673), "sk_upperarm_l"),
    "sk_hand_l":     ((0.769688, 1.467047, 0.053537), "sk_forearm_l"),
    "sk_shoulder_r": ((-0.105082, 1.467054, 0.033469), "sk_chest"),
    "sk_upperarm_r": ((-0.195002, 1.467055, 0.033469), "sk_shoulder_r"),
    "sk_forearm_r":  ((-0.483667, 1.467057, 0.005674), "sk_upperarm_r"),
    "sk_hand_r":     ((-0.769690, 1.467060, 0.053538), "sk_forearm_r"),
}

# ---------------------------------------------------------------------------
# The RENDER/SKIN bind (Fox coords), from the base package's body.skl and
# hand_l.skl / hand_r.skl (== the fmdl bone tables). Meshes, skin weights and
# the hand rig are authored in THIS pose. Four decimals, which is 0.05 mm -
# test_hand_rig holds the finger half against the two .skl files to within
# 0.1 mm. The mover has no render bone; it does not move between the poses,
# so it sits at the anim root here too.
#
# The finger bones are parentless in hand_[lr].skl, as every Fox helper bone
# is; the chains below are the anatomy, and match the unit order of the hand
# rig pes_human_hand_141203.frig (thumb, index, middle, pinky, ring - the
# thumb has three bones, the rest four).
PES_RENDER_BIND = {
    # bone: (global bind position, parent)
    "motion":        ((0.000000, 1.096120, 0.000000), None),
    "dsk_hip":       ((0.0000, 0.9921, -0.0184), "motion"),
    "sk_thigh_l":    ((0.0900, 0.9600, 0.0529), "dsk_hip"),
    "sk_leg_l":      ((0.1413, 0.5433, 0.0878), "sk_thigh_l"),
    "sk_foot_l":     ((0.1940, 0.1072, 0.0408), "sk_leg_l"),
    "sk_thigh_r":    ((-0.0900, 0.9600, 0.0529), "dsk_hip"),
    "sk_leg_r":      ((-0.1413, 0.5433, 0.0878), "sk_thigh_r"),
    "sk_foot_r":     ((-0.1940, 0.1072, 0.0408), "sk_leg_r"),
    "sk_belly":      ((0.0000, 1.0961, 0.0000), "motion"),
    "sk_chest":      ((0.0000, 1.2628, 0.0138), "sk_belly"),
    "sk_neck":       ((0.0000, 1.5321, 0.0335), "sk_chest"),
    "sk_head":       ((0.0000, 1.6401, 0.0543), "sk_neck"),
    "sk_shoulder_l": ((0.1051, 1.4671, 0.0335), "sk_chest"),
    "sk_upperarm_l": ((0.1950, 1.4671, 0.0335), "sk_shoulder_l"),
    "sk_forearm_l":  ((0.3991, 1.2620, 0.0138), "sk_upperarm_l"),
    "sk_hand_l":     ((0.6035, 1.0639, 0.0695), "sk_forearm_l"),
    "sk_shoulder_r": ((-0.1051, 1.4671, 0.0335), "sk_chest"),
    "sk_upperarm_r": ((-0.1950, 1.4671, 0.0335), "sk_shoulder_r"),
    "sk_forearm_r":  ((-0.3991, 1.2620, 0.0138), "sk_upperarm_r"),
    "sk_hand_r":     ((-0.6035, 1.0639, 0.0695), "sk_forearm_r"),

    # PES's hand rig, from hand_l.skl / hand_r.skl
    "skh_thumb_mata_l":  ((0.6147, 1.0545, 0.0881), "sk_hand_l"),
    "skh_thumb_mcp_l":   ((0.6269, 1.0178, 0.1276), "skh_thumb_mata_l"),
    "skh_thumb_pip_l":   ((0.6420, 1.0002, 0.1472), "skh_thumb_mcp_l"),
    "skh_index_mata_l":  ((0.6473, 1.0347, 0.0795), "sk_hand_l"),
    "skh_index_mcp_l":   ((0.6656, 1.0105, 0.0997), "skh_index_mata_l"),
    "skh_index_pip_l":   ((0.6897, 0.9758, 0.1107), "skh_index_mcp_l"),
    "skh_index_dip_l":   ((0.7032, 0.9564, 0.1169), "skh_index_pip_l"),
    "skh_middle_mata_l": ((0.6476, 1.0352, 0.0727), "sk_hand_l"),
    "skh_middle_mcp_l":  ((0.6689, 1.0061, 0.0741), "skh_middle_mata_l"),
    "skh_middle_pip_l":  ((0.6963, 0.9688, 0.0759), "skh_middle_mcp_l"),
    "skh_middle_dip_l":  ((0.7116, 0.9479, 0.0769), "skh_middle_pip_l"),
    "skh_pinky_mata_l":  ((0.6118, 1.0580, 0.0563), "sk_hand_l"),
    "skh_pinky_mcp_l":   ((0.6536, 1.0105, 0.0334), "skh_pinky_mata_l"),
    "skh_pinky_pip_l":   ((0.6722, 0.9832, 0.0212), "skh_pinky_mcp_l"),
    "skh_pinky_dip_l":   ((0.6826, 0.9678, 0.0143), "skh_pinky_pip_l"),
    "skh_ring_mata_l":   ((0.6474, 1.0348, 0.0655), "sk_hand_l"),
    "skh_ring_mcp_l":    ((0.6620, 1.0068, 0.0524), "skh_ring_mata_l"),
    "skh_ring_pip_l":    ((0.6868, 0.9721, 0.0456), "skh_ring_mcp_l"),
    "skh_ring_dip_l":    ((0.7006, 0.9527, 0.0418), "skh_ring_pip_l"),
    "skh_thumb_mata_r":  ((-0.6147, 1.0546, 0.0881), "sk_hand_r"),
    "skh_thumb_mcp_r":   ((-0.6269, 1.0179, 0.1276), "skh_thumb_mata_r"),
    "skh_thumb_pip_r":   ((-0.6420, 1.0002, 0.1472), "skh_thumb_mcp_r"),
    "skh_index_mata_r":  ((-0.6473, 1.0347, 0.0795), "sk_hand_r"),
    "skh_index_mcp_r":   ((-0.6656, 1.0104, 0.0997), "skh_index_mata_r"),
    "skh_index_pip_r":   ((-0.6898, 0.9758, 0.1107), "skh_index_mcp_r"),
    "skh_index_dip_r":   ((-0.7032, 0.9564, 0.1169), "skh_index_pip_r"),
    "skh_middle_mata_r": ((-0.6476, 1.0352, 0.0727), "sk_hand_r"),
    "skh_middle_mcp_r":  ((-0.6689, 1.0062, 0.0741), "skh_middle_mata_r"),
    "skh_middle_pip_r":  ((-0.6963, 0.9688, 0.0759), "skh_middle_mcp_r"),
    "skh_middle_dip_r":  ((-0.7116, 0.9479, 0.0769), "skh_middle_pip_r"),
    "skh_pinky_mata_r":  ((-0.6118, 1.0581, 0.0563), "sk_hand_r"),
    "skh_pinky_mcp_r":   ((-0.6536, 1.0106, 0.0334), "skh_pinky_mata_r"),
    "skh_pinky_pip_r":   ((-0.6722, 0.9832, 0.0212), "skh_pinky_mcp_r"),
    "skh_pinky_dip_r":   ((-0.6826, 0.9679, 0.0143), "skh_pinky_pip_r"),
    "skh_ring_mata_r":   ((-0.6474, 1.0348, 0.0655), "sk_hand_r"),
    "skh_ring_mcp_r":    ((-0.6621, 1.0068, 0.0524), "skh_ring_mata_r"),
    "skh_ring_pip_r":    ((-0.6868, 0.9721, 0.0456), "skh_ring_mcp_r"),
    "skh_ring_dip_r":    ((-0.7007, 0.9527, 0.0418), "skh_ring_pip_r"),
}


# ---------------------------------------------------------------------------
# Render-pose -> anim-pose alignment.

def _q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def _q_conj(q):
    return (-q[0], -q[1], -q[2], q[3])


def _q_rot(q, v):
    qv = (v[0], v[1], v[2], 0.0)
    r = _q_mul(_q_mul(q, qv), _q_conj(q))
    return (r[0], r[1], r[2])


def _minarc(a, b):
    """Smallest rotation (xyzw) taking direction a onto direction b."""
    la = math.sqrt(sum(c * c for c in a))
    lb = math.sqrt(sum(c * c for c in b))
    a = tuple(c / la for c in a)
    b = tuple(c / lb for c in b)
    d = sum(x * y for x, y in zip(a, b))
    if d > 1.0 - 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    cx = a[1] * b[2] - a[2] * b[1]
    cy = a[2] * b[0] - a[0] * b[2]
    cz = a[0] * b[1] - a[1] * b[0]
    s = math.sqrt((1.0 + d) * 2.0)
    return (cx / s, cy / s, cz / s, s * 0.5)


# Reference directions per bone: the child offset whose render->anim swing
# defines the bone's alignment. Interior bones use their anatomical child;
# leaves use Fox's own helpers (body.skl dsk_toe for the feet, dt13's
# body_high_skel.ask tip bones for hands and head), transcribed here.
_A_TOE_L = (0.202636, 0.045628, 0.145084)   # body.skl dsk_toe_l, global
_A_TOE_R = (-0.202654, 0.045476, 0.145096)  # body.skl dsk_toe_r, global
_T_TOE_OFF = (0.0, -0.062065, 0.104294)     # body_high_skel sk_toe - sk_foot
_T_HAND_TIP_OFF = (0.149994, 0.0, -0.001309)  # tip_sk_hand_l - sk_hand_l
_T_HEAD_TIP_OFF = (0.0, 0.2, 0.0)             # tip_sk_head - sk_head

_ALIGN_REF_CHILD = {
    "motion": "sk_belly", "dsk_hip": "sk_thigh_l",
    "sk_thigh_l": "sk_leg_l", "sk_leg_l": "sk_foot_l",
    "sk_thigh_r": "sk_leg_r", "sk_leg_r": "sk_foot_r",
    "sk_belly": "sk_chest", "sk_chest": "sk_neck", "sk_neck": "sk_head",
    "sk_shoulder_l": "sk_upperarm_l", "sk_upperarm_l": "sk_forearm_l",
    "sk_forearm_l": "sk_hand_l",
    "sk_shoulder_r": "sk_upperarm_r", "sk_upperarm_r": "sk_forearm_r",
    "sk_forearm_r": "sk_hand_r",
}


def _v_norm(v):
    l = math.sqrt(sum(c * c for c in v))
    return tuple(c / l for c in v)


def _v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _frame_align(a_primary, t_primary, a_secondary, t_secondary):
    """Rotation mapping a_primary onto t_primary AND (as closely as the
    primary constraint allows) a_secondary onto t_secondary: the minimal arc
    for the primary, then the twist about the primary that best matches the
    secondary. Two vectors pin all three degrees of freedom - this is how
    the hand's palm convention enters, where a single bone direction cannot
    say which way the palm faces."""
    swing = _minarc(a_primary, t_primary)
    axis = _v_norm(t_primary)
    swung = _q_rot(swing, a_secondary)
    # project both secondaries onto the plane perpendicular to the axis
    def flat(v):
        d = sum(x * y for x, y in zip(v, axis))
        return tuple(x - d * a for x, a in zip(v, axis))
    u = flat(swung)
    w = flat(t_secondary)
    lu = math.sqrt(sum(c * c for c in u))
    lw = math.sqrt(sum(c * c for c in w))
    if lu < 1e-9 or lw < 1e-9:
        return swing
    u = tuple(c / lu for c in u)
    w = tuple(c / lw for c in w)
    cos_t = max(-1.0, min(1.0, sum(x * y for x, y in zip(u, w))))
    sin_t = sum(x * y for x, y in zip(_v_cross(u, w), axis))
    theta = math.atan2(sin_t, cos_t)
    s = math.sin(theta * 0.5)
    twist = (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(theta * 0.5))
    return _q_mul(twist, swing)


def _build_align():
    def a_pos(b):
        return PES_RENDER_BIND[b][0]

    def t_pos(b):
        return PES_BIND[b][0]

    def off(pos, child, bone):
        return tuple(c - p for c, p in zip(pos(child), pos(bone)))

    align = {}
    for bone in PES_BIND:
        child = _ALIGN_REF_CHILD.get(bone)
        if child is not None:
            a_dir = off(a_pos, child, bone)
            t_dir = off(t_pos, child, bone)
            zero = 1e-9
            if (math.sqrt(sum(c * c for c in a_dir)) < zero or
                    math.sqrt(sum(c * c for c in t_dir)) < zero):
                align[bone] = (0.0, 0.0, 0.0, 1.0)
            else:
                align[bone] = _minarc(a_dir, t_dir)
        elif bone == "sk_foot_l":
            align[bone] = _minarc(tuple(t - f for t, f in zip(_A_TOE_L, a_pos(bone))), _T_TOE_OFF)
        elif bone == "sk_foot_r":
            align[bone] = _minarc(tuple(t - f for t, f in zip(_A_TOE_R, a_pos(bone))), _T_TOE_OFF)
        elif bone == "sk_head":
            align[bone] = _minarc((0.0, 1.0, 0.0), _T_HEAD_TIP_OFF)

    # Hands: one direction cannot say which way the palm faces, and a wrong
    # twist here corkscrews the wrist skin and every finger. The palm pins
    # it: at the anim pose the hand lies flat, fingers +X, back of the hand
    # +Y (both hand tip bones sit in the XZ plane and the finger cluster is
    # authored palm-down); in the render bind the back-of-hand normal comes
    # from the authored finger cluster itself.
    for side, suffix, tip in (("sk_hand_l", "_l", _T_HAND_TIP_OFF),
                              ("sk_hand_r", "_r",
                               (-_T_HAND_TIP_OFF[0], _T_HAND_TIP_OFF[1],
                                _T_HAND_TIP_OFF[2]))):
        def rel(bone):
            return tuple(c - p for c, p in
                         zip(PES_RENDER_BIND[bone][0], PES_RENDER_BIND[side][0]))
        fingers = rel("skh_middle_dip" + suffix)
        spread = tuple(a - b for a, b in
                       zip(PES_RENDER_BIND["skh_index_mata" + suffix][0],
                           PES_RENDER_BIND["skh_pinky_mata" + suffix][0]))
        back = _v_norm(_v_cross(spread, fingers))
        if suffix == "_r":              # mirrored winding flips the normal
            back = tuple(-c for c in back)
        align[side] = _frame_align(fingers, tip, back, (0.0, 1.0, 0.0))

    # Forearms carry the wrist skin (dsk_forearm, the cuff of a glove): they
    # inherit the hand's twist, corrected by the minimal arc that keeps the
    # bone's own position constraint exact. The pose difference that remains
    # lands at the elbow, where the arm actually pronates.
    for forearm, hand in (("sk_forearm_l", "sk_hand_l"),
                          ("sk_forearm_r", "sk_hand_r")):
        a_dir = tuple(c - p for c, p in zip(PES_RENDER_BIND[hand][0],
                                            PES_RENDER_BIND[forearm][0]))
        t_dir = tuple(c - p for c, p in zip(PES_BIND[hand][0],
                                            PES_BIND[forearm][0]))
        swung = _q_rot(align[hand], a_dir)
        align[forearm] = _q_mul(_minarc(swung, t_dir), align[hand])
    return align


# bone -> quat (Fox coords, xyzw): world rotation taking the render pose of
# that bone onto the anim pose. Identity for the trunk, neck, head and
# clavicles; ~7 deg for the legs, ~4 deg for the feet, ~45 deg for the arms.
PES_ALIGN = _build_align()


def _extend_bind_with_fingers():
    """Fingers ride the hand: anim-pose position = hand_T + W(hand).(p_A - hand_A)."""
    for bone, (pos, parent) in PES_RENDER_BIND.items():
        if not bone.startswith("skh_"):
            continue
        hand = "sk_hand_l" if bone.endswith("_l") else "sk_hand_r"
        w = PES_ALIGN[hand]
        rel = tuple(p - h for p, h in zip(pos, PES_RENDER_BIND[hand][0]))
        rot = _q_rot(w, rel)
        PES_BIND[bone] = (tuple(h + r for h, r in zip(PES_BIND[hand][0], rot)), parent)


_extend_bind_with_fingers()


def fox_to_gf(v):
    """Fox coords (Y up, +Z forward) -> GF coords (Z up, faces -Y)."""
    return (v[0], -v[2], v[1])


def fox_to_gf_quat(q):
    """Same change of basis for a rotation (both frames right-handed)."""
    return (q[0], -q[2], q[1], q[3])


# ---------------------------------------------------------------------------
# The engine's native skeleton: GF node -> PES bone. The list order IS the
# joint-ID order, and the twenty body joints come first because a model
# converted before the fingers existed names its joints by number. Within
# each group the order is player.object DFS order, which is how the engine
# rebuilds the same numbering (jointorder.cpp).
GF_BODY_NODES = [
    # (GF node, PES bone, GF parent)
    ("body",           "motion",        None),
    ("hip",            "dsk_hip",       "body"),
    ("left_thigh",     "sk_thigh_l",    "hip"),
    ("left_knee",      "sk_leg_l",      "left_thigh"),
    ("left_ankle",     "sk_foot_l",     "left_knee"),
    ("right_thigh",    "sk_thigh_r",    "hip"),
    ("right_knee",     "sk_leg_r",      "right_thigh"),
    ("right_ankle",    "sk_foot_r",     "right_knee"),
    ("middle",         "sk_belly",      "body"),
    ("chest",          "sk_chest",      "middle"),
    ("neck",           "sk_neck",       "chest"),
    ("head",           "sk_head",       "neck"),
    ("left_clavicle",  "sk_shoulder_l", "chest"),
    ("left_shoulder",  "sk_upperarm_l", "left_clavicle"),
    ("left_elbow",     "sk_forearm_l",  "left_shoulder"),
    ("left_hand",      "sk_hand_l",     "left_elbow"),
    ("right_clavicle", "sk_shoulder_r", "chest"),
    ("right_shoulder", "sk_upperarm_r", "right_clavicle"),
    ("right_elbow",    "sk_forearm_r",  "right_shoulder"),
    ("right_hand",     "sk_hand_r",     "right_elbow"),
]

# PES's hand rig, both hands. Names follow PES's own: mata is the metacarpal
# base, then mcp / pip / dip up the finger. The order is the hand rig's unit
# order, so a pose's channels arrive in the order they were authored in.
_FINGER_SEGMENTS = {
    "thumb": ("mata", "mcp", "pip"),        # PES gives the thumb three bones
    "index": ("mata", "mcp", "pip", "dip"),
    "middle": ("mata", "mcp", "pip", "dip"),
    "pinky": ("mata", "mcp", "pip", "dip"),
    "ring": ("mata", "mcp", "pip", "dip"),
}
_FINGER_ORDER = ("thumb", "index", "middle", "pinky", "ring")


def _finger_nodes():
    out = []
    for side, suffix in (("left", "_l"), ("right", "_r")):
        for finger in _FINGER_ORDER:
            segments = _FINGER_SEGMENTS[finger]
            for i, segment in enumerate(segments):
                node = "%s_%s_%s" % (side, finger, segment)
                bone = "skh_%s_%s%s" % (finger, segment, suffix)
                parent = ("%s_hand" % side if i == 0 else
                          "%s_%s_%s" % (side, finger, segments[i - 1]))
                out.append((node, bone, parent))
    return out


GF_NODES = GF_BODY_NODES + _finger_nodes()

GF_JOINT_ORDER = [name for name, _, _ in GF_NODES]
JOINT_ID = {name: i for i, name in enumerate(GF_JOINT_ORDER)}
GF_PARENT = {name: parent for name, parent in
             ((name, parent) for name, _, parent in GF_NODES)}
PES_OF_GF = {name: bone for name, bone, _ in GF_NODES}
GF_OF_PES = {bone: name for name, bone, _ in GF_NODES}

# GF node -> alignment W in GF coords. Fingers inherit the hand's.
ALIGN_GF = {}
for _name, _bone, _ in GF_NODES:
    if _bone.startswith("skh_"):
        _w = PES_ALIGN["sk_hand_l" if _bone.endswith("_l") else "sk_hand_r"]
    else:
        _w = PES_ALIGN[_bone]
    ALIGN_GF[_name] = fox_to_gf_quat(_w)

# GF node -> local quat (GF coords) of the AUTHORING pose on this rig: the
# pose whose world rotations are W^-1 per bone, i.e. the pose that puts the
# skeleton back into the render bind meshes are modelled in. This is what
# base.anim.util carries (gen_player_object.py); the engine's
# authoring->bind bake in HumanoidBase::PrepareFullbodyModel does the rest.
BASE_POSE = {}
for _name in GF_JOINT_ORDER:
    _parent = GF_PARENT[_name]
    _wb = ALIGN_GF[_name]
    _wp = ALIGN_GF[_parent] if _parent else (0.0, 0.0, 0.0, 1.0)
    BASE_POSE[_name] = _q_mul(_wp, _q_conj(_wb))

# The vertex-colour weight encoding is jointID*10 + weight*9 and has to fit a
# byte, so the highest joint a weight of any size can name is (255 - 9) / 10 = 24.
# (The engine's decode, floor(channel / 10), reads 255 as joint 25 - but only at
# weight 0.56, so 25 is not a joint anything can be fully bound to.) The body rig
# fits inside that; the fingers start at 20 and run to 57, which is what the
# sidecar weight file (skinweights.hpp) exists for.
MAX_VERTEX_COLOUR_JOINT = 24


def colour_fallback_joint(joint_id):
    """The joint a vertex colour names instead of `joint_id`.

    An .ase's colours are the fallback for an engine or a model without the
    sidecar, and they belong to the body rig: anything outside its twenty joints
    collapses onto the animated body joint it hangs off. For a finger that is
    the wrist - exactly the flat splayed hand this engine drew before the
    fingers were rigged.

    Five finger joints (20-24) would fit the byte, and collapsing them anyway is
    the point: a fallback that drove two knuckles from a colour and left the
    other thirty-three at the wrist would be a hand half in each pose.
    """
    name = GF_JOINT_ORDER[joint_id]
    while JOINT_ID[name] >= len(GF_BODY_NODES):
        parent = GF_PARENT[name]
        if parent is None:
            return 0
        name = parent
    return JOINT_ID[name]


def _build_gf_bind(source):
    """GF node -> (local offset from parent, parent), GF coords."""
    world = {bone: fox_to_gf(pos) for bone, (pos, _) in source.items()}
    bind = {}
    for name, bone, parent in GF_NODES:
        w = world[bone]
        if parent is None:
            bind[name] = (w, None)
        else:
            p = world[PES_OF_GF[parent]]
            bind[name] = ((w[0] - p[0], w[1] - p[1], w[2] - p[2]), parent)
    return bind


# GF node -> (offset from parent node, parent node). This is exactly what
# data/media/objects/players/player.object encodes (see
# gen_player_object.py, which writes that file from this table).
GF_BIND = _build_gf_bind(PES_BIND)

# The body node's height above the ground in the bind pose (the .anim
# player-line origin): PES's mover, sk_root_hip.
GF_BODY_HEIGHT = PES_BIND["motion"][0][1]


def _world_of(bind):
    out = {}
    for name in GF_JOINT_ORDER:
        offset, parent = bind[name]
        if parent is None:
            out[name] = offset
        else:
            p = out[parent]
            out[name] = (p[0] + offset[0], p[1] + offset[1], p[2] + offset[2])
    return out


def gf_world_bind():
    """GF node -> world bind position (GF coords) on the rig (anim pose)."""
    return _world_of(GF_BIND)


def gf_world_render_bind():
    """GF node -> world position (GF coords) of the RENDER pose - the pose
    meshes, weight sidecars and vertex colours are authored in. Offline
    skinning (skin_probe/pose_render) uses this as the base pose, exactly as
    the engine uses base.anim.util."""
    return _world_of(_build_gf_bind(PES_RENDER_BIND))


# ---------------------------------------------------------------------------
# Helper-bone collapse: every PES bone that is not independently animated
# resolves to the animated GF node it rigidly follows. Curated by anatomy
# from the body.skl bone list; resolve_bone() falls back on name patterns
# and, given bind positions, on proximity.
HELPER_TO_GF = {
    # pelvis / trunk
    "dsk_back": "middle", "dsk_belly_scale": "middle",
    "dsk_hip_l": "hip", "dsk_hip_r": "hip",
    "dsk_pants_l": "hip", "dsk_pants_r": "hip",
    "dsk_pos_pants_l": "hip", "dsk_pos_pants_r": "hip",
    "dsk_sternum_l": "chest", "dsk_sternum_r": "chest",
    "dsk_pectoralis_l": "chest", "dsk_pectoralis_r": "chest",
    "dsk_pos_pectoralis_l": "chest", "dsk_pos_pectoralis_r": "chest",
    "dsk_scapula_l": "chest", "dsk_scapula_r": "chest",
    "dsk_pos_scapula_l": "chest", "dsk_pos_scapula_r": "chest",
    # clavicle skin rides the animated clavicle bone; collar skin the chest
    "dsk_clavicle_l": "left_clavicle", "dsk_clavicle_r": "right_clavicle",
    "dsk_trapezius_l": "left_clavicle", "dsk_trapezius_r": "right_clavicle",
    "dsk_collar_l": "chest", "dsk_collar_r": "chest",
    # neck / head
    "dsk_scm": "neck", "dsk_neckback": "neck",
    # arms (deltoid/underarm/sleeve cluster sit on the upper arm)
    "dsk_deltoid_l": "left_shoulder", "dsk_deltoid_r": "right_shoulder",
    "dsk_underarm_l": "left_shoulder", "dsk_underarm_r": "right_shoulder",
    "dsk_underarm_ba_l": "left_shoulder", "dsk_underarm_ba_r": "right_shoulder",
    "dsk_elbow_l": "left_elbow", "dsk_elbow_r": "right_elbow",
    "dsk_forearm_t_l": "left_elbow", "dsk_forearm_t_r": "right_elbow",
    # wrist-twist carrier: statically parented to the forearm (candy-wrapper
    # twist distribution is a Fox constraint GF does not emulate)
    "dsk_forearm_l": "left_elbow", "dsk_forearm_r": "right_elbow",
    "dsk_wrist_l": "left_hand", "dsk_wrist_r": "right_hand",
    # legs
    "dsk_thighmain_l": "left_thigh", "dsk_thighmain_r": "right_thigh",
    "dsk_thigh_l": "left_thigh", "dsk_thigh_r": "right_thigh",
    "dsk_pos_thigh_l": "left_thigh", "dsk_pos_thigh_r": "right_thigh",
    "dsk_knee_l": "left_knee", "dsk_knee_r": "right_knee",
    "dsk_kneeback_l": "left_knee", "dsk_kneeback_r": "right_knee",
    "dsk_leg_l": "left_knee", "dsk_leg_r": "right_knee",
    "dsk_foot_l": "left_ankle", "dsk_foot_r": "right_ankle",
    "dsk_toe_l": "left_ankle", "dsk_toe_r": "right_ankle",
    "sk_toe_l": "left_ankle", "sk_toe_r": "right_ankle",
}
# name-pattern families (prefix match), checked after the exact table
_HELPER_FAMILIES = [
    ("dsk_hem_", {"_l": "left_thigh", "_r": "right_thigh"}),
    ("dsk_pos_hem_", {"_l": "left_thigh", "_r": "right_thigh"}),
    ("dsk_belly_", {"_l": "middle", "_r": "middle"}),
    ("dsk_pos_belly_", {"_l": "middle", "_r": "middle"}),
    ("dsk_sleeve_", {"_l": "left_shoulder", "_r": "right_shoulder"}),
    ("tip_dsk_sleeve_", {"_l": "left_shoulder", "_r": "right_shoulder"}),
    ("tip_dsk_pos_sleeve_", {"_l": "left_shoulder", "_r": "right_shoulder"}),
    ("pos_arm_target_", {"_l": "left_shoulder", "_r": "right_shoulder"}),
    ("dsk_upperarm_", {"_l": "left_shoulder", "_r": "right_shoulder"}),
    ("dsk_ear", {"_l": "head", "_r": "head"}),
    ("tip_dsk_toe", {"_l": "left_ankle", "_r": "right_ankle"}),
    # Face muscles collapse onto the head - the FaceRig deforms those
    # instead of rotating them. Fingers do NOT: every skh_* bone of PES's
    # hand rig is a GF joint of its own (GF_NODES above), and reaches this
    # table only if PES ships a name the rig does not have - a glove or
    # cloth variant - in which case the wrist is where it belongs.
    ("skh_", {"_l": "left_hand", "_r": "right_hand"}),
    ("skf_", {"_l": "head", "_r": "head", "": "head"}),
]


def resolve_bone(bone_name, bind_positions=None):
    """PES bone name -> GF node name (None if unresolvable).

    bind_positions: optional {bone: (x,y,z) Fox coords} (e.g. an fmdl bone
    table, which is the RENDER bind) for a nearest-animated-bone fallback on
    unknown names.
    """
    if bone_name in GF_OF_PES:
        return GF_OF_PES[bone_name]
    if bone_name in HELPER_TO_GF:
        return HELPER_TO_GF[bone_name]
    for prefix, sides in _HELPER_FAMILIES:
        if bone_name.startswith(prefix):
            if bone_name.endswith("_l") or "_l_" in bone_name[len(prefix):]:
                return sides.get("_l")
            if bone_name.endswith("_r") or "_r_" in bone_name[len(prefix):]:
                return sides.get("_r")
            for key in ("", "_l"):
                if key in sides:
                    return sides[key]
    # dsk_x -> sk_x (twist helper sharing its master's name)
    if bone_name.startswith("dsk_") and ("sk_" + bone_name[4:]) in GF_OF_PES:
        return GF_OF_PES["sk_" + bone_name[4:]]
    if bind_positions is not None and bone_name in bind_positions:
        p = bind_positions[bone_name]
        best, best_d = None, None
        for gf_name, bone, _ in GF_NODES:
            q = PES_RENDER_BIND[bone][0]
            d = sum((a - b) ** 2 for a, b in zip(p, q))
            if best_d is None or d < best_d:
                best, best_d = gf_name, d
        return best
    return None
