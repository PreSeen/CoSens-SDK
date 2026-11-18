"""Check for copied learning records before rules cross into Recipe testing."""

import re
from pathlib import Path

from .materials import read_snapshot

_RECORD_ID = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]*[-_][0-9][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*\b"
)
_MATERIAL_LOCATOR = re.compile(r"\bmaterial_[a-f0-9]{32}(?:/\d+)?\b", re.IGNORECASE)


class LearningLiterals:
    def __init__(self, files):
        self.ids = set()
        self.paths = set()
        self.rows = set()
        for file in files:
            self.paths.update((file["path"], file["snapshot"]))
            if Path(file["path"]).suffix.lower() not in {".csv", ".tsv"}:
                continue
            try:
                text = read_snapshot(file)["text"]
            except (OSError, ValueError):
                # Unreadable files can only contribute their catalog metadata.
                continue
            for line in text.splitlines():
                if ("," in line or "\t" in line) and re.search(r"\d", line):
                    self.rows.add(self.normalize_row(line))
                    key = re.split(r"[,\t]", line, maxsplit=1)[0].strip().strip('"')
                    if _RECORD_ID.fullmatch(key):
                        self.ids.add(key.lower())

    @staticmethod
    def normalize_row(row):
        return re.sub(r"[ \t]*([,\t])[ \t]*", r"\1", row.strip().lower())

    def found_in(self, fields):
        for field in fields:
            if _MATERIAL_LOCATOR.search(field) or any(
                path in field for path in self.paths
            ):
                return True
            if self.ids.intersection(_RECORD_ID.findall(field.lower())):
                return True
            normalized = self.normalize_row(field)
            if any(row in normalized for row in self.rows):
                return True
        return False


def rubric_rule_text(rubrics):
    return [
        value
        for criterion in rubrics["criteria"]
        for value in (criterion["id"], criterion["description"])
    ]
