"""
One-off diagnostic (read-only, no changes to Spotify):

1. Cross-checks Spotify's own "Release Radar" playlist against what our
   artist_albums(include_groups="album,single") pull would have found, to
   see whether we're missing tracks and if so why.
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
RELEASE_RADAR_NAME = "Release Radar"
TARGET_TRACK_ID = "6sbM9pwH5brMNFXbCtnpZs"


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
    track = sp.track(TARGET_TRACK_ID)
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
        f"have a genre tag containing \"house\".** The new-release scan checks every seed "
        f"artist equally and doesn't weight by genre, so its output share simply mirrors "
        f"your seed pool's genre makeup - if most of your seed artists are house-tagged, "
        f"most new-release output will be too.\n"
    )

    # --- Question 1: Release Radar cross-check ---
    lines.append("\n## Question 1: Release Radar cross-check\n")
    rr = find_playlist_by_name(sp, RELEASE_RADAR_NAME)
    if not rr:
        lines.append(f'Could not find a playlist named "{RELEASE_RADAR_NAME}" in your library.\n')
    else:
        rr_tracks = get_playlist_tracks(sp, rr["id"])
        lines.append(f"Release Radar has {len(rr_tracks)} tracks. Checking each one:\n\n")

        counts = {"found": 0, "not_followed": 0, "missing": 0}
        for t in rr_tracks:
            primary = t["artists"][0]
            is_followed = primary["id"] in followed
            found_in_pull = False
            if is_followed:
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
                counts["found"] += 1
            elif not is_followed:
                diagnosis = "artist is not in your followed list (not a seed artist)"
                counts["not_followed"] += 1
            else:
                diagnosis = (
                    'MISSING - not in artist_albums(include_groups="album,single") '
                    "top 50 results - likely a feature/guest appearance "
                    '(needs "appears_on") or the artist has 50+ releases and this one '
                    "fell outside that page"
                )
                counts["missing"] += 1

            lines.append(f"- **{t['name']}** by {primary['name']}: {diagnosis}\n")

        lines.append(
            f"\n**Summary: {counts['found']} would be found, "
            f"{counts['missing']} are missed by our current logic, "
            f"{counts['not_followed']} are from artists you don't follow.**\n"
        )

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))

    print("Diagnostic report written to job summary.")


if __name__ == "__main__":
    main()
