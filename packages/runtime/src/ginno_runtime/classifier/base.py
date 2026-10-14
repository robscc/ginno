# -*- coding: utf-8 -*-
"""Abstract classifier backend + the one exception it may raise."""
from abc import ABC, abstractmethod

from .questions import Answer, QuestionBase


class ClassifierUnavailable(Exception):
    """Backend not configured, call failed, timed out, or reply unparseable.

    Implementations wrap EVERY internal failure into this one type, so
    middleware can treat "anything went wrong" uniformly: degrade to the
    safe path (original model / full toolset) and keep the turn running.
    """


class ClassifierModelBase(ABC):
    """A model that answers typed questions instead of generating prose."""

    @abstractmethod
    async def classify(
        self, state: dict, questions: list[QuestionBase]
    ) -> list[Answer]:
        """Evaluate ``questions`` against the graph ``state``.

        Returns answers aligned 1:1 with ``questions`` (same order, same
        ids). Raises only :class:`ClassifierUnavailable`.
        """
