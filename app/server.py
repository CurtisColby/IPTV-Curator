#!/usr/bin/env python3
"""
IPTV Curator — consolidated app server (multi-profile)
======================================================
One FastAPI process. Supports multiple independent profiles, each with its own
playlist, EPG, matches, and backups, served at profile-scoped URLs:

    /<profile>/playlist.m3u
    /<profile>/epg.xml

"main" is the default profile. The bare /playlist.m3u and /epg.xml still work as
aliases to main, so anything already pointed at them keeps working.

Each profile lives in data/<profile>/. The nightly rebuild does every profile.
Guide sources are downloaded ONCE per rebuild and reused across profiles.
"""

import asyncio
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import epg_engine as engine

# ── paths / config ───────────────────────────────────────────────────────────
DATA_DIR      = os.environ.get("IPTV_DATA_DIR", "/data")
APP_DIR       = os.path.dirname(os.path.abspath(__file__))
HTML_FILE     = os.path.join(APP_DIR, "iptv-checker.html")
PROFILES_FILE = os.path.join(DATA_DIR, "profiles.json")
SOURCES_FILE  = os.path.join(DATA_DIR, "playlist-sources.json")   # editable dropdown (shared)

GUIDE_SOURCES_FILE = os.path.join(DATA_DIR, "guide-sources.json")
# FAST guides only list a few hours ahead, so guides are re-downloaded and every
# profile rebuilt on this interval (default every 3 hours).
REFRESH_HOURS = max(1, int(os.environ.get("IPTV_GUIDE_REFRESH_HOURS", "3")))

os.makedirs(DATA_DIR, exist_ok=True)

_run_lock = asyncio.Lock()
app = FastAPI(title="IPTV Curator", docs_url="/api/docs")
scheduler = AsyncIOScheduler(timezone="UTC")


# ── profile helpers ──────────────────────────────────────────────────────────
def slugify(name):
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "profile"


def profile_dir(slug):
    return os.path.join(DATA_DIR, slug)


def profile_paths(slug):
    d = profile_dir(slug)
    return {
        "dir": d,
        "playlist": os.path.join(d, "playlist.m3u"),
        "epg": os.path.join(d, "epg.xml"),
        "channels_json": os.path.join(d, "epgtalk-channels.json"),
        "aliases": os.path.join(d, "match-aliases.json"),
        "last_run": os.path.join(d, "last-run.json"),
        "mylist": os.path.join(d, "mylist.json"),          # My List lives on the server now
        "guide_map": os.path.join(d, "guide-map.json"),    # permanent id -> guide choice
        "backups": os.path.join(d, "backups"),
    }


def _save_profiles(profs):
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(profs, f, ensure_ascii=False, indent=2)


def load_profiles():
    """Return [{name, slug}]. On first run, create 'main' and migrate any legacy
    top-level playlist.m3u/epg.xml into the main profile folder."""
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                profs = json.load(f)
            if profs:
                return profs
        except Exception:
            pass
    profs = [{"name": "Main", "slug": "main"}]
    _save_profiles(profs)
    p = profile_paths("main")
    os.makedirs(p["dir"], exist_ok=True)
    os.makedirs(p["backups"], exist_ok=True)
    legacy = {
        os.path.join(DATA_DIR, "playlist.m3u"): p["playlist"],
        os.path.join(DATA_DIR, "epg.xml"): p["epg"],
        os.path.join(DATA_DIR, "epgtalk-channels.json"): p["channels_json"],
        os.path.join(DATA_DIR, "match-aliases.json"): p["aliases"],
        os.path.join(DATA_DIR, "last-run.json"): p["last_run"],
    }
    for src, dst in legacy.items():
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass
    return profs


def ensure_profile_dirs(slug):
    p = profile_paths(slug)
    os.makedirs(p["dir"], exist_ok=True)
    os.makedirs(p["backups"], exist_ok=True)
    return p


def valid_slug_or_404(slug):
    if not any(pr["slug"] == slug for pr in load_profiles()):
        raise HTTPException(404, f"No profile '{slug}'.")
    return slug


# ── file helpers ─────────────────────────────────────────────────────────────
def _sync_playlist_ids(m3u_text):
    """Fill empty tvg-ids with the synthetic ch<N> the EPG engine will assign,
    so playlist and EPG agree and NostalgiaTV shows each channel once."""
    lines = m3u_text.split("\n")
    out, idx = [], 0
    for line in lines:
        if line.startswith("#EXTINF"):
            id_m = re.search(r'tvg-id="([^"]*)"', line)
            chno_m = re.search(r'tvg-chno="([^"]*)"', line)
            existing = id_m.group(1) if id_m else ""
            if not existing:
                try:
                    n = int(chno_m.group(1)) if chno_m and chno_m.group(1) else idx + 1
                except ValueError:
                    n = idx + 1
                synth = f"ch{n}"
                if id_m:
                    line = re.sub(r'tvg-id="[^"]*"', f'tvg-id="{synth}"', line)
                else:
                    line = re.sub(r'(#EXTINF:[^\s]+)', rf'\1 tvg-id="{synth}"', line, count=1)
            idx += 1
        out.append(line)
    return "\n".join(out)


def _backup(path, backup_dir):
    if os.path.exists(path):
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, os.path.join(backup_dir, f"{os.path.basename(path)}.{stamp}"))
        except Exception:
            pass
        prefix = os.path.basename(path) + "."
        old = sorted([f for f in os.listdir(backup_dir) if f.startswith(prefix)], reverse=True)
        for f in old[10:]:
            try:
                os.remove(os.path.join(backup_dir, f))
            except Exception:
                pass


def _file_info(path):
    if not os.path.exists(path):
        return {"exists": False}
    st = os.stat(path)
    return {
        "exists": True, "bytes": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "age_seconds": int(time.time() - st.st_mtime),
    }


