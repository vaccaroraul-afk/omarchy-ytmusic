import os
import stat
import struct

import pytest
from mutagen.flac import FLAC, Picture

import ytm
import ytm_local as local


def flac(path, seconds=65, **tags):
    """A tiny valid FLAC (STREAMINFO only, no audio frames) with tags."""
    rate = 44100
    body = struct.pack(">HH", 4096, 4096) + bytes(6)
    body += ((rate << 44) | (1 << 41) | (15 << 36) | seconds * rate).to_bytes(8, "big") + bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(body).to_bytes(3, "big") + body)
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


@pytest.fixture
def music(tmp_path):
    root = tmp_path / "Music"
    album = root / "Artist - Album (2020)"
    flac(album / "b.flac", title="Second", artist="Artist", album="Album", tracknumber="2/3", date="2020-05-01")
    flac(album / "a.flac", title="First", artist=["Artist", "Guest"], album="Album", tracknumber="1/3",
         albumartist="Artist")
    flac(album / "10 untagged.flac")
    (album / "folder.png").write_bytes(b"png")
    (album / "Cover.jpg").write_bytes(b"jpg")
    (album / "notes.pdf").write_bytes(b"pdf")
    flac(root / "Loose.flac", title="Loose")
    (root / ".hidden").mkdir()
    flac(root / ".hidden" / "x.flac")
    (root / "Empty").mkdir()
    flac(root / "Various" / "Disc 2" / "track.flac", title="Deep", tracknumber="1")
    return root


@pytest.fixture
def library(music, tmp_path):
    return local.Library(roots=[str(music)], cache_dir=tmp_path / "cache")


