"""
Daily playlist agent.

Looks at:
  1. Artists you follow on Spotify
  2. Artists behind the tracks saved in your "ecstatic tracks" playlist

For those artists it finds new releases, and (since Spotify's
Recommendations/Related-Artists endpoints aren't available to apps created
after Nov 2024) discovers other new tracks in the same genres using
Spotify's search "tag:new" filter. Everything found gets grouped by genre
and pushed into one auto-managed playlist per genre.

State (which tracks we've already added, and which playlist ID belongs to
which genre) is kept in state.json, which this script rewrites in place.
The GitHub Actions workflow commits that file back to the repo after each
run so runs are idempotent and don't spam playlists with duplicates.
"""

import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta

import spotipy
from spotipy.oauth2 import SpotifyOAuth

SCOPES = (
    "user-follow-read "
    "playlist-read-private "
    "playlist-read-collaborative "
    "playlist-modify-public "
    "playlist-modify-private"
)

ECSTATIC_PLAYLIST_NAME = "ecstatic tracks"
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

NEW_RELEASE_LOOKBACK_DAYS = 14  # how far back to consider an artist's release "new"
STATE_PRUNE_DAYS = 180          # forget seen-track history older than this
MAX_GENRES_FOR_DISCOVERY = 12   # cap search calls for genre-based discovery
GENRE_SEARCH_LIMIT = 20         # tracks pulled per genre search
PLAYLIST_NAME_PREFIX = "Discover"


def log(msg):
    print(msg, flush=True)


def get_spotify_client():
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    refresh_token = os.environ["SPOTIFY_REFRESH_TOKEN"]

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://127.0.0.1:8080/callback",
        scope=SCOPES,
    )
    token_info = auth_manager.refresh_access_token(refresh_token)
    return spotipy.Spotify(auth=token_info["access_token"])


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {"seen_tracks": {}, "genre_playlists": {}}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def prune_state(state):
    cutoff = (date.today() - timedelta(days=STATE_PRUNE_DAYS)).isoformat()
    state["seen_tracks"] = {
        tid: seen_date
        for tid, seen_date in state["seen_tracks"].items()
        if seen_date >= cutoff
    }


def parse_release_date(release_date):
    parts = release_date.split("-")
    year = int(parts[0])
    month = int(parts[1]) if len(parts) > 1 else 1
    day = int(parts[2]) if len(parts) > 2 else 1
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, 1)


def get_followed_artists(sp):
    artists = {}
    results = sp.current_user_followed_artists(limit=50)
    while results:
        for artist in results["artists"]["items"]:
            artists[artist["id"]] = artist
        if results["artists"]["next"]:
            after = results["artists"]["items"][-1]["id"]
            results = sp.current_user_followed_artists(limit=50, after=after)
        else:
            results = None
    return artists


def find_playlist_by_name(sp, name):
    results = sp.current_user_playlists(limit=50)
    while results:
        for playlist in results["items"]:
            if playlist["name"].strip().casefold() == name.casefold():
                return playlist
        results = sp.next(results) if results["next"] else None
    return None


def get_ecstatic_seed_artists(sp):
    """Artist IDs (without genre info yet) behind tracks saved in the ecstatic-tracks playlist."""
    playlist = find_playlist_by_name(sp, ECSTATIC_PLAYLIST_NAME)
    if not playlist:
        log(f'WARNING: playlist "{ECSTATIC_PLAYLIST_NAME}" not found, skipping it')
        return {}

    artist_ids = {}
    results = sp.playlist_items(
        playlist["id"], additional_types=["track"], limit=100
    )
    while results:
        for item in results["items"]:
            track = item.get("track")
            if not track:
                continue
            for artist in track.get("artists", []):
                artist_ids[artist["id"]] = artist
        results = sp.next(results) if results["next"] else None
    return artist_ids


def hydrate_genres(sp, artists_without_genres, known_artists):
    """Batch-fetch full artist objects (with genres) for artists we only have a bare id/name for."""
    missing_ids = [
        aid
        for aid in artists_without_genres
        if aid not in known_artists or "genres" not in known_artists.get(aid, {})
    ]
    for i in range(0, len(missing_ids), 50):
        batch = missing_ids[i : i + 50]
        for artist in sp.artists(batch)["artists"]:
            if artist:
                known_artists[artist["id"]] = artist


def get_new_releases(sp, seed_artists, state):
    cutoff = date.today() - timedelta(days=NEW_RELEASE_LOOKBACK_DAYS)
    new_tracks = {}  # track_id -> {id, uri, name, genres}

    for artist_id, artist in seed_artists.items():
        try:
            albums = sp.artist_albums(
                artist_id, include_groups="album,single", limit=50
            )
        except spotipy.SpotifyException as e:
            log(f'WARNING: could not fetch albums for artist "{artist.get("name")}": {e}')
            continue

        recent_albums = [
            a
            for a in albums["items"]
            if parse_release_date(a["release_date"]) >= cutoff
        ]
        for album in recent_albums:
            try:
                tracks = sp.album_tracks(album["id"], limit=50)
            except spotipy.SpotifyException as e:
                log(f'WARNING: could not fetch tracks for album "{album["name"]}": {e}')
                continue
            for track in tracks["items"]:
                if track["id"] in state["seen_tracks"] or track["id"] in new_tracks:
                    continue
                new_tracks[track["id"]] = {
                    "id": track["id"],
                    "uri": track["uri"],
                    "name": track["name"],
                    "genres": artist.get("genres", []),
                }
    return new_tracks


