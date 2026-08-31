"""
One-off diagnostic (read-only, no changes to Spotify): for every track in
"Discover Daily" whose primary artist is NOT one of your seed artists
(followed + artists behind "ecstatic tracks" - i.e. it came from
genre-discovery search, not from an artist you follow/saved), lists every
genre tag those tracks carry and how many tracks/artists match each one.
"""

import os
from collections import Counter

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
DISCOVER_PLAYLIST_NAME = "Discover Daily"


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


def get_ecstatic_seed_artist_ids(sp):
    playlist = find_playlist_by_name(sp, ECSTATIC_PLAYLIST_NAME)
    if not playlist:
        return set()
    ids = set()
    results = sp.playlist_items(playlist["id"], additional_types=["track"], limit=100)
    while results:
        for item in results["items"]:
            track = item.get("track")
            if not track:
                continue
            for artist in track.get("artists", []):
                ids.add(artist["id"])
        results = sp.next(results) if results["next"] else None
    return ids


def get_playlist_tracks(sp, playlist_id):
    tracks = []
    results = sp.playlist_items(playlist_id, additional_types=["track"], limit=100)
    while results:
        for item in results["items"]:
            track = item.get("track")
            if track:
                tracks.append(track)
        results = sp.next(results) if results["next"] else None
    return tracks


def main():
    sp = get_spotify_client()
    summary_path = os.environ["GITHUB_STEP_SUMMARY"]
    lines = []

    followed_ids = set(get_followed_artists(sp).keys())
    ecstatic_ids = get_ecstatic_seed_artist_ids(sp)
    saved_artist_ids = followed_ids | ecstatic_ids

    discover = find_playlist_by_name(sp, DISCOVER_PLAYLIST_NAME)
    if not discover:
        lines.append(f'Could not find "{DISCOVER_PLAYLIST_NAME}" playlist.\n')
        with open(summary_path, "a") as f:
            f.write("\n".join(lines))
        return

    tracks = get_playlist_tracks(sp, discover["id"])

    non_seed_tracks = [
        t for t in tracks
        if t.get("artists") and t["artists"][0]["id"] not in saved_artist_ids
    ]

    primary_ids = sorted({t["artists"][0]["id"] for t in non_seed_tracks})
    artist_genres = {}
    for i in range(0, len(primary_ids), 50):
        batch = primary_ids[i : i + 50]
        for artist in sp.artists(batch)["artists"]:
            if artist:
                artist_genres[artist["id"]] = artist.get("genres", [])

    genre_track_counts = Counter()
    genre_artist_sets = {}
    for t in non_seed_tracks:
        aid = t["artists"][0]["id"]
        genres = artist_genres.get(aid, [])
        if not genres:
            genre_track_counts["(no genre tags)"] += 1
            genre_artist_sets.setdefault("(no genre tags)", set()).add(aid)
            continue
        for g in genres:
            genre_track_counts[g] += 1
            genre_artist_sets.setdefault(g, set()).add(aid)

    lines.append(
        f"## Genres captured outside your saved artists\n\n"
        f"Discover Daily has {len(tracks)} tracks total. "
        f"{len(non_seed_tracks)} were NOT from a followed/ecstatic-tracks artist "
        f"(i.e. came from genre-discovery search), spanning {len(primary_ids)} distinct artists.\n\n"
        f"### Genre frequency ({len(genre_track_counts)} distinct genres)\n\n"
    )
    for genre, track_count in genre_track_counts.most_common():
        artist_count = len(genre_artist_sets[genre])
        lines.append(f"- **{genre}** - {track_count} tracks, {artist_count} artists\n")

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))

    print(
        f"{len(non_seed_tracks)} non-seed tracks out of {len(tracks)}, "
        f"{len(genre_track_counts)} distinct genres."
    )


if __name__ == "__main__":
    main()
