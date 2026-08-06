import io
import json

import pytest

import installer_core as ic


# ---------------------------------------------------------------- parse_tag

@pytest.mark.parametrize("tag,expected", [
    ("v2.0", (2, 0)),
    ("V2.0", (2, 0)),
    ("2.0", (2, 0)),
    ("v2.10.1", (2, 10, 1)),
    ("v2.0-alpha", (2, 0)),
    ("v2.0-beta.3", (2, 0)),
    ("", None),
    (None, None),
    ("release-candidate", None),
])
def test_parse_tag(tag, expected):
    assert ic.parse_tag(tag) == expected


# ------------------------------------------------------------------ is_newer

def test_is_newer_by_numeric_tag():
    assert ic.is_newer("v2.1", None, "v2.0", None) is True
    assert ic.is_newer("v2.0", None, "v2.1", None) is False


def test_is_newer_same_tag_string_compares_by_date():
    assert ic.is_newer("v2.0", "2026-08-10T12:00:00Z", "v2.0", "2026-08-01T00:00:00Z") is True
    assert ic.is_newer("v2.0", "2026-08-01T00:00:00Z", "v2.0", "2026-08-10T12:00:00Z") is False


def test_is_newer_same_tag_missing_date_is_not_newer():
    assert ic.is_newer("v2.0", None, "v2.0", None) is False
    assert ic.is_newer("v2.0", "2026-08-10T12:00:00Z", "v2.0", None) is False


def test_is_newer_same_numeric_version_different_suffix_falls_back_to_date():
    # v2.0-beta переопубликован как v2.0 (финал) позже -> считаем новее.
    assert ic.is_newer("v2.0", "2026-08-10T12:00:00Z", "v2.0-beta", "2026-08-01T00:00:00Z") is True
    assert ic.is_newer("v2.0-beta", "2026-08-01T00:00:00Z", "v2.0", "2026-08-10T12:00:00Z") is False


def test_is_newer_unparsable_tags_never_guess():
    assert ic.is_newer("release-candidate", "2026-08-10T12:00:00Z", "v2.0", "2026-08-01T00:00:00Z") is False
    assert ic.is_newer("release-candidate", None, "also-not-a-version", None) is False


# ----------------------------------------------------------- is_windows_asset

@pytest.mark.parametrize("name,expected", [
    ("IVTrace-v2.0-win64.zip", True),
    ("IVTrace-v2.0-Win32.zip", True),
    ("IVTrace-v2.0-WINDOWS.zip", True),
    ("Windows-Setup.exe", True),
    ("ivtrace-darwin-arm64.tar.gz", False),
    ("ivtrace-linux-x86_64.AppImage", False),
    ("source.tar.gz", False),
    ("", False),
])
def test_is_windows_asset(name, expected):
    assert ic.is_windows_asset(name) is expected


# --------------------------------------------------------- pick_windows_asset

def test_pick_windows_asset_finds_first_match():
    release = {"assets": [
        {"name": "ivtrace-darwin.tar.gz"},
        {"name": "IVTrace-v2.0-win64.zip"},
    ]}
    assert ic.pick_windows_asset(release)["name"] == "IVTrace-v2.0-win64.zip"


def test_pick_windows_asset_none_when_no_match():
    assert ic.pick_windows_asset({"assets": [{"name": "linux.tar.gz"}]}) is None
    assert ic.pick_windows_asset({"assets": []}) is None
    assert ic.pick_windows_asset({}) is None


# ----------------------------------------------------- select_latest_release

def _release(tag, date, prerelease=False, draft=False, has_win_asset=True):
    assets = [{"name": f"IVTrace-{tag}-win64.zip"}] if has_win_asset else [{"name": f"IVTrace-{tag}.tar.gz"}]
    return {"tag_name": tag, "published_at": date, "prerelease": prerelease, "draft": draft, "assets": assets}


def test_select_latest_release_picks_highest_tag():
    releases = [_release("v2.0", "2026-08-10T00:00:00Z"), _release("v1.9", "2026-07-01T00:00:00Z")]
    assert ic.select_latest_release(releases)["tag_name"] == "v2.0"


def test_select_latest_release_ignores_draft_and_prerelease():
    releases = [
        _release("v3.0", "2026-09-01T00:00:00Z", draft=True),
        _release("v2.5", "2026-08-20T00:00:00Z", prerelease=True),
        _release("v2.0", "2026-08-10T00:00:00Z"),
    ]
    assert ic.select_latest_release(releases)["tag_name"] == "v2.0"


