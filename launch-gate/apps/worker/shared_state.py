"""Validated, thin state shared between worker steps."""

from __future__ import annotations

from typing import List, Optional

from pydantic import Field

from packages.contracts import ContractModel, NonEmptyString


class SharedState(ContractModel):
    """Identifiers and compact context carried between workflow steps."""

    ad_id: NonEmptyString
    client_id: NonEmptyString
    status: NonEmptyString
    summary: Optional[NonEmptyString] = None
    file_refs: List[NonEmptyString] = Field(default_factory=list)
