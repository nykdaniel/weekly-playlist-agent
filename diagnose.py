"""
One-off diagnostic (read-only, no changes to Spotify):

1. Checks specific example tracks (from Release Radar) against what our
   artist_albums(include_groups="album,single") pull would have found, to
   see whether we're missing them and if so why.
2. Reports the genre makeup of your seed artists, and looks up the genre
   of a specific track, to explain any lopsided genre skew in output.
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
GENRE_CHECK_TRACK_ID = "6sbM9pwH5brMNFXbCtnpZs"
EXAMPLE_TRACK_IDS = [
    "7DuWvmg3iYUQbFIYcxkVcd",
    "72tqgaYCgZSBB9KfL2feff",
    "1fyMlmjbH0JEyKT4qXcxRS",
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

    # --- Question 2: genre skew ---
    lines.append("## Question 2: genre skew\n")
    track = sp.track(GENRE_CHECK_TRACK_ID)
    primary_artist = sp.artist(track["artists"][0]["id"])
    lines.append(
        f"Track checked: **{track['name']}** by **{primary_artist['name']}**  \n"
        f"Artist genres: {', '.join(primary_artist['genres']) or '(none)'}\n"
    )

    followed = get_followed_artists(sp)
    ecstatic_playlist = find_playlist_by_name(sp, ECSTATIC_PLAYLIST_NAME)
    ecstatic_artist_stubs = {}
    if ecstatic_playlist:
        for t in get_playlist_tracks(sp, ecstatic_playlist["id"]):
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

    house_count = sum(
        1 for a in all_artists.values() if any("house" in g for g in a.get("genres", []))
    )
    total = len(all_artists)
    lines.append(
        f"\n**{house_count} of {total} seed artists ({house_count / total * 100:.0f}%) "
        f"have a genre tag containing \"house\".**\n"
    )

    # --- Question 1: check specific Release Radar example tracks ---
    lines.append("\n## Question 1: Release Radar example tracks\n")
    for tid in EXAMPLE_TRACK_IDS:
        t = sp.track(tid)
        primary = t["artists"][0]
        is_followed = primary["id"] in followed
        found_in_pull = False
        found_in_ecstatic = primary["id"] in ecstatic_artist_stubs
        if is_followed or found_in_ecstatic:
            try:
                albums = sp.artist_albums(
                    primary["id"], include_groups="album,single", limit=50
                )
                album_ids = {a["id"] for a in albums["items"]}
                found_in_pull = t["album"]["id"] in album_ids
            except spotipy.SpotifyException:
                pass

        if found_in_pull:
            diagnosis = "OK - would be found by our script"
        elif not is_followed and not found_in_ecstatic:
            diagnosis = "artist is not followed and not in ecstatic tracks (not a seed artist)"
        else:
            diagnosis = (
                'MISSING - not in artist_albums(include_groups="album,single") '
                "top 50 results - likely a feature/guest appearance "
                '(needs "appears_on") or the artist has 50+ releases and this one '
                "fell outside that page, or it's older than it looks"
            )

        lines.append(
            f"- **{t['name']}** by {primary['name']} "
            f"(album group: {t['album']['album_type']}, release date: {t['album']['release_date']}): "
            f"{diagnosis}\n"
        )

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))

    print("Diagnostic report written to job summary.")


if __name__ == "__main__":
    main()
