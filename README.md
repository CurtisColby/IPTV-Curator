[README.md](https://github.com/user-attachments/files/30395286/README.md)
# IPTV Curator

A single-file browser tool for finding, previewing, and building clean IPTV playlists. No install, no server, no dependencies — just open the HTML file and go.

Built to solve one problem: free IPTV playlists have hundreds of channels, most of them dead, foreign, or geo-blocked. This tool lets you check them all at once, preview the ones that work, hand-pick the best, and export a clean `.m3u` ready to drop into any IPTV player.

![IPTV Curator screenshot](screenshot.png)

---

## Features

- **Stream checker** — tests every channel concurrently with configurable timeout and worker count, flags each as Live, Dead, or 🚫 Geo-blocked
- **Built-in playlist directory** — 30+ popular free playlists pre-loaded in a dropdown (Pluto TV, Tubi, Roku, Samsung, LG, Plex, IPTV-org categories, and more)
- **In-page video player** — preview any channel without leaving the tool; player stays pinned at the top as you scroll through the list
- **Audio track detection** — reads HLS manifests and shows all available audio tracks as clickable pills; auto-switches to English if detected
- **English filter** — multi-layer detection using language tags, country codes, non-Latin script detection, and channel name keywords
- **Resolution filter** — filter by SD / HD (720p+) / FHD (1080p+) / 4K based on channel name tags
- **Geo-block detection** — HTTP 403/451/407 responses are flagged separately so you can hide or review them
- **Starring** — star any channel to instantly add it to My List; unstar to remove
- **Checkboxes + bulk add** — check multiple channels and hit "+ My list" to add them all at once; "All live" selects every working channel in one click
- **My List** — a persistent curated list that survives page refresh and browser close (stored in localStorage)
- **Import** — load a previously exported `.m3u` back into My List to pick up where you left off across sessions
- **Export** — export My List or starred-only as a clean `.m3u` file with all original metadata preserved
- **Hosting helper** — built-in "Host it" guide for serving your playlist as a URL (for IPTV players that don't accept files)
- **Confirm English / Exclude** — manual override buttons in the player for channels the filter gets wrong

---

## Getting started

1. Download `iptv-checker.html`
2. Open it in any modern browser (Chrome, Firefox, Edge) — no web server needed
3. Pick a playlist from the dropdown or paste a URL into the box
4. Hit **Check** and wait for the scan to finish
5. Preview channels with ▶, star or check the ones you want
6. Hit **+ My list** to add selected channels, then **Export .m3u** when you're done

---

## Interface overview

### Left panel — channel list

| Control | What it does |
|---|---|
| Playlist dropdown | Pre-loaded list of popular free M3U sources — pick one to fill the URL box |
| URL box | Paste any `.m3u` URL here |
| Check / Stop | Start or stop the stream checker |
| English filter | Filters channels to English-likely ones before checking (see [English filtering](#english-filtering)) |
| Hide geo-blocked | Hides channels that returned a geo-block response during checking |
| Res filter | Show only SD / HD+ / FHD+ channels based on name tags |
| Timeout | How long to wait per stream before marking it dead (7s is a good default) |
| Workers | How many streams to check simultaneously (6 default, raise to 10–15 for speed) |
| Search | Live search across channel name, country, language, and group |
| Filter tabs | All / Live / ★ Starred / 🚫 Geo / Excluded |
| All live | Checks every live channel's checkbox at once |
| None | Unchecks all |
| + My list | Adds all checked channels to My List |

### Right panel — player + My List

| Control | What it does |
|---|---|
| ▶ button | Preview the channel in the built-in player |
| ★ button | Star a channel — automatically adds it to My List |
| 🔄 button | Re-check a single stream without re-running the full scan |
| Audio track pills | Shows audio tracks from the HLS manifest; click to switch; English tracks highlighted green |
| ✓ Confirm English | Marks a channel as confirmed English (green left border on row) |
| ✕ Exclude | Hides a channel from the list and prevents it being added to My List |
| Export .m3u | Downloads My List as a dated `.m3u` file |
| ★ Starred | Downloads only starred channels from My List |
| Import | Load a previous export back into My List — merges without duplicates |
| Host it | Guide for serving the playlist as a URL your IPTV player can point to |

---

## Building a master list across multiple sessions

My List is saved in your browser's `localStorage`, so it persists between sessions. The intended workflow for building up a master list over time:

1. Load playlist A → scan → preview → star/select what you want → they go into My List
2. Close the tool, come back later — My List is still there
3. Load playlist B → add more channels to the same My List
4. Repeat across as many playlists as you want
5. When done, **Export .m3u** — one file with everything combined

To back up your list mid-way (recommended), hit **Export** and save the file. Next session, use **Import** to load it back in and pick up where you left off.

---

## Hosting your playlist as a URL

Most IPTV players (TiviMate, NostalgiaTV, Smarters, etc.) need a URL, not a file. The **Host it** button inside the tool covers three options in detail, but the short version for a home server running Docker:

**1. Export your list** and save it as `playlist.m3u` somewhere on your server.

**2. Add this to your `docker-compose.yml`:**

```yaml
services:
  iptv-playlist:
    image: python:3-alpine
    volumes:
      - /path/to/your/playlist:/data
    working_dir: /data
    command: python3 -m http.server 8765
    ports:
      - "8765:8765"
    restart: unless-stopped
```

**3. Start it:**
```bash
docker compose up -d iptv-playlist
```

**4. Point your IPTV player to:**
```
http://YOUR_SERVER_IP:8765/playlist.m3u
```

To update the playlist later, just overwrite the `.m3u` file — your IPTV player will pick up changes on its next refresh. No container restart needed.

**Alternatives:**
- **GitHub Gist** — paste the raw M3U content, set the filename to `playlist.m3u`, create a public gist, click Raw, use that URL
- **Local DNS** — if you run a local DNS server (e.g. Technitium), create an A record like `iptv.home` pointing to your server for a cleaner URL

---

## English filtering

The English filter runs before checking starts and uses several layers in order:

1. **Explicit language tag** — `tvg-language="English"` / `eng` / `en` → accepted
2. **Known English country codes** — US, GB, CA, AU, NZ, IE, ZA, plus Caribbean and African English-speaking countries → accepted
3. **Non-Latin script in channel name** — Cyrillic, Arabic, Chinese, Korean, Japanese, Thai, Hebrew, etc. → rejected
4. **Non-English language keywords in name** — Hindi, Arabic, French, Spanish, German, etc. → rejected
5. **English word match** — name contains words like `news`, `movies`, `channel`, `network`, `BBC`, `ESPN`, etc. → accepted
6. **No signals either way** — accepted (permissive fallback for untagged channels)

The filter is a best-effort heuristic — some channels will be wrong. Use the **✓ Confirm English** and **✕ Exclude** buttons in the player to manually correct mistakes as you preview.

---

## Geo-block detection

During the stream check, HTTP responses are inspected:

| Status | Meaning | Tagged as |
|---|---|---|
| 200, 206 | Stream reachable | Live |
| 301, 302 | Redirect (usually still works) | Live |
| 403 | Forbidden — often a geo-block | 🚫 Geo |
| 451 | Unavailable for legal reasons | 🚫 Geo |
| 407 | Proxy authentication required | 🚫 Geo |
| Timeout / other error | Stream unreachable | Dead |

Note: some geo-blocks return a `200` with an error video instead of a proper status code — those will appear as Live and only reveal themselves when you try to preview them. The **✕ Exclude** button handles those.

---

## Resolution filtering

Resolution is detected from the channel name and group title. The filter looks for common tags:

| Filter | Matches |
|---|---|
| All | Everything |
| HD+ | `HD`, `720p`, `HQ`, `FHD`, `1080p`, `4K`, `UHD` — plus untagged channels |
| FHD+ | `FHD`, `1080p`, `4K`, `UHD` — plus untagged channels |
| SD only | `SD`, `480p`, `360p`, `LOW` — plus untagged channels |

Untagged channels (no resolution in the name) always pass through to avoid accidentally hiding valid channels.

---

## CORS and network notes

The tool runs entirely in your browser. Because of browser security rules (CORS), fetching streams from a local file requires streams to have CORS headers — most public IPTV streams do.

- **Default (no proxy)** — works well when running from a local machine on the same network as your streams, or for streams from permissive CDNs
- **CORS proxy** — tick "CORS proxy" to route requests through `corsproxy.io` if streams aren't reachable directly; useful for some restricted sources but adds latency and is rate-limited
- If a playlist URL itself fails to fetch, try toggling the CORS proxy option

---

## Included playlist sources

All sources in the dropdown are free, publicly available, and legal (free-to-air / ad-supported):

| Source | Notes |
|---|---|
| **IPTV-org** | Largest public collection, ~42k channels, updated daily. Organized by category and country |
| **Pluto TV** | Ad-supported, US and international versions |
| **The Roku Channel** | Ad-supported, US |
| **Tubi TV** | Ad-supported, US |
| **Plex TV** | Ad-supported, US |
| **Samsung TV Plus** | US geo-locked |
| **LG Channels** | US geo-locked |
| **Vizio WatchFree** | US geo-locked |
| **XUMO** | US geo-locked |
| **Local Now** | US geo-locked |
| **Amazon Fire TV** | US geo-locked |
| **DistroTV** | International, no geo-lock |
| **Xiaomi TV** | International, no geo-lock |
| **Vidaa / Hisense** | International, no geo-lock |
| **Rakuten TV** | UK |
| **Free-TV** | Multi-region community list |

Stream availability changes frequently — if a source isn't working, the stream hosts may have changed their URLs.

---

## Tips

- Start with **IPTV-org · USA** or **Pluto TV (USA)** for the best English channel density
- Run the check first, then switch to the **Live** filter tab before previewing — saves a lot of time
- The **All live** button + **+ My list** is the fastest way to grab everything that works from a playlist, then you can remove what you don't want from My List
- Increase Workers to 15 and Timeout to 10s for large playlists (1000+ channels) to balance speed vs accuracy
- Export regularly as a backup — localStorage can be cleared by browser settings

---

## License

MIT — do whatever you want with it.
