# Copyright 2026 UW-IT, University of Washington
# SPDX-License-Identifier: Apache-2.0

import csv
import hashlib
import io
import json
import math
from importlib.metadata import version
from pathlib import Path

from whoosh.index import EmptyIndexError, FileIndex, create_in, open_dir

from .schema import INDEX_CONTRACT_VERSION, make_schema

INPUT_FILES = (
    "course_data.json",
    "major_data.json",
    "coi_scores.csv",
    "gateway_courses.csv",
    "bottleneck_courses.csv",
)
METADATA_FILE = "index-metadata.json"


class IndexValidationError(ValueError):
    """An input or built index does not satisfy the search contract."""


def _text(row: dict, field: str, *, nullable: bool = False) -> str | None:
    if field not in row:
        raise IndexValidationError(f"Missing field: {field}")
    value = row[field]
    if nullable and value is None:
        return None
    if not isinstance(value, str) or (not nullable and not value):
        raise IndexValidationError(f"{field} must be a nonempty string")
    return value


def _csv_rows(
    content: bytes,
    header: list[str],
    filename: str,
    *,
    alternate_header: list[str] | None = None,
) -> list[list[str]]:
    try:
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise IndexValidationError(f"{filename}: invalid UTF-8 CSV: {exc}") from exc
    if not rows or rows[0] not in (header, alternate_header):
        raise IndexValidationError(f"{filename}: expected header {header}")
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(header) or any(not cell.strip() for cell in row):
            raise IndexValidationError(f"{filename}: invalid row {number}")
    return rows[1:]


def _coi_scores(content: bytes) -> dict[str, float]:
    scores = {}
    seen = set()
    for course_id, value in _csv_rows(
        content, ["course_id", "score"], "coi_scores.csv"
    ):
        if course_id in seen:
            raise IndexValidationError(f"Duplicate COI ID: {course_id}")
        seen.add(course_id)
        try:
            score = float(value)
        except ValueError as exc:
            raise IndexValidationError(
                f"coi_scores.csv: invalid score for {course_id}: {value}"
            ) from exc
        if not math.isfinite(score) or not -5 <= score <= 5:
            raise IndexValidationError(f"Invalid COI score for {course_id}")
        if score != -1:
            scores[course_id] = round(score, 1)
    return scores


def _flags(content: bytes, filename: str) -> set[str]:
    # Pathways' checked-in bottleneck CSV uses the historical "dept_abbv" typo.
    alternate = (
        ["course", "dept_abbv", "course_no"]
        if filename == "bottleneck_courses.csv"
        else None
    )
    return {
        f"{department} {number}"
        for _, department, number in _csv_rows(
            content,
            ["course", "dept_abbr", "course_no"],
            filename,
            alternate_header=alternate,
        )
    }


def _json_input(content: bytes, filename: str) -> object:
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IndexValidationError(f"{filename}: invalid JSON: {exc}") from exc


def _documents(inputs: dict[str, bytes]) -> tuple[list[dict], list[dict]]:
    courses = _json_input(inputs["course_data.json"], "course_data.json")
    majors = _json_input(inputs["major_data.json"], "major_data.json")
    if not isinstance(courses, list) or not courses:
        raise IndexValidationError("course_data.json must be a nonempty list")
    if not isinstance(majors, dict) or not majors:
        raise IndexValidationError("major_data.json must be a nonempty object")
    scores = _coi_scores(inputs["coi_scores.csv"])
    gateway = _flags(inputs["gateway_courses.csv"], "gateway_courses.csv")
    bottleneck = _flags(inputs["bottleneck_courses.csv"], "bottleneck_courses.csv")
    course_docs = []
    seen = set()
    for row in courses:
        if not isinstance(row, dict):
            raise IndexValidationError("Each course must be an object")
        course_id = _text(row, "course_id")
        if course_id in seen:
            raise IndexValidationError(f"Duplicate course ID: {course_id}")
        seen.add(course_id)
        title = _text(row, "course_title")
        description = _text(row, "course_description", nullable=True)
        course_docs.append(
            {
                "course_id": course_id,
                "type": "course",
                "contents": f"{course_id} {title} {description or ''}",
                "campus": _text(row, "course_campus"),
                "is_gateway": course_id in gateway,
                "is_bottleneck": course_id in bottleneck,
                "coi_score": scores.get(course_id),
                "course_title": title,
                "description": description,
            }
        )
    major_docs = []
    seen = set()
    for row in majors.values():
        if not isinstance(row, dict):
            raise IndexValidationError("Each major must be an object")
        credential = _text(row, "credential_code")
        if credential in seen:
            raise IndexValidationError(f"Duplicate credential code: {credential}")
        seen.add(credential)
        abbr = _text(row, "major_code")
        title = _text(row, "credential_title", nullable=True)
        description = _text(row, "credential_description", nullable=True)
        major_docs.append(
            {
                "major_id": credential,
                "major_abbr": abbr,
                "major_title": title,
                # The legacy Major.get_search_string formats None as literal text.
                "contents": f"{abbr} {title} {description}",
                "type": "major",
                "campus": _text(row, "major_campus"),
                "description": description,
            }
        )
    return course_docs, major_docs


def _file_metadata(content: bytes) -> dict:
    return {
        "size_bytes": len(content),
        "checksum_sha256": hashlib.sha256(content).hexdigest(),
    }


