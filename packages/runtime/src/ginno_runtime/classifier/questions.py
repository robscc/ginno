# -*- coding: utf-8 -*-
"""Provider-independent question/answer types for classifier models.

AgentScope 风格（java.agentscope.io「Jev 与结构化决策」）：一个 classifier
调用接收共享 state + 一组类型化问题，返回带置信度的类型化答案。题型用
``type`` 字段判别，答案侧一一对应——上层 middleware 只依赖这套 schema，
不感知后端是 LLM structured output 还是未来的 System One API（Jev）。
"""
from typing import Annotated, Literal, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field


class QuestionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    """Answer payloads reference their question by this id."""

    prompt: str
    """What the classifier should decide, in one sentence."""


class BinaryQuestion(QuestionBase):
    type: Literal["binary"] = "binary"
    """A yes/no question; the answer is a probability."""


class ChoiceQuestion(QuestionBase):
    type: Literal["choice"] = "choice"
    """Pick one named option (model routing, category selection)."""

    options: list[str] = Field(min_length=1)
    """Allowed answers. Anything else is invalid → the caller degrades."""


class ScoreQuestion(QuestionBase):
    type: Literal["score"] = "score"
    """Score each item 0..1 against the prompt (tool selection)."""

    items: list[str] = Field(min_length=1)
    """Items to score, e.g. candidate tool names."""


#: A provider-independent classifier question.
Question: TypeAlias = Annotated[
    Union[BinaryQuestion, ChoiceQuestion, ScoreQuestion],
    Field(discriminator="type"),
]


class AnswerBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str


class BinaryAnswer(AnswerBase):
    type: Literal["binary"] = "binary"

    value: bool
    confidence: float = Field(ge=0.0, le=1.0)


class ChoiceAnswer(AnswerBase):
    type: Literal["choice"] = "choice"

    option: str
    """Must be one of the question's ``options``; validated by the caller."""

    confidence: float = Field(ge=0.0, le=1.0)


class ScoreAnswer(AnswerBase):
    type: Literal["score"] = "score"

    scores: dict[str, float]
    """item → score in [0, 1]; items missing here score 0."""


#: A provider-independent classifier answer.
Answer: TypeAlias = Annotated[
    Union[BinaryAnswer, ChoiceAnswer, ScoreAnswer],
    Field(discriminator="type"),
]


class ClassifierReply(BaseModel):
    """The structured-output schema: one call answers every question."""

    model_config = ConfigDict(extra="forbid")

    answers: list[Answer]
