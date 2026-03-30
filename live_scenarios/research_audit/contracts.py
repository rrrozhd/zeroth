"""Typed contracts for the live research-audit scenario."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AuditRequest(BaseModel):
    """Public input payload for the research-audit app."""

    model_config = ConfigDict(extra="forbid")

    question: str
    repo_path: str | None = None
    use_web: bool = False
    force_approval: bool = False


class EvidenceItem(BaseModel):
    """One piece of evidence gathered during the investigation."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    title: str
    location: str | None = None
    snippet: str | None = None
    url: str | None = None


class AuditState(BaseModel):
    """Shared intermediate payload passed between nodes."""

    model_config = ConfigDict(extra="forbid")

    question: str
    repo_path: str
    repo_query: str | None = None
    file_path: str | None = None
    use_web: bool = False
    requires_research: bool = True
    requires_approval: bool = False
    approval_reason: str | None = None
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    run_count: int = Field(default=1, ge=1)


class AuditResult(BaseModel):
    """Final public output of the scenario app."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    summary: str
    findings: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[str] = Field(default_factory=list)
    approval_used: bool = False
    run_count: int = Field(default=1, ge=1)


class RepoSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    repo_path: str
    max_matches: int = Field(default=5, ge=1, le=20)


class RepoSearchMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    line: int = Field(ge=1)
    text: str


class RepoSearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    repo_path: str
    matches: list[RepoSearchMatch] = Field(default_factory=list)


class ReadFileExcerptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=40, ge=1)


class ReadFileExcerptOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content: str


class WebSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    max_results: int = Field(default=5, ge=1, le=10)


class WebSearchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    snippet: str = ""


class WebSearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    provider: str
    items: list[WebSearchItem] = Field(default_factory=list)


class FetchUrlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    max_chars: int = Field(default=4000, ge=100, le=20000)


class FetchUrlOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    status_code: int
    title: str | None = None
    content: str
