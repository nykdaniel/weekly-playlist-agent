"""
Weekly genre lanes: finds small, good artists per genre and fills one playlist per lane
("New Finds - Grime", "New Finds - Dubstep", ...).

Same machinery as outside_loop.py (imported from it): similar-artist lookups from hand-picked
seed artists + Bandcamp tag pages, then Spotify verification (exact name, genre overlap,
follower cap). On top of that each lane enforces a quality filter: the artist must have
several releases and at least one in the last RECENT_YEARS years.

Feedback loop per lane (read from the lane playlist):
  - tracks you delete 14+ days after they were added -> artist never suggested again
  - artists that show up in "ecstatic tracks" -> extra seeds for their lane

Environment: SPOTIFY_CLIENT_ID, SPOTIFY_REFRESH_TOKEN (SPOTIFY_CLIENT_SECRET optional),
LASTFM_API_KEY optional, MAX_NEW_PER_LANE optional.
Flags: --dry-run (change nothing), --lane grime (only that lane), --seeds-only.
"""

import json
import os
import sys
import time
from collections import Counter
from datetime import date, timedelta

import outside_loop as ol
from outside_loop import Spotify, http, listenbrainz_similar, lastfm_similar, log, norm, verify

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lanes_state.json")
SEED_PLAYLIST = "ecstatic tracks"
MAX_NEW_PER_LANE = int(os.environ.get("MAX_NEW_PER_LANE") or 5)
RECENT_YEARS = 3
MIN_RELEASES = 3
TAGS_PER_RUN = 3
MAX_FOLLOWERS = 150000
MIN_FOLLOWERS = 100           # below this it is usually a bulk uploader, not a real following

LANES = {
    "grime": {
        "playlist": "New Finds - Grime",
        "genre_words": ["grime"],
        "tags": ["grime", "instrumental-grime", "grime-instrumentals", "uk-grime", "eskibeat", "uk-bass"],
        "seeds": ["Mr Mitch", "Logos", "Murlo", "Novelist", "Flowdan", "Preditah", "Terror Danjah",
                  "Spooky", "Visionist", "Faze Miyake", "Jammer", "JME", "Wiley", "Footsie"],
    },
    "global-bass": {
        "playlist": "New Finds - Global Bass",
        "genre_words": ["global bass", "moombahton", "tropical bass", "cumbia", "baile", "kuduro", "batida",
                        "gqom", "dancehall", "electro latino", "latin electronic"],
        "tags": ["global-bass", "moombahton", "tropical-bass", "baile-funk", "kuduro", "electro-cumbia",
                 "gqom", "batida", "digital-cumbia"],
        "seeds": ["Dengue Dengue Dengue", "Chancha Via Circuito", "Ghetto Kumbé", "Nguzunguzu", "DJ Python",
                  "Uproot Andy", "El Búho", "Systema Solar", "Frikstailers", "DJ Marfox", "Nídia",
                  "Tribilin Sound", "Zuli", "Los Rakas"],
    },
    "dubstep": {
        "playlist": "New Finds - Dubstep",
        "genre_words": ["dubstep"],
        "tags": ["dubstep", "deep-dubstep", "140", "post-dubstep", "bass-music", "dub"],
        "seeds": ["Mala", "Digital Mystikz", "Kode9", "Loefah", "Coki", "Silkie", "Commodo", "Distance",
                  "Kahn", "Pangaea", "Hodge", "Pinch", "Shackleton", "Vex'd", "Skream", "Benga"],
    },
    "ethnic-house": {
        "playlist": "New Finds - Ethnic House",
        "genre_words": ["afro house", "ethnic", "tribal house", "organic house", "afro tech", "oriental",
                        "world house"],
        "tags": ["ethnic-house", "afro-house", "tribal-house", "organic-house", "world-house",
                 "oriental-house", "arabic-house", "indian-house"],
        "seeds": ["Culoe De Song", "Caiiro", "Da Capo", "Enoo Napa", "Hyenah", "Ahmed Spins", "Sandrinho",
                  "Aero Manyelo", "Black Coffee", "Sabo", "Kususa", "Shimza"],
    },
}


def load_state():
    s = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            s = json.load(f)
    for k, v in (("added", {}), ("rejected", {}), ("unsure", {}), ("promoted", {}), ("mbid", {}),
                 ("tag_cursor", {}), ("playlist_ids", {}), ("runs", [])):
        s.setdefault(k, v)
    return s


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def bandcamp_tag_slice(tag, slice_name):
    body = {"category_id": 0, "tag_norm_names": [tag], "geoname_id": 0, "slice": slice_name,
            "cursor": "*", "size": ol.BANDCAMP_RESULTS_PER_TAG, "include_result_types": ["a"]}
    code, data = http("POST", "https://bandcamp.com/api/discover/1/discover_web", body)
    if code != 200:
        return []
    out = []
    for r in data.get("results", []):
        artist = r.get("album_artist") or r.get("band_name")
        if artist and "," not in artist and " / " not in artist:
            out.append((artist.strip(), r.get("title") or ""))
    return out


