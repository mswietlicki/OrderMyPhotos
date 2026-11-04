"""The People page (P40): every face cluster and every animal of the run, named by the user.

`people.html` sits next to the review's `index.html` in the bundle. Each card is one person (a face cluster)
or one animal, with its faces cut from the previews (`f/`). The user gives a full name and a nickname, marks
themselves ("This is me") or a card that isn't a person, and clicks the faces that aren't them to take them
out. Cards of one person (the clustering splits people) are merged by selecting them, or by giving them one
full name; a merge without a name yet is kept as a group ("@P1"). "I don't know them" keeps a stranger as one
anonymous person ("?P3"), never named or suggested again; for animals, "Not an animal of ours" does the same. Names are kept in the browser and leave with the
index page's "Export all decisions" (or this page's own export); `omp labels --file` stores them per face
(`name_label`), so they hold when the faces are clustered again. Album names use the nickname, else the first
name; captions use the names in titles ("Jan z Filemonem").
"""

import hashlib
import json
from collections import Counter
from pathlib import Path

from PIL import Image, ImageOps

from .config import Config

CROP = 160          # px, the square face or animal crop
SHOWN = 12          # crops a card shows before "show all"


def crop(cfg: Config, id_: str, box: list, out: Path, margin: float) -> str:
    """A square crop around a normalized box, from the preview, once; returns its path in the bundle."""
    name = hashlib.sha1(f"{id_}:{box}".encode()).hexdigest()[:16] + ".jpg"
    dst = out / "f" / name
    if not dst.exists():
        img = Image.open(cfg.preview(id_)).convert("RGB")
        x0, y0, x1, y1 = box[0] * img.width, box[1] * img.height, box[2] * img.width, box[3] * img.height
        side = max(x1 - x0, y1 - y0) * (1 + 2 * margin)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        sq = (int(cx - side / 2), int(cy - side / 2), int(cx + side / 2), int(cy + side / 2))
        ImageOps.fit(img.crop(sq), (CROP, CROP), Image.Resampling.LANCZOS).save(dst, quality=85)
    return f"f/{name}"


def write_page(cfg: Config, con, run_id: str, out: Path | None = None) -> str:
    bundle = Path(out) if out else cfg.bundle
    (bundle / "f").mkdir(parents=True, exist_ok=True)
    try:
        said = {r[0]: r[1] for r in con.execute("SELECT key, who FROM name_label")}
    except Exception:  # noqa: BLE001 - nothing named yet
        said = {}

    def label(keys):  # who the user said most of these are, "" if nobody
        votes = Counter(said[k] for k in keys if said.get(k))
        return votes.most_common(1)[0][0] if votes else ""

    people = []
    for p in con.execute("SELECT pid, name, short, source, owner, faces, photos FROM person WHERE run=?"
                         " ORDER BY faces DESC", (run_id,)).fetchall():
        fs = con.execute("SELECT id, box, size, yaw FROM face WHERE person=?", (p["pid"],)).fetchall()
        fs = sorted(fs, key=lambda f: -f["size"] * (1 - min(abs(f["yaw"] or 0), 90) / 90))  # big and frontal first
        people.append({
            "id": f"P{p['pid']}", "name": p["name"], "short": p["short"], "source": p["source"], "owner": p["owner"],
            "photos": p["photos"], "label": label([f"face:{f['id']}:{f['box']}" for f in fs]),
            "faces": [{"k": f"face:{f['id']}:{f['box']}", "src": crop(cfg, f["id"], json.loads(f["box"]), bundle, 0.35)}
                      for f in fs]})
    pets = []
    try:
        for p in con.execute("SELECT pid, name, short, source, kind, photos FROM pet WHERE run=? ORDER BY boxes DESC",
                             (run_id,)).fetchall():
            ms = con.execute("SELECT id, box, size FROM pet_member WHERE pid=? ORDER BY size DESC", (p["pid"],)).fetchall()
            pets.append({
                "id": f"A{p['pid']}", "name": p["name"], "short": p["short"], "source": p["source"], "kind": p["kind"],
                "photos": p["photos"], "label": label([f"pet:{m['id']}:{m['box']}" for m in ms]),
                "faces": [{"k": f"pet:{m['id']}:{m['box']}", "src": crop(cfg, m["id"], json.loads(m["box"]), bundle, 0.08)}
                          for m in ms]})
    except Exception:  # noqa: BLE001 - no pets yet
        pass
    unclustered = con.execute("SELECT COUNT(*) FROM face WHERE person IS NULL AND emb IS NOT NULL").fetchone()[0]
    data = {"run": run_id, "people": people, "pets": pets, "unclustered": unclustered, "shown": SHOWN}
    page = PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (bundle / "people.html").write_text(page, encoding="utf-8")
    return f"{len(people)} people and {len(pets)} animals -> {bundle}/people.html"


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OrderMyPhotos people</title>
<style>
:root { --bg: #f6f6f4; --panel: #fff; --ink: #1d1d1b; --muted: #6b6b66; --line: #deded8; --keep: #1a7a42; --accent: #2f5fd0;
  --chip: #efefea; --on-fill: #fff; --warn: #b4531a; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg: #141413; --panel: #1e1e1c; --ink: #ecece8;
  --muted: #a3a39c; --line: #34342f; --keep: #3fbf74; --accent: #7aa2ff; --chip: #2a2a27; --on-fill: #121211; --warn: #e8935a;
  color-scheme: dark; } }
