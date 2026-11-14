import os
import re
import mutagen.id3 as id3
from flask import Flask, request, jsonify, send_file, send_from_directory, abort
from mutagen.id3 import ID3, ID3NoHeaderError, APIC, COMM, USLT, TXXX
from mutagen.mp3 import MP3
import io
import time
import threading
import requests

ROOT = os.path.realpath(os.environ.get("MUSIC_DIR", "/music"))
app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

TEXT = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
    "date": "TDRC", "track": "TRCK", "disc": "TPOS", "genre": "TCON", "composer": "TCOM",
    "grouping": "TIT1", "bpm": "TBPM", "publisher": "TPUB",
}


def safe(rel):
    p = os.path.realpath(os.path.join(ROOT, (rel or "").lstrip("/")))
    if p != ROOT and not p.startswith(ROOT + os.sep):
        abort(400, "Path outside music folder")
    return p


def load(p):
    try:
        return ID3(p)
    except ID3NoHeaderError:
        return ID3()


def relpath(p):
    return os.path.relpath(p, ROOT).replace(os.sep, "/") if p != ROOT else ""


def read_tags(p):
    t = load(p)
    d = {k: (str(t[f]) if f in t else "") for k, f in TEXT.items()}
    c = t.getall("COMM")
    d["comment"] = str(c[0].text[0]) if c and c[0].text else ""
    u = t.getall("USLT")
    d["lyrics"] = u[0].text if u else ""
    d["custom"] = {f.desc: "; ".join(f.text) for f in t.getall("TXXX")}
    d["has_cover"] = bool(t.getall("APIC"))
    frames = []
    for f in t.values():
        try:
            txt = f"{f.mime}, {len(f.data) // 1024} KB" if f.FrameID == "APIC" else str(f)[:120]
        except Exception:
            txt = "(binary)"
        frames.append({"id": f.FrameID, "text": txt})
    d["frames"] = frames
    try:
        i = MP3(p).info
        d["info"] = {"length": round(i.length), "bitrate": i.bitrate // 1000, "size": os.path.getsize(p)}
    except Exception:
        d["info"] = {}
    return d


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/list")
def list_dir():
    p = safe(request.args.get("path", ""))
    if not os.path.isdir(p):
        abort(404)
    dirs, files = [], []
    for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
        if e.name.startswith("."):
            continue
        if e.is_dir():
            dirs.append({"name": e.name, "path": relpath(e.path)})
        elif e.name.lower().endswith(".mp3"):
            try:
                t = read_tags(e.path)
            except Exception as ex:
                t = {"title": "", "artist": "", "album": "", "track": "", "has_cover": False, "error": str(ex)}
            files.append({"name": e.name, "path": relpath(e.path), **{k: t.get(k) for k in ("title", "artist", "album", "track", "has_cover")}})
    return jsonify(path=relpath(p), dirs=dirs, files=files)


@app.post("/api/tags/read")
def read_many():
    return jsonify([read_tags(safe(r)) for r in request.get_json()["paths"]])


@app.post("/api/tags")
def write_tags():
    j = request.get_json()
    fields, custom, paths = j.get("fields", {}), j.get("custom"), j["paths"]
    for i, rel in enumerate(paths):
        p = safe(rel)
        t = load(p)
        for k, v in fields.items():
            if k in TEXT:
                t.delall(TEXT[k])
                if v != "":
                    t.add(getattr(id3, TEXT[k])(encoding=3, text=[v]))
            elif k == "comment":
                t.delall("COMM")
                if v:
                    t.add(COMM(encoding=3, lang="eng", desc="", text=[v]))
            elif k == "lyrics":
                t.delall("USLT")
                if v:
                    t.add(USLT(encoding=3, lang="eng", desc="", text=v))
        if j.get("autonumber"):
            t.delall("TRCK")
            t.add(id3.TRCK(encoding=3, text=[str(i + 1)]))
        if custom is not None:
            t.delall("TXXX")
            for d, v in custom.items():
                if d and v:
                    t.add(TXXX(encoding=3, desc=d, text=[v]))
        t.save(p, v2_version=4)
    return jsonify(ok=True, count=len(paths))


@app.get("/api/cover")
def get_cover():
    t = load(safe(request.args["path"]))
    pics = t.getall("APIC")
    if not pics:
        abort(404)
    return send_file(io.BytesIO(pics[0].data), mimetype=pics[0].mime or "image/jpeg")


@app.post("/api/cover")
def set_cover():
    import json
    paths = json.loads(request.form["paths"])
    img = request.files.get("image")
    data = img.read() if img else None
    mime = None
    if data:
        if data[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        else:
            abort(400, "Cover must be a JPEG or PNG image")
    for rel in paths:
        p = safe(rel)
        t = load(p)
        t.delall("APIC")
        if data:
            t.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        t.save(p, v2_version=4)
    return jsonify(ok=True, count=len(paths))


BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean(s):
    return re.sub(r"\s+", " ", BAD.sub("_", s)).strip(" .")


def plan_name(p):
    t = load(p)

    def first(f):
        return ", ".join(t[f].text).strip() if f in t else ""

    title, artist = first("TIT2"), first("TPE1")
    track = first("TRCK").split("/")[0].strip()
    if not (title and artist and track):
        return None
    if track.isdigit():
        track = track.zfill(2)
    return clean(f"{track} - {artist} - {title}")[:200].rstrip(" .") + ".mp3"


@app.post("/api/rename")
def rename_files():
    j = request.get_json()
    dry = j.get("dry", False)
    out, taken = [], set()
    for rel in j["paths"]:
        p = safe(rel)
        name = plan_name(p)
        if not name:
            out.append({"old": rel, "new": rel, "status": "missing track, artist or title"})
            continue
        dst = os.path.join(os.path.dirname(p), name)
        if dst == p:
            status = "unchanged"
        elif os.path.exists(dst) or dst in taken:
            status = "name already exists"
        else:
            taken.add(dst)
            if not dry:
                os.rename(p, dst)
            status = "ok"
        out.append({"old": rel, "new": relpath(dst) if status == "ok" else rel, "status": status})
    return jsonify(out)


# ---------- MusicBrainz lookup ----------
MB = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": os.environ.get("MB_USER_AGENT", "MP3Tagger/1.0 (you@example.com)")}
_mb_lock, _mb_last = threading.Lock(), 0.0


def mb_get(path, **params):
    """GET from MusicBrainz, at most one request per ~1.1 s as their rules require."""
    global _mb_last
    with _mb_lock:
        wait = 1.1 - (time.time() - _mb_last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(f"{MB}/{path}", params={**params, "fmt": "json"}, headers=MB_HEADERS, timeout=10)
        finally:
            _mb_last = time.time()
    r.raise_for_status()
    return r.json()


def lq(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def credit(ac):
    return "".join(c["name"] + c.get("joinphrase", "") for c in ac)


def search_recordings(title, artist, album=""):
    q = f'recording:"{lq(title)}" AND artist:"{lq(artist)}"'
    if album:
        q += f' AND release:"{lq(album)}"'
    return mb_get("recording", query=q, limit=15).get("recordings", [])


@app.post("/api/lookup")
def lookup():
    j = request.get_json()
    title, artist, album = (j.get(k, "").strip() for k in ("title", "artist", "album"))
    if not (title and artist):
        abort(400, "Title and artist are required")
    recs = search_recordings(title, artist, album) if album else []
    if not any(r.get("score", 0) >= 85 for r in recs):
        recs = search_recordings(title, artist)  # album tag may not match MusicBrainz's spelling
    out = []
    for rec in recs:
        if rec.get("score", 0) < 85:
            continue
        for rel in rec.get("releases", []):
            rg = rel.get("release-group", {})
            media = (rel.get("media") or [{}])[0]
            trk = (media.get("track") or [{}])[0]
            num, total = trk.get("number", ""), rel.get("track-count") or media.get("track-count")
            out.append({
                "release_id": rel["id"],
                "title": rec["title"],
                "artist": credit(rec["artist-credit"]),
                "album": rel["title"],
                "albumartist": credit(rel.get("artist-credit", rec["artist-credit"])),
                "date": rel.get("date", ""),
                "track": f"{num}/{total}" if num.isdigit() and total else (num if num.isdigit() else ""),
                "disc": str(media.get("position", "")),
                "country": rel.get("country", ""),
                # official studio albums first, then earliest
                "_rank": (
                    rel.get("status") != "Official",
                    not (rg.get("primary-type") == "Album" and not rg.get("secondary-types")),
                    rel.get("date") or "9999",
                ),
            })
    out.sort(key=lambda r: r["_rank"])
    for r in out:
        del r["_rank"]
    return jsonify(out[: int(j.get("n", 10))])


@app.get("/api/lookup/release")
def lookup_release():
    def top(o):
        g = sorted(o.get("genres") or [], key=lambda g: -g.get("count", 0))
        return g[0]["name"].title() if g else ""

    d = mb_get(f"release/{request.args['id']}", inc="genres+release-groups+artist-credits")
    genre = top(d)
    # Genres usually live on the release group, then the artist, not the release itself
    try:
        if not genre and d.get("release-group"):
            genre = top(mb_get(f"release-group/{d['release-group']['id']}", inc="genres"))
        if not genre and d.get("artist-credit"):
            genre = top(mb_get(f"artist/{d['artist-credit'][0]['artist']['id']}", inc="genres"))
    except requests.RequestException:
        pass
    return jsonify(genre=genre)


@app.post("/api/tags/fill")
def fill_tags():
    """Write fields that are empty, or whose key is in `overwrite`. items: [{path, fields}]"""
    n = 0
    j = request.get_json()
    ow = set(j.get("overwrite", []))
    for it in j["items"]:
        p = safe(it["path"])
        t = load(p)
        changed = False
        for k, v in it["fields"].items():
            f = TEXT.get(k)
            if f and v and (k in ow or not any(str(x).strip() for x in t.getall(f))):
                t.delall(f)
                t.add(getattr(id3, f)(encoding=3, text=[v]))
                changed = True
        if changed:
            t.save(p, v2_version=4)
            n += 1
    return jsonify(ok=True, count=n)
