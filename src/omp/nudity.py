"""Nudity (P26, P53): what the NudeNet detector's parts mean, and the tag a photo gets.

A photo with nudity isn't moved anywhere: it goes wherever the sorting puts it, and the tag marks it. P26
sent full nudity to the album's " PRIV" sibling; the user changed that (2026-11-05): "Let's not move nude
photos to PRIV, just mark them with nudity or full-nudity tags."
"""

import json

NUDITY = "nudenet@3.4.2"
# Full nudity, as the user put it (2026-11-03): exposed genitals or anus, or a bare body (bare buttocks and
# bare breasts together). Nudity: bare breasts or buttocks alone. Shirtless, swimwear or underwear, a bare
# belly or armpits get no tag.
FULL = {"FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"}
BARE = ({"BUTTOCKS_EXPOSED"}, {"FEMALE_BREAST_EXPOSED"})
PARTIAL = {"BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED"}
NUDITY_SCORE = 0.5     # detector score for a part to count
TAGS = {"full": ("People/Full nudity", "Pełna nagość"), "partial": ("People/Nudity", "Nagość")}


def full_nudity(detections) -> bool:
    parts = {d["class"] for d in detections if d["score"] >= NUDITY_SCORE}
    return bool(parts & FULL) or all(parts & group for group in BARE)


def levels(con) -> dict[str, str]:
    """Photo id -> "full" or "partial", from the stored detections (the `nudity` signal)."""
    out = {}
    for r in con.execute("SELECT id, data FROM signal WHERE kind='nudity'"):
        d = json.loads(r[1] or "{}")
        if d.get("full"):
            out[r[0]] = "full"
        elif set(d.get("exposed") or ()) & PARTIAL:
            out[r[0]] = "partial"
    return out
