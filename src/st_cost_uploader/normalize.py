"""Shared name normalization. Used by both the alias store and the resolver
so that a name matched one way is matched the same way everywhere."""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")


def normalize_name(value: str) -> str:
    """Lowercase and collapse whitespace. Nothing else.

    Deliberately conservative: campaign names carry meaningful punctuation
    such as 'Search||Google||Brand', so stripping symbols would merge
    campaigns that are genuinely distinct.
    """
    return _WHITESPACE.sub(" ", value).strip().lower()
