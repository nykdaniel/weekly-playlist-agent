"""
Weekly Playlist Agent
----------------------
Checks the artists you follow (and artists behind your saved tracks) for new
releases from the last 7 days, groups new tracks by genre, and creates/updates
a Spotify playlist per genre with the new material.

This script is meant to be run automatically by GitHub Actions once a week.
It reads credentials from environment variables (set as GitHub Secrets) and
keeps track of what it has already seen in state.json, so it never adds the
same track twice.
"""

import os
import json
import time
import datetime
import requests

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["SPOTIFY_REFRESH_TOKEN"]

STATE_FILE = "state.json"
LOOKBACK_DAYS = 7
API_BASE = "https://api.spotify.com/v1"


def get_access_token():
    """Exchange the long-lived refresh token for a short-lived access token."""
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get(session, path, params=None):
    r = session.get(f"{API_BASE}{path}", params=params)
    r.raise_for_status()
    return r.json()


def api_post(session, path, json_body=None):
    r = session.post(f"{API_BASE}{path}", json=json_body)
    r.raise_for_status()
    return r.json() if r.text else {}


def get_followed_artist_ids(session):
    ids = []
    after = None
    while True:
        params = {"type": "artist", "limit": 50}
        if after:
            params["after"] = after
        data = api_get(session, "/me/following", params)["artists"]
        ids.extend(a["id"] for a in data["items"])
        after = data.get("cursors", {}).get("after")
        if not after:
            break
    return ids


def get_saved_track_artist_ids(session, max_pages=5):
    """Sample recent saved tracks (not the whole library) to keep this fast."""
    ids = set()
    offset = 0
    for _ in range(max_pages):
        data = api_get(session, "/me/tracks", {"limit": 50, "offset": offset})
        items = data["items"]
        if not items:
            break
        for item in items:
            for artist in item["track"]["artists"]:
                ids.add(artist["id"])
        offset += 50
    return list(ids)


def get_recent_releases(session, artist_id, cutoff_date):
    """Return (release, genres) for this artist's albums/singles released after cutoff_date."""
    data = api_get(
        session,
        f"/artists/{artist_id}/albums",
        {"include_groups": "album,single", "limit": 10},
    )
    recent = []
    for album in data["items"]:
        release_date = album["release_date"]
        # Spotify sometimes gives just a year or year-month; pad it out
        parts = release_date.split("-")
        while len(parts) < 3:
            parts.append("01")
        try:
            rdate = datetime.date(*(int(p) for p in parts))
        except ValueError:
            continue
        if rdate >= cutoff_date:
            recent.append(album)
    return recent


def get_artist_genres(session, artist_id, cache):
    if artist_id in cache:
        return cache[artist_id]
    data = api_get(session, f"/artists/{artist_id}")
    genres = data.get("genres") or ["unclassified"]
    cache[artist_id] = genres
    return genres


def bucket_genre(genres):
    """Collapse Spotify's very specific genre tags into broader buckets."""
    text = " ".join(genres).lower()
    buckets = {
        "house": ["house"],
        "techno": ["techno"],
        "hip hop": ["hip hop", "rap", "trap"],
        "drum and bass": ["drum and bass", "dnb", "jungle"],
        "disco / funk": ["disco", "funk"],
        "pop": ["pop"],
        "rock": ["rock"],
        "electronic": ["electronic", "edm", "dance"],
        "reggae / dub": ["reggae", "dub"],
        "afrobeat": ["afrobeat", "afro"],
    }
    for bucket, keywords in buckets.items():
        if any(k in text for k in keywords):
            return bucket
    return "other"


def get_or_create_playlist(session, user_id, name, existing_playlists):
    if name in existing_playlists:
        return existing_playlists[name]
    playlist = api_post(
        session,
        f"/users/{user_id}/playlists",
        {
            "name": name,
            "description": "Auto-updated weekly by your playlist agent.",
            "public": False,
        },
    )
    existing_playlists[name] = playlist["id"]
    return playlist["id"]


def get_all_user_playlists(session, user_id):
    playlists = {}
    offset = 0
    while True:
        data = api_get(session, f"/users/{user_id}/playlists", {"limit": 50, "offset": offset})
        for p in data["items"]:
            playlists[p["name"]] = p["id"]
        if not data["next"]:
            break
        offset += 50
    return playlists


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen_track_ids": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    session = requests.Session()
    access_token = get_access_token()
    session.headers.update({"Authorization": f"Bearer {access_token}"})

    me = api_get(session, "/me")
    user_id = me["id"]

    state = load_state()
    seen = set(state["seen_track_ids"])

    cutoff_date = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)

    print("Gathering artists to check...")
    artist_ids = set(get_followed_artist_ids(session))
    artist_ids.update(get_saved_track_artist_ids(session))
    print(f"Checking {len(artist_ids)} artists for releases since {cutoff_date}")

    genre_cache = {}
    genre_to_tracks = {}

    for i, artist_id in enumerate(artist_ids):
        try:
            releases = get_recent_releases(session, artist_id, cutoff_date)
        except requests.HTTPError:
            continue
        if not releases:
            continue
        genres = get_artist_genres(session, artist_id, genre_cache)
        bucket = bucket_genre(genres)

        for album in releases:
            album_tracks = api_get(session, f"/albums/{album['id']}/tracks")["items"]
            for track in album_tracks:
                if track["id"] in seen:
                    continue
                genre_to_tracks.setdefault(bucket, []).append(track["uri"])
                seen.add(track["id"])

        # Be gentle on rate limits
        if i % 20 == 0:
            time.sleep(1)

    if not genre_to_tracks:
        print("No new releases found this week.")
        save_state({"seen_track_ids": list(seen)})
        return

    existing_playlists = get_all_user_playlists(session, user_id)

    for bucket, uris in genre_to_tracks.items():
        playlist_name = f"New This Week — {bucket.title()}"
        playlist_id = get_or_create_playlist(session, user_id, playlist_name, existing_playlists)
        # Add in batches of 100 (Spotify's max per request)
        for j in range(0, len(uris), 100):
            api_post(session, f"/playlists/{playlist_id}/tracks", {"uris": uris[j:j + 100]})
        print(f"Added {len(uris)} track(s) to '{playlist_name}'")

    save_state({"seen_track_ids": list(seen)})
    print("Done.")


if __name__ == "__main__":
    main()