def test_music_dirs_come_from_env_then_xdg_then_home(tmp_path):
    home = str(tmp_path)
    assert local.music_dirs({"YTM_MUSIC_DIRS": "/a/b::/c/../d:/a/b"}, home) == ["/a/b", "/d"]
    assert local.music_dirs({}, home) == [os.path.join(home, "Music")]
    config = tmp_path / ".config"
    config.mkdir()
    (config / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/Downloads"\nXDG_MUSIC_DIR="$HOME/Música"\n')
    assert local.music_dirs({}, home) == [os.path.join(home, "Música")]
    (config / "user-dirs.dirs").write_text('XDG_MUSIC_DIR="$HOME/"\n')
    assert local.xdg_music_dir({}, home) is None, "$HOME/ disables the directory"


def test_top_level_lists_folders_and_loose_songs(library, music):
    page = library.folder("")
    assert (page["kind"], page["id"], page["title"]) == ("folder", str(music), "Music")
    assert page["subtitle"].endswith("2 folders • 1 song")
    assert [f["title"] for f in page["folders"]] == ["Artist - Album (2020)", "Various"], "hidden and empty skipped"
    album = page["folders"][0]
    assert album["subtitle"] == "3 songs"
    assert album["thumb"] == (music / "Artist - Album (2020)" / "Cover.jpg").as_uri(), "cover beats folder"
    assert "%20" in album["thumb"] and "(" not in album["thumb"]
    assert page["folders"][1]["subtitle"] == "1 folder"
    assert [t["title"] for t in page["tracks"]] == ["Loose"]


def test_folder_tracks_are_tagged_and_ordered(library, music):
    page = library.folder(str(music / "Artist - Album (2020)"))
    assert [t["title"] for t in page["tracks"]] == ["First", "Second", "10 untagged"]
    first = page["tracks"][0]
    assert first["local"] is True and first["videoId"] == "" and first["path"].endswith("/a.flac")
    assert first["artistText"] == "Artist, Guest" and first["artists"][1] == {"name": "Guest", "id": None}
    assert first["album"] == {"name": "Album", "id": None}
    assert first["durationSec"] == 65 and first["available"] is True
    assert page["tracks"][2]["artists"] == [] and page["tracks"][2]["album"] is None
    assert page["subtitle"] == "Artist • 3 songs"
    assert library.folder(str(music / "Artist - Album (2020)") + "/")["id"] == page["id"]


def test_paths_outside_the_music_folders_are_refused(library, music, tmp_path):
    for path in ("/etc", str(music / ".." / "elsewhere"), str(tmp_path)):
        with pytest.raises(local.LocalError) as err:
            library.folder(path)
        assert err.value.code == "BAD_REQUEST"
    with pytest.raises(local.LocalError) as err:
        library.folder(str(music / "missing"))
    assert err.value.code == "NOT_FOUND"
    with pytest.raises(local.LocalError) as err:
        local.Library(roots=[str(tmp_path / "nope")], cache_dir=tmp_path / "c").folder("")
    assert err.value.code == "NOT_FOUND" and "nope" in err.value.message


def test_several_roots_get_a_virtual_top_level(music, tmp_path):
    other = tmp_path / "Other"
    flac(other / "x.flac")
    library = local.Library(roots=[str(music), str(other)], cache_dir=tmp_path / "cache")
    page = library.folder("")
    assert page["id"] == "" and [f["path"] for f in page["folders"]] == [str(music), str(other)]
    assert library.folder(str(other))["tracks"][0]["title"] == "x"
    assert len(library.tracks_under("")["tracks"]) == 6


def test_tracks_under_walks_subfolders_in_order(library, music):
    os.symlink(music, music / "Various" / "loop")
    result = library.tracks_under(str(music))
    titles = [t["title"] for t in result["tracks"]]
    assert titles == ["Loose", "First", "Second", "10 untagged", "Deep"], "symlink loop visited once"
    assert result["trackCount"] == 5 and result["truncated"] is False
    deep = result["tracks"][-1]
    assert deep["thumb"] is None


def test_tags_are_read_once_per_file_version(music, tmp_path):
    calls = []

    def reader(path):
        calls.append(path)
        return local.read_tags(path)

    album = str(music / "Artist - Album (2020)")
    library = local.Library(roots=[str(music)], cache_dir=tmp_path / "cache", reader=reader)
    library.folder(album)
    assert len(calls) == 3
    cache_file = tmp_path / "cache" / "local-tags.json"
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
    again = local.Library(roots=[str(music)], cache_dir=tmp_path / "cache", reader=reader)
    again.folder(album)
    assert len(calls) == 3, "answered from local-tags.json"
    flac(music / "Artist - Album (2020)" / "a.flac", seconds=70, title="Renamed", tracknumber="1")
    assert again.folder(album)["tracks"][0]["title"] == "Renamed"
    assert len(calls) == 4


def test_embedded_cover_is_extracted_once(music, tmp_path):
    song = flac(music / "Embedded" / "song.flac", title="Song")
    picture = Picture()
    picture.type, picture.mime, picture.data = 3, "image/png", b"\x89PNG fake"
    audio = FLAC(song)
    audio.add_picture(picture)
    audio.save()
    flac(music / "Bare" / "song.flac")
    calls = []

    def spy(path):
        calls.append(path)
        return local.embedded_picture(path)

    library = local.Library(roots=[str(music)], cache_dir=tmp_path / "cache", picture=spy)
    page = library.folder(str(music / "Embedded"))
    thumb = page["thumb"]
    assert thumb.startswith((tmp_path / "cache" / "local-art").as_uri()) and thumb.endswith(".png")
    assert page["tracks"][0]["thumb"] == thumb
    assert library.folder(str(music / "Bare"))["thumb"] is None
    library.folder(str(music / "Embedded"))
    library.folder(str(music / "Bare"))
    assert len(calls) == 2, "pictures (and their absence) are remembered"


def test_worker_method_and_lane(library, music):
    worker = ytm.Worker(object(), "none", library=library)
    reply = worker.handle({"id": 1, "method": "localFolder", "params": {}})
    assert reply["ok"] and reply["result"]["id"] == str(music)
    reply = worker.handle({"id": 2, "method": "localFolder", "params": {"path": str(music), "recursive": True}})
    assert reply["ok"] and len(reply["result"]["tracks"]) == 5
    assert worker.handle({"id": 3, "method": "localFolder", "params": {"path": 5}})["error"]["code"] == "BAD_REQUEST"
    assert worker.handle({"id": 4, "method": "localFolder", "params": {"path": "/etc"}})["error"]["code"] == "BAD_REQUEST"
    assert ytm.LANES["localFolder"] == "local"