def _read_last_run(slug):
    try:
        with open(profile_paths(slug)["last_run"], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── My List on the server + permanent channel ids ─────────────────────────────
# Every channel gets a permanent id (its tvg-id in OUR playlist and guide).
# It never changes when you pick a different guide, renumber, reorder or swap
# in a replacement stream, so NostalgiaTV bindings stay valid forever.
# Existing channels keep the id they already had, so nothing needs re-binding.
import threading
_mylist_lock = threading.Lock()   # guards mylist.json (used by the page AND nightly jobs)
_KEEP_FIELDS = ("uid", "name", "url", "logo", "group", "extinf", "tvgId", "epgPicked",
                "starred", "testState", "testDetail", "testedAt", "language", "country",
                "replacement", "addedAt", "epgDirty")


def _clean_item(it):
    return {k: it[k] for k in _KEEP_FIELDS if k in it and it[k] not in (None,)}


def load_mylist(slug):
    try:
        with open(profile_paths(slug)["mylist"], "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_mylist(slug, items):
    p = ensure_profile_dirs(slug)
    tmp = p["mylist"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    os.replace(tmp, p["mylist"])


def merge_server_fields(slug, items):
    """If the nightly check tested a channel more recently than the page did,
    keep the server's test result and replacement offer (the page may have been
    left open overnight with an older copy)."""
    cur = {x.get("url"): x for x in load_mylist(slug)}
    for it in items:
        sv = cur.get(it.get("url"))
        if not sv:
            continue
        if (sv.get("testedAt") or "") > (it.get("testedAt") or ""):
            for k in ("testState", "testDetail", "testedAt", "replacement"):
                if k in sv:
                    it[k] = sv[k]
                else:
                    it.pop(k, None)
        if not it.get("uid") and sv.get("uid"):
            it["uid"] = sv["uid"]
    return items


def assign_uids(slug, items):
    """Give every item a unique permanent id. Order of preference:
    1. the id it already has  2. the id it is published under today (from the
    last rebuild — this is the no-re-binding migration)  3. a new icNNNN id."""
    last = _read_last_run(slug) or {}
    by_url = last.get("channel_results_by_url") or {}
    used = set()
    for it in items:
        u = it.get("uid")
        if u and u not in used:
            used.add(u)
        else:
            it["uid"] = None
    for it in items:
        if not it.get("uid"):
            cid = (by_url.get(it.get("url", "")) or {}).get("channel_id")
            if cid and cid not in used:
                it["uid"] = cid
                used.add(cid)
    n = max([int(m.group(1)) for u in used if (m := re.fullmatch(r"ic(\d+)", u or ""))] + [0])
    for it in items:
        if not it.get("uid"):
            n += 1
            while f"ic{n:04d}" in used:
                n += 1
            it["uid"] = f"ic{n:04d}"
            used.add(it["uid"])
    return items


def _set_attr(line, attr, value):
    v = str(value).replace('"', "'")
    if re.search(rf'{attr}="[^"]*"', line, re.I):
        return re.sub(rf'{attr}="[^"]*"', f'{attr}="{v}"', line, count=1, flags=re.I)
    return re.sub(r'^(#EXTINF:\s*-?\d+)', rf'\1 {attr}="{v}"', line, count=1)


def write_playlist_and_map(slug, items, start_num=None, epg_url=""):
    """Write playlist.m3u (permanent ids) and guide-map.json (guide choices)."""
    p = ensure_profile_dirs(slug)
    _backup(p["playlist"], p["backups"])
    head = f'#EXTM3U url-tvg="{epg_url}" x-tvg-url="{epg_url}"' if epg_url else "#EXTM3U"
    lines, gmap = [head], {}
    for i, it in enumerate(items):
        line = it.get("extinf") or f'#EXTINF:-1,{it.get("name", "Channel")}'
        if not line.startswith("#EXTINF"):
            line = f'#EXTINF:-1,{it.get("name", "Channel")}'
        line = _set_attr(line, "tvg-id", it["uid"])
        if start_num:
            line = _set_attr(line, "tvg-chno", int(start_num) + i)
        if it.get("logo") and 'tvg-logo="' not in line:
            line = _set_attr(line, "tvg-logo", it["logo"])
        lines += [line, it.get("url", "")]
        guide = (it.get("tvgId") or "").strip()
        if it.get("epgPicked"):
            mode = "pick" if guide else "placeholder"
        else:
            mode = "auto"
        gmap[it["uid"]] = {"guide": guide, "mode": mode,
                           "source_id": _orig_tvg_id(it.get("extinf", ""))}
    with open(p["playlist"], "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(p["guide_map"], "w", encoding="utf-8") as f:
        json.dump(gmap, f, ensure_ascii=False, indent=1)


def _orig_tvg_id(extinf):
    m = re.search(r'tvg-id="([^"]*)"', extinf or "", re.I)
    return m.group(1) if m else ""


# ── guide sources (on/off + custom) ──────────────────────────────────────────
def load_guide_sources():
    """Built-in sources (with your on/off choices) + any custom XMLTV URLs."""
    saved = {}
    try:
        with open(GUIDE_SOURCES_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        pass
    toggles = saved.get("enabled", {})
    out = []
    for src in engine.DEFAULT_SOURCES:
        s2 = dict(src)
        if src["key"] in toggles:
            s2["enabled"] = bool(toggles[src["key"]])
        out.append(s2)
    for c in saved.get("custom", []):
        c = dict(c)
        c["enabled"] = bool(toggles.get(c["key"], True))
        c["custom"] = True
        out.append(c)
    return out


def _save_guide_sources(toggles=None, custom=None):
    saved = {}
    try:
        with open(GUIDE_SOURCES_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        pass
    if toggles is not None:
        saved["enabled"] = toggles
    if custom is not None:
        saved["custom"] = custom
    with open(GUIDE_SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(saved, f, indent=2)
    return saved


# ── guide cache ──────────────────────────────────────────────────────────────
# Downloaded guide data is kept in memory, so Save & Rebuild only re-downloads
# when the cache is older than REFRESH_HOURS (or you press "Refresh guides").
_guides = {"prog": None, "chans": None, "status": [], "fetched_at": None}


def _guides_stale():
    if _guides["prog"] is None or not _guides["fetched_at"]:
        return True
    return (time.time() - _guides["fetched_at"]) > REFRESH_HOURS * 3600 - 300


def _blocking_rebuild(slugs, force_refresh=False):
    """Refresh guide sources if needed, then build each requested profile."""
    if force_refresh or _guides_stale():
        prog, chans, status = engine.download_all_sources(load_guide_sources())
        _guides.update(prog=prog, chans=chans, status=status, fetched_at=time.time())
    results = {}
    for slug in slugs:
        p = ensure_profile_dirs(slug)
        _backup(p["epg"], p["backups"])
        res = engine.build_profile(
            p["playlist"], p["epg"], p["channels_json"], p["aliases"],
            _guides["prog"], _guides["chans"], guide_map_path=p["guide_map"],
        )
        res["log_tail"] = engine.drain_log()[-3000:]
        res["guides_fetched_at"] = datetime.fromtimestamp(_guides["fetched_at"], timezone.utc).isoformat()
        try:
            with open(p["last_run"], "w", encoding="utf-8") as f:
                json.dump(res, f)
        except Exception:
            pass
        results[slug] = res
    return results


async def _rebuild(slugs, force_refresh=False):
    async with _run_lock:
        return await asyncio.to_thread(_blocking_rebuild, slugs, force_refresh)


# ── lifecycle ────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup():
    load_profiles()

    async def _refresh_all():
        slugs = [pr["slug"] for pr in load_profiles()]
        await _rebuild(slugs, force_refresh=True)

    scheduler.add_job(_refresh_all, "interval", hours=REFRESH_HOURS,
                      id="guide_refresh", misfire_grace_time=3600, coalesce=True,
                      next_run_time=datetime.now(timezone.utc))   # also once at startup
    scheduler.add_job(_nightly, "cron", hour=NIGHTLY_HOUR_UTC, minute=15, id="nightly_discover",
                      misfire_grace_time=3600, coalesce=True)
    scheduler.start()
    print(f"[startup] guides refresh + rebuild every {REFRESH_HOURS} h (first run now); "
          f"Discover + My List health check nightly at {NIGHTLY_HOUR_UTC:02d}:15 UTC")


# ── UI ───────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
@app.get("/iptv-checker.html", response_class=HTMLResponse)
async def curator_ui():
    if not os.path.exists(HTML_FILE):
        raise HTTPException(500, "UI file missing from image.")
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ── profile-scoped hosted files ──────────────────────────────────────────────
@app.get("/{slug}/playlist.m3u")
async def get_playlist(slug: str):
    valid_slug_or_404(slug)
    path = profile_paths(slug)["playlist"]
    if not os.path.exists(path):
        raise HTTPException(404, f"No playlist for '{slug}' yet — save one first.")
    return FileResponse(path, media_type="application/x-mpegurl", filename="playlist.m3u")


@app.get("/{slug}/epg.xml")
async def get_epg(slug: str):
    valid_slug_or_404(slug)
    path = profile_paths(slug)["epg"]
    if not os.path.exists(path):
        raise HTTPException(404, f"No epg.xml for '{slug}' yet — rebuild first.")
    return FileResponse(path, media_type="application/xml", filename="epg.xml")


@app.get("/{slug}/epgtalk-channels.json")
async def get_channels_json(slug: str):
    valid_slug_or_404(slug)
    path = profile_paths(slug)["channels_json"]
    if not os.path.exists(path):
        return JSONResponse([])
    return FileResponse(path, media_type="application/json")


# ── legacy aliases -> main ───────────────────────────────────────────────────
@app.get("/playlist.m3u")
async def get_playlist_legacy():
    return await get_playlist("main")


@app.get("/epg.xml")
async def get_epg_legacy():
    return await get_epg("main")


@app.get("/epgtalk-channels.json")
async def get_channels_legacy():
    return await get_channels_json("main")


# ── profile management ───────────────────────────────────────────────────────
@app.get("/api/profiles")
async def api_profiles():
    return load_profiles()


@app.post("/api/profiles")
async def api_create_profile(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Profile name required.")
    slug = slugify(name)
    profs = load_profiles()
    if any(pr["slug"] == slug for pr in profs):
        raise HTTPException(409, f"A profile '{slug}' already exists.")
    profs.append({"name": name, "slug": slug})
    _save_profiles(profs)
    ensure_profile_dirs(slug)
    return {"ok": True, "name": name, "slug": slug}


@app.delete("/api/profiles/{slug}")
async def api_delete_profile(slug: str):
    if slug == "main":
        raise HTTPException(400, "The main profile can't be deleted.")
    profs = load_profiles()
    if not any(pr["slug"] == slug for pr in profs):
        raise HTTPException(404, f"No profile '{slug}'.")
    _save_profiles([pr for pr in profs if pr["slug"] != slug])
    try:
        shutil.rmtree(profile_dir(slug))
    except Exception:
        pass
    return {"ok": True, "deleted": slug}


# ── status (per profile) ─────────────────────────────────────────────────────
@app.get("/api/status")
async def status(request: Request, profile: str = "main"):
    valid_slug_or_404(profile)
    base = str(request.base_url).rstrip("/")
    p = profile_paths(profile)
    return {
        "profile": profile,
        "playlist_url": f"{base}/{profile}/playlist.m3u",
        "epg_url": f"{base}/{profile}/epg.xml",
        "playlist": _file_info(p["playlist"]),
        "epg": _file_info(p["epg"]),
        "running": _run_lock.locked(),
        "last_run": _read_last_run(profile),
        "scheduled": f"every {REFRESH_HOURS} h",
        "guides_fetched_at": (datetime.fromtimestamp(_guides["fetched_at"], timezone.utc).isoformat()
                              if _guides["fetched_at"] else None),
    }


# ── rebuild / save (per profile) ─────────────────────────────────────────────
@app.post("/api/run")
async def run_now(profile: str = "main"):
    valid_slug_or_404(profile)
    if _run_lock.locked():
        raise HTTPException(409, "A rebuild is already in progress.")
    results = await _rebuild([profile])
    return results[profile]


@app.get("/api/guide-sources")
async def api_guide_sources():
    status = {st["key"]: st for st in (_guides["status"] or [])}
    out = []
    for src in load_guide_sources():
        out.append({"key": src["key"], "label": src["label"], "kind": src.get("kind", "cable"),
                    "enabled": src.get("enabled", True), "custom": bool(src.get("custom")),
                    "url": (src.get("urls") or [src.get("path", "")])[0],
                    "status": status.get(src["key"])})
    return {"sources": out, "refresh_hours": REFRESH_HOURS,
            "fetched_at": (datetime.fromtimestamp(_guides["fetched_at"], timezone.utc).isoformat()
                           if _guides["fetched_at"] else None),
            "running": _run_lock.locked()}


@app.post("/api/guide-sources")
async def api_set_guide_source(request: Request):
    """{key, enabled}  -> turn a source on/off
       {add: {label, url, kind}} -> add a custom XMLTV source
       {remove: key}   -> remove a custom source"""
    body = await request.json()
    saved = {}
    try:
        with open(GUIDE_SOURCES_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        pass
    toggles = saved.get("enabled", {})
    custom = saved.get("custom", [])
    if "add" in body:
        a = body["add"] or {}
        url = (a.get("url") or "").strip()
        if not url.startswith("http"):
            raise HTTPException(400, "Provide an http(s) XMLTV url.")
        key = "custom-" + slugify(a.get("label") or url)[:40]
        custom = [c for c in custom if c["key"] != key]
        custom.append({"key": key, "label": (a.get("label") or url.split("/")[-1])[:60],
                       "kind": "fast" if a.get("kind") == "fast" else "cable",
                       "provider": "", "urls": [url]})
    elif "remove" in body:
        custom = [c for c in custom if c["key"] != body["remove"]]
        toggles.pop(body["remove"], None)
    elif "key" in body:
        toggles[body["key"]] = bool(body.get("enabled"))
    _save_guide_sources(toggles, custom)
    return {"ok": True}


@app.post("/api/now-next")
async def api_now_next(request: Request):
    """{ids:[guide ids]} -> {id: {now, next, source}} from the cached guides.
    Lets the page show 'on now' so you can compare a guide with the live stream."""
    body = await request.json()
    ids = [i for i in (body.get("ids") or []) if isinstance(i, str)][:60]
    if _guides["prog"] is None:
        return {}          # guides still downloading (e.g. just restarted)
    prog, chans = _guides["prog"], _guides["chans"] or {}
    now = datetime.now(timezone.utc)
    out = {}
    for gid in ids:
        cur = nxt = None
        for p in prog.get(gid, []):
            st, sp = engine.parse_xmltv_time(p.get("start")), engine.parse_xmltv_time(p.get("stop"))
            if not st or not sp:
                continue
            if st <= now < sp:
                cur = p
            elif st > now:
                nxt = p
                break
        fmt = lambda p: ({"title": p.get("title", ""), "start": p.get("start", ""), "stop": p.get("stop", "")}
                         if p else None)
        out[gid] = {"now": fmt(cur), "next": fmt(nxt), "source": chans.get(gid, {}).get("source", "")}
    return out


@app.post("/api/refresh-guides")
async def api_refresh_guides():
    """Re-download every enabled guide source now and rebuild all profiles."""
    if _run_lock.locked():
        raise HTTPException(409, "A rebuild is already in progress.")
    slugs = [pr["slug"] for pr in load_profiles()]
    results = await _rebuild(slugs, force_refresh=True)
    return {"ok": True, "profiles": {k: {"matched": v.get("matched"), "stubbed": v.get("stubbed")}
                                     for k, v in results.items()},
            "sources": _guides["status"]}


@app.post("/api/save-and-rebuild")
async def save_and_rebuild(request: Request, profile: str = "main"):
    valid_slug_or_404(profile)
    body = await request.json()
    if isinstance(body.get("list"), list):
        items = [_clean_item(x) for x in body["list"] if isinstance(x, dict) and x.get("url")]
        with _mylist_lock:
            merge_server_fields(profile, items)
            assign_uids(profile, items)
            save_mylist(profile, items)
            write_playlist_and_map(profile, items, body.get("start_num"), body.get("epg_url", ""))
        results = await _rebuild([profile])
        r = results[profile]
        r["saved_playlist"] = True
        r["list"] = items
        return r
    m3u = body.get("m3u", "")
    if not m3u.strip().startswith("#EXTM3U"):
        raise HTTPException(400, "Body 'm3u' must start with #EXTM3U.")
    # if a scheduled refresh is running, this simply waits its turn
    p = ensure_profile_dirs(profile)
    _backup(p["playlist"], p["backups"])
    with open(p["playlist"], "w", encoding="utf-8") as f:
        f.write(_sync_playlist_ids(m3u))
    results = await _rebuild([profile])
    r = results[profile]
    r["saved_playlist"] = True
    return r


@app.get("/api/mylist")
async def get_mylist(profile: str = "main"):
    valid_slug_or_404(profile)
    return {"list": load_mylist(profile)}


@app.put("/api/mylist")
async def put_mylist(request: Request, profile: str = "main"):
    """Save My List (called by the page, debounced). Returns it with permanent ids."""
    valid_slug_or_404(profile)
    body = await request.json()
    items = [_clean_item(x) for x in (body.get("list") or []) if isinstance(x, dict) and x.get("url")]
    with _mylist_lock:
        merge_server_fields(profile, items)
        assign_uids(profile, items)
        save_mylist(profile, items)
    return {"ok": True, "list": items}


@app.post("/api/import-playlist")
async def import_playlist(request: Request, profile: str = "main"):
    valid_slug_or_404(profile)
    body = await request.json()
    m3u = body.get("m3u", "")
    if "#EXTINF" not in m3u:
        raise HTTPException(400, "That doesn't look like an M3U playlist.")
    p = ensure_profile_dirs(profile)
    _backup(p["playlist"], p["backups"])
    m3u = m3u if m3u.startswith("#EXTM3U") else "#EXTM3U\n" + m3u
    with open(p["playlist"], "w", encoding="utf-8") as f:
        f.write(_sync_playlist_ids(m3u))
    return {"ok": True, "bytes": len(m3u)}


# ── guide search with confidence scores (match modal) ────────────────────────
@app.post("/api/search-guide")
async def search_guide(request: Request, profile: str = "main"):
    valid_slug_or_404(profile)
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        return []
    return await asyncio.to_thread(engine.search_guide, query, profile_dir(profile))


# ── editable dropdown (shared) ───────────────────────────────────────────────
DEFAULT_SOURCES = [
    {"name": "Free-TV (Multi-region)",
     "url": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"},
]

def _load_sources():
    if not os.path.exists(SOURCES_FILE):
        return list(DEFAULT_SOURCES)
    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return list(DEFAULT_SOURCES)


@app.get("/api/sources")
async def get_sources():
    return _load_sources()


@app.post("/api/sources")
async def set_sources(request: Request):
    items = await request.json()
    if not isinstance(items, list):
        raise HTTPException(400, "Expected a JSON array of {name, url}.")
    cleaned = [{"name": str(i.get("name", "")).strip(), "url": str(i.get("url", "")).strip()}
               for i in items if i.get("url")]
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    return {"ok": True, "count": len(cleaned)}


# ── stream testing ───────────────────────────────────────────────────────────
# A stream only counts as LIVE if we can actually pull video bytes:
#   master playlist -> first variant playlist -> first segment.
# (Many dead FAST channels still serve the playlist file while the video
#  segments behind it are gone, so checking only the first layer lies.)
_UA = "Mozilla/5.0 (IPTVCurator/2.0)"
_GEO_CODES = (403, 451, 407)


def _fetch(url, timeout, max_bytes, rng=None):
    """GET url -> (code, final_url, content_type, bytes). Raises on network error."""
    import urllib.request, urllib.error
    headers = {"User-Agent": _UA}
    if rng:
        headers["Range"] = rng
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.getcode(), resp.geturl(), resp.headers.get("Content-Type", ""),
                    resp.read(max_bytes))
    except urllib.error.HTTPError as e:
        return e.code, url, "", b""


def _first_uri(text):
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            return ln
    return None


def _probe_blocking(url, timeout):
    from urllib.parse import urljoin
    try:
        code, final, ctype, body = _fetch(url, timeout, 256 * 1024)
        if code in _GEO_CODES:
            return {"ok": False, "geo": True, "code": code, "detail": f"HTTP {code} (likely geo-blocked)"}
        if not (200 <= code < 300):
            return {"ok": False, "geo": False, "code": code, "detail": f"HTTP {code}"}
        text = body.decode("utf-8", "ignore")
        head = text.lstrip()[:200].lower()

        if "#extm3u" in head or "#ext-x" in text[:2000].lower():
            # master playlist? follow the first variant
            if "#EXT-X-STREAM-INF" in text:
                v = _first_uri(text)
                if not v:
                    return {"ok": False, "geo": False, "code": code, "detail": "master playlist has no variants"}
                vurl = urljoin(final, v)
                vcode, final, _, vbody = _fetch(vurl, timeout, 256 * 1024)
                if vcode in _GEO_CODES:
                    return {"ok": False, "geo": True, "code": vcode, "detail": f"variant HTTP {vcode} (likely geo-blocked)"}
                if not (200 <= vcode < 300):
                    return {"ok": False, "geo": False, "code": vcode, "detail": f"variant playlist HTTP {vcode}"}
                text = vbody.decode("utf-8", "ignore")
            seg = _first_uri(text)
            if not seg:
                return {"ok": False, "geo": False, "code": code, "detail": "playlist has no video segments"}
            surl = urljoin(final, seg)
            scode, _, _, sbody = _fetch(surl, timeout, 4096, rng="bytes=0-4095")
            if scode in _GEO_CODES:
                return {"ok": False, "geo": True, "code": scode, "detail": f"segment HTTP {scode} (likely geo-blocked)"}
            if 200 <= scode < 300 and sbody:
                return {"ok": True, "geo": False, "code": code, "detail": "video segment loads"}
            return {"ok": False, "geo": False, "code": scode, "detail": f"playlist OK but video segment fails (HTTP {scode})"}

        # not HLS: reject web/error pages, accept a raw media stream with bytes
        if "text/html" in ctype.lower() or head.startswith("<!doctype") or head.startswith("<html"):
            return {"ok": False, "geo": False, "code": code, "detail": "returned a web page, not video"}
        if body:
            return {"ok": True, "geo": False, "code": code, "detail": "stream returns data"}
        return {"ok": False, "geo": False, "code": code, "detail": "empty response"}
    except Exception as e:
        msg = str(e)
        if "timed out" in msg.lower():
            msg = "timed out"
        return {"ok": False, "geo": False, "code": 0, "detail": msg[:120]}


async def _probe_stream(url, timeout=8.0):
    result = await asyncio.to_thread(_probe_blocking, url, timeout)
    result["url"] = url
    result["tested_at"] = datetime.now(timezone.utc).isoformat()
    return result


def _timeout_from(body, default=8.0):
    try:
        return max(2.0, min(30.0, float(body.get("timeout", default))))
    except (TypeError, ValueError):
        return default


@app.post("/api/test-stream")
async def test_stream(request: Request):
    body = await request.json()
    url = body.get("url", "").strip()
    if not url.startswith("http"):
        raise HTTPException(400, "Provide an http(s) stream url.")
    return await _probe_stream(url, _timeout_from(body))


@app.post("/api/test-streams")
async def test_streams(request: Request):
    body = await request.json()
    urls = body.get("urls", [])
    if not isinstance(urls, list) or not urls:
        raise HTTPException(400, "Provide a non-empty 'urls' array.")
    timeout = _timeout_from(body)
    sem = asyncio.Semaphore(8)
    async def guarded(u):
        async with sem:
            return await _probe_stream(u, timeout)
    results = await asyncio.gather(*(guarded(u) for u in urls))
    ok = sum(1 for r in results if r["ok"])
    return {"total": len(results), "ok": ok, "dead": len(results) - ok, "results": results}


# ── fetch a source playlist for the browser (replaces the outside CORS proxy) ─
@app.post("/api/fetch-playlist")
async def fetch_playlist(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        raise HTTPException(400, "Provide an http(s) playlist url.")
    def _get():
        import gzip as _gz
        code, _, _, data = _fetch(url, 60, 60 * 1024 * 1024)
        if not (200 <= code < 300):
            raise HTTPException(502, f"Source returned HTTP {code}.")
        if data[:2] == b"\x1f\x8b":
            data = _gz.decompress(data)
        return data.decode("utf-8", "ignore")
    try:
        text = await asyncio.to_thread(_get)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not fetch playlist: {str(e)[:150]}")
    return {"text": text}


# ── learn a manual match (per profile) ───────────────────────────────────────
@app.post("/api/learn-alias")
async def learn_alias(request: Request, profile: str = "main"):
    valid_slug_or_404(profile)
    body = await request.json()
    name = (body.get("name") or "").strip()
    gid = (body.get("guide_id") or "").strip()
    url = (body.get("url") or "").strip()
    group = (body.get("group") or "").strip()
    if not name or not gid:
        raise HTTPException(400, "Both 'name' and 'guide_id' are required.")
    fast = engine.looks_fast({"name": name, "url": url, "group": group})
    alias_path = profile_paths(profile)["aliases"]   # explicit: no shared state
    def _save():
        return engine.save_alias(name, gid, alias_path, fast=fast)
    ok = await asyncio.to_thread(_save)
    return {"ok": ok}


# ═════════════════════════════════════════════════════════════════════════════
# DISCOVER — watch trusted playlists, test new channels, check guide data, and
# fill a "New channels" inbox.   SELF-HEALING — nightly retest of My List, with
# a working replacement offered for any channel that died.
# ═════════════════════════════════════════════════════════════════════════════
from concurrent.futures import ThreadPoolExecutor, as_completed

DISCOVER_FILE = os.path.join(DATA_DIR, "discover.json")
DISCOVER_SOURCES_FILE = os.path.join(DATA_DIR, "discover-sources.json")
NIGHTLY_HOUR_UTC = int(os.environ.get("IPTV_NIGHTLY_HOUR_UTC", "8"))   # 8 UTC = 2-3 AM Central
RETEST_DAYS = 3          # a found channel is re-tested after this many days
_BCC = "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists"
DEFAULT_DISCOVER_SOURCES = [
    # FAST playlists whose channel ids match the FAST guides exactly
    {"key": "d-samsung", "label": "Samsung TV Plus (US)", "url": f"{_BCC}/samsungtvplus_us.m3u", "enabled": True},
    {"key": "d-plex", "label": "Plex (US)", "url": f"{_BCC}/plex_us.m3u", "enabled": True},
    {"key": "d-pluto", "label": "Pluto TV (US)", "url": f"{_BCC}/plutotv_us.m3u", "enabled": True},
    {"key": "d-roku", "label": "Roku Channel", "url": f"{_BCC}/roku_all.m3u", "enabled": True},
    # community lists
    {"key": "d-iptvorg-us", "label": "iptv-org · USA", "url": "https://iptv-org.github.io/iptv/countries/us.m3u", "enabled": True},
    {"key": "d-freetv", "label": "Free-TV (multi-region)", "url": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8", "enabled": True},
    {"key": "d-iptvorg-uk", "label": "iptv-org · UK", "url": "https://iptv-org.github.io/iptv/countries/uk.m3u", "enabled": False},
    {"key": "d-iptvorg-eng", "label": "iptv-org · every English-language channel (large)", "url": "https://iptv-org.github.io/iptv/languages/eng.m3u", "enabled": False},
]

_disc = {"running": False, "phase": "", "done": 0, "total": 0, "started_at": None,
         "finished_at": None, "error": "", "sources": [], "found": 0, "live": 0}
_heal = {"running": False, "phase": "", "done": 0, "total": 0, "finished_at": None,
         "dead": 0, "replacements": 0}
_disc_lock = threading.Lock()


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_discover_sources():
    saved = _read_json(DISCOVER_SOURCES_FILE, {})
    toggles = saved.get("enabled", {})
    out = []
    for src in DEFAULT_DISCOVER_SOURCES:
        s2 = dict(src)
        if src["key"] in toggles:
            s2["enabled"] = bool(toggles[src["key"]])
        out.append(s2)
    for c in saved.get("custom", []):
        c = dict(c); c["enabled"] = bool(toggles.get(c["key"], True)); c["custom"] = True
        out.append(c)
    return out


# ── English filter (same rules as the page) ──
_ENG_COUNTRIES = {"US", "GB", "UK", "CA", "AU", "NZ", "IE", "ZA", "JM", "TT", "BB", "GY", "BZ",
                  "GH", "NG", "KE", "UG", "TZ", "ZW"}
_NON_LATIN = re.compile("[\u0400-\u04FF\u0600-\u06FF\u0900-\u097F\u4E00-\u9FFF\u3040-\u309F"
                        "\u30A0-\u30FF\uAC00-\uD7AF\u0E00-\u0E7F\u0590-\u05FF]")
_NON_ENG = re.compile(r"\b(hindi|arabic|french|fran[cç]ais|español|espanol|spanish|german|deutsch|italiano|"
                      r"italian|portugu[eê]s|portuguese|russian|chinese|korean|japanese|dutch|polish|"
                      r"turkish|persian|urdu|thai|vietnamese|greek|hebrew|tagalog|latino|en vivo|noticias)\b", re.I)


def _is_english(e):
    lang = (e.get("language") or "").lower().strip()
    if lang:
        return "english" in lang or lang in ("eng", "en") or lang.startswith("en-")
    country = (e.get("country") or "").upper().strip()
    if not country:
        m = re.search(r"\.([a-z]{2})(?:@|$)", e.get("tvg_id") or "")
        country = m.group(1).upper() if m else ""
    name = e.get("name") or ""
    if _NON_LATIN.search(name) or _NON_ENG.search(f"{name} {e.get('group','')}"):
        return False
    if country:
        return country in _ENG_COUNTRIES
    return True


def _parse_m3u_text(text, source):
    out, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            g = lambda a: (re.search(rf'{a}="([^"]*)"', line, re.I) or [None, ""])[1]
            nm = re.search(r",([^,]*)$", line)
            cur = {"name": (nm.group(1).strip() if nm else "") or g("tvg-name") or "Channel",
                   "tvg_id": g("tvg-id"), "logo": g("tvg-logo"), "group": g("group-title"),
                   "language": g("tvg-language"), "country": g("tvg-country"), "extinf": line,
                   "source": source["label"], "source_key": source["key"]}
        elif line.startswith("http") and cur:
            cur["url"] = line
            out.append(cur)
            cur = None
        elif line and not line.startswith("#"):
            cur = None
    return out


def _all_list_urls():
    urls = set()
    for pr in load_profiles():
        urls |= {x.get("url") for x in load_mylist(pr["slug"])}
    return urls


def _blocking_discover():
    st = _disc
    st.update(running=True, phase="Downloading playlists", done=0, total=0, error="",
              started_at=datetime.now(timezone.utc).isoformat(), sources=[])
    try:
        srcs = [x for x in load_discover_sources() if x.get("enabled")]
        st["total"] = len(srcs)
        data = _read_json(DISCOVER_FILE, {})
        items, dismissed = data.get("items", {}), data.get("dismissed", {})
        fetched = {}
        for src in srcs:
            try:
                code, _, _, raw = _fetch(src["url"], 90, 80 * 1024 * 1024)
                if not (200 <= code < 300):
                    raise RuntimeError(f"HTTP {code}")
                if raw[:2] == b"\x1f\x8b":
                    import gzip as _gz
                    raw = _gz.decompress(raw)
                ents = _parse_m3u_text(raw.decode("utf-8", "ignore"), src)
                eng = [e for e in ents if _is_english(e)]
                for e in eng:
                    fetched.setdefault(e["url"], e)
                st["sources"].append({"key": src["key"], "label": src["label"], "ok": True,
                                      "count": len(ents), "english": len(eng)})
            except Exception as e:
                st["sources"].append({"key": src["key"], "label": src["label"], "ok": False,
                                      "error": str(e)[:150]})
            st["done"] += 1

        now = time.time()
        failed = {x["key"] for x in st["sources"] if not x["ok"]}
        merged = {}
        for url, e in fetched.items():
            it = dict(items.get(url, {}))
            it.update(e)
            it.setdefault("first_seen", now)
            it["last_seen"] = now
            merged[url] = it
        for url, it in items.items():          # a source that failed today keeps yesterday's finds
            if url not in merged and it.get("source_key") in failed:
                merged[url] = it

        in_list = _all_list_urls()
        to_test = [u for u, it in merged.items()
                   if u not in dismissed and u not in in_list
                   and (now - it.get("tested_at", 0)) > RETEST_DAYS * 86400]
        st.update(phase="Testing streams", done=0, total=len(to_test))
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = {ex.submit(_probe_blocking, u, 8.0): u for u in to_test}
            for f in as_completed(futs):
                u = futs[f]
                try:
                    r = f.result()
                except Exception as e:
                    r = {"ok": False, "detail": str(e)[:100]}
                merged[u].update(status="live" if r.get("ok") else ("geo" if r.get("geo") else "dead"),
                                 detail=r.get("detail", ""), tested_at=time.time())
                st["done"] += 1

        st.update(phase="Checking guide data", done=0, total=0)
        if _guides["prog"] is not None:
            match = engine.make_matcher(_guides["prog"], _guides["chans"])
            for it in merged.values():
                if it.get("status") == "live":
                    it["guide"] = match({"name": it["name"], "tvgId": it.get("tvg_id", ""),
                                         "url": it["url"], "group": it.get("group", "")})
        _write_json(DISCOVER_FILE, {"items": merged, "dismissed": dismissed,
                                    "last_run": {"finished_at": datetime.now(timezone.utc).isoformat(),
                                                 "sources": st["sources"]}})
        st.update(found=len(merged), live=sum(1 for i in merged.values() if i.get("status") == "live"))
    except Exception as e:
        st["error"] = str(e)[:200]
        engine.log(f"Discover failed: {e}")
    finally:
        st.update(running=False, phase="Done", finished_at=datetime.now(timezone.utc).isoformat())


def _blocking_selfheal():
    """Retest every profile's My List; for each dead channel, look for the same
    channel (by name) in the Discover pool and offer the first one that works."""
    h = _heal
    h.update(running=True, phase="Retesting My List", done=0, total=0, dead=0, replacements=0)
    try:
        pool = {}
        for it in _read_json(DISCOVER_FILE, {}).get("items", {}).values():
            pool.setdefault(engine.normalize_name(it.get("name", "")), []).append(it)
        for pr in load_profiles():
            slug = pr["slug"]
            items = load_mylist(slug)
            if not items:
                continue
            h["total"] += len(items)
            results = {}
            with ThreadPoolExecutor(max_workers=10) as ex:
                futs = {ex.submit(_probe_blocking, it["url"], 8.0): it["url"] for it in items}
                for f in as_completed(futs):
                    try:
                        results[futs[f]] = f.result()
                    except Exception as e:
                        results[futs[f]] = {"ok": False, "detail": str(e)[:100]}
                    h["done"] += 1
            stamp = datetime.now(timezone.utc).isoformat()
            updates = {}
            for it in items:
                r = results.get(it["url"], {})
                up = {"testState": "live" if r.get("ok") else "dead",
                      "testDetail": f'{r.get("detail", "")} · nightly check', "testedAt": stamp,
                      "replacement": None}
                if not r.get("ok"):
                    h["dead"] += 1
                    key = engine.normalize_name(it.get("name", ""))
                    cands = [c for c in pool.get(key, []) if c.get("url") != it["url"]]
                    cands.sort(key=lambda c: c.get("status") != "live")   # known-live first
                    for c in cands[:6]:
                        if _probe_blocking(c["url"], 8.0).get("ok"):
                            up["replacement"] = {k: c.get(k, "") for k in
                                                 ("url", "name", "logo", "extinf", "source", "tvg_id")}
                            h["replacements"] += 1
                            break
                updates[it["url"]] = up
            with _mylist_lock:                       # merge into the CURRENT list
                cur = load_mylist(slug)
                for it in cur:
                    up = updates.get(it.get("url"))
                    if not up:
                        continue
                    for k, v in up.items():
                        if v is None:
                            it.pop(k, None)
                        else:
                            it[k] = v
                save_mylist(slug, cur)
    except Exception as e:
        engine.log(f"Self-heal failed: {e}")
    finally:
        h.update(running=False, phase="Done", finished_at=datetime.now(timezone.utc).isoformat())


async def _nightly():
    if not _disc["running"]:
        await asyncio.to_thread(_blocking_discover)
    if not _heal["running"]:
        await asyncio.to_thread(_blocking_selfheal)


def _start_bg(fn):
    def runner():
        try:
            fn()
        except Exception as e:
            engine.log(f"background job failed: {e}")
    threading.Thread(target=runner, daemon=True).start()


@app.get("/api/discover")
async def api_discover(profile: str = "main"):
    valid_slug_or_404(profile)
    data = _read_json(DISCOVER_FILE, {})
    dismissed = data.get("dismissed", {})
    mine = {x.get("url") for x in load_mylist(profile)}
    my_names = {engine.normalize_name(x.get("name", "")) for x in load_mylist(profile)}
    keys = ("name", "url", "logo", "group", "source", "source_key", "first_seen", "detail",
            "tvg_id", "extinf", "guide", "language", "country")
    out = []
    for url, it in data.get("items", {}).items():
        if it.get("status") != "live" or url in dismissed or url in mine:
            continue
        row = {k: it.get(k) for k in keys}
        row["have_name"] = engine.normalize_name(it.get("name", "")) in my_names
        out.append(row)
    out.sort(key=lambda r: (-(r.get("first_seen") or 0), (r.get("name") or "").lower()))
    srcs = []
    status = {x["key"]: x for x in (data.get("last_run") or {}).get("sources", [])}
    for src in load_discover_sources():
        srcs.append({"key": src["key"], "label": src["label"], "url": src["url"],
                     "enabled": src.get("enabled", True), "custom": bool(src.get("custom")),
                     "status": status.get(src["key"])})
    return {"items": out, "state": _disc, "last_run": data.get("last_run"),
            "dismissed": len(dismissed), "sources": srcs, "nightly_hour_utc": NIGHTLY_HOUR_UTC}


@app.post("/api/discover/run")
async def api_discover_run():
    if _disc["running"]:
        return {"ok": True, "already_running": True}
    _disc["running"] = True
    _start_bg(_blocking_discover)
    return {"ok": True}


@app.post("/api/discover/dismiss")
async def api_discover_dismiss(request: Request):
    body = await request.json()
    urls = [u for u in (body.get("urls") or []) if isinstance(u, str)]
    with _disc_lock:
        data = _read_json(DISCOVER_FILE, {})
        d = data.setdefault("dismissed", {})
        for u in urls:
            d[u] = time.time()
        _write_json(DISCOVER_FILE, data)
    return {"ok": True, "dismissed": len(urls)}


@app.post("/api/discover/sources")
async def api_discover_sources(request: Request):
    body = await request.json()
    saved = _read_json(DISCOVER_SOURCES_FILE, {})
    toggles, custom = saved.get("enabled", {}), saved.get("custom", [])
    if "add" in body:
        a = body["add"] or {}
        url = (a.get("url") or "").strip()
        if not url.startswith("http"):
            raise HTTPException(400, "Provide an http(s) playlist url.")
        key = "dc-" + slugify(a.get("label") or url)[:40]
        custom = [c for c in custom if c["key"] != key]
        custom.append({"key": key, "label": (a.get("label") or url.split("/")[-1])[:60], "url": url})
    elif "remove" in body:
        custom = [c for c in custom if c["key"] != body["remove"]]
        toggles.pop(body["remove"], None)
    elif "key" in body:
        toggles[body["key"]] = bool(body.get("enabled"))
    _write_json(DISCOVER_SOURCES_FILE, {"enabled": toggles, "custom": custom})
    return {"ok": True}


@app.get("/api/selfheal")
async def api_selfheal_state():
    return _heal


@app.post("/api/selfheal/run")
async def api_selfheal_run():
    if _heal["running"]:
        return {"ok": True, "already_running": True}
    _heal["running"] = True
    _start_bg(_blocking_selfheal)
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}