def top_genres(seed_artists, limit):
    counts = Counter()
    for artist in seed_artists.values():
        for genre in artist.get("genres", []):
            counts[genre] += 1
    return [genre for genre, _ in counts.most_common(limit)]


def get_genre_discovery_tracks(sp, genres, state, already_found, artist_genre_cache):
    new_tracks = {}
    for genre in genres:
        try:
            results = sp.search(
                q=f'genre:"{genre}" tag:new', type="track", limit=GENRE_SEARCH_LIMIT
            )
        except spotipy.SpotifyException as e:
            log(f'WARNING: search failed for genre "{genre}": {e}')
            continue

        for track in results["tracks"]["items"]:
            tid = track["id"]
            if tid in state["seen_tracks"] or tid in already_found or tid in new_tracks:
                continue
            primary_artist = track["artists"][0]
            hydrate_genres(sp, [primary_artist["id"]], artist_genre_cache)
            track_genres = artist_genre_cache.get(primary_artist["id"], {}).get(
                "genres", [genre]
            )
            new_tracks[tid] = {
                "id": tid,
                "uri": track["uri"],
                "name": track["name"],
                "genres": track_genres or [genre],
            }
    return new_tracks


def ensure_playlist(sp, user_id, genre, state):
    playlist_name = f"{PLAYLIST_NAME_PREFIX}: {genre.title()}"
    playlist_id = state["genre_playlists"].get(genre)
    if playlist_id:
        try:
            sp.playlist(playlist_id, fields="id")
            return playlist_id
        except spotipy.SpotifyException:
            pass  # playlist was deleted/renamed on Spotify's side; recreate below

    existing = find_playlist_by_name(sp, playlist_name)
    if existing:
        playlist_id = existing["id"]
    else:
        playlist = sp.user_playlist_create(
            user_id,
            playlist_name,
            public=False,
            description="Auto-updated by the daily playlist agent. Don't rename this playlist.",
        )
        playlist_id = playlist["id"]
        log(f'Created playlist "{playlist_name}"')

    state["genre_playlists"][genre] = playlist_id
    return playlist_id


def add_tracks_to_playlist(sp, playlist_id, uris):
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(playlist_id, uris[i : i + 100])


def main():
    sp = get_spotify_client()
    user_id = sp.current_user()["id"]
    state = load_state()
    prune_state(state)

    log("Fetching followed artists...")
    seed_artists = get_followed_artists(sp)
    log(f"  {len(seed_artists)} followed artists")

    log(f'Fetching artists from "{ECSTATIC_PLAYLIST_NAME}"...')
    ecstatic_artist_stubs = get_ecstatic_seed_artists(sp)
    new_ids = [aid for aid in ecstatic_artist_stubs if aid not in seed_artists]
    hydrate_genres(sp, new_ids, seed_artists)
    for aid in new_ids:
        if aid in seed_artists:
            continue
        seed_artists[aid] = seed_artists.get(aid, ecstatic_artist_stubs[aid])
    log(f"  {len(ecstatic_artist_stubs)} artists from the playlist ({len(seed_artists)} total seed artists)")

    log("Looking for new releases from seed artists...")
    new_release_tracks = get_new_releases(sp, seed_artists, state)
    log(f"  {len(new_release_tracks)} new tracks from releases")

    log("Looking for new tracks in matching genres...")
    genres = top_genres(seed_artists, MAX_GENRES_FOR_DISCOVERY)
    discovery_tracks = get_genre_discovery_tracks(
        sp, genres, state, new_release_tracks, seed_artists
    )
    log(f"  {len(discovery_tracks)} new tracks from genre discovery")

    all_new_tracks = {**new_release_tracks, **discovery_tracks}
    if not all_new_tracks:
        log("Nothing new today.")
        save_state(state)
        return

    by_genre = {}
    for track in all_new_tracks.values():
        genres_for_track = track["genres"] or ["other"]
        for genre in genres_for_track:
            by_genre.setdefault(genre, []).append(track)

    today = date.today().isoformat()
    for genre, tracks in by_genre.items():
        playlist_id = ensure_playlist(sp, user_id, genre, state)
        uris = [t["uri"] for t in tracks]
        add_tracks_to_playlist(sp, playlist_id, uris)
        log(f'Added {len(uris)} track(s) to "{PLAYLIST_NAME_PREFIX}: {genre.title()}"')

    for track in all_new_tracks.values():
        state["seen_tracks"][track["id"]] = today

    save_state(state)
    log("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        log(f"Missing required environment variable: {e}")
        sys.exit(1)
