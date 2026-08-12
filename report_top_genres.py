"""
Read-only report: prints the genre frequency across all seed artists
(followed artists + artists behind "ecstatic tracks"), so you can see
exactly which genres genre-discovery searches today.

Makes no changes to Spotify - doesn't create/modify any playlists.
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
MAX_GENRES_FOR_DISCOVERY = 12


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


def get_ecstatic_seed_artists(sp):
    playlist = find_playlist_by_name(sp, ECSTATIC_PLAYLIST_NAME)
    if not playlist:
        return {}
    artist_ids = {}
    results = sp.playlist_items(playlist["id"], additional_types=["track"], limit=100)
    while results:
        for item in results["items"]:
            track = item.get("track")
            if not track:
                continue
            for artist in track.get("artists", []):
                artist_ids[artist["id"]] = artist
        results = sp.next(results) if results["next"] else None
    return artist_ids


def hydrate_genres(sp, ids_without_genres, known):
    missing = [
        aid
        for aid in ids_without_genres
        if aid not in known or "genres" not in known.get(aid, {})
    ]
    for i in range(0, len(missing), 50):
        batch = missing[i : i + 50]
        for artist in sp.artists(batch)["artists"]:
            if artist:
                known[artist["id"]] = artist


def main():
    sp = get_spotify_client()

    followed = get_followed_artists(sp)
    ecstatic_stubs = get_ecstatic_seed_artists(sp)
    new_ids = [aid for aid in ecstatic_stubs if aid not in followed]
    hydrate_genres(sp, new_ids, followed)

    seed_artists = dict(followed)
    for aid in new_ids:
        seed_artists[aid] = seed_artists.get(aid, ecstatic_stubs[aid])

    counts = Counter()
    for artist in seed_artists.values():
        for genre in artist.get("genres", []):
            counts[genre] += 1

    summary_path = os.environ["GITHUB_STEP_SUMMARY"]
    with open(summary_path, "a") as f:
        f.write(f"## Genre frequency across {len(seed_artists)} seed artists\n\n")
        f.write(
            f"### Top {MAX_GENRES_FOR_DISCOVERY} - what genre-discovery searches today\n\n"
        )
        for genre, count in counts.most_common(MAX_GENRES_FOR_DISCOVERY):
            f.write(f"- **{genre}** - {count} artists\n")

        f.write(f"\n### Full genre list ({len(counts)} distinct genres)\n\n")
        for genre, count in counts.most_common():
            f.write(f"- {genre} - {count} artists\n")

    print(f"Wrote genre report for {len(seed_artists)} seed artists, {len(counts)} distinct genres.")


if __name__ == "__main__":
    main()
