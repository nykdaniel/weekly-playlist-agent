"""
Weekly "outside the loop" discovery.

The daily agent (main.py) only sees what Spotify itself surfaces. This script finds
artists from outside that loop and appends a few tracks each to the playlist
"New Finds - Outside the Loop":

  1. Seeds: artists behind the tracks in your "ecstatic tracks" playlist. Seeds are
     rotated (least recently used first) so the whole playlist gets covered over time.
     Artists you have promoted from New Finds into ecstatic tracks count triple.
  2. Candidates:
       - similar artists of each seed (Last.fm if LASTFM_API_KEY is set, otherwise
         ListenBrainz, which needs no key)
       - fresh releases from Bandcamp discover pages for a rotating set of genre tags
  3. Filters: drop anyone already in ecstatic tracks / New Finds, anyone you removed
     from New Finds earlier, and anyone previously rejected.
  4. Verification on Spotify: exact artist-name match, genre overlap with your seeds
     (or an exact release-name match when Spotify has no genre tags), and a follower
     cap so it stays "outside the loop". Ambiguous names are skipped, not guessed.
  5. Adds up to MAX_NEW_ARTISTS artists with 2-3 tracks each.

Feedback loop (read from the playlists, no manual input needed):
  - an artist from New Finds that shows up in ecstatic tracks becomes a heavy seed
  - an artist whose tracks you deleted from New Finds is never suggested again

Environment: SPOTIFY_CLIENT_ID, SPOTIFY_REFRESH_TOKEN (SPOTIFY_CLIENT_SECRET optional),
LASTFM_API_KEY optional, MAX_NEW_ARTISTS optional. Pass --dry-run to change nothing.
Stdlib only.
"""

import base64
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, timedelta

TARGET_PLAYLIST = "New Finds - Outside the Loop"
SEED_PLAYLIST = "ecstatic tracks"
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outside_state.json")

MAX_NEW_ARTISTS = int(os.environ.get("MAX_NEW_ARTISTS") or 15)
TRACKS_PER_ARTIST = 3
SEEDS_PER_RUN = 40
PROMOTED_SEEDS_PER_RUN = 10
SIMILAR_PER_SEED = 25
BANDCAMP_TAGS_PER_RUN = 6
BANDCAMP_RESULTS_PER_TAG = 40
MAX_FOLLOWERS = 150000        # above this an artist is already inside the loop
REJECT_AFTER_DAYS = 14        # deleted from New Finds this long after adding = rejected
UNSURE_RETRY_DAYS = 60
CHECK_BUDGET = MAX_NEW_ARTISTS * 6

BANDCAMP_TAGS = [
    "organic-house", "ecstatic-dance", "handpan", "downtempo", "psychill", "tribal-house",
    "afro-house", "world-fusion", "shamanic", "ethnic-electronica", "psybass", "glitch-hop",
    "dub", "deep-house", "bass-music", "melodic-house", "electrocumbia", "dub-techno",
    "jungle", "latin-house", "tribal", "psydub", "chillout", "world",
]

UA = "OutsideTheLoop/1.0 (personal playlist agent)"


def log(msg):
    print(msg, flush=True)


def norm(name):
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ø", "o")
    return re.sub(r"[^a-z0-9]+", "", s)


# ---------------------------------------------------------------- http

def http(method, url, body=None, headers=None, form=False, retries=4):
    data = None
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.code == 429 or e.code >= 500:
                wait = int(e.headers.get("Retry-After") or 2 ** attempt) + 1
                time.sleep(min(wait, 30))
                continue
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw[:200].decode("utf-8", "replace")}
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 ** attempt)
    return 0, {}


# ---------------------------------------------------------------- spotify

