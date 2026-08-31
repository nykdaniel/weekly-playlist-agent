"""
One-off diagnostic (read-only, no changes to Spotify): lists every seed
artist (followed + artists behind "ecstatic tracks") whose genres match the
funk/phonk family, so the user can see how many artists a genre-based
filter would actually affect before deciding its scope.
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
FUNK_GENRE_KEYWORDS = ["funk", "phonk", "sertanejo", "brega", "forr"]


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


def is_funk_family(genres):
    return any(
        keyword in genre.lower()
        for genre in genres
        for keyword in FUNK_GENRE_KEYWORDS
    )


def main():
    sp = get_spotify_client()

    followed = get_followed_artists(sp)
    ecstatic_stubs = get_ecstatic_seed_artists(sp)
    new_ids = [aid for aid in ecstatic_stubs if aid not in followed]
    hydrate_genres(sp, new_ids, followed)

    ecstatic_ids = set(ecstatic_stubs.keys())

    seed_artists = dict(followed)
    for aid in new_ids:
        seed_artists[aid] = seed_artists.get(aid, ecstatic_stubs[aid])

    matches = []
    for aid, artist in seed_artists.items():
        genres = artist.get("genres", [])
        if is_funk_family(genres):
            source = []
            if aid in followed:
                source.append("followed")
            if aid in ecstatic_ids:
                source.append("in ecstatic tracks")
            matches.append((artist["name"], genres, source))

    matches.sort(key=lambda m: m[0].lower())

    summary_path = os.environ["GITHUB_STEP_SUMMARY"]
    with open(summary_path, "a") as f:
        f.write(
            f"## Funk/phonk-family artists in your seed pool\n\n"
            f"{len(seed_artists)} total seed artists. "
            f"{len(matches)} match funk/phonk/sertanejo/brega/forro genres.\n\n"
        )
        for name, genres, source in matches:
            f.write(
                f"- **{name}** - genres: {', '.join(genres)} - "
                f"source: {', '.join(source)}\n"
            )

    print(f"{len(matches)} funk-family artists out of {len(seed_artists)} seed artists.")


if __name__ == "__main__":
    main()
