#!/usr/bin/env python3
"""
EPG Engine for IPTV Curator (consolidated app)
==============================================
Ported from the standalone epg_manager.py (v3). Preserves the proven logic:
  - Three sources in priority order: EPGTalk > GlobeTV > DirecTV scraper
  - @variant ID stripping (History.us@East -> History.us)
  - Stub programme data for unmatched channels

NEW in the consolidated app — a smart-matching layer that finds programme
data for channels whose playlist name doesn't line up with the guide's ID.
This is what solves the "Showtime 2 in my list, SHO2 in the guide" problem.
The matcher tries, in order:
  1. exact tvg-id           (unchanged from before)
  2. @variant-stripped id   (unchanged from before)
  3. name normalization     (strip HD/TV/spaces/punctuation, lowercase)
  4. alias table            (learned + built-in: "showtime 2" -> "SHO2")
  5. fuzzy token match       (above a confidence threshold)

Anything the fuzzy matcher finds below the auto-accept threshold but above a
floor is recorded as a "suggestion" the UI can surface as "did you mean?".

This module is imported by server.py and called in-process. It can also be
run directly for a one-off merge:  python3 epg_engine.py
"""

import gzip
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

# ── CONFIG ──────────────────────────────────────────────────────────────────
# In the container, DATA_DIR is a mounted volume so files survive restarts.
DATA_DIR       = os.environ.get("IPTV_DATA_DIR", "/data")
PLAYLIST_FILE  = os.path.join(DATA_DIR, "playlist.m3u")
EPG_OUTPUT     = os.path.join(DATA_DIR, "epg.xml")
CHANNELS_JSON  = os.path.join(DATA_DIR, "epgtalk-channels.json")   # guide channel list for the UI
ALIAS_FILE     = os.path.join(DATA_DIR, "match-aliases.json")      # learned name->guide-id memory
LOG_FILE       = os.path.join(DATA_DIR, "epg_manager.log")

STUB_DAYS      = 7
STUB_HOURS     = 1

# Fuzzy matching thresholds (0.0 - 1.0). Tuned conservatively.
FUZZY_AUTO_ACCEPT = 1.01   # >1.0 = fuzzy NEVER auto-matches; it only suggests
FUZZY_SUGGEST     = 0.86   # only fairly close names are offered as "did you mean?"

EPGTALK_URL = "https://raw.githubusercontent.com/acidjesuz/EPGTalk/master/US_guide.xml.gz"

GLOBETV_URLS = [
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa1.xml",
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa2.xml",
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa3.xml",
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa4.xml",
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa5.xml",
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa6.xml",
]

# DirecTV scraper (bundled compose service) writes its guide here inside the
# shared volume. If the file isn't present, this source is simply skipped.
DIRECTV_GUIDE = os.path.join(DATA_DIR, "directv", "guide.xml")

# Built-in alias seeds — the classic name/guide-id mismatches. The learned
# alias file (ALIAS_FILE) is merged on top of this and takes priority.
BUILTIN_ALIASES = {
    "showtime 2": "SHO2",
    "showtime too": "SHO2",
    "sho 2": "SHO2",
    "showtime2": "SHO2",
}

# Known renames: playlist name (normalized) -> guide name (normalized).
# Used only when the playlist name itself isn't found in any guide.
NAME_ALIASES = {
    "showtime": "paramountpluswithshowtime",     # Showtime's main channel rebrand
    "showtimeeast": "paramountpluswithshowtime",
    "showtimewest": "paramountpluswithshowtimepacific",
    "showtime2": "sho2",
    "shoutfactory": "shout",                     # Shout! Factory TV -> Shout! TV
}

