"""Shared helpers for contract validation."""

from __future__ import annotations


class ContractViolation(ValueError):
    """Raised when a record breaks a rule of the shared data contracts.

    A contract violation is a programming or data error. It is never a
    scientific outcome and must not be shown to users as one.
    """


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractViolation(message)


def non_empty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""