class Spotify:
    def __init__(self):
        cid = os.environ["SPOTIFY_CLIENT_ID"]
        secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        headers = {}
        body = {"grant_type": "refresh_token",
                "refresh_token": os.environ["SPOTIFY_REFRESH_TOKEN"], "client_id": cid}
        if secret:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()
        code, tok = http("POST", "https://accounts.spotify.com/api/token", body, headers, form=True)
        if code != 200:
            raise SystemExit(f"Spotify token refresh failed: {code} {tok}")
        self.token = tok["access_token"]
        self.me = self.get("/me")["id"]

    def call(self, method, path, body=None):
        url = path if path.startswith("http") else "https://api.spotify.com/v1" + path
        return http(method, url, body, {"Authorization": "Bearer " + self.token})

    def get(self, path):
        code, data = self.call("GET", path)
        if code != 200:
            raise RuntimeError(f"GET {path} -> {code} {data}")
        return data

    def pages(self, path):
        sep = "&" if "?" in path else "?"
        url = f"{path}{sep}limit=50" if "/me/playlists" in path else f"{path}{sep}limit=100"
        while url:
            code, data = self.call("GET", url)
            if code != 200:
                raise RuntimeError(f"GET {url} -> {code} {data}")
            for it in data.get("items", []):
                yield it
            url = data.get("next")

    def find_playlist(self, name):
        for p in self.pages("/me/playlists"):
            if p and p["name"].strip().casefold() == name.casefold() and p["owner"]["id"] == self.me:
                return p
        return None

    def playlist_tracks(self, pid):
        out = []
        for path in (f"/playlists/{pid}/items", f"/playlists/{pid}/tracks"):
            try:
                for it in self.pages(path):
                    t = it.get("track") or it.get("item")
                    if t and t.get("id"):
                        out.append(t)
                return out
            except RuntimeError:
                out = []
        raise RuntimeError(f"cannot read playlist {pid}")

    def add(self, pid, uris):
        for i in range(0, len(uris), 50):
            chunk = uris[i:i + 50]
            code, data = self.call("POST", f"/playlists/{pid}/items", {"uris": chunk})
            if code >= 400:
                code, data = self.call("POST", f"/playlists/{pid}/tracks", {"uris": chunk})
            if code >= 400:
                raise RuntimeError(f"add failed {code} {data}")

    def artists(self, ids):
        """Full artist objects (genres, followers). Batch endpoint first, singles as fallback."""
        out = {}
        for i in range(0, len(ids), 50):
            code, data = self.call("GET", "/artists?ids=" + ",".join(ids[i:i + 50]))
            if code == 200:
                for a in data.get("artists", []):
                    if a:
                        out[a["id"]] = a
            else:
                for aid in ids[i:i + 50]:
                    c, a = self.call("GET", f"/artists/{aid}")
                    if c == 200:
                        out[aid] = a
        return out

    def search(self, q, typ, limit=10):
        code, data = self.call("GET", f"/search?type={typ}&limit={limit}&q=" + urllib.parse.quote(q))
        return data if code == 200 else {}


# ---------------------------------------------------------------- state

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            s = json.load(f)
    else:
        s = {}
    for k, v in (("added", {}), ("rejected", {}), ("unsure", {}), ("promoted", {}),
                 ("seed_last_used", {}), ("mbid", {}), ("tag_cursor", 0), ("runs", [])):
        s.setdefault(k, v)
    return s


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------- similar-artist sources

def lastfm_similar(name, key):
    q = urllib.parse.urlencode({"method": "artist.getsimilar", "artist": name, "api_key": key,
                                "format": "json", "limit": SIMILAR_PER_SEED, "autocorrect": 1})
    code, data = http("GET", "https://ws.audioscrobbler.com/2.0/?" + q)
    if code != 200:
        return []
    return [(a["name"], float(a.get("match") or 0)) for a in data.get("similarartists", {}).get("artist", [])]