def test_select_latest_release_skips_releases_without_windows_asset():
    releases = [
        _release("v2.1", "2026-08-15T00:00:00Z", has_win_asset=False),
        _release("v2.0", "2026-08-10T00:00:00Z"),
    ]
    assert ic.select_latest_release(releases)["tag_name"] == "v2.0"


def test_select_latest_release_same_tag_reupload_wins_by_date():
    releases = [
        _release("v2.0", "2026-08-01T00:00:00Z"),
        _release("v2.0", "2026-08-15T00:00:00Z"),
    ]
    assert ic.select_latest_release(releases)["published_at"] == "2026-08-15T00:00:00Z"


def test_select_latest_release_empty_list_returns_none():
    assert ic.select_latest_release([]) is None
    assert ic.select_latest_release(None) is None


# --------------------------------------------------------------- own_build_tag

def test_own_build_tag_none_without_version_file(monkeypatch, tmp_path):
    monkeypatch.setattr("apppaths.assets_dir", lambda: tmp_path)
    assert ic.own_build_tag() is None


def test_own_build_tag_reads_version_file(monkeypatch, tmp_path):
    monkeypatch.setattr("apppaths.assets_dir", lambda: tmp_path)
    (tmp_path / "VERSION").write_text("v2.0\n", encoding="utf-8")
    assert ic.own_build_tag() == "v2.0"


def test_own_build_tag_none_for_empty_version_file(monkeypatch, tmp_path):
    monkeypatch.setattr("apppaths.assets_dir", lambda: tmp_path)
    (tmp_path / "VERSION").write_text("   \n", encoding="utf-8")
    assert ic.own_build_tag() is None


# ---------------------------------------------------------------- copy_payload

def test_copy_payload_copies_tree(tmp_path):
    src = tmp_path / "src"
    (src / "nested").mkdir(parents=True)
    (src / "IVTrace.exe").write_bytes(b"exe")
    (src / "nested" / "data.json").write_text("{}")

    dest = tmp_path / "dest"
    ic.copy_payload(src, dest)

    assert (dest / "IVTrace.exe").read_bytes() == b"exe"
    assert (dest / "nested" / "data.json").read_text() == "{}"


def test_copy_payload_overwrites_existing_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "IVTrace.exe").write_bytes(b"new")

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "IVTrace.exe").write_bytes(b"old")
    (dest / "leftover.txt").write_text("stale but unrelated")

    ic.copy_payload(src, dest)

    assert (dest / "IVTrace.exe").read_bytes() == b"new"
    assert (dest / "leftover.txt").exists()  # copytree(dirs_exist_ok=True) не чистит лишнее — это не rsync --delete


# -------------------------------------------------------------- create_shortcut

def test_create_shortcut_invokes_powershell_with_expected_paths(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ic.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))

    target = tmp_path / "IVTrace.exe"
    shortcut = tmp_path / "Desktop" / "IVTrace.lnk"
    ic.create_shortcut(target, shortcut)

    assert shortcut.parent.is_dir()  # родительская папка создаётся заранее
    assert len(calls) == 1
    args, kwargs = calls[0]
    command = args[0]
    assert command[0] == "powershell"
    script = command[-1]
    assert str(shortcut) in script
    assert str(target) in script
    assert kwargs.get("check") is True


# ---------------------------------------------------------------- fetch_releases

class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_releases_parses_json_list(monkeypatch):
    payload = json.dumps([{"tag_name": "v2.0"}]).encode("utf-8")
    monkeypatch.setattr(ic.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(payload))
    releases = ic.fetch_releases()
    assert releases == [{"tag_name": "v2.0"}]


def test_fetch_releases_rejects_non_list_response(monkeypatch):
    payload = json.dumps({"not": "a list"}).encode("utf-8")
    monkeypatch.setattr(ic.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(payload))
    with pytest.raises(ValueError):
        ic.fetch_releases()


def test_fetch_releases_propagates_network_errors(monkeypatch):
    def _raise(*a, **kw):
        raise OSError("no internet")
    monkeypatch.setattr(ic.urllib.request, "urlopen", _raise)
    with pytest.raises(OSError):
        ic.fetch_releases()
