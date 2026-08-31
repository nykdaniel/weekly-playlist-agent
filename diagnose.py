"""
One-off diagnostic (read-only, no changes to Spotify): given a playlist the
user says is curated by a specific record label, looks at the tracks'
actual Spotify "label" metadata (from each track's album) to find the exact
label string as registered on Spotify, then tests whether Spotify's
`label:"X" tag:new` search filter actually returns anything for it - before
wiring a new label into the daily discovery pipeline.
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

# playlist_id -> what the user called it
TARGET_PLAYLISTS = {
    "4GGqmAHV52sOymfg9bEkQR": "Make The Girls Dance",
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


def get_playlist_tracks(sp, playlist_id, limit=100):
    tracks = []
    results = sp.playlist_items(playlist_id, additional_types=["track"], limit=100)
    while results and len(tracks) < limit:
        for item in results["items"]:
            track = item.get("track")
            if track:
                tracks.append(track)
        results = sp.next(results) if results["next"] else None
    return tracks[:limit]


def main():
    sp = get_spotify_client()
    summary_path = os.environ["GITHUB_STEP_SUMMARY"]
    lines = ["## Label discovery check\n\n"]

    for playlist_id, user_label_name in TARGET_PLAYLISTS.items():
        try:
            playlist = sp.playlist(
                playlist_id, fields="name,description,owner,tracks.total"
            )
        except spotipy.SpotifyException as e:
            lines.append(f"### Could not open playlist `{playlist_id}`: {e}\n\n")
            continue

        lines.append(
            f"### Playlist: {playlist['name']} (`{playlist_id}`)\n\n"
            f"- called by user: \"{user_label_name}\"\n"
            f"- owner: {playlist['owner']['display_name']}\n"
            f"- description: {playlist.get('description') or '(none)'}\n"
            f"- total tracks: {playlist['tracks']['total']}\n\n"
        )

        tracks = get_playlist_tracks(sp, playlist_id, limit=50)
        album_ids = list({t["album"]["id"] for t in tracks if t.get("album")})

        label_counts = Counter()
        for i in range(0, len(album_ids), 20):
            batch = album_ids[i : i + 20]
            for album in sp.albums(batch)["albums"]:
                if album and album.get("label"):
                    label_counts[album["label"]] += 1

        lines.append(
            f"Sampled {len(tracks)} tracks / {len(album_ids)} albums. "
            f"Label field values found:\n\n"
        )
        for label, count in label_counts.most_common():
            lines.append(f"- `{label}` - {count} albums\n")
        lines.append("\n")

        top_label = label_counts.most_common(1)
        if top_label:
            label_name, _ = top_label[0]
            try:
                results = sp.search(
                    q=f'label:"{label_name}" tag:new', type="track", limit=20
                )
                found = results["tracks"]["items"]
            except spotipy.SpotifyException as e:
                lines.append(f"Search test for `label:\"{label_name}\"` failed: {e}\n\n")
                found = None

            if found is not None:
                lines.append(
                    f'`label:"{label_name}" tag:new` search returns {len(found)} tracks:\n\n'
                )
                for t in found[:10]:
                    artist_names = ", ".join(a["name"] for a in t["artists"])
                    lines.append(f"  - {t['name']} by {artist_names}\n")
                lines.append("\n")

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))

    print("Done.")


if __name__ == "__main__":
    main()
