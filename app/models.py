from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class JobState(StrEnum):
    DRAFT = "draft"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNNING = "running"
    ROLLING_BACK = "rolling_back"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(slots=True)
class MigrationUnit:
    id: str
    label: str
    serials: list[str]
    stack_name: str | None = None
    state: str = "pending"


@dataclass(slots=True)
class Plan:
    id: str
    organization_id: str
    source_network_id: str
    destination_network_id: str
    units: list[MigrationUnit]
    groups: list[str]
    source_snapshot: dict[str, Any]
    destination_baseline: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, **kwargs: Any) -> "Plan":
        return cls(id=str(uuid4()), **kwargs)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["units"] = [asdict(unit) for unit in self.units]
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Plan":
        return cls(**{**value, "units": [MigrationUnit(**u) for u in value["units"]]})


@dataclass(slots=True)
class Job:
    id: str
    plan_id: str
    state: JobState
    current_index: int = 0
    completed_unit_ids: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, plan_id: str) -> "Job":
        return cls(id=str(uuid4()), plan_id=plan_id, state=JobState.WAITING_CONFIRMATION)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "state": self.state.value}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Job":
        return cls(**{**value, "state": JobState(value["state"])})
