"""
SoundCloud discovery digest.

Finds new tracks - including flips/remixes/bootlegs surfaced via reposts -
from two sources:
  1. Accounts you follow: via your personalized /stream, which includes
     both their uploads and anything they repost (this is where flips
     usually show up - someone reposting a track they found).
  2. Owners of tracks you've liked but don't follow: SoundCloud's stream
     only reflects people you follow, so for these we check each artist's
     own upload history directly.

SoundCloud's unofficial API (there's no official developer registration
open to new apps) blocks automated writes like playlist creation behind a
CAPTCHA, so instead of a playlist this generates a static digest page
(written to _site/index.html) that a GitHub Actions workflow publishes to
GitHub Pages.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import escape

CLIENT_ID = os.environ["SOUNDCLOUD_CLIENT_ID"]
OAUTH_TOKEN = os.environ["SOUNDCLOUD_OAUTH_TOKEN"]
API = "https://api-v2.soundcloud.com"

LOOKBACK_DAYS = 14
STATE_PRUNE_DAYS = 180
STREAM_MAX_PAGES = 10
EXTRA_ARTIST_WORKERS = 10

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "soundcloud_state.json")
SITE_DIR = os.path.join(BASE_DIR, "_site")


def log(msg):
    print(msg, flush=True)


def _request(url):
    req = urllib.request.Request(url, headers={"Authorization": f"OAuth {OAUTH_TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def api_get(path, params=None):
    params = dict(params or {})
    params["client_id"] = CLIENT_ID
    return _request(f"{API}{path}?{urllib.parse.urlencode(params)}")


def api_get_next(next_href):
    url = next_href
    if "client_id=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}client_id={CLIENT_ID}"
    return _request(url)


def paginate(path, params=None, max_pages=None):
    """Yields every item across all pages of a SoundCloud collection endpoint."""
    data = api_get(path, params)
    pages = 0
    while True:
        pages += 1
        for item in data.get("collection", []):
            yield item
        next_href = data.get("next_href")
        if not next_href or (max_pages and pages >= max_pages):
            return
        data = api_get_next(next_href)


def parse_dt(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"seen_tracks": {}}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def prune_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STATE_PRUNE_DAYS)).isoformat()
    state["seen_tracks"] = {
        tid: seen for tid, seen in state["seen_tracks"].items() if seen >= cutoff
    }


def get_me():
    return api_get("/me")


def get_followings(user_id):
    users = {}
    for u in paginate(f"/users/{user_id}/followings", {"limit": 200}):
        users[u["id"]] = u
    return users


def get_liked_track_owners(user_id, following_ids):
    owners = {}
    for item in paginate(f"/users/{user_id}/track_likes", {"limit": 200}):
        track = item.get("track")
        if not track:
            continue
        owner = track.get("user") or {}
        oid = owner.get("id")
        if oid and oid not in following_ids:
            owners[oid] = owner
    return owners


def normalize_track(track, uploader=None, reposted_by=None, source="stream"):
    uploader = uploader or track.get("user") or {}
    return {
        "id": track["id"],
        "title": track.get("title") or "Untitled",
        "permalink_url": track.get("permalink_url"),
        "artwork_url": track.get("artwork_url") or uploader.get("avatar_url"),
        "uploader": uploader.get("username") or "Unknown",
        "genre": (track.get("genre") or "").strip(),
        "created_at": track.get("created_at"),
        "reposted_by": reposted_by,
        "source": source,
    }


def get_stream_tracks(cutoff):
    found = []
    pages = 0
    data = api_get("/stream", {"limit": 50})
    while True:
        pages += 1
        stop = False
        for item in data.get("collection", []):
            created = item.get("created_at")
            if not created:
                continue
            if parse_dt(created) < cutoff:
                stop = True
                break
            track = item.get("track")
            if not track:
                continue  # skip playlists/playlist-reposts
            reposted_by = None
            if item.get("type") == "track-repost":
                reposted_by = (item.get("user") or {}).get("username")
            found.append(normalize_track(track, reposted_by=reposted_by, source="stream"))
        next_href = data.get("next_href")
        if stop or not next_href or pages >= STREAM_MAX_PAGES:
            break
        data = api_get_next(next_href)
    return found


def get_extra_artist_tracks(owner, cutoff):
    found = []
    try:
        data = api_get(f"/users/{owner['id']}/tracks", {"limit": 20})
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        log(f'WARNING: could not fetch tracks for "{owner.get("username")}": {e}')
        return found
    for track in data.get("collection", []):
        created = track.get("created_at")
        if not created or parse_dt(created) < cutoff:
            continue
        found.append(normalize_track(track, uploader=owner, source="liked_artist"))
    return found


def render_html(new_tracks, generated_at):
    genres_present = sorted({t["bucket"] for t in new_tracks})

    def track_card(t):
        embed_src = (
            "https://w.soundcloud.com/player/?url="
            + urllib.parse.quote(t["permalink_url"], safe="")
            + "&color=%23ff7a00&auto_play=false&hide_related=true"
            "&show_comments=false&show_user=true&show_reposts=false&visual=false"
        )
        repost_badge = (
            f'<span class="badge">via {escape(t["reposted_by"])}</span>'
            if t["reposted_by"]
            else ""
        )
        genre_badge = f'<span class="badge badge-genre">{escape(t["bucket"])}</span>' if t["bucket"] else ""
        return f"""
        <article class="card" data-genre="{escape(t['bucket'])}">
          <div class="card-head">
            <h3>{escape(t['title'])}</h3>
            <div class="meta">{escape(t['uploader'])} {repost_badge} {genre_badge}</div>
          </div>
          <iframe loading="lazy" width="100%" height="120" scrolling="no" frameborder="no"
            src="{embed_src}"></iframe>
          <a class="open-link" href="{escape(t['permalink_url'])}" target="_blank" rel="noopener">Open in SoundCloud &#8599;</a>
        </article>"""

    cards_html = "\n".join(track_card(t) for t in new_tracks)

    chips_html = '<button class="chip active" data-filter="all">All</button>' + "".join(
        f'<button class="chip" data-filter="{escape(g)}">{escape(g)}</button>' for g in genres_present
    )

    empty_state = (
        ""
        if new_tracks
        else '<p class="empty">Nothing new since last check. Check back tomorrow.</p>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daily SoundCloud Discover</title>
<style>
  :root {{
    color-scheme: dark light;
    --bg: #0d0d10;
    --card: #17171c;
    --text: #f2f2f2;
    --muted: #9a9aa5;
    --accent: #ff7a00;
    --border: #26262e;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg: #f7f7f9;
      --card: #ffffff;
      --text: #17171c;
      --muted: #6b6b76;
      --border: #e4e4ea;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 24px 16px 60px;
  }}
  header {{
    max-width: 1100px;
    margin: 0 auto 24px;
  }}
  h1 {{
    margin: 0 0 4px;
    font-size: 28px;
  }}
  .subtitle {{
    color: var(--muted);
    font-size: 14px;
  }}
  .chips {{
    max-width: 1100px;
    margin: 20px auto 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }}
  .chip {{
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 14px;
    border-radius: 999px;
    font-size: 13px;
    cursor: pointer;
  }}
  .chip.active {{
    background: var(--accent);
    border-color: var(--accent);
    color: #1a1a1a;
    font-weight: 600;
  }}
  .grid {{
    max-width: 1100px;
    margin: 20px auto 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
  }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px;
    overflow: hidden;
  }}
  .card-head h3 {{
    margin: 0 0 4px;
    font-size: 16px;
    line-height: 1.3;
  }}
  .meta {{
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 10px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }}
  .badge {{
    background: var(--border);
    color: var(--text);
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
  }}
  .badge-genre {{
    background: var(--accent);
    color: #1a1a1a;
    font-weight: 600;
  }}
  iframe {{
    border-radius: 8px;
    display: block;
  }}
  .open-link {{
    display: inline-block;
    margin-top: 10px;
    color: var(--accent);
    text-decoration: none;
    font-size: 13px;
    font-weight: 600;
  }}
  .open-link:hover {{ text-decoration: underline; }}
  .empty {{
    max-width: 1100px;
    margin: 60px auto;
    text-align: center;
    color: var(--muted);
  }}
</style>
</head>
<body>
  <header>
    <h1>Daily SoundCloud Discover</h1>
    <div class="subtitle">{len(new_tracks)} new track(s) &middot; last updated {escape(generated_at)}</div>
  </header>
  <div class="chips">{chips_html}</div>
  <main class="grid">
    {cards_html}
  </main>
  {empty_state}
  <script>
    document.querySelectorAll('.chip').forEach(chip => {{
      chip.addEventListener('click', () => {{
        document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        const filter = chip.dataset.filter;
        document.querySelectorAll('.card').forEach(card => {{
          card.style.display = (filter === 'all' || card.dataset.genre === filter) ? '' : 'none';
        }});
      }});
    }});
  </script>
</body>
</html>
"""


