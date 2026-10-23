# Copyright 2026 UW-IT, University of Washington
# SPDX-License-Identifier: Apache-2.0

import copy
import gzip
import io
import json
import socket
import tarfile
from unittest.mock import patch

from support import SnapshotTestCase

from dawgpath_search import (
    IndexValidationError,
    create_bundle,
    open_index,
    prepare_bundle,
)
from dawgpath_search.builder import METADATA_FILE, _file_metadata
from dawgpath_search.bundle import MAX_INDEX_METADATA_BYTES


class BundleTest(SnapshotTestCase):
    def setUp(self):
        super().setUp()
        self.metadata = self.build()
        self.destination = self.root / "prepared"
        self.members = []
        for name in sorted([*self.metadata["files"], METADATA_FILE]):
            member = tarfile.TarInfo(name)
            content = (self.output / name).read_bytes()
            member.size = len(content)
            member.mode = 0o644
            self.members.append((member, content))

    def archive(self, members=None):
        buffer = io.BytesIO()
        with (
            gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed,
            tarfile.open(fileobj=compressed, mode="w") as archive,
        ):
            for member, content in self.members if members is None else members:
                archive.addfile(member, io.BytesIO(content))
        return buffer.getvalue()

    def prepare(self, bundle=None, metadata=None):
        return prepare_bundle(
            self.archive() if bundle is None else bundle,
            self.destination,
            metadata=self.metadata if metadata is None else metadata,
            release_id="test-release",
        )

    def assert_clean(self):
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".prepare-index-*")), [])

    def test_producer_preserves_deployed_archive_format_and_is_repeatable(self):
        bundle = create_bundle(self.output, release_id="test-release")
        self.assertEqual(bundle, self.archive())
        self.assertEqual(bundle, create_bundle(self.output, release_id="test-release"))

    def test_round_trip_is_offline_and_preserves_every_document(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network")):
            bundle = create_bundle(self.output, release_id="test-release")
            self.assertEqual(self.prepare(bundle), self.metadata)
        before = open_index(self.output, release_id="test-release")
        after = open_index(self.destination, release_id="test-release")
        try:
            with before.searcher() as first, after.searcher() as second:
                self.assertEqual(
                    list(first.all_stored_fields()), list(second.all_stored_fields())
                )
        finally:
            before.close()
            after.close()
        self.assertEqual(list(self.root.glob(".prepare-index-*")), [])
        self.assertFalse((self.destination / "MAIN_WRITELOCK").exists())

    def test_existing_directory_and_dangling_symlink_are_not_replaced(self):
        self.destination.mkdir()
        marker = self.destination / "active"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(marker.read_text(), "keep")
        marker.unlink()
        self.destination.rmdir()
        self.destination.symlink_to(self.root / "absent")
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertTrue(self.destination.is_symlink())

    def test_unsafe_members_are_rejected_and_staging_is_cleaned(self):
        original, content = self.members[0]
        for kind in (
            "traversal",
            "absolute",
            "unknown",
            "symlink",
            "hardlink",
            "directory",
            "duplicate",
            "missing",
            "oversized",
            "undersized",
            "corrupt",
            "metadata-oversized",
            "metadata-json",
            "metadata-mismatch",
        ):
            with self.subTest(kind=kind):
                members = copy.deepcopy(self.members)
                member, data = members[0]
                if kind in {"traversal", "absolute", "unknown"}:
                    member.name = {
                        "traversal": "../escape",
                        "absolute": "/escape",
                        "unknown": "extra",
                    }[kind]
                elif kind in {"symlink", "hardlink", "directory"}:
                    member.type = {
                        "symlink": tarfile.SYMTYPE,
                        "hardlink": tarfile.LNKTYPE,
                        "directory": tarfile.DIRTYPE,
                    }[kind]
                    member.linkname = original.name
                elif kind == "duplicate":
                    members.append(copy.deepcopy(members[0]))
                elif kind == "missing":
                    members.pop()
                elif kind.startswith("metadata-"):
                    for position, (entry, _) in enumerate(members):
                        if entry.name == METADATA_FILE:
                            data = {
                                "metadata-oversized": b" "
                                * (MAX_INDEX_METADATA_BYTES + 1),
                                "metadata-json": b"{",
                                "metadata-mismatch": b"{}",
                            }[kind]
                            entry.size = len(data)
                            members[position] = (entry, data)
                            break
                else:
                    data = {
                        "oversized": content + b"x",
                        "undersized": content[:-1],
                        "corrupt": bytes([content[0] ^ 1]) + content[1:],
                    }[kind]
                    member.size = len(data)
                    members[0] = (member, data)
                with self.assertRaises(IndexValidationError):
                    self.prepare(self.archive(members))
                self.assert_clean()
        self.assertFalse((self.root / "escape").exists())

    def test_invalid_metadata_is_rejected_before_opening_whoosh(self):
        for kind in ("identity", "files", "name", "hash", "size", "contract"):
            metadata = copy.deepcopy(self.metadata)
            with self.subTest(kind=kind):
                if kind == "identity":
                    metadata["release_id"] = "another"
                elif kind == "files":
                    metadata["files"] = {}
                elif kind == "name":
                    metadata["files"]["../escape"] = _file_metadata(b"")
                elif kind == "hash":
                    next(iter(metadata["files"].values()))["checksum_sha256"] = "bad"
                elif kind == "size":
                    next(iter(metadata["files"].values()))["size_bytes"] = True
                else:
                    metadata["index_contract_version"] = -1
                with (
                    patch(
                        "dawgpath_search.builder.open_dir",
                        side_effect=AssertionError("must not unpickle"),
                    ),
                    self.assertRaises(IndexValidationError),
                ):
                    self.prepare(metadata=metadata)
                self.assert_clean()

    def test_invalid_and_truncated_archives_fail_cleanly(self):
        for bundle in (b"not gzip", self.archive()[:40]):
            with self.subTest(bundle=bundle):
                with self.assertRaises(IndexValidationError):
                    self.prepare(bundle)
                self.assert_clean()

    def test_matching_metadata_does_not_bypass_reader_validation(self):
        metadata = copy.deepcopy(self.metadata)
        metadata["counts"]["courses"] += 1
        members = copy.deepcopy(self.members)
        for position, (member, _) in enumerate(members):
            if member.name == METADATA_FILE:
                content = json.dumps(metadata).encode()
                member.size = len(content)
                members[position] = member, content
        with self.assertRaisesRegex(IndexValidationError, "counts mismatch"):
            self.prepare(self.archive(members), metadata)
        self.assert_clean()

    def test_creation_rejects_wrong_identity_and_corrupt_files(self):
        with self.assertRaises(IndexValidationError):
            create_bundle(self.output, release_id="another")
        name = next(iter(self.metadata["files"]))
        (self.output / name).write_bytes(b"corrupt")
        with self.assertRaises(IndexValidationError):
            create_bundle(self.output, release_id="test-release")

    def test_metadata_size_limit_includes_exact_boundary(self):
        size = (self.output / METADATA_FILE).stat().st_size
        with patch("dawgpath_search.bundle.MAX_INDEX_METADATA_BYTES", size - 1):
            with self.assertRaisesRegex(IndexValidationError, "size limit"):
                create_bundle(self.output, release_id="test-release")
            with self.assertRaisesRegex(IndexValidationError, "recorded size"):
                self.prepare()
            self.assert_clean()
        with patch("dawgpath_search.bundle.MAX_INDEX_METADATA_BYTES", size):
            bundle = create_bundle(self.output, release_id="test-release")
            self.assertEqual(self.prepare(bundle), self.metadata)

    def test_rename_failure_removes_only_reserved_empty_destination(self):
        with (
            patch("dawgpath_search.bundle.os.replace", side_effect=OSError("rename")),
            self.assertRaisesRegex(OSError, "rename"),
        ):
            self.prepare()
        self.assert_clean()
