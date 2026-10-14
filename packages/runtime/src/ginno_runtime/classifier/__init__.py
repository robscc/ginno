# -*- coding: utf-8 -*-
"""Classifier middleware layer: typed decisions (model routing, tool
selection) in front of the main model call. See middlewares.py for the
OFF/SHADOW/ENFORCE semantics and config.py for the settings schema."""
from .base import ClassifierModelBase, ClassifierUnavailable
from .config import classifier_settings
from .middlewares import (
    ModelRouterMiddleware,
    ToolSelectionMiddleware,
    build_middlewares,
)
from .questions import (
    Answer,
    BinaryAnswer,
    BinaryQuestion,
    ChoiceAnswer,
    ChoiceQuestion,
    ClassifierReply,
    Question,
    QuestionBase,
    ScoreAnswer,
    ScoreQuestion,
)

__all__ = [
    "ClassifierModelBase",
    "ClassifierUnavailable",
    "classifier_settings",
    "ModelRouterMiddleware",
    "ToolSelectionMiddleware",
    "build_middlewares",
    "Answer",
    "BinaryAnswer",
    "BinaryQuestion",
    "ChoiceAnswer",
    "ChoiceQuestion",
    "ClassifierReply",
    "Question",
    "QuestionBase",
    "ScoreAnswer",
    "ScoreQuestion",
]
