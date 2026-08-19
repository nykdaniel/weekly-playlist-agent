"""
One-off export: writes a CSV listing every track in "ecstatic tracks"
alongside its artist(s) and their combined genre tags.
"""

import csv
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
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ecstatic_tracks_genres.csv"
)


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

    playlist = find_playlist_by_name(sp, ECSTATIC_PLAYLIST_NAME)
    if not playlist:
        raise SystemExit(f'Playlist "{ECSTATIC_PLAYLIST_NAME}" not found')

    tracks = get_playlist_tracks(sp, playlist["id"])

    artist_ids = set()
    for t in tracks:
        for a in t.get("artists", []):
            artist_ids.add(a["id"])

    artist_genres = {}
    ids = list(artist_ids)
    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        for a in sp.artists(batch)["artists"]:
            if a:
                artist_genres[a["id"]] = a.get("genres", [])

    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Track Name", "Artists", "Genres"])
        for t in tracks:
            artists = t.get("artists", [])
            artist_names = "; ".join(a["name"] for a in artists)
            genres = sorted({g for a in artists for g in artist_genres.get(a["id"], [])})
            writer.writerow([t["name"], artist_names, "; ".join(genres)])

    print(f"Wrote {len(tracks)} rows to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