def build_index(
    input_root: str | Path, output_dir: str | Path, *, release_id: str
) -> dict:
    """Build a fresh index from exact local release files; never fetch data."""
    if not isinstance(release_id, str) or not release_id.strip():
        raise IndexValidationError("A nonempty release_id is required")
    input_root = Path(input_root)
    inputs = {name: (input_root / name).read_bytes() for name in INPUT_FILES}
    courses, majors = _documents(inputs)
    builder_version = version("dawgpath-search")
    whoosh_version = version("Whoosh")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    ix = create_in(str(output_dir), make_schema())
    try:
        with ix.writer() as writer:
            for document in courses:
                writer.add_document(**document)
            for document in majors:
                writer.add_document(**document)
    finally:
        ix.close()
    metadata = {
        "index_contract_version": INDEX_CONTRACT_VERSION,
        "builder_version": builder_version,
        "whoosh_version": whoosh_version,
        "release_id": release_id,
        "counts": {"courses": len(courses), "majors": len(majors)},
        "inputs": {name: _file_metadata(content) for name, content in inputs.items()},
        "files": {
            path.name: _file_metadata(path.read_bytes())
            for path in sorted(output_dir.iterdir())
            if path.is_file() and path.name != "MAIN_WRITELOCK"
        },
    }
    # Written last: an interrupted build must not appear complete.
    (output_dir / METADATA_FILE).write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    return metadata


def _valid_file_info(info: object) -> bool:
    if not isinstance(info, dict) or set(info) != {"size_bytes", "checksum_sha256"}:
        return False
    size = info["size_bytes"]
    checksum = info["checksum_sha256"]
    return (
        type(size) is int
        and size >= 0
        and isinstance(checksum, str)
        and len(checksum) == 64
        and all(char in "0123456789abcdef" for char in checksum)
    )


def _read_metadata(index_dir: Path, release_id: str) -> dict:
    if not isinstance(release_id, str) or not release_id.strip():
        raise IndexValidationError("A nonempty release_id is required")
    try:
        content = (index_dir / METADATA_FILE).read_bytes()
    except OSError as exc:
        raise IndexValidationError(f"Cannot read {METADATA_FILE}: {exc}") from exc
    metadata = _json_input(content, METADATA_FILE)
    if not isinstance(metadata, dict):
        raise IndexValidationError("Index metadata must be an object")
    expected = {
        "index_contract_version": INDEX_CONTRACT_VERSION,
        "whoosh_version": version("Whoosh"),
        "release_id": release_id,
    }
    for key, value in expected.items():
        if type(metadata.get(key)) is not type(value) or metadata[key] != value:
            raise IndexValidationError(f"Index {key} mismatch")
    if (
        not isinstance(metadata.get("builder_version"), str)
        or not metadata["builder_version"].strip()
    ):
        raise IndexValidationError("Index builder_version is missing")
    counts = metadata.get("counts")
    if (
        not isinstance(counts, dict)
        or set(counts) != {"courses", "majors"}
        or any(type(count) is not int or count <= 0 for count in counts.values())
    ):
        raise IndexValidationError("Invalid index document counts")
    inputs = metadata.get("inputs")
    if (
        not isinstance(inputs, dict)
        or set(inputs) != set(INPUT_FILES)
        or not all(_valid_file_info(info) for info in inputs.values())
    ):
        raise IndexValidationError("Invalid index input metadata")
    return metadata


def open_index(index_dir: str | Path, *, release_id: str) -> FileIndex:
    """Verify trusted index files and compatibility before opening with Whoosh."""
    index_dir = Path(index_dir)
    metadata = _read_metadata(index_dir, release_id)
    files = metadata.get("files")
    if not isinstance(files, dict) or not files:
        raise IndexValidationError("Index file checksums are missing")
    for name, info in files.items():
        if (
            not isinstance(name, str)
            or not name
            or name
            in {
                ".",
                "..",
                METADATA_FILE,
                "MAIN_WRITELOCK",
            }
            or "/" in name
            or "\\" in name
        ):
            raise IndexValidationError("Invalid index filename")
        if not _valid_file_info(info):
            raise IndexValidationError(f"Invalid index file metadata: {name}")
        path = index_dir / name
        if path.is_symlink() or not path.is_file():
            raise IndexValidationError(f"Index file missing or not regular: {name}")
        if _file_metadata(path.read_bytes()) != info:
            raise IndexValidationError(f"Index checksum mismatch: {name}")
    actual = {
        path.name
        for path in index_dir.iterdir()
        if path.name not in {METADATA_FILE, "MAIN_WRITELOCK"}
    }
    if actual != set(files):
        raise IndexValidationError("Index file set mismatch")
    try:
        ix = open_dir(str(index_dir))
    except EmptyIndexError as exc:
        raise IndexValidationError("No readable Whoosh index found") from exc
    verified = False
    try:
        if ix.schema != make_schema():
            raise IndexValidationError("Index schema mismatch")
        with ix.searcher() as searcher:
            counts = {
                "courses": searcher.doc_frequency("type", "course"),
                "majors": searcher.doc_frequency("type", "major"),
            }
            if (
                counts != metadata["counts"]
                or searcher.doc_count_all() != sum(counts.values())
                or searcher.doc_count() != searcher.doc_count_all()
            ):
                raise IndexValidationError("Index document counts mismatch")
        verified = True
        return ix
    finally:
        if not verified:
            ix.close()
