"""Tenant-qualified local ObjectStore rooted strictly under data_dir."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from oria.rag.errors import ObjectStoreError

if TYPE_CHECKING:
    from oria.core.context import Context

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_OBJECT_BYTES = 8 * 1024 * 1024


class LocalObjectStore:
    """Store immutable objects without allowing traversal or symlink escapes."""

    def __init__(self, root: Path, data_root: Path) -> None:
        self._root = root.resolve(strict=False)
        self._data_root = data_root.resolve(strict=False)
        if not self._root.is_relative_to(self._data_root):
            raise ValueError("object store root escapes data_dir")

    async def __aenter__(self) -> LocalObjectStore:
        self._safe_directory(self._root)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def put(self, key: str, path: str, ctx: Context) -> str:
        source = Path(path)
        try:
            if source.is_symlink() or not source.is_file():
                raise ValueError("object source must be a regular file")
            data = source.read_bytes()
        except OSError as exc:
            raise ObjectStoreError("object source is unavailable") from exc
        return self.put_bytes(key, data, ctx)

    def put_bytes(self, key: str, data: bytes, ctx: Context) -> str:
        if len(data) > _MAX_OBJECT_BYTES:
            raise ValueError("object exceeds the local size limit")
        destination = self._key_path(key, ctx.tenant_id)
        self._safe_directory(destination.parent)
        if destination.exists():
            try:
                if destination.is_symlink() or destination.read_bytes() != data:
                    raise ObjectStoreError("existing object conflicts with immutable content")
            except OSError as exc:
                raise ObjectStoreError("existing object cannot be verified") from exc
            return f"object://{key}"
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            raise ObjectStoreError("object write failed") from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return f"object://{key}"

    async def get(self, key: str, dest: str, ctx: Context) -> str:
        data = self.read_bytes(f"object://{key}", ctx)
        destination = Path(dest).resolve(strict=False)
        if not destination.is_relative_to(self._data_root):
            raise ValueError("object destination escapes data_dir")
        self._safe_directory(destination.parent)
        if destination.exists() and destination.is_symlink():
            raise ValueError("object destination cannot be a symlink")
        try:
            destination.write_bytes(data)
        except OSError as exc:
            raise ObjectStoreError("object copy failed") from exc
        return str(destination)

    def read_bytes(self, object_ref: str, ctx: Context) -> bytes:
        if not object_ref.startswith("object://"):
            raise ValueError("invalid object reference")
        key = object_ref.removeprefix("object://")
        path = self._key_path(key, ctx.tenant_id)
        try:
            if path.is_symlink() or not path.is_file():
                raise ObjectStoreError("object is unavailable")
            return path.read_bytes()
        except OSError as exc:
            raise ObjectStoreError("object read failed") from exc

    def delete_ref(self, object_ref: str, ctx: Context) -> None:
        if not object_ref.startswith("object://"):
            raise ValueError("invalid object reference")
        path = self._key_path(object_ref.removeprefix("object://"), ctx.tenant_id)
        try:
            if path.is_symlink():
                raise ValueError("object path cannot be a symlink")
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise ObjectStoreError("object deletion failed") from exc

    def _key_path(self, key: str, tenant_id: str) -> Path:
        parts = key.split("/")
        if (
            len(parts) < 2
            or parts[0] != tenant_id
            or any(_SEGMENT.fullmatch(part) is None for part in parts)
        ):
            raise ValueError("object key is not tenant-qualified")
        path = self._root.joinpath(*parts)
        if not path.resolve(strict=False).is_relative_to(self._root):
            raise ValueError("object key escapes the configured root")
        return path

    def _safe_directory(self, path: Path) -> None:
        if not path.resolve(strict=False).is_relative_to(self._data_root):
            raise ValueError("object directory escapes data_dir")
        relative = path.relative_to(self._data_root)
        current = self._data_root
        if current.exists() and current.is_symlink():
            raise ValueError("data_dir cannot be a symlink")
        current.mkdir(parents=True, exist_ok=True)
        for part in relative.parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise ValueError("object directory cannot contain symlinks")
            current.mkdir(exist_ok=True)
