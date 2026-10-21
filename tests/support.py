import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from dawgpath_search import build_index


class SnapshotTestCase(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "snapshot"
        self.inputs.mkdir()
        self.output = self.root / "index"
        self.courses = [
            {
                "course_id": "INFO 201",
                "course_title": "Data Foundations",
                "course_description": "Analyzing data and running experiments.",
                "course_campus": "seattle",
            },
            {
                "course_id": "B INFO 301",
                "course_title": "Running Systems",
                "course_description": None,
                "course_campus": "bothell",
            },
            {
                "course_id": "INFO 401",
                "course_title": "Advanced Data",
                "course_description": "",
                "course_campus": "tacoma",
            },
            {
                "course_id": "INFO 402",
                "course_title": "Special Topics",
                "course_description": "Independent research",
                "course_campus": "seattle",
            },
        ]
        self.majors = {
            "INFO-40-1-6": {
                "credential_code": "INFO-40-1-6",
                "major_code": "INFO",
                "credential_title": "Informatics",
                "credential_description": "Data and information studies",
                "major_campus": "Seattle",
            },
            "BINFO-40-1-6": {
                "credential_code": "BINFO-40-1-6",
                "major_code": "BINFO",
                "credential_title": None,
                "credential_description": None,
                "major_campus": "Bothell",
            },
        }
        self.write_catalog()
        self.write(
            "coi_scores.csv",
            (
                "course_id,score\nINFO 201,0.12\nB INFO 301,-1\nINFO 401,3.26\n"
                "ABSENT 100,5\n"
            ),
        )
        self.write(
            "gateway_courses.csv",
            ("course,dept_abbr,course_no\nINFO201,INFO,201\nABSENT100,ABSENT,100\n"),
        )
        self.write(
            "bottleneck_courses.csv",
            ("course,dept_abbv,course_no\nB INFO301,B INFO,301\n"),
        )

    def write(self, name, content):
        (self.inputs / name).write_text(content, encoding="utf-8")

    def write_catalog(self):
        self.write("course_data.json", json.dumps(self.courses))
        self.write("major_data.json", json.dumps(self.majors))

    def build(self):
        return build_index(self.inputs, self.output, release_id="test-release")
