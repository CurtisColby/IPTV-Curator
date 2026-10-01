# IPTV Curator

A self-hosted app for building a clean, English-language IPTV lineup from free public sources — with real TV-guide data — and serving it to NostalgiaTV (or any IPTV player) as a playlist URL and an XMLTV guide URL.

One folder, one command: `docker compose up -d`. Then open `http://<server>:8787`.

> **Looking for the original single-file version** (open the HTML in a browser, no server)? It's preserved as the [`v1-standalone`](../../releases/tag/v1-standalone) release.

## Getting started

1. Install Docker with the Compose plugin on your server.
2. Download this repository and open `docker-compose.yml`. Adjust the port and the `data` volume path if you like.
3. Start it:
   ```
   docker compose up -d
   ```
4. Open `http://<server>:8787`. The first start downloads the TV guides, which takes about a minute.
5. Go to **✨ Discover** → **Look for new channels now**, or use **Scan a playlist**. Add channels to My List, then press **💾 Save & Rebuild**.
6. In your IPTV player, add the playlist URL and the XMLTV guide URL shown under **Use in NostalgiaTV**.

## What it does

- **Discover** — watches trusted free playlists (Samsung TV Plus, Plex, Pluto TV, Roku Channel, iptv-org, Free-TV). Every night it downloads them, keeps English channels you don't already have, tests that each one really plays, and checks whether it will get guide data. You browse the results and press **+ Add** or **✕ Not interested**.
- **Scan a playlist** — paste any M3U URL or load a file, test every stream, and add what you like.
- **My List** — your lineup, stored on the server per profile. Filters for *✓ Guide*, *Needs guide* and *Dead*. Click a channel name to preview it.
- **Guide data** — merges several XMLTV guides (EPGTalk, epgshare01, DirecTV scraper, and the Samsung/Roku/Plex/Pluto FAST guides). Matching is source-aware: a free-streaming channel never gets the cable network's schedule. Anything uncertain becomes a suggestion you review; anything with no guide shows the channel name as a placeholder.
- **"On now"** — the player, the guide picker and the suggestions show what each guide says is airing, so you can compare it against the live stream before choosing.
- **Permanent channel ids** — each channel's id in the playlist and guide never changes (guide picks, renumbering and stream replacements keep it), so NostalgiaTV bindings stay valid.
- **Self-healing** — a nightly check retests My List; when a channel dies and a working copy exists in another source, a **↻ Replace** button swaps it in with one click.
- **Profiles** — separate lineups, each at `/<profile>/playlist.m3u` and `/<profile>/epg.xml`.

## Addresses

| What | URL |
|---|---|
| App | `http://<server>:8787/` |
| Playlist | `http://<server>:8787/<profile>/playlist.m3u` (`main` by default) |
| TV guide (XMLTV) | `http://<server>:8787/<profile>/epg.xml` |

## Schedule (automatic)

| Job | When |
|---|---|
| Re-download guides + rebuild every profile | every 3 h (and at startup) — `IPTV_GUIDE_REFRESH_HOURS` |
| Discover + My List health check | nightly, 08:15 UTC — `IPTV_NIGHTLY_HOUR_UTC` |
| DirecTV scraper (separate container) | its own schedule, writes `data/directv/guide.xml` |

## Layout

```
iptv-curator-app/
├── docker-compose.yml           # app + DirecTV scraper (ghcr.io/iptv-org/epg)
├── app/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── server.py                # FastAPI: UI, hosted files, API, scheduler, Discover, self-healing
│   ├── epg_engine.py            # guide download, matching, epg.xml builder
│   └── iptv-checker.html        # the whole UI (single file, no build step)
├── directv-config/channels.xml  # channels the DirecTV scraper fetches (premium networks)
└── data/                        # created on first run — NOT in git (your lists, guides, backups)
```

## License

MIT — do whatever you want with it.

## Notes

- Everything in `data/` is personal (your lists, choices, backups) and is excluded by `.gitignore`.
- Sources are free, ad-supported (FAST) or free-to-air channels. Availability changes often; Discover and the nightly check keep up with it.
