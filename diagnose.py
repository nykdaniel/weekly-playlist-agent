"""
One-off diagnostic (read-only, no changes to Spotify): looks up the genre
tags for a specific track/artist the user wants to check.
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
TARGET_TRACK_ID = "3PqCjQF4bbJkZMkFKpfc0N"


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


def main():
    sp = get_spotify_client()
    summary_path = os.environ["GITHUB_STEP_SUMMARY"]
    lines = []

    track = sp.track(TARGET_TRACK_ID)
    lines.append("## Track genre check\n")
    lines.append(f"Track: **{track['name']}**\n")
    lines.append(f"Album: {track['album']['name']} ({track['album']['release_date']})\n\n")

    for artist_stub in track["artists"]:
        artist = sp.artist(artist_stub["id"])
        lines.append(
            f"- **{artist['name']}** - genres: {', '.join(artist['genres']) or '(none)'}\n"
        )

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))

    print("Diagnostic report written to job summary.")


if __name__ == "__main__":
    main()
