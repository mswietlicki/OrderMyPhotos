"""Album profiles (S11, decisions.md P35): what each album and thematic folder is for, so that placing a
photo can compare it with a description, not only with a look.

A dated album's profile is its name, its days, its places and people, and the captions summary S7 wrote
for it ("v2 summary"). A thematic folder's profile is one sentence the local LLM writes from what its photos
show: their captions, or for folders without captions (a sample's example copies) a one-line description of
a few of its photos. Folder profiles are cached in `album_context` (source "profile@1").
"""

import json
import random

from .vlm import VLM, image_part

VERSION = "profile@1"
LINE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["line"],
               "properties": {"line": {"type": "string"}}}
LINE = "In one short line (at most 12 words), what does this photo show?"
PROFILE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["en", "pl"],
                  "properties": {"en": {"type": "string"}, "pl": {"type": "string"}}}
PROFILE = """The owner of a photo library keeps these photos in the folder "{name}" ({path}). What they show:
{lines}

In one sentence, say what this folder collects, specific enough to tell whether another photo belongs in it
(for example "Photos of one particular black cat, the owner's pet", "Receipts and invoices", "Climbing and
bouldering, indoors and on rock"). Give it in English (en) and Polish (pl)."""
# Folders the sorting may use although the library doesn't have them yet, with their profile.
DEFAULT_FOLDERS = {
    "Pictures/Other/Cats": "Cats (koty) that aren't the owner's own pet: any other cat, met outside, friends' cats,"
                           " cats for adoption, cat pictures. Use this for cats; no other cat folders.",
    "Pictures/Other/Random": "Photos that fit nowhere else.",
}


def folder_profiles(cfg, con, vlm: VLM, folders: dict[str, list[str]], sample: int = 8) -> dict[str, str]:
    """{folder: one-sentence description} for thematic folders, given each folder's photo ids in the DB."""
    have = {r[0]: json.loads(r[1]) for r in con.execute("SELECT album, text FROM album_context WHERE source=?", (VERSION,))}
    caps = {r[0]: json.loads(r[1]) for r in con.execute("SELECT id, data FROM caption")}
    out = {f: d for f, d in DEFAULT_FOLDERS.items()}
    for folder, ids in sorted(folders.items()):
        if folder in have:
            out[folder] = have[folder]["en"]
            continue
        lines = [caps[i].get("description_en") or caps[i].get("title_en") for i in ids if i in caps][:20]
        if len(lines) < 5:  # not captioned (a sample's examples): a line for a few of its photos
            for i in random.Random(folder).sample(ids, min(sample, len(ids))):
                try:
                    lines.append(vlm.json([image_part(cfg.preview(i)), {"type": "text", "text": LINE}], LINE_SCHEMA,
                                          max_tokens=60)["line"])
                except (ValueError, KeyError, FileNotFoundError):
                    continue
        if not lines:
            continue
        name = folder.split("/")[-1]
        d = vlm.json([{"type": "text", "text": PROFILE.format(name=name, path=folder,
                                                              lines="\n".join(f"- {x}" for x in lines))}],
                     PROFILE_SCHEMA, max_tokens=200)
        con.execute("INSERT OR REPLACE INTO album_context(album, source, trusted, text) VALUES (?,?,0,?)",
                    (folder, VERSION, json.dumps(d, ensure_ascii=False)))
        con.commit()
        out[folder] = d["en"]
    return out


def album_summary(con) -> dict[str, str]:
    """{album: the English captions summary S7 wrote for it}, for dated albums that have one."""
    out = {}
    for album, text in con.execute("SELECT album, text FROM album_context WHERE source LIKE 'v2 summary%'"):
        try:
            out[album] = json.loads(text).get("en", "")
        except (ValueError, AttributeError):
            continue
    return out
