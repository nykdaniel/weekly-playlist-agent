"""
One-off diagnostic (read-only, no changes to Spotify): tests several
variants of Spotify's `label:` search filter for "Make The Girls Dance
Records" to figure out why `label:"X" tag:new` returned zero results, and
whether searching without tag:new (then filtering by release date
ourselves, like we already do for seed artists) is a better approach.
"""

import os
from datetime import date, timedelta

import spotipy
from spotipy.oauth2 import SpotifyOAuth

SCOPES = (
    "user-follow-read "
    "playlist-read-private "
    "playlist-read-collaborative "
    "playlist-modify-public "
    "playlist-modify-private"
)

LABEL = "Make The Girls Dance Records"


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


def try_search(sp, q, type_="track", limit=20):
    try:
        results = sp.search(q=q, type=type_, limit=limit)
        key = type_ + "s"
        items = results[key]["items"]
        return items, None
    except spotipy.SpotifyException as e:
        return None, str(e)


def main():
    sp = get_spotify_client()
    summary_path = os.environ["GITHUB_STEP_SUMMARY"]
    lines = ["## Label search variants\n\n"]

    queries = [
        (f'label:"{LABEL}" tag:new', "track"),
        (f'label:"{LABEL}"', "track"),
        (f'label:"{LABEL}"', "album"),
        (f"label:{LABEL}", "track"),
        (f'label:"make the girls dance records"', "track"),
    ]

    for q, type_ in queries:
        items, err = try_search(sp, q, type_=type_, limit=20)
        if err:
            lines.append(f"- `{q}` (type={type_}) -> ERROR: {err}\n")
            continue
        lines.append(f"- `{q}` (type={type_}) -> {len(items)} results\n")
        for item in items[:5]:
            if type_ == "track":
                artists = ", ".join(a["name"] for a in item["artists"])
                rd = item.get("album", {}).get("release_date", "?")
                lines.append(f"  - {item['name']} by {artists} (released {rd})\n")
            else:
                artists = ", ".join(a["name"] for a in item["artists"])
                lines.append(f"  - {item['name']} by {artists} (released {item.get('release_date', '?')})\n")

    # Also: how many of the label's albums, found via plain label search,
    # fall inside our normal 14-day new-release lookback window?
    lines.append("\n### Release-date check (last 14 days) via plain label search, type=album\n\n")
    albums, err = try_search(sp, f'label:"{LABEL}"', type_="album", limit=50)
    if err:
        lines.append(f"ERROR: {err}\n")
    else:
        cutoff = date.today() - timedelta(days=14)
        lines.append(f"{len(albums)} albums returned total.\n\n")
        for a in albums:
            rd = a.get("release_date", "")
            try:
                parts = rd.split("-")
                y, m, d = int(parts[0]), int(parts[1]) if len(parts) > 1 else 1, int(parts[2]) if len(parts) > 2 else 1
                rd_date = date(y, m, d)
                is_new = rd_date >= cutoff
            except Exception:
                is_new = False
            flag = "NEW" if is_new else ""
            lines.append(f"- {a['name']} - released {rd} {flag}\n")

    with open(summary_path, "a") as f:
        f.write("\n".join(lines))

    print("Done.")


if __name__ == "__main__":
    main()
