# worker/contract.py
"""The worker's half of docs/04_API_CONTRACT.md, as pydantic models.

Validating the server's response here means a contract change shows up as a
clear error at startup rather than as a wrong crop three layers down. These
mirror the server's ninja schemas deliberately: one shape, written twice, on
purpose — the worker must not be able to load a config the server would not
have produced.
"""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ReferenceFrame(BaseModel):
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class ModelSpec(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_size: tuple[int, int]        # [height, width]

    @field_validator("input_size")
    @classmethod
    def _positive(cls, value):
        if min(value) < 1:
            raise ValueError("input_size must be positive")
        return value


class Smoothing(BaseModel):
    window: int = Field(ge=1)
    min_agreement: int = Field(ge=1)

    @field_validator("min_agreement")
    @classmethod
    def _within_window(cls, value, info):
        window = info.data.get("window")
        if window is not None and value > window:
            raise ValueError("min_agreement cannot exceed window")
        return value


class Sampling(BaseModel):
    target_fps: float = Field(gt=0, le=60)


class SlotSpec(BaseModel):
    id: UUID
    label: str
    polygon: list[list[float]]

    @field_validator("polygon")
    @classmethod
    def _four_points(cls, value):
        if len(value) != 4 or any(len(point) != 2 for point in value):
            raise ValueError("polygon must be four [x, y] points")
        return value


class WorkerConfig(BaseModel):
    camera_id: UUID
    lot_name: str
    source_uri: str
    reference_frame: ReferenceFrame
    model: ModelSpec
    smoothing: Smoothing
    sampling: Sampling
    slots: list[SlotSpec]
    config_version: str