# ── logging ─────────────────────────────────────────────────────────────────
_log_buffer = []

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    _log_buffer.append(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def drain_log():
    """Return and clear the in-memory log buffer (used by the /run endpoint)."""
    global _log_buffer
    out = "\n".join(_log_buffer)
    _log_buffer = []
    return out


# ── guide sources (configurable) ───────────────────────────────────────────
# Each source: {key, label, kind: "cable"|"fast", provider, urls|path, enabled}
#   kind "fast"  = a FAST service guide (Pluto/Samsung/Roku/Plex). Its channels
#                  are only matched to FAST streams, never to cable streams.
#   provider     = which service the guide belongs to, so a Samsung stream
#                  prefers the Samsung guide's schedule.
# Order = priority when two sources use the same channel id.
MJH = "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master"
DEFAULT_SOURCES = [
    {"key": "epgtalk", "label": "EPGTalk (US cable)", "kind": "cable", "provider": "",
     "urls": [EPGTALK_URL], "enabled": True},
    # GlobeTV stopped updating (newest listings end Jan 2026) — off by default;
    # flip it back on in Guide sources if it ever comes back to life.
    {"key": "globetv", "label": "GlobeTV (US cable)", "kind": "cable", "provider": "",
     "urls": GLOBETV_URLS, "enabled": False},
    {"key": "directv", "label": "DirecTV scraper (premium)", "kind": "cable", "provider": "",
     "path": DIRECTV_GUIDE, "enabled": True},
    {"key": "samsung", "label": "Samsung TV Plus (FAST)", "kind": "fast", "provider": "samsung",
     "urls": [f"{MJH}/SamsungTVPlus/us.xml.gz"], "enabled": True},
    {"key": "roku", "label": "Roku Channel (FAST)", "kind": "fast", "provider": "roku",
     "urls": [f"{MJH}/Roku/all.xml.gz"], "enabled": True},
    {"key": "plex", "label": "Plex Live TV (FAST)", "kind": "fast", "provider": "plex",
     "urls": [f"{MJH}/Plex/us.xml.gz"], "enabled": True},
    {"key": "pluto", "label": "Pluto TV (FAST)", "kind": "fast", "provider": "pluto",
     "urls": [f"{MJH}/PlutoTV/us.xml.gz"], "enabled": True},
    {"key": "epgshare_us2", "label": "epgshare01 US national", "kind": "cable", "provider": "",
     "urls": ["https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz"], "enabled": True},
]


def _open_source_bytes(url_or_path, timeout=180):
    """Return raw bytes (gunzipped if needed) from a URL or local path."""
    if url_or_path.startswith("http"):
        req = urllib.request.Request(url_or_path, headers={"User-Agent": "IPTVCurator/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    else:
        with open(url_or_path, "rb") as f:
            raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def parse_xmltv_bytes(raw, source):
    """Stream-parse XMLTV (low memory). Returns (channels, programmes).
    Listings that already ended more than 6 hours ago are skipped right here."""
    import io
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=6)
    channels, programmes = {}, {}
    label, kind, provider = source["label"], source.get("kind", "cable"), source.get("provider", "")
    for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        tag = el.tag
        if tag == "channel":
            cid = el.get("id", "")
            if cid:
                dn = el.find("display-name")
                icon = el.find("icon")
                channels[cid] = {"id": cid, "name": (dn.text if dn is not None and dn.text else cid),
                                 "icon": icon.get("src", "") if icon is not None else "",
                                 "source": label, "kind": kind, "provider": provider}
            el.clear()
        elif tag == "programme":
            cid = el.get("channel", "")
            stop = el.get("stop", "")
            st = parse_xmltv_time(stop) if stop else None
            if cid and not (st and st < cutoff_dt):
                t, d, c = el.find("title"), el.find("desc"), el.find("category")
                ic, ep = el.find("icon"), el.find("episode-num")
                programmes.setdefault(cid, []).append({
                    "start": el.get("start", ""), "stop": stop,
                    "title": t.text if t is not None and t.text else "",
                    "desc": d.text if d is not None and d.text else "",
                    "category": c.text if c is not None and c.text else "",
                    "icon": ic.get("src", "") if ic is not None else "",
                    "episode": ep.text if ep is not None and ep.text else "",
                    "episode_sys": ep.get("system", "") if ep is not None else "",
                })
            el.clear()
    return channels, programmes


def load_source(source):
    """Fetch + parse one source (all its files). Returns (channels, programmes, status)."""
    t0 = time.time()
    chans, progs, errors, files_ok = {}, {}, [], 0
    targets = list(source.get("urls") or [])
    if source.get("path"):
        targets.append(source["path"])
    for tgt in targets:
        if not tgt.startswith("http") and not os.path.exists(tgt):
            errors.append("no guide file yet (the DirecTV scraper writes it on its nightly run)")
            continue
        try:
            c, p = parse_xmltv_bytes(_open_source_bytes(tgt), source)
            for cid, info in c.items():
                chans.setdefault(cid, info)
            for cid, plist in p.items():
                progs.setdefault(cid, []).extend(plist)
            files_ok += 1
        except Exception as e:
            errors.append(f"{tgt.split('/')[-1]}: {str(e)[:100]}")
    if files_ok and not progs and not errors:
        errors.append("downloaded, but every listing is out of date — this source has stopped updating")
    status = {"key": source["key"], "label": source["label"], "ok": files_ok > 0 and bool(progs),
              "files_ok": files_ok, "files": len(targets),
              "channels": len(chans), "with_listings": len(progs),
              "seconds": round(time.time() - t0, 1),
              "error": "; ".join(errors)[:300] if errors else "",
              "checked_at": datetime.now(timezone.utc).isoformat()}
    log(f"  {source['label']}: {status['channels']} channels, {status['with_listings']} with listings"
        + (f" — {status['error']}" if errors else ""))
    return chans, progs, status


# ── smart matching ──────────────────────────────────────────────────────────
def normalize_name(s):
    """Lowercase, strip resolution/quality tags, common suffixes, punctuation."""
    s = (s or "").lower()
    # keep feed markers that live in brackets: "(Pacific)" must not look like East
    s = re.sub(r"[\(\[]\s*(east|west|pacific|mountain)\s*[\)\]]", r" \1 ", s)
    s = re.sub(r"\(.*?\)", " ", s)                 # drop (1080p), (720p), etc.
    s = re.sub(r"\[.*?\]", " ", s)                 # drop [Geo-blocked], etc.
    # NOTE: east/west/pacific are deliberately KEPT so "HBO West" never takes
    # the East schedule (3 hours off).
    s = s.replace("+", " plus ")                       # AMC+ is AMC plus, not AMC
    s = re.sub(r"^\s*the\s+", " ", s)                # "The Bob Ross Channel" = "Bob Ross Channel"
    s = re.sub(r"\b(hd|sd|fhd|uhd|4k|tv|channel|network|us|usa)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)               # keep only alnum
    return s


def load_aliases(alias_path=None):
    """Merge built-in aliases with the learned alias file (learned wins)."""
    aliases = dict(BUILTIN_ALIASES)
    try:
        with open(alias_path or ALIAS_FILE, "r", encoding="utf-8") as f:
            learned = json.load(f)
        for k, v in learned.items():
            aliases[k.lower()] = v
    except Exception:
        pass
    return aliases


def alias_key(name, fast=False):
    """FAST channels get their own alias namespace, so a pick made for the cable
    'Comedy Central' never leaks onto Pluto's 'Comedy Central' (or vice versa)."""
    k = (name or "").strip().lower()
    return ("fast:" + k) if fast else k


def save_alias(playlist_name, guide_id, alias_path=None, fast=False):
    """Remember a manual match so it's applied automatically next time.
    alias_path is explicit so each profile writes only to its own file."""
    path = alias_path or ALIAS_FILE
    try:
        learned = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                learned = json.load(f)
        learned[alias_key(playlist_name, fast)] = guide_id
        with open(path, "w", encoding="utf-8") as f:
            json.dump(learned, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log(f"  ERROR saving alias: {e}")
        return False


# ── time helpers ────────────────────────────────────────────────────────────
def parse_xmltv_time(t):
    """'20261001153000 +0000' -> aware UTC datetime (None if unparseable)."""
    try:
        t = (t or "").strip()
        base = datetime.strptime(t[:14], "%Y%m%d%H%M%S")
        off = t[14:].strip()
        if off and off[0] in "+-" and len(off) >= 5:
            sign = 1 if off[0] == "+" else -1
            delta = timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
            base = base - sign * delta
        return base.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def coverage_until(progs):
    """Latest programme stop time for a channel (UTC), or None."""
    latest = None
    for p in progs:
        st = parse_xmltv_time(p.get("stop"))
        if st and (latest is None or st > latest):
            latest = st
    return latest


def tidy_programmes(all_programmes, keep_past_hours=6):
    """Drop exact duplicates (GlobeTV files overlap) and long-past listings.
    Sorts each channel's programmes by start time."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=keep_past_hours)
    removed_dupes = removed_old = 0
    for cid, progs in all_programmes.items():
        seen, out = set(), []
        for p in progs:
            key = (p.get("start"), p.get("stop"), p.get("title"))
            if key in seen:
                removed_dupes += 1
                continue
            seen.add(key)
            st = parse_xmltv_time(p.get("stop"))
            if st and st < cutoff:
                removed_old += 1
                continue
            out.append(p)
        out.sort(key=lambda p: p.get("start", ""))
        all_programmes[cid] = out
    log(f"Tidied guide: removed {removed_dupes} duplicate and {removed_old} expired listings")


# ── FAST-channel guard ──────────────────────────────────────────────────────
# FAST services (Pluto, Samsung, Roku, ...) often carry channels named exactly
# like cable networks but airing DIFFERENT shows. For those, a name-based match
# is only offered as a suggestion, never applied automatically.
FAST_URL_HINTS = ("pluto.tv", "plex.tv", "roku", "samsung", "jmp2.uk", "xumo",
                  "tubi", "stirr", "localnow", "distro", "amagi.tv", "wurl",
                  "vizio", "lgchannels", "frequency.stream", "fubo")
FAST_NAME_RX = re.compile(r"\b(pluto|samsung|roku|plex|xumo|tubi|stirr|local now|distro|vizio|lg channels|fast)\b", re.I)


PROVIDER_URL_HINTS = [("pluto.tv", "pluto"), ("jmp2.uk/plu", "pluto"),
                      ("jmp2.uk/sam", "samsung"), ("samsung", "samsung"),
                      ("roku", "roku"), ("plex.tv", "plex"), ("plex.direct", "plex")]


def stream_provider(ch):
    """Which FAST service a stream URL comes from, if we can tell."""
    url = (ch.get("url") or "").lower()
    for hint, prov in PROVIDER_URL_HINTS:
        if hint in url:
            return prov
    return ""


def looks_fast(ch):
    url = (ch.get("url") or "").lower()
    if any(h in url for h in FAST_URL_HINTS):
        return True
    return bool(FAST_NAME_RX.search(f"{ch.get('name','')} {ch.get('group','')}"))


def build_guide_indexes(guide_channels):
    """Pre-index guide channels by normalized name.

    Returns:
        norm_index: {normalized_name: set(guide_ids)}  (a set, so we can tell
                    when a name is ambiguous and refuse to guess)
        norm_pairs: [(normalized_name, guide_id, display_name)]  (for suggestions)
    """
    norm_index = {}
    norm_pairs = []
    for gid, info in guide_channels.items():
        for candidate in (info.get("name", ""), gid):
            n = normalize_name(candidate)
            if n:
                norm_index.setdefault(n, set()).add(gid)
                norm_pairs.append((n, gid, info.get("name", gid)))
                # "Starz Cinema East" is what a plain "Starz Cinema" means;
                # West/Pacific feeds are NOT folded in, so they stay separate
                if n.endswith("east") and len(n) > 4:
                    norm_index.setdefault(n[:-4], set()).add(gid)
    return norm_index, norm_pairs


def schedule_fingerprint(progs, n=4):
    """The next few (start, title) pairs from now — two guide entries with the
    same fingerprint are the same channel (e.g. 'TLC' and 'TLC HD')."""
    now = datetime.now(timezone.utc)
    upcoming = [p for p in progs if (parse_xmltv_time(p.get("stop")) or now) > now]
    # start time + first word of the title: services word titles differently
    # ("Court TV" vs "Court TV Live", "Bones: The Final Chapter" vs "Bones")
    def first_word(t):
        w = re.findall(r"[a-z0-9]+", (t or "").lower())
        return w[0] if w else ""
    return tuple((p.get("start", "")[:12], first_word(p.get("title"))) for p in upcoming[:n])


def is_synthetic_id(tvg):
    return bool(re.fullmatch(r"ch\d+", tvg or ""))


def smart_match(ch, has_data, guide_channels, aliases, norm_index, norm_pairs, fingerprint=None):
    """Find the guide id whose listings cover NOW for this channel.

    has_data(gid) -> True only if that guide channel has listings that have not
    run out yet. Returns (guide_id_or_None, method, suggestion_or_None, note).
    """
    tvg = ch.get("tvgId", "")
    if is_synthetic_id(tvg):
        tvg = ""            # our own ch<N> placeholder id, not a real guide id
    name = ch.get("name", "")
    fast = looks_fast(ch)
    note = ""

    # 1. exact tvg-id
    if tvg and has_data(tvg):
        return tvg, "exact", None, ""
    if tvg and tvg in guide_channels:
        note = "expired"   # right id, but its listings have run out

    # 2. @variant-stripped id
    if tvg and "@" in tvg:
        stripped = tvg.split("@")[0]
        if has_data(stripped):
            return stripped, "variant", None, ""

    # 3. alias table (your remembered picks + built-ins; FAST picks kept separate)
    alias_hit = aliases.get(alias_key(name, fast))
    if alias_hit and has_data(alias_hit):
        return alias_hit, "alias", None, ""

    # 4. normalized-name exact match, source-aware:
    #    cable stream -> cable guides only;  FAST stream -> FAST guides only,
    #    preferring the guide from the same service the stream comes from.
    n = normalize_name(name)
    if n and n not in norm_index and NAME_ALIASES.get(n) in norm_index:
        n = NAME_ALIASES[n]
    if n and n in norm_index:
        live = sorted(g for g in norm_index[n] if has_data(g))
        kind = lambda g: guide_channels.get(g, {}).get("kind", "cable")
        prov = lambda g: guide_channels.get(g, {}).get("provider", "")
        if fast:
            sp = stream_provider(ch)
            same_service = [g for g in live if sp and prov(g) == sp]
            pool = same_service or [g for g in live if kind(g) == "fast"]
            method = "same-service" if same_service else "fast-guide"
        else:
            pool = [g for g in live if kind(g) != "fast"]
            method = "normalized"
            if not pool:
                # name exists ONLY in FAST guides -> it's a FAST channel whose
                # host we didn't recognise; no cable schedule to confuse it with
                pool = [g for g in live if kind(g) == "fast"]
                method = "fast-guide"
        same = len(pool) > 1 and fingerprint and len({fingerprint(g) for g in pool}) == 1
        if pool and (len(pool) == 1 or same):
            return pool[0], method, None, ""
        if live:
            # suggest the entry whose name matches most literally (AMC, not AMC+)
            want = name.strip().lower()
            cands = pool or live
            g = sorted(cands, key=lambda x: (guide_channels.get(x, {}).get("name", "").strip().lower() != want, x))[0]
            disp = guide_channels.get(g, {}).get("name", g)
            why = "ambiguous" if pool else "fast"
            return None, "", (g, disp, 1.0), why

    # 5. fuzzy — suggestion only, never auto-applied
    best_gid, best_name, best_score = None, None, 0.0
    if n:
        for gn, gid, disp in norm_pairs:
            if not has_data(gid):
                continue
            if fast and guide_channels.get(gid, {}).get("kind", "cable") != "fast":
                continue   # never suggest a cable schedule for a FAST stream
            score = SequenceMatcher(None, n, gn).ratio()
            if score > best_score:
                best_gid, best_name, best_score = gid, disp, score
    if best_gid and best_score >= FUZZY_AUTO_ACCEPT and not fast:
        return best_gid, "fuzzy", None, ""
    if best_gid and best_score >= FUZZY_SUGGEST:
        return None, "", (best_gid, best_name, round(best_score, 2)), note or "fuzzy"

    return None, "", None, note


# ── playlist parsing ────────────────────────────────────────────────────────
def parse_playlist(path):
    channels = []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    cur = None
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF"):
            def g(rx):
                m = re.search(rx, line)
                return m.group(1) if m else ""
            name_match = re.search(r",(.+)$", line)
            cur = {
                "name": name_match.group(1).strip() if name_match else "Unknown",
                "tvgId": g(r'tvg-id="([^"]+)"'),
                "logo": g(r'tvg-logo="([^"]+)"'),
                "chno": g(r'tvg-chno="([^"]+)"'),
                "group": g(r'group-title="([^"]+)"'),
            }
        elif line.startswith("http") and cur:
            cur["url"] = line
            channels.append(cur)
            cur = None
    return channels


# ── XML helpers ─────────────────────────────────────────────────────────────
def xml_escape(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def xmltv_date(dt):
    return dt.strftime("%Y%m%d%H%M%S") + " +0000"


def serialize_programme(prog, ch_id, fallback_name=""):
    lines = []
    title = prog["title"] or fallback_name
    lines.append(f'  <programme start="{prog["start"]}" stop="{prog["stop"]}" channel="{xml_escape(ch_id)}">')
    lines.append(f'    <title lang="en">{xml_escape(title)}</title>')
    if prog["desc"]:
        lines.append(f'    <desc lang="en">{xml_escape(prog["desc"])}</desc>')
    if prog["category"]:
        lines.append(f'    <category lang="en">{xml_escape(prog["category"])}</category>')
    if prog["icon"]:
        lines.append(f'    <icon src="{xml_escape(prog["icon"])}"/>')
    if prog["episode"]:
        lines.append(f'    <episode-num system="{xml_escape(prog["episode_sys"])}">{xml_escape(prog["episode"])}</episode-num>')
    lines.append('  </programme>')
    return lines


def synthetic_id(ch, i):
    """The ch<N> id used for channels without a tvg-id. Must match server.py."""
    try:
        n = int(ch.get("chno") or i + 1)
    except ValueError:
        n = i + 1
    return f"ch{n}"


def build_merged_epg(playlist_channels, all_programmes, guide_channels, alias_path=None, gmap=None):
    """Build epg.xml. Real programmes where a trusted match covers NOW, a
    placeholder (the channel's own name) otherwise. Returns (xml_text, stats)."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        '<tv generator-info-name="IPTV Curator (consolidated) EPG Engine">',
    ]
    now_exact = datetime.now(timezone.utc)
    now = now_exact.replace(minute=0, second=0, microsecond=0)

    aliases = load_aliases(alias_path)
    norm_index, norm_pairs = build_guide_indexes(guide_channels)

    # which guide channels still have listings that haven't run out
    cov = {gid: coverage_until(p) for gid, p in all_programmes.items()}
    def has_data(gid):
        c = cov.get(gid)
        return bool(c and c > now_exact)

    # dedupe by tvg-id (synthetic ch<N> id when none present); remember every
    # stream URL that shares each id so results can be reported per URL
    seen, urls_for = {}, {}
    for i, ch in enumerate(playlist_channels):
        ch_id = ch["tvgId"] or synthetic_id(ch, i)
        seen.setdefault(ch_id, ch)
        urls_for.setdefault(ch_id, []).append(ch.get("url", ""))

    for ch_id, ch in seen.items():
        lines.append(f'  <channel id="{xml_escape(ch_id)}">')
        lines.append(f'    <display-name>{xml_escape(ch["name"])}</display-name>')
        if ch.get("chno"):
            lines.append(f'    <display-name>{xml_escape(ch["chno"])}</display-name>')
        if ch.get("logo"):
            lines.append(f'    <icon src="{xml_escape(ch["logo"])}"/>')
        lines.append('  </channel>')

    stats = {"matched": 0, "stubbed": 0, "by_method": {}, "suggestions": [],
             "channel_results": {}, "channel_results_by_url": {}}

    gmap = gmap or {}
    for ch_id, ch in seen.items():
        # The channel's published id (ch_id) and its guide choice are separate:
        # guide-map.json says which guide to use and how it was chosen.
        entry = gmap.get(ch_id)
        mode = (entry or {}).get("mode", "auto")
        if entry is not None:
            ch = dict(ch, tvgId=(entry.get("guide") or entry.get("source_id") or ""))
        if mode == "placeholder":
            gid, method, suggestion, note = None, "", None, "placeholder"
        elif mode == "pick":
            g = ch["tvgId"]
            if g and has_data(g):
                gid, method, suggestion, note = g, "your pick", None, ""
            else:
                gid, method, suggestion, note = None, "", None, ("expired" if g in guide_channels else "")
        else:
            gid, method, suggestion, note = smart_match(
                ch, has_data, guide_channels, aliases, norm_index, norm_pairs,
                fingerprint=lambda g: schedule_fingerprint(all_programmes.get(g, [])))
        if gid:
            until = cov.get(gid)
            res = {"state": "matched", "method": method, "guide_id": gid,
                   "guide_name": guide_channels.get(gid, {}).get("name", gid),
                   "guide_source": guide_channels.get(gid, {}).get("source", ""),
                   "until": until.isoformat() if until else None}
            stats["matched"] += 1
            stats["by_method"][method] = stats["by_method"].get(method, 0) + 1
            if method not in ("exact", "variant"):
                log(f'  Matched "{ch["name"]}" -> {gid} via {method}')
            for prog in all_programmes[gid]:
                lines.extend(serialize_programme(prog, ch_id, ch["name"]))
        else:
            stats["stubbed"] += 1
            has_real_id = bool(ch.get("tvgId")) and not is_synthetic_id(ch.get("tvgId"))
            if note == "expired":
                state = "stub_expired"
            elif note == "placeholder":
                state = "stub_chosen"
            else:
                state = "stub_with_id" if has_real_id else "stub_no_id"
            res = {"state": state, "reason": note,
                   "suggestion": suggestion[0] if suggestion else None,
                   "suggestion_name": suggestion[1] if suggestion else None,
                   "suggestion_source": guide_channels.get(suggestion[0], {}).get("source", "") if suggestion else ""}
            if suggestion:
                stats["suggestions"].append({
                    "channel": ch["name"], "channel_id": ch_id,
                    "urls": urls_for.get(ch_id, []),
                    "guide_id": suggestion[0], "guide_name": suggestion[1],
                    "score": suggestion[2], "reason": note,
                    "guide_source": guide_channels.get(suggestion[0], {}).get("source", ""),
                })
            clean = re.sub(r"\s*\(.*?\)\s*", "", ch["name"]).strip()
            for h in range(STUB_DAYS * 24):
                start = now + timedelta(hours=h * STUB_HOURS)
                stop = now + timedelta(hours=(h + 1) * STUB_HOURS)
                lines.append(f'  <programme start="{xmltv_date(start)}" stop="{xmltv_date(stop)}" channel="{xml_escape(ch_id)}">')
                lines.append(f'    <title lang="en">{xml_escape(clean)}</title>')
                lines.append(f'    <desc lang="en">Live stream of {xml_escape(clean)}</desc>')
                if ch.get("group") and ch["group"] != "Undefined":
                    lines.append(f'    <category lang="en">{xml_escape(ch["group"])}</category>')
                lines.append('  </programme>')
        res["channel_id"] = ch_id
        stats["channel_results"][ch_id] = res
        for u in urls_for.get(ch_id, []):
            if u:
                stats["channel_results_by_url"][u] = res

    lines.append("</tv>")
    log(f"EPG built: {stats['matched']} real, {stats['stubbed']} stub "
        f"(methods: {stats['by_method']}, {len(stats['suggestions'])} suggestions)")
    return "\n".join(lines), stats


def make_matcher(all_programmes, guide_channels, alias_path=None):
    """Return match(ch) -> {state, guide_id, guide_name, guide_source} using the
    same rules as a rebuild. Used by Discover to tell you, BEFORE you add a
    channel, whether it will get real guide data."""
    now_exact = datetime.now(timezone.utc)
    aliases = load_aliases(alias_path)
    norm_index, norm_pairs = build_guide_indexes(guide_channels)
    cov = {gid: coverage_until(p) for gid, p in all_programmes.items()}
    has_data = lambda g: bool(cov.get(g) and cov[g] > now_exact)
    fp = lambda g: schedule_fingerprint(all_programmes.get(g, []))
    def match(ch):
        gid, method, sugg, note = smart_match(ch, has_data, guide_channels, aliases,
                                              norm_index, norm_pairs, fingerprint=fp)
        if gid:
            info = guide_channels.get(gid, {})
            return {"state": "guide", "guide_id": gid, "guide_name": info.get("name", gid),
                    "guide_source": info.get("source", ""), "method": method}
        if sugg:
            info = guide_channels.get(sugg[0], {})
            return {"state": "maybe", "guide_id": sugg[0], "guide_name": sugg[1],
                    "guide_source": info.get("source", ""), "reason": note}
        return {"state": "none"}
    return match


def save_channels_json(out_path, all_programmes, *channel_dicts):
    """Write the guide-channel list the match window reads. Each entry says
    whether that guide channel has current listings and until when."""
    now = datetime.now(timezone.utc)
    ch_list, seen = [], set()
    for d in channel_dicts:
        for cid, info in d.items():
            if cid in seen:
                continue
            seen.add(cid)
            until = coverage_until(all_programmes.get(cid, []))
            ch_list.append({
                "id": cid, "name": info["name"], "logo": info.get("icon", ""),
                "country": "US", "source": info.get("source", "unknown"),
                "kind": info.get("kind", "cable"),
                "has_data": bool(until and until > now),
                "until": until.isoformat() if until else None,
            })
    ch_list.sort(key=lambda c: c["name"].lower())
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(ch_list, f, ensure_ascii=False)
    log(f"Saved {len(ch_list)} guide channels to {os.path.basename(out_path)}")


# ── main merge (importable + CLI) ───────────────────────────────────────────
def download_all_sources(sources=None):
    """Fetch every ENABLED guide source once. Returns
    (all_programmes, guide_channels, source_status_list)."""
    sources = sources or DEFAULT_SOURCES
    all_programmes, guide_channels, statuses = {}, {}, []
    log("Downloading guide sources...")
    for src in sources:
        if not src.get("enabled", True):
            statuses.append({"key": src["key"], "label": src["label"], "ok": None,
                             "disabled": True, "channels": 0, "with_listings": 0})
            continue
        chans, progs, st = load_source(src)
        statuses.append(st)
        for cid, plist in progs.items():          # earlier sources win on id clashes
            all_programmes.setdefault(cid, plist)
        for cid, info in chans.items():
            guide_channels.setdefault(cid, info)
    tidy_programmes(all_programmes)
    log(f"Combined guide: {len(all_programmes)} channels with listings from "
        f"{sum(1 for s in statuses if s.get('ok'))} sources")
    return all_programmes, guide_channels, statuses


def build_profile(playlist_path, epg_out_path, channels_json_path, alias_path,
                  all_programmes, guide_channels, _unused=None, guide_map_path=None):
    """Build one profile's epg.xml from already-downloaded guide data.
    All paths are passed explicitly — nothing shared between profiles."""
    if not os.path.exists(playlist_path):
        log(f"  (profile) no playlist at {playlist_path} — skipping")
        return {"ok": False, "error": "playlist missing"}

    playlist_channels = parse_playlist(playlist_path)
    log(f"  Profile playlist: {len(playlist_channels)} channels")

    save_channels_json(channels_json_path, all_programmes, guide_channels)

    gmap = {}
    if guide_map_path and os.path.exists(guide_map_path):
        try:
            with open(guide_map_path, "r", encoding="utf-8") as f:
                gmap = json.load(f)
        except Exception as e:
            log(f"  WARNING: could not read guide map: {e}")
    xml_out, stats = build_merged_epg(playlist_channels, all_programmes,
                                      guide_channels, alias_path, gmap)
    tmp = epg_out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(xml_out)
    os.replace(tmp, epg_out_path)   # atomic: clients never read a half-written file
    log(f"  Wrote {epg_out_path} ({os.path.getsize(epg_out_path):,} bytes)")

    return {
        "ok": True,
        "playlist_channels": len(playlist_channels),
        "matched": stats["matched"],
        "stubbed": stats["stubbed"],
        "by_method": stats["by_method"],
        "suggestions": stats["suggestions"],
        "channel_results": stats["channel_results"],
        "channel_results_by_url": stats["channel_results_by_url"],
        "guide_channels": len(all_programmes),
        "epg_bytes": os.path.getsize(epg_out_path),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def search_guide(query, data_dir=None, limit=12):
    """Search the saved guide-channel list, returning candidates WITH confidence
    scores (0-100). Powers the match modal's ranked 'did you mean?' list."""
    cj = os.path.join(data_dir, "epgtalk-channels.json") if data_dir else CHANNELS_JSON
    try:
        with open(cj, "r", encoding="utf-8") as f:
            guide = json.load(f)
    except Exception:
        return []
    qn = normalize_name(query)
    scored = []
    for g in guide:
        best = max(
            SequenceMatcher(None, qn, normalize_name(g["name"])).ratio(),
            SequenceMatcher(None, qn, normalize_name(g["id"])).ratio(),
        )
        scored.append((round(best * 100), g))
    # entries with current listings first, then by score
    scored.sort(key=lambda x: (x[1].get("has_data", True), x[0]), reverse=True)
    return [{"score": s, **g} for s, g in scored[:limit]]


def run_merge():
    """Single-profile merge (backward compatible). Uses module-level paths."""
    t0 = time.time()
    log("=" * 60)
    log("EPG merge starting")
    log("=" * 60)
    if not os.path.exists(PLAYLIST_FILE):
        log(f"ERROR: Playlist not found at {PLAYLIST_FILE}")
        return {"ok": False, "error": "playlist missing"}
    all_programmes, guide_channels, _ = download_all_sources()
    result = build_profile(PLAYLIST_FILE, EPG_OUTPUT, CHANNELS_JSON, ALIAS_FILE,
                           all_programmes, guide_channels)
    log(f"Done in {time.time() - t0:.1f}s")
    return result


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    result = run_merge()
    sys.exit(0 if result.get("ok") else 1)
