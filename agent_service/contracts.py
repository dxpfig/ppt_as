"""Typed domain contracts shared by job, document and artifact boundaries."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


JobStatusValue = Literal[
    'queued',
    'running',
    'completed',
    'failed',
    'cancelled',
    'interrupted',
]


class JobRecord(BaseModel):
    model_config = ConfigDict(extra='allow')

    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,120}$')
    projectId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,120}$')
    status: JobStatusValue
    stage: float = Field(default=0, ge=0, le=100)
    kind: str = ''
    message: str = ''


class DocumentRevision(BaseModel):
    id: str
    revision: int = Field(ge=1)
    label: str = ''
    pageCount: int = Field(ge=1, le=60)
    createdAt: str = ''


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra='allow')

    id: str
    revision: int = Field(default=0, ge=0)
    pptxUrl: str
    pdfUrl: str = ''
    sourceUrl: str = ''
    reportUrl: str = ''
    previewUrls: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
