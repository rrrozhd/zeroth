"""Shared Pydantic contract models used across the examples.

Contracts are just Pydantic models that the graph's nodes agree on. Every
example imports the handful of shapes it needs from here instead of
inventing a throwaway ``DemoPayload``, which keeps the examples short and
shows how contract reuse actually looks in practice.

See ``_common.register_contracts`` for how these get registered against a
``ContractRegistry`` at bootstrap time.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Question(BaseModel):
    """A plain user question. Input to the simplest agent examples."""

    question: str


class Answer(BaseModel):
    """A one-field answer produced by an agent."""

    answer: str


class Topic(BaseModel):
    """A single topic handed from one agent to another in multi-agent graphs."""

    topic: str


class Research(BaseModel):
    """Intermediate findings produced by a research agent."""

    topic: str
    findings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class Article(BaseModel):
    """Final article produced from research."""

    topic: str
    title: str
    body: str


class ScoredPayload(BaseModel):
    """Payload with a numeric score, used for conditional branching."""

    message: str
    score: float


class BranchDecision(BaseModel):
    """Outcome of a conditional branch — which lane fired."""

    message: str
    branch: str


class ToolInput(BaseModel):
    """Input to the shared ``format_article`` native tool."""

    topic: str
    body: str


class ToolOutput(BaseModel):
    """Output of the shared ``format_article`` native tool."""

    topic: str
    formatted: str


class ApprovalPayload(BaseModel):
    """Shape exchanged through a human-approval gate."""

    subject: str
    rationale: str


class AuditNote(BaseModel):
    """Short audit note used by the audit-query and observability examples."""

    message: str
    severity: str = "info"