def mbid_for(name, state):
    key = norm(name)
    if key in state["mbid"]:
        return state["mbid"][key]
    time.sleep(1.1)  # MusicBrainz asks for max 1 request/second
    q = urllib.parse.quote(f'artist:"{name}"')
    code, data = http("GET", f"https://musicbrainz.org/ws/2/artist/?query={q}&fmt=json&limit=3")
    mbid = None
    if code == 200:
        for a in data.get("artists", []):
            if a.get("score", 0) >= 90 and norm(a.get("name")) == key:
                mbid = a["id"]
                break
    state["mbid"][key] = mbid
    return mbid


def listenbrainz_similar(name, state):
    mbid = mbid_for(name, state)
    if not mbid:
        return []
    algo = "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
    code, data = http("GET", "https://labs.api.listenbrainz.org/similar-artists/json?"
                      + urllib.parse.urlencode({"artist_mbids": mbid, "algorithm": algo}))
    if code != 200 or not isinstance(data, list) or not data:
        return []
    top = max(d.get("score", 0) for d in data) or 1
    return [(d["name"], d.get("score", 0) / top) for d in data[:SIMILAR_PER_SEED] if d.get("name")]


def bandcamp_tag(tag):
    body = {"category_id": 0, "tag_norm_names": [tag], "geoname_id": 0, "slice": "new",
            "cursor": "*", "size": BANDCAMP_RESULTS_PER_TAG, "include_result_types": ["a"]}
    code, data = http("POST", "https://bandcamp.com/api/discover/1/discover_web", body)
    if code != 200:
        return []
    out = []
    for r in data.get("results", []):
        artist = r.get("album_artist") or r.get("band_name")
        if artist and "," not in artist and " / " not in artist:
            out.append((artist.strip(), r.get("title") or ""))
    return out


# ---------------------------------------------------------------- core

def pick_tracks(sp, artist, hint):
    pool = {}
    queries = [f'artist:"{artist["name"]}"']
    if hint:
        queries.append(f'{artist["name"]} {hint}')
    for q in queries:
        for t in sp.search(q, "track").get("tracks", {}).get("items", []) or []:
            if t and artist["id"] in [a["id"] for a in t["artists"]]:
                pool[t["id"]] = t
    hint_n = norm(hint)

    def rank(t):
        album = norm(t["album"]["name"])
        return (0 if hint_n and hint_n in album else 1,
                1 if re.search(r"remix|mixed|\bedit\b", t["name"], re.I) else 0,
                0 if t["artists"][0]["id"] == artist["id"] else 1,
                len(t["artists"]))

    picks, seen = [], set()
    for t in sorted(pool.values(), key=rank):
        base = re.sub(r" - .*", "", t["name"]).lower()
        if base in seen:
            continue
        seen.add(base)
        picks.append(t)
        if len(picks) == TRACKS_PER_ARTIST:
            break
    release_match = bool(hint_n) and any(hint_n in norm(t["album"]["name"]) for t in picks)
    return picks, release_match


def verify(sp, name, hint, allowed_genres):
    # allowed_genres: genres the matched Spotify artist must overlap with
    """Returns (artist, tracks, reason). artist is None when the match is not trustworthy."""
    items = sp.search(f'artist:"{name}"', "artist").get("artists", {}).get("items", []) or []
    if not items:
        items = sp.search(name, "artist").get("artists", {}).get("items", []) or []
    exact = [a for a in items if a and norm(a["name"]) == norm(name)]
    if not exact:
        return None, [], "no exact-name artist on Spotify"
    exact.sort(key=lambda a: -((a.get("followers") or {}).get("total") or 0))
    with_genre = [a for a in exact if set(a.get("genres") or []) & allowed_genres]
    if with_genre:
        artist = with_genre[0]
        if len(with_genre) > 1:
            f0 = (with_genre[0].get("followers") or {}).get("total") or 0
            f1 = (with_genre[1].get("followers") or {}).get("total") or 0
            if f0 < 5 * max(f1, 1):
                return None, [], "several same-name artists with matching genres"
    elif len(exact) == 1 and not exact[0].get("genres"):
        artist = exact[0]
    else:
        return None, [], "same-name artist(s) with unrelated genres"
    if ((artist.get("followers") or {}).get("total") or 0) > MAX_FOLLOWERS:
        return None, [], "already mainstream (follower cap)"
    tracks, release_match = pick_tracks(sp, artist, hint)
    if not tracks:
        return None, [], "no tracks found"
    if not artist.get("genres") and not release_match:
        return None, [], "no genre tags and no release-name match"
    return artist, tracks, "genre match" if artist.get("genres") else "release-name match"


