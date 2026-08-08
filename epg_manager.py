#!/usr/bin/env python3
"""
EPG Manager for IPTV Curator
=============================
Downloads EPG data from multiple sources (EPGTalk + GlobeTV), merges real
programme data with stub entries for unmatched channels, and writes a
single epg.xml.

Also extracts combined channel lists as JSON for the IPTV Curator tool's
EPG matching modal.

EPG Sources:
  - EPGTalk (Schedules Direct IDs): ~694 US channels, IDs like I520.59961.schedulesdirect.org
  - GlobeTV (iptv-org IDs): US channels across 6 files, IDs like ESPN.us, CBSNews.us

Run manually or via cron:
    python3 /media/colby/NAS3/iptvcurator/epg_manager.py

Cron (daily at 3 AM — keeps EPG fresh and prevents stale data):
    0 3 * * * python3 /media/colby/NAS3/iptvcurator/epg_manager.py >> /tmp/epg_manager.log 2>&1
"""

import gzip
import json
import os
import re
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────
WORK_DIR       = "/media/colby/NAS3/iptvcurator"
PLAYLIST_FILE  = os.path.join(WORK_DIR, "playlist.m3u")
EPG_OUTPUT     = os.path.join(WORK_DIR, "epg.xml")
CHANNELS_JSON  = os.path.join(WORK_DIR, "epgtalk-channels.json")  # keep filename for backward compat
STUB_DAYS      = 7        # days of stub programme data for unmatched channels
STUB_HOURS     = 1        # hours per stub programme block

# EPGTalk — Schedules Direct IDs (e.g. I520.59961.schedulesdirect.org)
EPGTALK_URL = "https://raw.githubusercontent.com/acidjesuz/EPGTalk/master/US_guide.xml.gz"

# GlobeTV — iptv-org IDs (e.g. ESPN.us, CBSNews.us)
# 6 files covering different sets of US channels, updated daily at 3 AM UTC
GLOBETV_URLS = [
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa1.xml",
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa2.xml",
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa3.xml",
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa4.xml",
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa5.xml",
    "https://raw.githubusercontent.com/globetvapp/epg/main/Usa/usa6.xml",
]
# ─────────────────────────────────────────────────────────────────────────────


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def download_url(url, timeout=120, is_gzip=False):
    """Download a URL and return the text content. Returns None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "EPGManager/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if is_gzip:
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8")
        return text
    except Exception as e:
        log(f"  ERROR downloading {url}: {e}")
        return None


def parse_xmltv(xml_text, source_label="unknown"):
    """Parse any XMLTV XML into channel dict and programme dict.

    Returns:
        channels:   {id: {id, name, icon, source}}
        programmes: {channel_id: [ET.Element, ...]}
    """
    root = ET.fromstring(xml_text)

    channels = {}
    for ch_el in root.findall("channel"):
        ch_id = ch_el.get("id", "")
        if not ch_id:
            continue
        names = ch_el.findall("display-name")
        name = names[0].text if names else ch_id
        icon_el = ch_el.find("icon")
        icon = icon_el.get("src", "") if icon_el is not None else ""
        channels[ch_id] = {"id": ch_id, "name": name, "icon": icon, "source": source_label}

    programmes = {}
    for prog_el in root.findall("programme"):
        ch_id = prog_el.get("channel", "")
        if ch_id:
            programmes.setdefault(ch_id, []).append(prog_el)

    return channels, programmes


# ── Source: EPGTalk ──────────────────────────────────────────────────────────

def download_epgtalk():
    """Download and decompress EPGTalk's US guide."""
    log(f"Downloading EPGTalk guide...")
    xml_text = download_url(EPGTALK_URL, is_gzip=True)
    if xml_text:
        log(f"  EPGTalk: {len(xml_text):,} bytes decompressed")
    return xml_text


# ── Source: GlobeTV ──────────────────────────────────────────────────────────

