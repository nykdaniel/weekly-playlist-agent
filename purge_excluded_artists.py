"""
One-off cleanup: removes tracks by explicitly-excluded artists AND tracks
whose primary artist carries a Brazilian funk/phonk-family genre tag, from
the existing "Discover Daily" playlist. Only removes those specific tracks -
does not touch anything else in the playlist.
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
DISCOVER_PLAYLIST_NAME = "Discover Daily"
EXCLUDED_ARTIST_IDS = {
    "3xvaSlT4xsyk6lY1ESOspO",  # Disney
    "4hV3aU0WKvFaiX5ugXP5hp",  # MC MN
    "4mb1xtQVGSK5dh8AbtwBiR",  # MC Lan
    "3l4fsEzoeabsET7ddv0lZW",  # Mc Delux
    "6Vxu4TDCN5TMlRpdu6a2Ag",  # MC K9
    "7r1L3aZERnrbKkMXUgVRdX",  # Tom Lysar
    "1APqNiQUA2XpwLEbywSWmZ",  # Tropa da W&S
}

# Exact genre tags (NOT substrings - "liquid funk"/"uk funky"/"funky house"/"g-funk"
# are unrelated genres and must never match).
BRAZILIAN_FUNK_GENRES = {
    "brazilian funk",
    "brazilian phonk",
    "funk carioca",
    "funk bruxaria",
    "brega funk",
    "funk consciente",
    "funk de bh",
    "funk pop",
    "trap funk",
    "brazilian trap",
    "sertanejo universitário",
    "sertanejo",
}


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


def has_excluded_genre(genres):
    return any(g in BRAZILIAN_FUNK_GENRES for g in genres)


def main():
    sp = get_spotify_client()
    summary_path = os.environ["GITHUB_STEP_SUMMARY"]
    lines = ["## Purge excluded artists/genres from Discover Daily\n\n"]

    playlist = find_playlist_by_name(sp, DISCOVER_PLAYLIST_NAME)
    if not playlist:
        lines.append(f'Could not find "{DISCOVER_PLAYLIST_NAME}" playlist.\n')
        with open(summary_path, "a") as f:
            f.write("\n".join(lines))
        return

    tracks = get_playlist_tracks(sp, playlist["id"])

    # Batch-fetch genres for every primary artist in the playlist once.
    primary_artist_ids = sorted({
        t["artists"][0]["id"] for t in tracks if t.get("artists")
    })
    artist_genres = {}
    for i in range(0, len(primary_artist_ids), 50):
        batch = primary_artist_ids[i : i + 50]
        for artist in sp.artists(batch)["artists"]:
            if artist:
                artist_genres[artist["id"]] = artist.get("genres", [])

    by_artist_id = []
    by_genre = []
    for t in tracks:
        if not t.get("artists"):
            continue
        primary_id = t["artists"][0]["id"]
        if primary_id in EXCLUDED_ARTIST_IDS:
            by_artist_id.append(t)
        elif has_excluded_genre(artist_genres.get(primary_id, [])):
            by_genre.append(t)

    to_remove = by_artist_id + by_genre
    to_remove_uris = [t["uri"] for t in to_remove]

    lines.append(
        f"Playlist had {len(tracks)} tracks. "
        f"Removing {len(to_remove_uris)} tracks "
        f"({len(by_artist_id)} by explicitly excluded artists, "
        f"{len(by_genre)} by Brazilian funk/phonk genre tag).\n\n"
    )
    if by_genre:
        lines.append("### Removed via genre match\n\n")
        seen_names = set()
        for t in by_genre:
            name = t["artists"][0]["name"]
            if name not in seen_names:
                seen_names.add(name)
                lines.append(f"- {name}\n")
        lines.append("\n")

    if to_remove_uris:
        for i in range(0, len(to_remove_uris), 100):
            batch = to_remove_uris[i : i + 100]
            sp.playlist_remove_all_occurrences_of_items(playlist["id"], batch)
        lines.append(f"Removed {len(to_remove_uris)} tracks.\n")
    else:
        lines.append("Nothing to remove.\n")

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))

    print(f"Removed {len(to_remove_uris)} tracks.")


if __name__ == "__main__":
    main()
