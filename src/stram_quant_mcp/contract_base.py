"""The immutable, closed base model and shared literals of every transport contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

AvailabilityMode = Literal["strict", "estimated", "ex_post"]
ViewMode = Literal["raw", "clean"]


class ContractModel(BaseModel):
    """Immutable transport contract that rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)
