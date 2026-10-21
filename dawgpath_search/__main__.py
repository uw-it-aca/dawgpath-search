# Copyright 2026 UW-IT, University of Washington
# SPDX-License-Identifier: Apache-2.0

import argparse
import json

from .builder import IndexValidationError, build_index


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a DawgPath search index offline"
    )
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    try:
        metadata = build_index(
            args.input_root, args.output_dir, release_id=args.release_id
        )
    except (IndexValidationError, OSError) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
