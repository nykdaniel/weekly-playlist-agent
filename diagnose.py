"""
One-off diagnostic (read-only, no changes to Spotify): scans the actual
"Discover Daily" playlist for tracks whose artist name starts with "MC "
(the classic Brazilian funk naming convention) and reports, for each one,
its genres and whether it came from a seed artist or genre-discovery search.
"""

import os
import re

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
MC_PATTERN = re.compile(r"(^|[;,/&]|\bfeat\.?\s|\bft\.?\s)\s*MC[.\s]", re.IGNORECASE)


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
    lines = []

    discover = find_playlist_by_name(sp, DISCOVER_PLAYLIST_NAME)
    if not discover:
        lines.append(f'Could not find "{DISCOVER_PLAYLIST_NAME}" playlist.\n')
        with open(summary_path, "a") as f:
            f.write("\n".join(lines))
        return

    discover_tracks = get_playlist_tracks(sp, discover["id"])
    mc_tracks = [
        t for t in discover_tracks
        if any(MC_PATTERN.search(f" {a['name']} ") for a in t.get("artists", []))
    ]

    lines.append(
        f"## MC-named tracks in Discover Daily\n\n"
        f"Discover Daily has {len(discover_tracks)} tracks total. "
        f"{len(mc_tracks)} have an artist matching \"MC ...\".\n\n"
    )

    followed = get_followed_artists(sp)
    ecstatic_playlist = find_playlist_by_name(sp, ECSTATIC_PLAYLIST_NAME)
    ecstatic_artist_ids = set()
    if ecstatic_playlist:
        for t in get_playlist_tracks(sp, ecstatic_playlist["id"]):
            for a in t.get("artists", []):
                ecstatic_artist_ids.add(a["id"])

    for t in mc_tracks:
        primary = t["artists"][0]
        artist = sp.artist(primary["id"])
        is_followed = primary["id"] in followed
        in_ecstatic = primary["id"] in ecstatic_artist_ids
        source = []
        if is_followed:
            source.append("followed")
        if in_ecstatic:
            source.append("in ecstatic tracks")
        if not source:
            source.append("NOT a seed artist -> came from genre-discovery search")
        lines.append(
            f"- **{t['name']}** by {primary['name']} - "
            f"genres: {', '.join(artist['genres']) or '(none)'} - "
            f"source: {', '.join(source)}\n"
        )

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))

    print(f"Found {len(mc_tracks)} MC-named tracks out of {len(discover_tracks)}.")


if __name__ == "__main__":
    main()