:root[data-theme="dark"] { --bg: #141413; --panel: #1e1e1c; --ink: #ecece8; --muted: #a3a39c; --line: #34342f; --keep: #3fbf74;
  --accent: #7aa2ff; --chip: #2a2a27; --on-fill: #121211; --warn: #e8935a; color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1400px; margin: 0 auto; padding: 16px; }
header { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin: 4px 0 6px; }
h1 { font-size: 18px; margin: 0; } h1 small { color: var(--muted); font-weight: 400; }
h2 { font-size: 15px; margin: 26px 0 10px; } h2 small { color: var(--muted); font-weight: 400; }
a { color: var(--accent); }
.sp { flex: 1; }
.intro { color: var(--muted); max-width: 900px; margin: 0 0 12px; }
button { font: inherit; color: inherit; background: var(--chip); border: 1px solid var(--line); border-radius: 6px;
  padding: 3px 10px; cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--on-fill); }
label.tg { display: inline-flex; gap: 6px; align-items: center; color: var(--muted); cursor: pointer; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px; display: flex;
  flex-direction: column; gap: 8px; }
.card.done { border-color: var(--keep); }
.card.ignored { opacity: .55; }
.card.unknown { border-style: dashed; }
.top { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.id { font: 600 12px ui-monospace, monospace; background: var(--chip); border-radius: 4px; padding: 1px 6px; }
.meta { color: var(--muted); font-size: 12px; }
.hint { color: var(--muted); font-size: 12px; }
.hint button { font-size: 12px; padding: 0 6px; }
.same { color: var(--accent); font-size: 12px; }
.faces { display: grid; grid-template-columns: repeat(6, 1fr); gap: 4px; }
.faces button { padding: 0; border: 0; border-radius: 4px; overflow: hidden; position: relative; aspect-ratio: 1; background: #000; }
.faces img { width: 100%; height: 100%; object-fit: cover; display: block; }
.faces button.out img { opacity: .25; }
.faces button.out::after { content: "✕"; position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 22px; text-shadow: 0 0 3px #000; }
.more { align-self: flex-start; font-size: 12px; }
.fields { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.fields label { display: flex; flex-direction: column; gap: 2px; font-size: 12px; color: var(--muted); }
input[type=text] { font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
  padding: 5px 8px; width: 100%; }
input[type=text]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.flags { display: flex; gap: 14px; flex-wrap: wrap; }
.empty { color: var(--muted); }
details { margin-top: 14px; } summary { cursor: pointer; color: var(--muted); margin-bottom: 10px; }
.card.sel { outline: 2px solid var(--accent); }
.top .pick { margin: 0 2px 0 0; }
.merged { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; font-size: 12px; color: var(--muted); }
.merged button { font-size: 12px; padding: 0 6px; }
.mergebox { display: flex; gap: 6px; align-items: center; }
.mergebox input { width: 220px; }
.bar { position: sticky; bottom: 12px; margin: 12px auto 0; width: max-content; max-width: 100%; display: flex; gap: 10px;
  align-items: center; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, .18); }
.bar[hidden] { display: none; }
@media (max-width: 420px) { .faces { grid-template-columns: repeat(4, 1fr); } .fields { grid-template-columns: 1fr; } }
</style></head><body><main>
<header><h1>People <small id="sub"></small></h1><span class="sp"></span>
<a id="home" href="index.html">← Albums</a>
<span class="mergebox"><input type="text" id="mergeids" placeholder="Merge: P1 P7 P8 … or A1 A4" autocomplete="off">
<button id="mergego">Merge</button></span>
<label class="tg"><input type="checkbox" id="todo"> unnamed only</label>
<button class="primary" id="exp" title="the names also leave with the albums page's Export all decisions">Export names</button></header>
<p class="intro">Give each person a full name and a nickname: album names use the nickname, or the first name, and photo
titles use the names ("Jan z Filemonem"). One person split into several cards: tick them and press Merge (or type their Ids
above); the same full name on two cards merges them too. Click a face that isn't them to take it out. Mark yourself with
"This is me": you're never named in album names. "I don't know them" keeps a stranger out of the names; they still count
as someone else when photos are sorted.</p>
<datalist id="known"></datalist>
<div class="grid" id="people"></div>
<h2>Animals <small id="petsub"></small></h2>
<div class="grid" id="pets"></div>
<details id="oncebox" hidden><summary id="oncesum"></summary><p class="intro">Once you name the pets above, the animals
here that match them join them on the next run; name one here only if it's a pet seen in a single photo.</p>
<div class="grid" id="once"></div></details>
<div class="bar" id="bar" hidden><span id="barsum"></span><button class="primary" id="merge">Merge into one</button>
<button id="clear">Clear</button></div>
<script>
const DATA = __DATA__;
const KEY = "omp-people-" + DATA.run;
// Opened from the web app (P47), the state lives on the server: loaded on opening, saved on every change, the
// same from any device. Opened as a file from a bundle, it stays in the browser and the names are exported.
const inApp = location.pathname.startsWith("/library/");
if (inApp) document.getElementById("home").href = "/review";
else if (location.protocol !== "file:") document.getElementById("home").href = "./";
let state = {};
if (!inApp) try { state = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
const cards = [...DATA.people.map(c => ({...c, pet: false})), ...DATA.pets.map(c => ({...c, pet: true}))];
const byId = Object.fromEntries(cards.map(c => [c.id, c]));
const topKey = c => c.faces.length ? c.faces[0].k : c.id;  // a card's state belongs to its cluster, not to its number
let dbLabels = {};  // per face, who the DB says it is ("" = taken out), for a page with no saved state yet
function seed() { for (const c of cards) {
  if (state[c.id] && state[c.id].top !== topKey(c)) delete state[c.id];  // clustered differently since: start from the DB
  if (!state[c.id] && c.source === "user")  // what the DB knows from an earlier import
    state[c.id] = {top: topKey(c), name: c.name || "", nick: c.short || "", me: !!c.owner, ignore: false, out: [],
                   unknown: (c.label || "").startsWith("?"),
                   group: (c.label || "").startsWith("@") && c.label !== "@me" ? c.label : "",
                   out: c.faces.filter(f => dbLabels[f.k] === "").map(f => f.k)};
} }
const st = c => state[c.id] || (state[c.id] = {top: topKey(c), name: "", nick: "", me: false, ignore: false, out: []});
const who = s => (s.name || "").trim() || (s.nick || "").trim();
const root = c => { let r = c; for (let n = 0; n < 99 && st(r).into && byId[st(r).into] && byId[st(r).into] !== r; n++) r = byId[st(r).into]; return r; };
const members = r => cards.filter(c => root(c) === r);
const facesOf = r => members(r).flatMap(c => c.faces);
const photos = fs => new Set(fs.map(f => f.k.split(":")[1])).size;
const esc = s => String(s).replace(/[&<>"]/g, ch => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch]));
const P = document.getElementById("people"), A = document.getElementById("pets"), O = document.getElementById("once");
const selected = new Set();

function names() {  // per face (or animal box): who it is, for the import; "" = not them / not a person
  const out = {};
  for (const c of cards) {
    const r = root(c), s = st(r);
    // "@me": the owner without a name yet; "?P3": someone the user doesn't know (an animal not theirs), kept as
    // one anonymous person; "@P1": cards merged but not named yet, kept together
    const w = who(s) || (s.me && !r.pet ? "@me" : "") || (s.unknown ? "?" + r.id : "") || s.group ||
      (members(r).length > 1 ? "@" + r.id : "");
    for (const f of c.faces) {
      if (s.ignore || (s.out || []).includes(f.k)) out[f.k] = {who: ""};
      else if (w) out[f.k] = {who: w, name: (s.name || "").trim(), nick: (s.nick || "").trim(), owner: !!s.me && !r.pet};
    }
  }
  return out;
}
let saveTimer = null;
function save() {
  status();
  if (!inApp) {
    try { localStorage.setItem(KEY, JSON.stringify(state)); localStorage.setItem(KEY + "::names", JSON.stringify(names())); } catch (e) {}
    return;
  }
  const btn = document.getElementById("exp");
  btn.textContent = "Saving…";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {  // a short pause, so typing a name isn't a save per letter
    try {
      const r = await fetch("/api/people", {method: "PUT", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify({state, names: names(), keys: cards.flatMap(c => c.faces.map(f => f.k))})});
      btn.textContent = r.ok ? "Saved" : "Not saved: " + r.status;
    } catch (e) { btn.textContent = "Not saved: " + e.message; }
  }, 600);
}
function status() {
  const roots = cards.filter(c => root(c) === c);
  const named = roots.filter(c => !c.pet && who(st(c))).length, pets = roots.filter(c => c.pet && who(st(c))).length;
  const unknown = roots.filter(c => !c.pet && !who(st(c)) && st(c).unknown).length;
  document.getElementById("sub").textContent = `run ${DATA.run} · ${roots.filter(c => !c.pet).length} people, ${named} named` +
    (unknown ? `, ${unknown} not known` : "") +
    (DATA.unclustered ? ` · ${DATA.unclustered} more faces (small, unsure, or seen only once or twice) aren't shown` : "");
  document.getElementById("petsub").textContent = DATA.pets.length ? `${roots.filter(c => c.pet).length} found, ${pets} named` : "none found";
  const seen = {};
  for (const c of roots) { const w = who(st(c)).toLowerCase(); if (w) (seen[w] = seen[w] || []).push(c.id); }
  document.getElementById("known").innerHTML = [...new Set(roots.map(c => (st(c).name || "").trim()).filter(Boolean))]
    .map(n => `<option value="${esc(n)}">`).join("");
  const todo = document.getElementById("todo").checked;
  for (const c of roots) {
    const el = document.getElementById("c-" + c.id), s = st(c), w = who(s).toLowerCase();
    if (!el) continue;
    el.classList.toggle("done", !!w && !s.ignore);
    el.classList.toggle("ignored", !!s.ignore);
    el.classList.toggle("unknown", !w && !!s.unknown && !s.ignore);
    el.classList.toggle("sel", selected.has(c.id));
    const others = w ? seen[w].filter(i => i !== c.id) : [];
    el.querySelector(".same").textContent = others.length ? `the same person as ${others.join(", ")}` : "";
    el.hidden = todo && (!!w || !!s.ignore || !!s.unknown);
  }
  const n = selected.size, kinds = new Set([...selected].map(i => byId[i].pet));
  document.getElementById("bar").hidden = !n;
  document.getElementById("barsum").textContent = kinds.size > 1 ? "people and animals can't be merged together" :
    `${n} selected: ${[...selected].join(", ")}`;
  document.getElementById("merge").disabled = n < 2 || kinds.size > 1;
}
function merge(list) {
  const rs = [...new Set(list.map(root))];
  if (rs.length < 2 || new Set(rs.map(r => r.pet)).size > 1) return;
  const into = rs.reduce((a, b) => facesOf(b).length > facesOf(a).length ? b : a), t = st(into);
  for (const r of rs) {
    if (r === into) continue;
    const s = st(r);
    for (const f of ["name", "nick"]) if (!(t[f] || "").trim() && (s[f] || "").trim()) t[f] = s[f];
    t.me = t.me || s.me;
    t.unknown = t.unknown && s.unknown;  // one known card makes the merged person known
    t.group = t.group || s.group;
    t.out = [...new Set([...(t.out || []), ...(s.out || [])])];
    s.into = into.id;
  }
  selected.clear();
  save(); render();
}
function card(c) {
  const s = st(c), el = document.createElement("div"), fs = facesOf(c), parts = members(c).filter(m => m !== c);
  el.className = "card"; el.id = "c-" + c.id;
  const sug = parts.concat([c]).find(m => m.name && m.source !== "user") || {}, owner = parts.concat([c]).some(m => m.owner);
  const hint = !who(s) && s.unknown ? "" : !who(s) && sug.name ? `<span class="hint">suggested: ${esc(sug.name)} (${sug.source === "album-names" ? "from album names"
    : sug.source === "people-folder" ? "from a People folder" : esc(sug.source || "")}) <button data-use>use</button></span>` :
    !who(s) && !s.me && owner ? `<span class="hint">probably you (from the front camera) <button data-me>yes, me</button></span>` : "";
  el.innerHTML = `<div class="top"><input type="checkbox" class="pick" title="select to merge"${selected.has(c.id) ? " checked" : ""}>
    <span class="id">${c.id}</span><span class="meta">${fs.length} ${c.pet ? esc(c.kind || "animal") + (fs.length > 1 ? "s" : "") :
    "faces"} in ${photos(fs)} photos</span><span class="same"></span></div>${hint}
    ${parts.length ? `<div class="merged">merged: ${parts.map(m => `<button data-un="${m.id}" title="take ${m.id} out again">${m.id} ✕</button>`).join("")}</div>` : ""}
    <div class="faces"></div>${fs.length > DATA.shown ? `<button class="more">show all ${fs.length}</button>` : ""}
    <div class="fields"><label>${c.pet ? "Name" : "Full name"}<input type="text" data-f="name" list="known" autocomplete="off"
      placeholder="${c.pet ? "Filemon" : "Anna Kowalska"}"></label>
    <label>Nickname<input type="text" data-f="nick" autocomplete="off" placeholder="${c.pet ? "Filuś" : "Ania"}"></label></div>
    <div class="flags">${c.pet ? `<label class="tg"><input type="checkbox" data-f="unknown"> Not an animal of ours</label>` :
      `<label class="tg"><input type="checkbox" data-f="me"> This is me</label>
       <label class="tg"><input type="checkbox" data-f="unknown"> I don't know them</label>
       <label class="tg"><input type="checkbox" data-f="ignore"> Not a person</label>`}</div>`;
  const faces = el.querySelector(".faces");
  const draw = all => {
    faces.innerHTML = "";
    for (const f of all ? fs : fs.slice(0, DATA.shown)) {
      const b = document.createElement("button");
      b.title = "click if this isn't them"; b.className = (s.out || []).includes(f.k) ? "out" : "";
      b.innerHTML = `<img src="${f.src}" alt="" loading="lazy">`;
      b.onclick = () => { s.out = (s.out || []).includes(f.k) ? s.out.filter(k => k !== f.k) : [...(s.out || []), f.k]; b.classList.toggle("out"); save(); };
      faces.append(b);
    }
  };
  draw(false);
  const more = el.querySelector(".more");
  if (more) more.onclick = () => { const all = more.dataset.all !== "1"; draw(all); more.dataset.all = all ? "1" : "";
    more.textContent = all ? "show fewer" : `show all ${fs.length}`; };
  el.querySelector(".pick").onchange = e => { e.target.checked ? selected.add(c.id) : selected.delete(c.id); status(); };
  for (const b of el.querySelectorAll("[data-un]")) b.onclick = () => { delete st(byId[b.dataset.un]).into; save(); render(); };
  for (const i of el.querySelectorAll("input[data-f]")) {
    const f = i.dataset.f;
    if (i.type === "checkbox") { i.checked = !!s[f]; i.onchange = () => {
      if (f === "me" && i.checked) for (const o of cards) if (o !== c && st(o).me) { st(o).me = false;
        const x = document.querySelector(`#c-${o.id} [data-f=me]`); if (x) x.checked = false; }
      for (const [a, b] of [["me", "unknown"], ["unknown", "me"], ["unknown", "ignore"], ["ignore", "unknown"]])
        if (f === a && i.checked && s[b]) { s[b] = false; const x = el.querySelector(`[data-f=${b}]`); if (x) x.checked = false; }
      s[f] = i.checked; save(); }; }
    else { i.value = s[f] || ""; i.oninput = () => { s[f] = i.value; save(); };
      i.onkeydown = e => { if (e.key === "Enter") { const all = [...document.querySelectorAll("input[data-f=name]")];
        const n = all[all.indexOf(el.querySelector("[data-f=name]")) + 1]; if (n) n.focus(); } }; }
  }
  const use = el.querySelector("[data-use]");
  if (use) use.onclick = () => { s.nick = sug.short || sug.name; el.querySelector("[data-f=nick]").value = s.nick; use.parentElement.remove(); save(); };
  const me = el.querySelector("[data-me]");
  if (me) me.onclick = () => { s.me = true; el.querySelector("[data-f=me]").checked = true; me.parentElement.remove(); save(); };
  return el;
}
function render() {
  P.innerHTML = A.innerHTML = O.innerHTML = "";
  const roots = cards.filter(c => root(c) === c);
  for (const c of roots) (!c.pet ? P : facesOf(c).length > 1 ? A : O).append(card(c));
  const once = roots.filter(c => c.pet && facesOf(c).length === 1).length;
  document.getElementById("oncebox").hidden = !once;
  document.getElementById("oncesum").textContent = `${once} more animals, each seen in one photo`;
  if (!DATA.people.length) P.innerHTML = `<p class="empty">No faces seen three times or more yet.</p>`;
  status();
}
document.getElementById("todo").onchange = status;
document.getElementById("merge").onclick = () => merge([...selected].map(i => byId[i]));
document.getElementById("clear").onclick = () => { selected.clear(); render(); };
const mergeIds = () => {
  const box = document.getElementById("mergeids");
  const ids = (box.value.match(/[PA]\\d+/gi) || []).map(i => i.toUpperCase()).filter(i => byId[i]);
  if (ids.length >= 2) { merge(ids.map(i => byId[i])); box.value = ""; }
};
document.getElementById("mergego").onclick = mergeIds;
document.getElementById("mergeids").onkeydown = e => { if (e.key === "Enter") mergeIds(); };
document.getElementById("exp").onclick = () => {
  if (inApp) return;  // in the app every change is saved already; the button only shows that
  const spec = {run: DATA.run, exported: new Date().toISOString(), names: names()};
  const blob = new Blob([JSON.stringify(spec, null, 1)], {type: "application/json"});
  const l = document.createElement("a"); l.href = URL.createObjectURL(blob); l.download = `names-${DATA.run}.json`; l.click();
};
(async () => {
  if (inApp) {
    const btn = document.getElementById("exp");
    btn.textContent = "Loading…"; btn.disabled = true;
    try {
      const r = await fetch("/api/people");
      if (r.ok) { const d = await r.json(); state = d.state || {}; dbLabels = d.labels || {}; }
      btn.textContent = "Saved";
    }
    catch (e) { btn.textContent = "Not loaded: " + e.message; }
  }
  seed();
  render();
  status();
  if (!inApp) save();
})();
</script></main></body></html>
"""