def resolve_seed(sp, name, words):
    """Exact-name artist whose Spotify genres touch the lane, most followers first."""
    items = sp.search(f'artist:"{name}"', "artist").get("artists", {}).get("items", []) or []
    ok = [a for a in items if a and norm(a["name"]) == norm(name)
          and any(w in g for g in a.get("genres", []) for w in words)]
    ok.sort(key=lambda a: -((a.get("followers") or {}).get("total") or 0))
    return ok[0] if ok else None


def quality_ok(sp, artist):
    """Several releases, at least one recent. If Spotify refuses the call, don't block."""
    followers = (artist.get("followers") or {}).get("total") or 0
    if followers < MIN_FOLLOWERS:
        return False, f"only {followers} followers"
    code, data = sp.call("GET", f"/artists/{artist['id']}/albums?include_groups=album,single&limit=50")
    if code != 200:
        return True, "release list unavailable"
    items = data.get("items", [])
    names = {norm(i["name"]) for i in items}
    if len(names) < MIN_RELEASES:
        return False, f"only {len(names)} releases"
    newest = max((i.get("release_date", "0")[:4] for i in items), default="0")
    if newest < str(date.today().year - RECENT_YEARS):
        return False, f"last release {newest}"
    return True, f"{len(names)} releases, newest {newest}"


