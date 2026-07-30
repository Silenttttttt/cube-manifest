"""Config error type - wraps a pydantic ValidationError with the source file
path, so a malformed app.yml fails with a clear "which file, which field"
message instead of a raw traceback (the old system's confirmed failure mode
for e.g. a malformed storage: entry - a raw KeyError deep inside a generator
function, with no file/field context at all)."""

from __future__ import annotations

from pathlib import Path


class ConfigError(Exception):
    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")
