"""T27-v1 strict contracts. No production entry point or persistence is added here."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from water_agent.runtime.governance.contracts import Principal

ID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
ArtifactID = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
UUIDText = Annotated[
    str, Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
]
Unit = Literal["L/s", "mm", "m", "m/s", "m/s²", "1"]
Formula = Literal["velocity", "head_loss_darcy_weisbach", "pump_head"]


def number(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("PROJECT_INVALID_VALUE")
    if isinstance(value, int) and abs(value) > 2**53:
        raise ValueError("PROJECT_INVALID_VALUE")
    result = float(value)  # type: ignore[arg-type]
    if not math.isfinite(result) or (result == 0 and math.copysign(1, result) < 0):
        raise ValueError("PROJECT_INVALID_VALUE")
    return result


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)


class UserInputBinding(StrictModel):
    kind: Literal["user"]
    input_id: ID
    input_set_id: UUIDText
    value: float
    unit: Unit

    @model_validator(mode="before")
    @classmethod
    def raw_number(cls, data):
        if isinstance(data, dict) and "value" in data:
            number(data["value"])
        return data


class PlannedOutputBinding(StrictModel):
    kind: Literal["planned_output"]
    node_id: ID
    case_id: ID
    artifact_slot_id: ID
    output_key: Literal["velocity_mps", "head_loss_m", "pump_head_m"]
    unit: Unit


class ProgramOutputBinding(StrictModel):
    kind: Literal["program"]
    node_id: ID
    case_id: ID
    artifact_id: ArtifactID
    revision: int = Field(ge=1)
    output_key: Literal["velocity_mps", "head_loss_m", "pump_head_m"]
    payload_hash: Hash
    unit: Unit


Binding = Annotated[UserInputBinding | PlannedOutputBinding, Field(discriminator="kind")]
ResolvedBinding = Annotated[UserInputBinding | ProgramOutputBinding, Field(discriminator="kind")]
FORMULAS = ("velocity", "head_loss_darcy_weisbach", "pump_head")
OUTPUTS = ("velocity_mps", "head_loss_m", "pump_head_m")
TOOL_NAMES = ("project_calc_velocity", "project_calc_darcy", "project_calc_pump_head")
INPUT_UNITS = (
    {"flow_lps": "L/s", "diameter_mm": "mm"},
    {
        "length_m": "m",
        "diameter_mm": "mm",
        "velocity_mps": "m/s",
        "friction_factor": "1",
        "gravity_mps2": "m/s²",
    },
    {"static_head_m": "m", "friction_head_m": "m", "local_head_m": "m", "residual_head_m": "m"},
)


class PlanNode(StrictModel):
    node_id: ID
    case_id: ID
    formula: Formula
    depends_on: list[ID]
    inputs: dict[str, Binding]
    requested_outputs: list[str]


class ProjectCalculationPlan(StrictModel):
    contract_version: Literal["t27-v1"]
    project_id: ID
    plan_id: ID
    branch_id: ID
    system_id: ID
    segment_id: ID
    revision: int = Field(ge=1)
    base_revision: int = Field(ge=0)
    supersedes_plan_hash: Hash | None
    system: Literal["water_supply"]
    input_set_id: UUIDText
    nodes: list[PlanNode] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def graph(self):
        if [n.node_id for n in self.nodes] != ["v", "hf", "H"]:
            raise ValueError("PROJECT_INVALID_PLAN")
        if len({n.case_id for n in self.nodes}) != 3:
            raise ValueError("PROJECT_INVALID_PLAN")
        slots = []
        for i, node in enumerate(self.nodes):
            if node.formula != FORMULAS[i] or node.requested_outputs != [OUTPUTS[i]]:
                raise ValueError("PROJECT_INVALID_PLAN")
            if node.depends_on != ([] if i == 0 else [self.nodes[i - 1].node_id]):
                raise ValueError("PROJECT_INVALID_PLAN")
            if set(node.inputs) - set(INPUT_UNITS[i]):
                raise ValueError("PROJECT_INVALID_PLAN")
            if set(INPUT_UNITS[i]) - set(node.inputs):
                raise ValueError("PROJECT_MISSING_INPUT")
            program_key = None if i == 0 else "velocity_mps" if i == 1 else "friction_head_m"
            for key, binding in node.inputs.items():
                if binding.unit != INPUT_UNITS[i][key]:
                    raise ValueError("PROJECT_UNIT_MISMATCH")
                if key == program_key:
                    prev = self.nodes[i - 1]
                    if (
                        not isinstance(binding, PlannedOutputBinding)
                        or binding.node_id != prev.node_id
                        or binding.case_id != prev.case_id
                        or binding.output_key != OUTPUTS[i - 1]
                    ):
                        raise ValueError("PROJECT_INVALID_PLAN")
                    slots.append(binding.artifact_slot_id)
                elif (
                    not isinstance(binding, UserInputBinding)
                    or binding.input_set_id != self.input_set_id
                    or binding.value < 0
                    or (i < 2 and binding.value <= 0)
                ):
                    raise ValueError("PROJECT_INVALID_VALUE")
        if len(set(slots)) != len(slots):
            raise ValueError("PROJECT_INVALID_PLAN")
        if self.nodes[0].inputs["diameter_mm"] != self.nodes[1].inputs["diameter_mm"]:
            raise ValueError("PROJECT_INVALID_PLAN")
        if self.nodes[0].inputs["flow_lps"].value > 5000:  # type: ignore[union-attr]
            raise ValueError("PROJECT_INVALID_VALUE")
        if self.nodes[0].inputs["diameter_mm"].value > 3000:  # type: ignore[union-attr]
            raise ValueError("PROJECT_INVALID_VALUE")
        return self


@dataclass(frozen=True)
class ProjectExecutionContext:
    owner_user_id: str
    thread_id: str
    run_id: str
    project_id: str
    plan_id: str
    branch_id: str
    plan_revision: int
    generation: int
    authorization_epoch: int
    checkpoint_namespace: str
    principal: Principal
    cancel_check: Callable[[], bool]

    def __post_init__(self) -> None:
        for value in (self.owner_user_id, self.thread_id, self.run_id):
            TypeAdapter(UUIDText).validate_python(value, strict=True)
        for value in (self.project_id, self.plan_id, self.branch_id):
            TypeAdapter(ID).validate_python(value, strict=True)
        for counter in (self.plan_revision, self.generation, self.authorization_epoch):
            if type(counter) is not int or counter < 1:
                raise ValueError("PROJECT_INVALID_VALUE")
        if not callable(self.cancel_check) or not self.checkpoint_namespace:
            raise ValueError("PROJECT_INVALID_VALUE")


class Disclosure(StrictModel):
    source_node_id: ID
    code: str
    message: str
    severity: Literal["info", "warning"]


class ProjectError(StrictModel):
    code: str
    node_id: str | None
    field_path: str | None
    retryable: Literal[False] = False
    message: str


class NodeExecutionRecord(StrictModel):
    execution_id: UUIDText
    invocation_id: str | None
    context_binding: dict[str, str | int]
    node_id: ID
    case_id: ID
    formula: Formula
    tool_name: str
    tool_identity_version: str
    plan_hash: Hash
    args_hash: Hash
    result_hash: Hash | None
    resolved_inputs: dict[str, float]
    input_sources: dict[str, ResolvedBinding]
    outputs: dict[str, float]
    units: dict[str, Unit]
    algorithm_version: str
    contract_version: Literal["t27-v1"]
    status: Literal["started", "verified", "missing_inputs", "failed", "cancelled", "indeterminate"]
    assumptions: list[Disclosure]
    warnings: list[Disclosure]
    error: ProjectError | None
    started_at: str
    finished_at: str | None

    @model_validator(mode="after")
    def state_fields(self):
        for value in [*self.resolved_inputs.values(), *self.outputs.values()]:
            number(value)
        if self.status == "verified":
            if (
                not self.invocation_id
                or not self.result_hash
                or not self.outputs
                or self.error
                or not self.finished_at
            ):
                raise ValueError("PROJECT_UNTRUSTED_SOURCE")
        elif self.outputs:
            raise ValueError("PROJECT_UNTRUSTED_SOURCE")
        if self.status == "started" and (self.result_hash or self.error or self.finished_at):
            raise ValueError("PROJECT_INVALID_VALUE")
        if self.status not in ("started", "verified") and (not self.error or not self.finished_at):
            raise ValueError("PROJECT_INVALID_VALUE")
        return self


class ProjectCalculationResult(StrictModel):
    contract_version: Literal["t27-v1"] = "t27-v1"
    plan_id: str
    project_id: str
    branch_id: str
    revision: int
    plan_hash: str
    input_set_id: str
    node_records: list[NodeExecutionRecord]
    status: Literal["ready", "missing_inputs", "blocked", "failed", "cancelled"]
    assumptions: list[Disclosure]
    warnings: list[Disclosure]
    errors: list[ProjectError]
    manifest_hash: str | None
