from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class IngestFailure(Exception):
    """A classified ingestion failure safe to expose through the local API."""

    code: str
    message: str
    http_status: int = 400
    retryable: bool = False

    def __str__(self) -> str:
        return self.message
