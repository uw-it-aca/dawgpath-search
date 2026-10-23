# Copyright 2026 UW-IT, University of Washington
# SPDX-License-Identifier: Apache-2.0

"""Offline archive handling for trusted, immutable search indexes."""

import gzip
import io
import os
import tarfile
import tempfile
import zlib
from pathlib import Path

from .builder import (
    METADATA_FILE,
    IndexValidationError,
    _file_metadata,
    _index_files,
    _json_input,
    _read_metadata,
    _validate_metadata,
    open_index,
)

MAX_INDEX_METADATA_BYTES = 1024 * 1024


def create_bundle(index_dir: str | Path, *, release_id: str) -> bytes:
    """Verify a trusted, inactive index and archive its immutable files.

    Do not modify the index while bundling. Identical files produce identical
    archive bytes; independently rebuilt Whoosh indexes may differ.
    """
    root = Path(index_dir)
    index = open_index(root, release_id=release_id)
    index.close()
    metadata = _read_metadata(root, release_id)
    content = (root / METADATA_FILE).read_bytes()
    if len(content) > MAX_INDEX_METADATA_BYTES:
        raise IndexValidationError("Index metadata exceeds archive size limit")
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for name in sorted([*metadata["files"], METADATA_FILE]):
            content = (root / name).read_bytes()
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


def prepare_bundle(
    bundle: bytes,
    output_dir: str | Path,
    *,
    metadata: dict,
    release_id: str,
) -> dict:
    """Verify/extract a trusted bundle to a fresh directory, without activation.

    The caller must authenticate the source and verify the bundle's outer
    checksum and expected metadata. Whoosh schemas use Python serialization;
    checksums alone do not make an untrusted index safe to open.
    """
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output directory already exists: {output}")
    _validate_metadata(metadata, release_id)
    files = _index_files(metadata)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".prepare-index-", dir=output.parent
    ) as temporary:
        staging = Path(temporary) / "index"
        staging.mkdir()
        seen = set()
        expected = {*files, METADATA_FILE}
        try:
            with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
                for member in archive:
                    if (
                        member.name not in expected
                        or member.name in seen
                        or not member.isfile()
                        or member.size < 0
                    ):
                        raise IndexValidationError(
                            "Unexpected or unsafe archive member"
                        )
                    limit = (
                        MAX_INDEX_METADATA_BYTES
                        if member.name == METADATA_FILE
                        else files[member.name]["size_bytes"]
                    )
                    if member.size > limit:
                        raise IndexValidationError(
                            "Archive member exceeds recorded size"
                        )
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise IndexValidationError("Cannot read archive member")
                    with stream:
                        content = stream.read(limit + 1)
                    if member.name == METADATA_FILE:
                        if _json_input(content, METADATA_FILE) != metadata:
                            raise IndexValidationError(
                                "Bundle metadata differs from expected metadata"
                            )
                    elif _file_metadata(content) != files[member.name]:
                        raise IndexValidationError(
                            f"Index checksum/size mismatch: {member.name}"
                        )
                    (staging / member.name).write_bytes(content)
                    seen.add(member.name)
        except (tarfile.TarError, EOFError, zlib.error) as exc:
            raise IndexValidationError(f"Invalid index archive: {exc}") from exc
        if seen != expected:
            raise IndexValidationError("Archive is missing index files")
        index = open_index(staging, release_id=release_id)
        index.close()
        # Reserve the fresh destination; never replace an existing active index.
        output.mkdir()
        try:
            os.replace(staging, output)
        except OSError:
            output.rmdir()
            raise
    return metadata
