from typing import BinaryIO, Protocol


class StorageBackend(Protocol):
    """Keys are relative paths built from validated ids only (docs/data-model.md § Storage)."""

    def put_bytes(self, key: str, data: bytes, overwrite: bool = False) -> None: ...

    def open_read(self, key: str) -> BinaryIO: ...

    def delete(self, key: str) -> None: ...