def download_globetv():
    """Download all GlobeTV US XML files and merge them.

    Returns merged (channels, programmes) or (None, None) on total failure.
    """
    log(f"Downloading GlobeTV guides ({len(GLOBETV_URLS)} files)...")
    all_channels = {}
    all_programmes = {}
    ok_count = 0

    for i, url in enumerate(GLOBETV_URLS, 1):
        fname = url.split("/")[-1]
        log(f"  [{i}/{len(GLOBETV_URLS)}] {fname}...")
        xml_text = download_url(url, timeout=90)
        if not xml_text:
            log(f"  SKIP {fname} — download failed")
            continue

        try:
            channels, programmes = parse_xmltv(xml_text, source_label="GlobeTV")
            # Merge — first file wins for channel info, but programmes accumulate
            for ch_id, ch_info in channels.items():
                if ch_id not in all_channels:
                    all_channels[ch_id] = ch_info
            for ch_id, prog_list in programmes.items():
                all_programmes.setdefault(ch_id, []).extend(prog_list)
            ok_count += 1
            log(f"  {fname}: {len(channels)} channels, {len(programmes)} with programmes")
        except ET.ParseError as e:
            log(f"  SKIP {fname} — XML parse error: {e}")

    if ok_count == 0:
        log("  WARNING: All GlobeTV downloads failed")
        return None, None

    log(f"  GlobeTV total: {len(all_channels)} channels, "
        f"{sum(len(v) for v in all_programmes.values())} programmes from {ok_count} files")
    return all_channels, all_programmes


# ── Channel list JSON for Curator tool ───────────────────────────────────────

def save_channels_json(epgtalk_channels, globetv_channels):
    """Save combined channel list as JSON for the IPTV Curator tool's matching modal.

    Each entry includes a 'source' field so the Curator can show where it came from.
    """
    ch_list = []
    seen = set()

    # EPGTalk channels first
    for ch_id, info in epgtalk_channels.items():
        if ch_id not in seen:
            seen.add(ch_id)
            ch_list.append({
                "id": ch_id,
                "name": info["name"],
                "logo": info.get("icon", ""),
                "country": "US",
                "source": "EPGTalk"
            })

    # GlobeTV channels — add any not already covered
    for ch_id, info in globetv_channels.items():
        if ch_id not in seen:
            seen.add(ch_id)
            ch_list.append({
                "id": ch_id,
                "name": info["name"],
                "logo": info.get("icon", ""),
                "country": "US",
                "source": "GlobeTV"
            })

    ch_list.sort(key=lambda c: c["name"].lower())
    with open(CHANNELS_JSON, "w", encoding="utf-8") as f:
        json.dump(ch_list, f, ensure_ascii=False)

    epgtalk_count = sum(1 for c in ch_list if c["source"] == "EPGTalk")
    globetv_count = sum(1 for c in ch_list if c["source"] == "GlobeTV")
    log(f"Saved {len(ch_list)} channels to {CHANNELS_JSON} "
        f"(EPGTalk: {epgtalk_count}, GlobeTV: {globetv_count})")


# ── Playlist parser ──────────────────────────────────────────────────────────

def parse_playlist(path):
    """Parse M3U playlist and return list of channel dicts."""
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


# ── XML helpers ──────────────────────────────────────────────────────────────

