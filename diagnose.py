"""
One-off diagnostic (read-only, no changes to Spotify): looks up two specific
artists the user flagged as unwanted, reports their name/genres, whether
they're a followed artist / in "ecstatic tracks", and how many of their
tracks are currently sitting in "Discover Daily".
"""

import os

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
TARGET_ARTIST_IDS = [
    "3xvaSlT4xsyk6lY1ESOspO",
    "4hV3aU0WKvFaiX5ugXP5hp",
]


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


def find_playlist_by_name(sp, name):
    results = sp.current_user_playlists(limit=50)
    while results:
        for playlist in results["items"]:
            if playlist["name"].strip().casefold() == name.casefold():
                return playlist
        results = sp.next(results) if results["next"] else None
    return None


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
    lines = ["## Target artist lookup\n\n"]

    followed = get_followed_artists(sp)
    ecstatic_playlist = find_playlist_by_name(sp, ECSTATIC_PLAYLIST_NAME)
    ecstatic_artist_ids = set()
    if ecstatic_playlist:
        for t in get_playlist_tracks(sp, ecstatic_playlist["id"]):
            for a in t.get("artists", []):
                ecstatic_artist_ids.add(a["id"])

    discover = find_playlist_by_name(sp, DISCOVER_PLAYLIST_NAME)
    discover_tracks = get_playlist_tracks(sp, discover["id"]) if discover else []

    for artist_id in TARGET_ARTIST_IDS:
        artist = sp.artist(artist_id)
        is_followed = artist_id in followed
        in_ecstatic = artist_id in ecstatic_artist_ids
        matching_tracks = [
            t for t in discover_tracks
            if any(a["id"] == artist_id for a in t.get("artists", []))
        ]
        source = []
        if is_followed:
            source.append("followed")
        if in_ecstatic:
            source.append("in ecstatic tracks")
        if not source:
            source.append("NOT a seed artist -> came from genre-discovery search")

        lines.append(
            f"### {artist['name']} (`{artist_id}`)\n\n"
            f"- genres: {', '.join(artist['genres']) or '(none)'}\n"
            f"- source: {', '.join(source)}\n"
            f"- tracks currently in Discover Daily: {len(matching_tracks)}\n"
        )
        for t in matching_tracks:
            lines.append(f"  - {t['name']}\n")
        lines.append("\n")

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))

    print("Done.")


if __name__ == "__main__":
    main()
