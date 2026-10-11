from pathlib import Path

import pytest

from apps.worker.file_storage import LocalFileStore


def test_store_returns_relative_ref_and_round_trips_bytes(tmp_path):
    store = LocalFileStore(tmp_path / "storage")
    content = b"\x00page output\xff"

    file_ref = store.put(content)

    assert "/" not in file_ref and "\\" not in file_ref
    assert store.path_for(file_ref).parent == (tmp_path / "storage").resolve()
    assert store.get(file_ref) == content


def test_empty_bytes_are_supported(tmp_path):
    store = LocalFileStore(tmp_path / "storage")
    file_ref = store.put(b"")
    assert store.get(file_ref) == b""


def test_storage_root_comes_from_storage_dir_environment_variable(tmp_path, monkeypatch):
    configured = tmp_path / "configured-files"
    monkeypatch.setenv("STORAGE_DIR", str(configured))
    store = LocalFileStore()
    file_ref = store.put(b"configured")

    assert store.path_for(file_ref).parent == configured.resolve()


@pytest.mark.parametrize(
    "file_ref",
    ["../outside", "..\\outside", "C:\\outside", "/etc/passwd", "", ".", "a" * 31, "A" * 32],
)
def test_rejects_invalid_or_traversal_references(tmp_path, file_ref):
    store = LocalFileStore(tmp_path / "storage")
    with pytest.raises(ValueError, match="invalid storage file reference"):
        store.get(file_ref)


def test_delete_removes_only_referenced_object(tmp_path):
    store = LocalFileStore(tmp_path / "storage")
    file_ref = store.put(b"remove me")
    target = store.path_for(file_ref)

    store.delete(file_ref)

    assert not target.exists()
    with pytest.raises(FileNotFoundError):
        store.get(file_ref)


def test_reference_to_symlink_escaping_storage_is_rejected(tmp_path):
    store = LocalFileStore(tmp_path / "storage")
    store.root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = store.root / ("a" * 32)
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable in this Windows environment")

    with pytest.raises(ValueError, match="escapes the storage directory"):
        store.get("a" * 32)


def test_rejects_non_binary_payload(tmp_path):
    store = LocalFileStore(tmp_path / "storage")
    with pytest.raises(TypeError, match="content must be bytes"):
        store.put("not bytes")