def xml_escape(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def xmltv_date(dt):
    return dt.strftime("%Y%m%d%H%M%S") + " +0000"


# ── EPG builder ──────────────────────────────────────────────────────────────

def serialize_programme(prog_el, ch_id, fallback_name=""):
    """Convert an ET programme element to XML string lines."""
    lines = []
    start = prog_el.get("start", "")
    stop = prog_el.get("stop", "")

    title_el = prog_el.find("title")
    title = title_el.text if title_el is not None and title_el.text else fallback_name

    desc_el = prog_el.find("desc")
    desc = desc_el.text if desc_el is not None and desc_el.text else ""

    cat_el = prog_el.find("category")
    category = cat_el.text if cat_el is not None and cat_el.text else ""

    icon_el = prog_el.find("icon")
    icon_src = icon_el.get("src", "") if icon_el is not None else ""

    episode_el = prog_el.find("episode-num")
    episode = episode_el.text if episode_el is not None and episode_el.text else ""
    episode_sys = episode_el.get("system", "") if episode_el is not None else ""

    lines.append(f'  <programme start="{start}" stop="{stop}" channel="{xml_escape(ch_id)}">')
    lines.append(f'    <title lang="en">{xml_escape(title)}</title>')
    if desc:
        lines.append(f'    <desc lang="en">{xml_escape(desc)}</desc>')
    if category:
        lines.append(f'    <category lang="en">{xml_escape(category)}</category>')
    if icon_src:
        lines.append(f'    <icon src="{xml_escape(icon_src)}"/>')
    if episode:
        lines.append(f'    <episode-num system="{xml_escape(episode_sys)}">{xml_escape(episode)}</episode-num>')
    lines.append(f'  </programme>')
    return lines


def build_merged_epg(playlist_channels, all_programmes):
    """Build merged EPG XML: real data where IDs match any source, stubs otherwise.

    all_programmes is a merged dict {channel_id: [ET.Element, ...]} from all sources.
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        '<tv generator-info-name="IPTV Curator EPG Manager v2">',
    ]

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    matched = 0
    stubbed = 0

    # Deduplicate channels by tvg-id (same as the tool does)
    seen_ids = {}
    for i, ch in enumerate(playlist_channels):
        ch_id = ch["tvgId"] or f"ch{int(ch.get('chno') or i + 1)}"
        if ch_id not in seen_ids:
            seen_ids[ch_id] = ch

    # Write channel blocks
    for ch_id, ch in seen_ids.items():
        lines.append(f'  <channel id="{xml_escape(ch_id)}">')
        lines.append(f'    <display-name>{xml_escape(ch["name"])}</display-name>')
        if ch.get("chno"):
            lines.append(f'    <display-name>{xml_escape(ch["chno"])}</display-name>')
        if ch.get("logo"):
            lines.append(f'    <icon src="{xml_escape(ch["logo"])}"/>')
        lines.append(f'  </channel>')

    # Write programme blocks
    for ch_id, ch in seen_ids.items():
        if ch_id in all_programmes and all_programmes[ch_id]:
            # Real data from one of the sources
            matched += 1
            for prog_el in all_programmes[ch_id]:
                lines.extend(serialize_programme(prog_el, ch_id, ch["name"]))
        else:
            # Stub data — channel name repeating every hour for STUB_DAYS
            stubbed += 1
            clean_name = re.sub(r'\s*\(.*?\)\s*', '', ch["name"]).strip()
            total_hours = STUB_DAYS * 24
            for h in range(total_hours):
                start = now + timedelta(hours=h * STUB_HOURS)
                stop = now + timedelta(hours=(h + 1) * STUB_HOURS)
                lines.append(f'  <programme start="{xmltv_date(start)}" stop="{xmltv_date(stop)}" channel="{xml_escape(ch_id)}">')
                lines.append(f'    <title lang="en">{xml_escape(clean_name)}</title>')
                lines.append(f'    <desc lang="en">Live stream of {xml_escape(clean_name)}</desc>')
                if ch.get("group") and ch["group"] != "Undefined":
                    lines.append(f'    <category lang="en">{xml_escape(ch["group"])}</category>')
                lines.append(f'  </programme>')

    lines.append('</tv>')

    log(f"EPG built: {matched} channels with real data, {stubbed} with stubs")
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("EPG Manager v2 starting")
    log("=" * 60)

    # Check playlist exists
    if not os.path.exists(PLAYLIST_FILE):
        log(f"ERROR: Playlist not found at {PLAYLIST_FILE}")
        sys.exit(1)

    # Parse playlist
    playlist_channels = parse_playlist(PLAYLIST_FILE)
    log(f"Playlist has {len(playlist_channels)} channels")

    # ── Source 1: EPGTalk ──
    epgtalk_channels = {}
    epgtalk_programmes = {}
    epgtalk_xml = download_epgtalk()
    if epgtalk_xml:
        try:
            epgtalk_channels, epgtalk_programmes = parse_xmltv(epgtalk_xml, "EPGTalk")
            log(f"EPGTalk: {len(epgtalk_channels)} channels, "
                f"{sum(len(v) for v in epgtalk_programmes.values())} programmes")
        except ET.ParseError as e:
            log(f"ERROR parsing EPGTalk XML: {e}")
    else:
        log("WARNING: Could not download EPGTalk")

    # ── Source 2: GlobeTV ──
    globetv_channels, globetv_programmes = download_globetv()
    if globetv_channels is None:
        globetv_channels = {}
        globetv_programmes = {}

    # ── Save combined channel list JSON ──
    save_channels_json(epgtalk_channels, globetv_channels)

    # ── Merge all programmes (EPGTalk first, GlobeTV fills gaps) ──
    all_programmes = {}
    # EPGTalk data takes priority
    for ch_id, progs in epgtalk_programmes.items():
        all_programmes[ch_id] = progs
    # GlobeTV fills in channels that EPGTalk doesn't have
    for ch_id, progs in globetv_programmes.items():
        if ch_id not in all_programmes:
            all_programmes[ch_id] = progs

    log(f"Merged programmes: {len(all_programmes)} channels with real data "
        f"(EPGTalk: {len(epgtalk_programmes)}, "
        f"GlobeTV added: {len(all_programmes) - len(epgtalk_programmes)})")

    # ── Build merged EPG ──
    merged = build_merged_epg(playlist_channels, all_programmes)

    # Write output
    with open(EPG_OUTPUT, "w", encoding="utf-8") as f:
        f.write(merged)
    log(f"Wrote {EPG_OUTPUT} ({os.path.getsize(EPG_OUTPUT):,} bytes)")

    log("Done!")


if __name__ == "__main__":
    main()
