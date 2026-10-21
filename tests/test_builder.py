import hashlib
import json
import subprocess
import sys
from importlib.metadata import version
from unittest.mock import patch

from whoosh.fields import TEXT, Schema
from whoosh.index import create_in, open_dir

from dawgpath_search import build_index, open_index
from dawgpath_search.builder import INPUT_FILES, METADATA_FILE, IndexValidationError
from tests.support import SnapshotTestCase


class BuilderTests(SnapshotTestCase):
    def test_build_and_open(self):
        metadata = self.build()
        self.assertEqual(metadata["counts"], {"courses": 4, "majors": 2})
        self.assertEqual(metadata["builder_version"], version("dawgpath-search"))
        self.assertEqual(metadata["whoosh_version"], "2.7.4")
        self.assertEqual(metadata["index_contract_version"], 1)
        self.assertEqual(
            metadata, json.loads((self.output / METADATA_FILE).read_text())
        )
        self.assertEqual(set(metadata["inputs"]), set(INPUT_FILES))
        for name, info in metadata["inputs"].items():
            content = (self.inputs / name).read_bytes()
            self.assertEqual(
                info,
                {
                    "size_bytes": len(content),
                    "checksum_sha256": hashlib.sha256(content).hexdigest(),
                },
            )
        self.assertNotIn(METADATA_FILE, metadata["files"])
        self.assertNotIn("MAIN_WRITELOCK", metadata["files"])
        ix = open_index(self.output, release_id="test-release")
        self.addCleanup(ix.close)
        with ix.searcher() as searcher:
            self.assertEqual(searcher.doc_count_all(), 6)

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("active")
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(marker.read_text(), "active")
        self.assertEqual(list(self.output.iterdir()), [marker])

    def test_missing_input_does_not_create_output(self):
        (self.inputs / "coi_scores.csv").unlink()
        with self.assertRaises(FileNotFoundError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_empty_release_is_rejected_before_build(self):
        for release_id in ("", " ", None):
            with self.subTest(release_id=release_id):
                with self.assertRaises(IndexValidationError):
                    build_index(self.inputs, self.output, release_id=release_id)
                self.assertFalse(self.output.exists())

    def test_invalid_inputs_fail_before_creating_output(self):
        cases = [
            ("course_data.json", "[]"),
            ("course_data.json", "{}"),
            ("course_data.json", "[null]"),
            ("course_data.json", "{invalid"),
            ("course_data.json", json.dumps(self.courses * 2)),
            ("course_data.json", '[{"course_id": "INFO 201"}]'),
            ("major_data.json", "{}"),
            ("major_data.json", "[]"),
            ("major_data.json", '{"a": null}'),
            (
                "major_data.json",
                json.dumps(
                    {"a": self.majors["INFO-40-1-6"], "b": self.majors["INFO-40-1-6"]}
                ),
            ),
            ("coi_scores.csv", "bad,header\nINFO 201,1\n"),
            ("coi_scores.csv", "course_id,score\nINFO 201,nope\n"),
            ("coi_scores.csv", "course_id,score\nINFO 201,nan\n"),
            ("coi_scores.csv", "course_id,score\nINFO 201,inf\n"),
            ("coi_scores.csv", "course_id,score\nINFO 201,5.01\n"),
            ("coi_scores.csv", "course_id,score\nINFO 201,-5.01\n"),
            ("coi_scores.csv", "course_id,score\nINFO 201,1\nINFO 201,2\n"),
            ("coi_scores.csv", "course_id,score\nINFO 201\n"),
            ("coi_scores.csv", "course_id,score\nINFO 201, \n"),
            ("coi_scores.csv", 'course_id,score\n"INFO 201,1\n'),
            ("gateway_courses.csv", "course,dept_abbr,course_no\nINFO201,INFO\n"),
            ("bottleneck_courses.csv", "course,bad,course_no\n"),
        ]
        for name, content in cases:
            with self.subTest(name=name, content=content):
                original = (self.inputs / name).read_bytes()
                try:
                    self.write(name, content)
                    with self.assertRaises(IndexValidationError):
                        self.build()
                    self.assertFalse(self.output.exists())
                finally:
                    (self.inputs / name).write_bytes(original)

    def test_invalid_encoding_is_reported_as_validation_error(self):
        (self.inputs / "coi_scores.csv").write_bytes(b"\xff")
        with self.assertRaises(IndexValidationError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_header_only_reference_files_are_allowed(self):
        self.write("coi_scores.csv", "\ufeffcourse_id,score\n")
        self.write("gateway_courses.csv", "course,dept_abbr,course_no\n")
        self.write("bottleneck_courses.csv", "course,dept_abbr,course_no\n")
        self.assertEqual(self.build()["counts"]["courses"], 4)

    def test_indexing_failure_does_not_publish_metadata(self):
        with patch(
            "whoosh.writing.SegmentWriter.add_document",
            side_effect=RuntimeError("write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.build()
        self.assertFalse((self.output / METADATA_FILE).exists())

    def test_build_is_offline(self):
        with patch("socket.socket", side_effect=AssertionError("network used")):
            self.build()
            ix = open_index(self.output, release_id="test-release")
            ix.close()


class ReaderTests(SnapshotTestCase):
    def setUp(self):
        super().setUp()
        self.metadata = self.build()

    def save_metadata(self):
        (self.output / METADATA_FILE).write_text(json.dumps(self.metadata))

    def test_wrong_release_is_rejected(self):
        with self.assertRaisesRegex(IndexValidationError, "release_id"):
            open_index(self.output, release_id="another-release")

    def test_incompatible_or_malformed_metadata(self):
        cases = [
            ("index_contract_version", 2),
            ("index_contract_version", True),
            ("whoosh_version", "3.0"),
            ("builder_version", None),
            ("counts", {"courses": True, "majors": 2}),
            ("counts", {"courses": 4, "majors": 3}),
            ("inputs", {}),
            ("files", {}),
            ("files", {"../outside": {}}),
            ("files", {".": {}}),
            ("files", {"MAIN_WRITELOCK": {}}),
        ]
        original = json.dumps(self.metadata)
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.metadata = json.loads(original)
                self.metadata[key] = value
                self.save_metadata()
                with self.assertRaises(IndexValidationError):
                    open_index(self.output, release_id="test-release")

    def test_non_object_metadata(self):
        (self.output / METADATA_FILE).write_text("[]")
        with self.assertRaises(IndexValidationError):
            open_index(self.output, release_id="test-release")

    def test_invalid_json_metadata(self):
        (self.output / METADATA_FILE).write_text("{bad")
        with self.assertRaises(IndexValidationError):
            open_index(self.output, release_id="test-release")

    def test_missing_metadata(self):
        (self.output / METADATA_FILE).unlink()
        with self.assertRaises(IndexValidationError):
            open_index(self.output, release_id="test-release")

    def test_corrupted_file_is_rejected(self):
        name = next(iter(self.metadata["files"]))
        with (self.output / name).open("ab") as stream:
            stream.write(b"corrupt")
        with self.assertRaisesRegex(IndexValidationError, "checksum"):
            open_index(self.output, release_id="test-release")

    def test_missing_file_is_rejected(self):
        (self.output / next(iter(self.metadata["files"]))).unlink()
        with self.assertRaises(IndexValidationError):
            open_index(self.output, release_id="test-release")

    def test_unlisted_file_is_rejected(self):
        (self.output / "_MAIN_999.toc").write_bytes(b"unexpected generation")
        with self.assertRaisesRegex(IndexValidationError, "file"):
            open_index(self.output, release_id="test-release")

    def test_unlisted_file_checksum_cannot_be_skipped(self):
        self.metadata["files"].pop(next(iter(self.metadata["files"])))
        self.save_metadata()
        with self.assertRaises(IndexValidationError):
            open_index(self.output, release_id="test-release")

    def test_symlinked_index_file_is_rejected(self):
        name = next(iter(self.metadata["files"]))
        target = self.root / name
        (self.output / name).rename(target)
        (self.output / name).symlink_to(target)
        with self.assertRaises(IndexValidationError):
            open_index(self.output, release_id="test-release")

    def test_invalid_checksum_metadata(self):
        name = next(iter(self.metadata["files"]))
        for info in (
            None,
            {},
            {"size_bytes": True, "checksum_sha256": "a" * 64},
            {"size_bytes": 1, "checksum_sha256": "z" * 64},
            {"size_bytes": -1, "checksum_sha256": "a" * 64},
        ):
            with self.subTest(info=info):
                self.metadata["files"][name] = info
                self.save_metadata()
                with self.assertRaises(IndexValidationError):
                    open_index(self.output, release_id="test-release")

    def update_file_metadata(self):
        self.metadata["files"] = {
            path.name: {
                "size_bytes": path.stat().st_size,
                "checksum_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in self.output.iterdir()
            if path.name not in {METADATA_FILE, "MAIN_WRITELOCK"}
        }
        self.save_metadata()

    def test_schema_is_verified_after_checksums(self):
        ix = create_in(str(self.output), Schema(contents=TEXT()))
        ix.close()
        self.update_file_metadata()
        with self.assertRaisesRegex(IndexValidationError, "schema"):
            open_index(self.output, release_id="test-release")

    def test_document_counts_are_verified_after_checksums(self):
        ix = open_dir(str(self.output))
        with ix.writer() as writer:
            writer.add_document(type="course", course_id="EXTRA 100")
        ix.close()
        self.update_file_metadata()
        with self.assertRaisesRegex(IndexValidationError, "counts"):
            open_index(self.output, release_id="test-release")

    def test_unexpected_document_type_is_rejected(self):
        ix = open_dir(str(self.output))
        with ix.writer() as writer:
            writer.add_document(type="other", contents="unexpected")
        ix.close()
        self.update_file_metadata()
        with self.assertRaisesRegex(IndexValidationError, "counts"):
            open_index(self.output, release_id="test-release")

    def test_reader_rejects_empty_release(self):
        with self.assertRaises(IndexValidationError):
            open_index(self.output, release_id="")


class CliTests(SnapshotTestCase):
    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "dawgpath_search", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_build(self):
        result = self.invoke(
            "--input-root",
            str(self.inputs),
            "--output-dir",
            str(self.output),
            "--release-id",
            "cli-release",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["release_id"], "cli-release")
        ix = open_index(self.output, release_id="cli-release")
        ix.close()

    def test_cli_requires_arguments(self):
        self.assertNotEqual(self.invoke().returncode, 0)

    def test_cli_reports_invalid_input_without_success_output(self):
        self.write("course_data.json", "{bad")
        result = self.invoke(
            "--input-root",
            str(self.inputs),
            "--output-dir",
            str(self.output),
            "--release-id",
            "cli-release",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("course_data.json", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
