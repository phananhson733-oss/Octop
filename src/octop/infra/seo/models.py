"""Validated SEO commands shared by HTTP and internal callers."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(Command):
    name: str = Field(min_length=1, max_length=120)
    domain: str = Field(min_length=1, max_length=253)
    agent_id: str = Field(min_length=1, max_length=100)
    location_code: Literal[2840, 2826, 2124, 2036] = 2840
    language_code: Literal["en"] = "en"


class ProviderPut(Command):
    login: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=1000)


class ResearchCreate(Command):
    keywords: list[str] = Field(min_length=1, max_length=5)
    mode: Literal["auto", "related", "suggestions", "ideas"] = "auto"
    result_limit: Literal[150, 300, 500] = 150
    clickstream: bool = False
    group_keywords: bool = False
    idempotency_key: str = Field(min_length=1, max_length=100)
    confirm_paid: Literal[True]


class KeywordsSave(Command):
    run_id: str
    keywords: list[str] = Field(min_length=1, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=20)
    tag_mode: Literal["append", "replace"] = "append"


class ReportCreate(Command):
    run_id: str
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=2500)
    html: str = Field(min_length=1, max_length=500000)
    skill: str = Field(default="keyword-research", min_length=1, max_length=60)