def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    state = load_state()
    prune_state(state)

    me = get_me()
    user_id = me["id"]
    log(f"Authenticated as {me.get('username')}")

    log("Fetching followings...")
    followings = get_followings(user_id)
    log(f"  {len(followings)} followings")

    log("Fetching liked-track owners not already followed...")
    extra_owners = get_liked_track_owners(user_id, set(followings.keys()))
    log(f"  {len(extra_owners)} extra artists from liked tracks")

    log("Checking your stream (uploads + reposts from people you follow)...")
    stream_tracks = get_stream_tracks(cutoff)
    log(f"  {len(stream_tracks)} tracks/reposts in stream within lookback window")

    log("Checking extra artists' upload history...")
    extra_tracks = []
    with ThreadPoolExecutor(max_workers=EXTRA_ARTIST_WORKERS) as executor:
        futures = [
            executor.submit(get_extra_artist_tracks, owner, cutoff)
            for owner in extra_owners.values()
        ]
        for future in as_completed(futures):
            extra_tracks.extend(future.result())
    log(f"  {len(extra_tracks)} new tracks from extra artists")

    all_tracks = {}
    for t in stream_tracks + extra_tracks:
        if str(t["id"]) in state["seen_tracks"]:
            continue
        all_tracks[t["id"]] = t  # last write wins if duplicate (e.g. reposted + own upload)

    new_tracks = sorted(all_tracks.values(), key=lambda t: t["created_at"], reverse=True)
    for t in new_tracks:
        t["bucket"] = t["genre"].strip().title() if t["genre"].strip() else "Other"

    log(f"{len(new_tracks)} new track(s) total")

    today = datetime.now(timezone.utc).date().isoformat()
    for t in new_tracks:
        state["seen_tracks"][str(t["id"])] = today
    save_state(state)

    os.makedirs(SITE_DIR, exist_ok=True)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_out = render_html(new_tracks, generated_at)
    with open(os.path.join(SITE_DIR, "index.html"), "w") as f:
        f.write(html_out)

    log("Wrote digest page.")


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        log(f"Missing required environment variable: {e}")
        sys.exit(1)
