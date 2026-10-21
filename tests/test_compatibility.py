"""Contract v1 reference: Pathways' original builder and model search strings."""

import csv
import json

from whoosh import qparser
from whoosh.analysis import StemmingAnalyzer
from whoosh.fields import BOOLEAN, NUMERIC, TEXT, Schema
from whoosh.index import create_in
from whoosh.query import And, NumericRange, Term

from dawgpath_search import make_schema, open_index
from tests.support import SnapshotTestCase


def legacy_schema():
    return Schema(
        course_id=TEXT(stored=True),
        major_id=TEXT(stored=True),
        major_abbr=TEXT(stored=True),
        major_title=TEXT(stored=True),
        course_title=TEXT(stored=True),
        type=TEXT(stored=True),
        contents=TEXT(analyzer=StemmingAnalyzer(), stored=True),
        campus=TEXT(stored=True),
        is_gateway=BOOLEAN(),
        is_bottleneck=BOOLEAN(),
        coi_score=NUMERIC(),
        description=TEXT(stored=True),
    )


def build_legacy_reference(input_root, output_dir):
    """Freeze legacy importer semantics without requiring Django or a database."""
    courses = json.loads((input_root / "course_data.json").read_text())
    majors = json.loads((input_root / "major_data.json").read_text())
    with (input_root / "coi_scores.csv").open() as stream:
        coi_data = list(csv.reader(stream))[1:]
    flags = {}
    for flag in ("gateway", "bottleneck"):
        with (input_root / f"{flag}_courses.csv").open() as stream:
            flags[flag] = {row[1] + " " + row[2] for row in csv.reader(stream)}
    output_dir.mkdir()
    ix = create_in(str(output_dir), legacy_schema())
    with ix.writer() as writer:
        for course in courses:
            coi = next(
                (
                    round(float(row[1]), 1)
                    for row in coi_data
                    if row[0] == course["course_id"] and float(row[1]) != -1
                ),
                None,
            )
            writer.add_document(
                course_id=course["course_id"],
                type="course",
                contents="{} {} {}".format(
                    course["course_id"],
                    course["course_title"],
                    course["course_description"] or "",
                ),
                campus=course["course_campus"],
                is_gateway=course["course_id"] in flags["gateway"],
                is_bottleneck=course["course_id"] in flags["bottleneck"],
                coi_score=coi,
                course_title=course["course_title"],
                description=course["course_description"],
            )
        for major in majors.values():
            writer.add_document(
                major_id=major["credential_code"],
                major_abbr=major["major_code"],
                major_title=major["credential_title"],
                type="major",
                contents="{} {} {}".format(
                    major["major_code"],
                    major["credential_title"],
                    major["credential_description"],
                ),
                campus=major["major_campus"],
                description=major["credential_description"],
            )
    return ix


class CompatibilityTests(SnapshotTestCase):
    def setUp(self):
        super().setUp()
        self.build()
        self.index = open_index(self.output, release_id="test-release")
        self.legacy = build_legacy_reference(self.inputs, self.root / "legacy")
        self.addCleanup(self.index.close)
        self.addCleanup(self.legacy.close)

    def test_schema_matches_legacy(self):
        self.assertEqual(make_schema(), legacy_schema())
        self.assertIs(make_schema()["coi_score"].numtype, int)

    def test_all_stored_documents_match_legacy(self):
        with self.index.searcher() as current, self.legacy.searcher() as legacy:
            documents = list(current.all_stored_fields())
            self.assertEqual(documents, list(legacy.all_stored_fields()))
            self.assertEqual(
                documents[0]["contents"],
                "INFO 201 Data Foundations Analyzing data and running experiments.",
            )
            self.assertEqual(
                documents[1]["contents"],
                "B INFO 301 Running Systems ",
            )
            major = documents[5]
            self.assertEqual(major["contents"], "BINFO None None")
            self.assertEqual(major["campus"], "Bothell")

    def test_queries_filters_ranking_and_stemming_match_legacy(self):
        cases = [
            ("course_id", "INFO 201", None),
            ("course_id", "B INFO 301", None),
            ("major_title", "Informatics", None),
            ("contents", "run", None),
            ("contents", '"data foundations"', None),
            ("contents", "information data", None),
            ("contents", "info", Term("type", "course")),
            ("contents", "info", Term("campus", "seattle")),
            ("contents", "info", Term("campus", "Seattle")),
            ("contents", "info", Term("is_gateway", True)),
            ("contents", "info", Term("is_bottleneck", True)),
            ("contents", "info", NumericRange("coi_score", 0, 1)),
            ("contents", "info", NumericRange("coi_score", 3, 3)),
            (
                "contents",
                "info",
                And(
                    [
                        Term("type", "course"),
                        Term("campus", "seattle"),
                        Term("is_gateway", True),
                        NumericRange("coi_score", 0, 1),
                    ]
                ),
            ),
        ]
        with self.index.searcher() as current, self.legacy.searcher() as legacy:
            for field, text, filters in cases:
                with self.subTest(field=field, text=text, filters=filters):
                    query = qparser.QueryParser(
                        field, current.schema, group=qparser.OrGroup
                    ).parse(text)
                    actual = current.search(query, filter=filters, limit=None)
                    expected = legacy.search(query, filter=filters, limit=None)
                    self.assertEqual(
                        [(dict(hit), hit.score) for hit in actual],
                        [(dict(hit), hit.score) for hit in expected],
                    )

    def test_numeric_contract_keeps_integer_truncation_and_missing_scores(self):
        with self.index.searcher() as searcher:
            self.assertEqual(
                [
                    hit["course_id"]
                    for hit in searcher.search(
                        NumericRange("coi_score", 3, 3), limit=None
                    )
                ],
                ["INFO 401"],
            )
            self.assertEqual(
                [
                    hit["course_id"]
                    for hit in searcher.search(
                        NumericRange("coi_score", 0, 0), limit=None
                    )
                ],
                ["INFO 201"],
            )
            self.assertEqual(
                {
                    hit["course_id"]
                    for hit in searcher.search(
                        NumericRange("coi_score", -5, 5), limit=None
                    )
                },
                {"INFO 201", "INFO 401"},
            )

    def test_flag_filters_match_department_and_number(self):
        with self.index.searcher() as searcher:
            self.assertEqual(
                [
                    hit["course_id"]
                    for hit in searcher.search(Term("is_gateway", True), limit=None)
                ],
                ["INFO 201"],
            )
            self.assertEqual(
                [
                    hit["course_id"]
                    for hit in searcher.search(Term("is_bottleneck", True), limit=None)
                ],
                ["B INFO 301"],
            )
