# Copyright 2026 UW-IT, University of Washington
# SPDX-License-Identifier: Apache-2.0

from whoosh.analysis import StemmingAnalyzer
from whoosh.fields import BOOLEAN, NUMERIC, TEXT, Schema

INDEX_CONTRACT_VERSION = 1


def make_schema() -> Schema:
    """Match Pathways' original schema, including its integer NUMERIC field."""
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
