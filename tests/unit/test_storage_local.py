import pytest

from doc_intel.storage.local import LocalStorage


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "storage")


def test_put_and_read(storage):
    storage.put_bytes("users/u1/projects/p1/uploads/d1/original.pdf", b"%PDF-1.7")
    with storage.open_read("users/u1/projects/p1/uploads/d1/original.pdf") as f:
        assert f.read() == b"%PDF-1.7"


def test_write_once_refuses_overwrite(storage):
    storage.put_bytes("a/b.bin", b"one")
    with pytest.raises(FileExistsError):
        storage.put_bytes("a/b.bin", b"two")
    with storage.open_read("a/b.bin") as f:
        assert f.read() == b"one"


def test_overwrite_true_replaces(storage):
    storage.put_bytes("parsed/d1.json", b"{}")
    storage.put_bytes("parsed/d1.json", b'{"v": 2}', overwrite=True)
    with storage.open_read("parsed/d1.json") as f:
        assert f.read() == b'{"v": 2}'


def test_no_temp_files_left_behind(storage):
    storage.put_bytes("a/b.bin", b"x")
    with pytest.raises(FileExistsError):
        storage.put_bytes("a/b.bin", b"y")
    assert [p.name for p in (storage.root / "a").iterdir()] == ["b.bin"]


def test_delete(storage):
    storage.put_bytes("a/b.bin", b"x")
    storage.delete("a/b.bin")
    with pytest.raises(FileNotFoundError):
        storage.open_read("a/b.bin")


@pytest.mark.parametrize(
    "key", ["", "../escape.bin", "a/../../escape.bin", "/etc/passwd", "a/./../.."]
)
def test_keys_outside_root_are_rejected(storage, key):
    with pytest.raises(ValueError):
        storage.put_bytes(key, b"x")


def test_symlink_escape_is_rejected(storage, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    storage.root.mkdir(parents=True, exist_ok=True)
    (storage.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        storage.put_bytes("link/evil.bin", b"x")
    assert not (outside / "evil.bin").exists()
