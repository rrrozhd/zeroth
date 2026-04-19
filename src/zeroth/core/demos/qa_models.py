"""Contract models for demo workflows."""
from pydantic import BaseModel, Field

# --- Simple Q&A (single agent) ---

class QuestionInput(BaseModel):
    question: str


class AnswerOutput(BaseModel):
    answer: str


# --- Research Pipeline (multi-node) ---

class ResearchInput(BaseModel):
    question: str


class ResearchOutput(BaseModel):
    question: str = Field(description="The original question")
    findings: str = Field(description="Detailed research findings")
    confidence: str = Field(description="high, medium, or low")


class FormatInput(BaseModel):
    findings: str


class FormatOutput(BaseModel):
    formatted: str
    word_count: int


class SummaryInput(BaseModel):
    formatted: str
    word_count: int


class SummaryOutput(BaseModel):
    summary: str = Field(description="One-line summary of the research")
