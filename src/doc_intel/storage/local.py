import os
import tempfile
from pathlib import Path
from typing import BinaryIO


class LocalStorage:
    """`StorageBackend` on the local filesystem under `root`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        if not key or Path(key).is_absolute():
            raise ValueError("invalid storage key")
        root = self.root.resolve()
        path = (root / key).resolve()  # resolves `..` and symlinks before the containment check
        if not path.is_relative_to(root) or path == root:
            raise ValueError("storage key escapes root")
        return path

    def put_bytes(self, key: str, data: bytes, overwrite: bool = False) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if overwrite:
                os.replace(tmp, path)
            else:
                os.link(tmp, path)  # atomic create-if-absent: raises FileExistsError
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def open_read(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
