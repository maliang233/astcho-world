from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator


class PlannerDecision(BaseModel):
    action: Literal["reply", "no_reply"] = "no_reply"
    reason: str = Field(default="", max_length=200)
    target_message_id: str | None = None
    excitement_delta: float = Field(
        default=0.0,
        ge=-0.2,
        le=0.2,
        validation_alias=AliasChoices("excitement_delta", "excitement"),
    )
    shyness_delta: float = Field(
        default=0.0, ge=-0.2, le=0.2, validation_alias=AliasChoices("shyness_delta", "shyness")
    )
    affinity_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    should_meme: bool = False
    meme_query: str | None = Field(default=None, max_length=80)


class ReplyPayload(BaseModel):
    reply: str = Field(min_length=1, max_length=1000)

    @field_validator("reply")
    @classmethod
    def clean_reply(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reply cannot be blank")
        return value


class ReasoningReplyPayload(BaseModel):
    thinking: str = Field(default="", max_length=4000)
    reply: str = Field(min_length=1, max_length=1000)

    @field_validator("reply")
    @classmethod
    def clean_reply(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reply cannot be blank")
        return value


class MemoryAtom(BaseModel):
    content: str = Field(min_length=2, max_length=500)
    importance: int = Field(default=5, ge=1, le=10)
    kind: Literal["short", "long"] = "short"


class MemoryExtraction(BaseModel):
    memories: list[MemoryAtom] = Field(default_factory=list, max_length=10)


class VisionResult(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    is_sticker: bool = False
    heart_throb: bool = False
    contains_real_person: bool = False
    is_screenshot: bool = False
    tags: list[str] = Field(default_factory=list, max_length=10)
    inclination: str = Field(default="neutral", max_length=80)

    @property
    def is_safe_forced_meme(self) -> bool:
        """The automatic curator requires explicit checks of both forbidden categories."""
        return (
            {"contains_real_person", "is_screenshot"} <= self.model_fields_set
            and not self.contains_real_person
            and not self.is_screenshot
        )

    @property
    def is_meme_learning_candidate(self) -> bool:
        """Fail closed: only exceptional non-real, non-screenshot stickers may be learned."""
        return (
            self.is_safe_forced_meme
            and self.is_sticker
            and self.heart_throb
        )


class MemeSelection(BaseModel):
    selected_index: int | None = Field(default=None, ge=0)


class MemeAnalysis(BaseModel):
    tags: list[str] = Field(min_length=1, max_length=10)
    inclination: str = Field(min_length=1, max_length=200)
    content_desc: str = Field(min_length=1, max_length=500)


class MemeTasteDecision(BaseModel):
    heart_throb: bool = False
    reason: str = Field(default="", max_length=200)


class LearnedExpression(BaseModel):
    situation: str = Field(min_length=2, max_length=40)
    style: str = Field(min_length=1, max_length=40)
    source_id: int = Field(ge=1)


class ExpressionExtraction(BaseModel):
    expressions: list[LearnedExpression] = Field(default_factory=list, max_length=10)


class ExpressionReview(BaseModel):
    accepted_ids: list[int] = Field(default_factory=list, max_length=20)
    rejected_ids: list[int] = Field(default_factory=list, max_length=20)


class RetrievedMemory(BaseModel):
    memory_id: str
    content: str
    score: float
    kind: str
    group_id: str
    user_id: str


class ChatMessage(BaseModel):
    message_id: str
    user_id: str
    nickname: str
    text: str
    timestamp: float
    is_bot: bool = False
    mentioned_bot: bool = False
    replied_to_bot: bool = False
    reply_to_message_id: str | None = None
    reply_to_user_id: str | None = None
    reply_to_nickname: str = ""
    reply_text: str = ""
    at_targets: list[str] = Field(default_factory=list, max_length=20)
    image_description: str = ""

    def text_with_mentions(self) -> str:
        at_prefix = f"@{','.join(self.at_targets)}" if self.at_targets else ""
        return " ".join(part for part in (at_prefix, self.text) if part).strip()