def main():
    dry = "--dry-run" in sys.argv
    today = date.today()
    state = load_state()
    sp = Spotify()
    lastfm_key = os.environ.get("LASTFM_API_KEY", "")
    log(f"Similar-artist source: {'Last.fm' if lastfm_key else 'ListenBrainz'}; dry-run={dry}")

    seed_pl = sp.find_playlist(SEED_PLAYLIST)
    target_pl = sp.find_playlist(TARGET_PLAYLIST)
    if not seed_pl or not target_pl:
        raise SystemExit(f"Missing playlist: seed={bool(seed_pl)} target={bool(target_pl)}")

    # --- what the user already has
    seed_counts, seed_artist_ids = Counter(), {}
    for t in sp.playlist_tracks(seed_pl["id"]):
        for a in t["artists"]:
            seed_counts[a["name"]] += 1
            seed_artist_ids[a["name"]] = a["id"]
    ecstatic_norm = {norm(n) for n in seed_counts}
    target_tracks = sp.playlist_tracks(target_pl["id"])
    target_uris = {t["uri"] for t in target_tracks}
    target_norm = {norm(a["name"]) for t in target_tracks for a in t["artists"]}
    log(f"{len(seed_counts)} artists in '{SEED_PLAYLIST}', {len(target_norm)} in '{TARGET_PLAYLIST}'")

    # --- feedback from what the user did since last run
    for key, rec in list(state["added"].items()):
        if key in ecstatic_norm and key not in state["promoted"]:
            state["promoted"][key] = {"name": rec["name"], "date": today.isoformat()}
            log(f"  promoted into ecstatic tracks: {rec['name']}")
        age = (today - date.fromisoformat(rec["date"])).days
        if age >= REJECT_AFTER_DAYS and key not in state["rejected"] and not (set(rec["uris"]) & target_uris):
            state["rejected"][key] = {"name": rec["name"], "date": today.isoformat()}
            log(f"  removed by you, will not suggest again: {rec['name']}")

    unsure_cut = (today - timedelta(days=UNSURE_RETRY_DAYS)).isoformat()
    exclude = ecstatic_norm | target_norm | set(state["rejected"]) | set(state["added"]) | {
        k for k, v in state["unsure"].items() if v["date"] >= unsure_cut}

    # --- allowed genres: those shared by at least 2 of the user's top seed artists
    top_names = [n for n, _ in seed_counts.most_common(150)]
    full = sp.artists([seed_artist_ids[n] for n in top_names])
    gcount = Counter(g for a in full.values() for g in a.get("genres", []))
    allowed = {g for g, c in gcount.items() if c >= 2}
    log(f"{len(allowed)} allowed genres derived from seeds")

    # --- choose seeds (promoted first, then least-recently-used)
    weight = {n: c * (3 if norm(n) in state["promoted"] else 1) for n, c in seed_counts.items()}
    promoted = [n for n in seed_counts if norm(n) in state["promoted"]]
    promoted_names = {norm(p["name"]): p["name"] for p in state["promoted"].values()}
    seeds = list(dict.fromkeys(promoted + list(promoted_names.values())))[:PROMOTED_SEEDS_PER_RUN]
    rest = sorted((n for n in seed_counts if n not in seeds),
                  key=lambda n: (state["seed_last_used"].get(norm(n), "0000"), -weight[n]))
    seeds += rest[:SEEDS_PER_RUN - len(seeds)]

    # --- gather candidates
    cands = {}  # norm -> {name, score, hint, sources}

    def bump(name, score, hint, source, genres=None):
        key = norm(name)
        if not key or key in exclude:
            return
        c = cands.setdefault(key, {"name": name, "score": 0.0, "hint": "", "sources": set(),
                                   "genres": set()})
        c["score"] += score
        c["genres"] |= set(genres or [])
        c["sources"].add(source)
        if hint and not c["hint"]:
            c["hint"] = hint

    need = [seed_artist_ids[n] for n in seeds if seed_artist_ids[n] not in full]
    full.update(sp.artists(need))
    for n in seeds:
        sgen = set(full.get(seed_artist_ids[n], {}).get("genres", [])) & allowed
        sims = lastfm_similar(n, lastfm_key) if lastfm_key else listenbrainz_similar(n, state)
        for rank, (sim, m) in enumerate(sims):
            bump(sim, m * min(weight.get(n, 1), 9) ** 0.5 / (1 + rank / 15.0), "", "similar", sgen)
        state["seed_last_used"][norm(n)] = today.isoformat()
    log(f"{len(cands)} candidates from similar-artist lookups on {len(seeds)} seeds")

    start = state["tag_cursor"] % len(BANDCAMP_TAGS)
    tags = [BANDCAMP_TAGS[(start + i) % len(BANDCAMP_TAGS)] for i in range(BANDCAMP_TAGS_PER_RUN)]
    state["tag_cursor"] = (start + BANDCAMP_TAGS_PER_RUN) % len(BANDCAMP_TAGS)
    for tag in tags:
        for artist, release in bandcamp_tag(tag):
            bump(artist, 1.0, release, "bandcamp:" + tag)
        time.sleep(0.5)
    log(f"{len(cands)} candidates after Bandcamp tags {tags}")

    # --- verify on Spotify and add
    ranked = sorted(cands.values(), key=lambda c: (-len(c["sources"]), -c["score"]))
    added, skipped = [], []
    for c in ranked[:CHECK_BUDGET]:
        if len(added) >= MAX_NEW_ARTISTS:
            break
        only_similar = all(x == "similar" for x in c["sources"])
        artist, tracks, reason = verify(
            sp, c["name"], c["hint"], c["genres"] if only_similar and c["genres"] else allowed)
        key = norm(c["name"])
        if not artist:
            state["unsure"][key] = {"name": c["name"], "date": today.isoformat(), "why": reason}
            skipped.append((c["name"], reason))
            continue
        if key in {norm(a["name"]) for a, _, _ in added} or artist["id"] in {a["id"] for a, _, _ in added}:
            continue
        added.append((artist, tracks, sorted(c["sources"])))

    log(f"\nVerified {len(added)} new artists ({len(skipped)} skipped as unsure):")
    for artist, tracks, src in added:
        log(f"  + {artist['name']} [{', '.join(artist.get('genres') or ['no genre tags'])}] "
            f"<- {', '.join(src)}: " + " | ".join(t["name"] for t in tracks))

    if added and not dry:
        sp.add(target_pl["id"], [t["uri"] for _, tracks, _ in added for t in tracks])
        for artist, tracks, src in added:
            state["added"][norm(artist["name"])] = {
                "name": artist["name"], "date": today.isoformat(),
                "uris": [t["uri"] for t in tracks], "sources": src}
        state["runs"] = (state["runs"] + [{"date": today.isoformat(), "added": len(added)}])[-52:]
        log(f"Added {sum(len(t) for _, t, _ in added)} tracks to '{TARGET_PLAYLIST}'.")
    if not dry:
        save_state(state)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## Outside the loop: {len(added)} new artists\n\n")
            for artist, tracks, src in added:
                f.write(f"- **{artist['name']}** ({', '.join(src)}): " + ", ".join(t["name"] for t in tracks) + "\n")
            f.write(f"\nSkipped as unsure: {len(skipped)}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        log(f"Missing required environment variable: {e}")
        sys.exit(1)
