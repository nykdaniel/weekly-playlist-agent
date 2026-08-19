"""
One-off diagnostic (read-only, no changes to Spotify):

Checks specific example tracks against seed artist data to see whether
they're coming from the new-release scan (seed artists) or genre-discovery
search, and reports how many seed artists are funk-tagged.
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
EXAMPLE_TRACK_IDS = [
    "73Wt5ggl54NiNxAE9Rtye2",
    "0Z5JzTCqXK6Z5I6a2cKUWl",
    "0RiDC2HAe0QWlr2SdAmTDW",
    "15gQGITHHgbrfHVyJ0ha1d",
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
    lines = []

    followed = get_followed_artists(sp)
    ecstatic_playlist = find_playlist_by_name(sp, ECSTATIC_PLAYLIST_NAME)
    ecstatic_artist_stubs = {}
    ecstatic_track_count = 0
    if ecstatic_playlist:
        ecstatic_tracks = get_playlist_tracks(sp, ecstatic_playlist["id"])
        ecstatic_track_count = len(ecstatic_tracks)
        for t in ecstatic_tracks:
            for a in t.get("artists", []):
                ecstatic_artist_stubs[a["id"]] = a

    all_artists = dict(followed)
    for aid, stub in ecstatic_artist_stubs.items():
        all_artists.setdefault(aid, stub)

    missing_genre_ids = [aid for aid in all_artists if "genres" not in all_artists[aid]]
    for i in range(0, len(missing_genre_ids), 50):
        batch = missing_genre_ids[i : i + 50]
        for a in sp.artists(batch)["artists"]:
            if a:
                all_artists[a["id"]] = a

    # --- Which example tracks come from which source ---
    lines.append("## Example tracks: source check\n")
    for tid in EXAMPLE_TRACK_IDS:
        t = sp.track(tid)
        primary = t["artists"][0]
        artist_full = all_artists.get(primary["id"])
        genres = artist_full.get("genres", []) if artist_full else sp.artist(primary["id"]).get("genres", [])
        is_followed = primary["id"] in followed
        in_ecstatic = primary["id"] in ecstatic_artist_stubs
        source = []
        if is_followed:
            source.append("followed")
        if in_ecstatic:
            source.append("in ecstatic tracks")
        if not source:
            source.append("NOT a seed artist (would only reach us via genre-discovery search)")
        lines.append(
            f"- **{t['name']}** by {primary['name']} - genres: {', '.join(genres) or '(none)'} "
            f"- source: {', '.join(source)}\n"
        )

    # --- Scale of funk representation among seed artists ---
    funk_artists = [
        a for a in all_artists.values()
        if any("funk" in g and "funk rock" not in g for g in a.get("genres", []))
    ]
    total = len(all_artists)
    lines.append(
        f"\n## Funk representation among seed artists\n\n"
        f"**{len(funk_artists)} of {total} seed artists ({len(funk_artists) / total * 100:.1f}%) "
        f"have a genre tag containing \"funk\".**\n\n"
        f"Your \"ecstatic tracks\" playlist currently has **{ecstatic_track_count} tracks** "
        f"contributing **{len(ecstatic_artist_stubs)} distinct artists** as seed artists - "
        f"every one of those is scanned equally for new releases regardless of genre.\n\n"
        f"Sample of funk-tagged seed artist names and their genres:\n\n"
    )
    for a in funk_artists[:25]:
        lines.append(f"- {a['name']}: {', '.join(a.get('genres', []))}\n")

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))

    print("Diagnostic report written to job summary.")


if __name__ == "__main__":
    main()