def main():
    dry = "--dry-run" in sys.argv
    seeds_only = "--seeds-only" in sys.argv
    only = sys.argv[sys.argv.index("--lane") + 1] if "--lane" in sys.argv else None
    today = date.today()
    state = load_state()
    sp = Spotify()
    lastfm_key = os.environ.get("LASTFM_API_KEY", "")
    ol.MAX_FOLLOWERS = MAX_FOLLOWERS
    log(f"Similar-artist source: {'Last.fm' if lastfm_key else 'ListenBrainz'}; dry-run={dry}")

    # --- what the user already has: everything in ecstatic tracks, the outside-loop list and every lane
    known = set()
    ecstatic_norm = set()
    seed_pl = sp.find_playlist(SEED_PLAYLIST)
    if seed_pl:
        ecstatic_norm = {norm(a["name"]) for t in sp.playlist_tracks(seed_pl["id"]) for a in t["artists"]}
        known |= ecstatic_norm
    outside = sp.find_playlist(ol.TARGET_PLAYLIST)
    if outside:
        known |= {norm(a["name"]) for t in sp.playlist_tracks(outside["id"]) for a in t["artists"]}

    lane_tracks = {}
    for lane, cfg in LANES.items():
        pl = sp.find_playlist(cfg["playlist"])
        if pl:
            state["playlist_ids"][lane] = pl["id"]
            lane_tracks[lane] = sp.playlist_tracks(pl["id"])
            known |= {norm(a["name"]) for t in lane_tracks[lane] for a in t["artists"]}

    summary = []
    for lane, cfg in LANES.items():
        if only and lane != only:
            continue
        log(f"\n=== {lane} -> '{cfg['playlist']}' ===")
        words = cfg["genre_words"]

        # feedback
        uris_now = {t["uri"] for t in lane_tracks.get(lane, [])}
        for key, rec in list(state["added"].items()):
            if rec["lane"] != lane:
                continue
            if key in ecstatic_norm and key not in state["promoted"]:
                state["promoted"][key] = {"name": rec["name"], "lane": lane, "date": today.isoformat()}
                log(f"  promoted into ecstatic tracks: {rec['name']}")
            age = (today - date.fromisoformat(rec["date"])).days
            if lane in lane_tracks and age >= ol.REJECT_AFTER_DAYS and key not in state["rejected"] \
                    and not (set(rec["uris"]) & uris_now):
                state["rejected"][key] = {"name": rec["name"], "date": today.isoformat()}
                log(f"  removed by you, will not suggest again: {rec['name']}")

        unsure_cut = (today - timedelta(days=ol.UNSURE_RETRY_DAYS)).isoformat()
        exclude = known | set(state["rejected"]) | set(state["added"]) | {
            k for k, v in state["unsure"].items() if v["date"] >= unsure_cut}

        # seeds
        seeds = []
        for name in cfg["seeds"]:
            a = resolve_seed(sp, name, words)
            if a:
                seeds.append(a)
        for p in state["promoted"].values():
            if p["lane"] == lane:
                a = resolve_seed(sp, p["name"], words) or None
                if a and a["id"] not in {s["id"] for s in seeds}:
                    seeds.append(a)
        exclude |= {norm(a["name"]) for a in seeds}
        gcount = Counter(g for a in seeds for g in a.get("genres", []))
        allowed = {g for g, c in gcount.items() if c >= 2} | {
            g for a in seeds for g in a.get("genres", []) if any(w in g for w in words)}
        log(f"  seeds ({len(seeds)}): " + ", ".join(
            f"{a['name']} [{(a.get('followers') or {}).get('total', 0)}]" for a in seeds))
        log(f"  allowed genres: {sorted(allowed)}")
        if seeds_only:
            continue

        cands = {}

        def bump(name, score, hint, source, genres=None):
            key = norm(name)
            if not key or key in exclude:
                return
            c = cands.setdefault(key, {"name": name, "score": 0.0, "hint": "", "sources": set(), "genres": set()})
            c["score"] += score
            c["genres"] |= set(genres or [])
            c["sources"].add(source)
            if hint and not c["hint"]:
                c["hint"] = hint

        for a in seeds:
            sims = lastfm_similar(a["name"], lastfm_key) if lastfm_key else listenbrainz_similar(a["name"], state)
            sgen = set(a.get("genres", []))
            for rank, (sim, m) in enumerate(sims):
                bump(sim, m / (1 + rank / 15.0), "", "similar", sgen)
        log(f"  {len(cands)} candidates from similar-artist lookups")

        start = state["tag_cursor"].get(lane, 0) % len(cfg["tags"])
        tags = [cfg["tags"][(start + i) % len(cfg["tags"])] for i in range(min(TAGS_PER_RUN, len(cfg["tags"])))]
        state["tag_cursor"][lane] = (start + TAGS_PER_RUN) % len(cfg["tags"])
        for tag in tags:
            for sl in ("new", "top"):
                res = bandcamp_tag_slice(tag, sl)
                log(f"    bandcamp {tag}/{sl}: {len(res)} results")
                for artist, release in res:
                    bump(artist, 1.0, release, "bandcamp:" + tag)
                time.sleep(0.5)

        ranked = sorted(cands.values(), key=lambda c: (-len(c["sources"]), -c["score"]))
        added, skipped = [], []
        for c in ranked[:MAX_NEW_PER_LANE * 8]:
            if len(added) >= MAX_NEW_PER_LANE:
                break
            only_similar = all(x == "similar" for x in c["sources"])
            lane_allowed = c["genres"] & allowed if only_similar and c["genres"] else allowed
            artist, tracks, reason = verify(sp, c["name"], c["hint"], lane_allowed or allowed)
            key = norm(c["name"])
            if artist:
                ok, why = quality_ok(sp, artist)
                if not ok:
                    artist, reason = None, "quality: " + why
            if not artist:
                state["unsure"][key] = {"name": c["name"], "date": today.isoformat(), "why": reason}
                skipped.append((c["name"], reason))
                continue
            if artist["id"] in {a["id"] for a, _, _ in added}:
                continue
            added.append((artist, tracks, sorted(c["sources"])))

        log(f"  verified {len(added)} new artists ({len(skipped)} skipped):")
        for artist, tracks, src in added:
            log(f"    + {artist['name']} [{', '.join(artist.get('genres') or ['no genre tags'])}] "
                f"({(artist.get('followers') or {}).get('total', 0)} followers) <- {', '.join(src)}: "
                + " | ".join(t["name"] for t in tracks))
        for n, r in skipped[:12]:
            log(f"    - skipped {n}: {r}")
        summary.append((lane, added, len(skipped)))

        if added and not dry:
            pid = state["playlist_ids"].get(lane)
            if not pid:
                code, pl = sp.call("POST", "/me/playlists", {
                    "name": cfg["playlist"], "public": False,
                    "description": f"Weekly {lane.replace('-', ' ')} finds, auto-curated. Delete what you dislike."})
                if code >= 400:
                    raise RuntimeError(f"create playlist failed {code} {pl}")
                pid = state["playlist_ids"][lane] = pl["id"]
                log(f"  created playlist {cfg['playlist']}")
            sp.add(pid, [t["uri"] for _, tracks, _ in added for t in tracks])
            for artist, tracks, src in added:
                state["added"][norm(artist["name"])] = {
                    "name": artist["name"], "lane": lane, "date": today.isoformat(),
                    "uris": [t["uri"] for t in tracks], "sources": src}
            known |= {norm(a["name"]) for a, _, _ in added}

    if not dry and not seeds_only:
        state["runs"] = (state["runs"] + [{"date": today.isoformat(),
                                           "added": sum(len(a) for _, a, _ in summary)}])[-52:]
        save_state(state)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            for lane, added, nskip in summary:
                f.write(f"## {lane}: {len(added)} new artists\n\n")
                for artist, tracks, src in added:
                    f.write(f"- **{artist['name']}** ({', '.join(src)}): " + ", ".join(t["name"] for t in tracks) + "\n")
                f.write(f"\nSkipped: {nskip}\n\n")


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        log(f"Missing required environment variable: {e}")
        sys.exit(1)
