"""Boolean expressions over component-active state.

A ``Gate`` declares "this surface or job is relevant only when these
components are opted-in." It is a small typed AST — ``Component`` leaves
combined with ``And`` / ``Or`` / ``Not`` — so it serializes to JSON,
evaluates in either Python or JS, and is introspectable for diagnostics.

The first consumer is ``work_buddy.dashboard.cards``: every dashboard
card may carry a gate, and the card endpoint evaluates it against the
control graph to decide whether the card mounts. The same ``Gate`` type
is the intended home for future scheduler-side job gating — a job that
should only fire when its supporting components are opted-in.

A component counts as "active" when its control-graph node's
``effective_state`` is anything other than ``disabled``; ``disabled``
occurs only on an explicit opt-out. Callers pass the set of active
component ids to :func:`evaluate`.

Gates can be authored two ways:

* Directly: ``And((Component("obsidian"), Or((Component("thunderbird"),
  Component("outlook")))))``.
* Via the string DSL: ``parse_gate("obsidian & (thunderbird | outlook)")``.

Both produce the same AST.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class Component:
    """Leaf node: true when ``id`` is in the active-component set."""

    id: str


@dataclass(frozen=True)
class And:
    """True when every operand is true. Empty operands → true (vacuous)."""

    operands: tuple["Gate", ...]


@dataclass(frozen=True)
class Or:
    """True when any operand is true. Empty operands → false (vacuous)."""

    operands: tuple["Gate", ...]


@dataclass(frozen=True)
class Not:
    """True when its operand is false."""

    operand: "Gate"


Gate = Union[Component, And, Or, Not]


# ---------------------------------------------------------------------------
# Evaluation + introspection
# ---------------------------------------------------------------------------


def evaluate(gate: Gate | None, active_ids: set[str]) -> bool:
    """Evaluate ``gate`` against the set of active component ids.

    A ``None`` gate is always active — an ungated card always mounts.
    """
    if gate is None:
        return True
    if isinstance(gate, Component):
        return gate.id in active_ids
    if isinstance(gate, And):
        return all(evaluate(o, active_ids) for o in gate.operands)
    if isinstance(gate, Or):
        return any(evaluate(o, active_ids) for o in gate.operands)
    if isinstance(gate, Not):
        return not evaluate(gate.operand, active_ids)
    raise TypeError(f"unknown gate node: {type(gate)!r}")


def referenced_components(gate: Gate | None) -> set[str]:
    """Return every ``Component`` leaf id referenced by ``gate``.

    Used for registration-time validation and for diagnostics such as
    "which cards depend on component X?".
    """
    if gate is None:
        return set()
    if isinstance(gate, Component):
        return {gate.id}
    if isinstance(gate, Not):
        return referenced_components(gate.operand)
    if isinstance(gate, (And, Or)):
        out: set[str] = set()
        for operand in gate.operands:
            out |= referenced_components(operand)
        return out
    raise TypeError(f"unknown gate node: {type(gate)!r}")


def validate(gate: Gate | None, known_components: set[str]) -> None:
    """Raise ``ValueError`` if ``gate`` references an unknown component id.

    Catches typos at card-registration time rather than at runtime when
    the gate would silently evaluate false forever.
    """
    unknown = referenced_components(gate) - known_components
    if unknown:
        raise ValueError(
            f"gate references unknown components: {sorted(unknown)}"
        )


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


def to_json(gate: Gate | None) -> dict | None:
    """Serialize a gate to a plain JSON-safe dict (or ``None``)."""
    if gate is None:
        return None
    if isinstance(gate, Component):
        return {"op": "component", "id": gate.id}
    if isinstance(gate, And):
        return {"op": "and", "operands": [to_json(o) for o in gate.operands]}
    if isinstance(gate, Or):
        return {"op": "or", "operands": [to_json(o) for o in gate.operands]}
    if isinstance(gate, Not):
        return {"op": "not", "operand": to_json(gate.operand)}
    raise TypeError(f"unknown gate node: {type(gate)!r}")


def from_json(data: dict | None) -> Gate | None:
    """Inverse of :func:`to_json`."""
    if data is None:
        return None
    op = data.get("op")
    if op == "component":
        return Component(data["id"])
    if op == "and":
        return And(tuple(from_json(o) for o in data.get("operands", [])))
    if op == "or":
        return Or(tuple(from_json(o) for o in data.get("operands", [])))
    if op == "not":
        return Not(from_json(data["operand"]))
    raise ValueError(f"unknown gate op: {op!r}")


# ---------------------------------------------------------------------------
# String DSL parser
# ---------------------------------------------------------------------------
#
# Grammar (precedence low → high):
#
#   or_expr   := and_expr ( '|' and_expr )*
#   and_expr  := not_expr ( '&' not_expr )*
#   not_expr  := '!' not_expr | atom
#   atom      := IDENT | '(' or_expr ')'
#
# IDENT is [A-Za-z0-9_]+ (component ids). Whitespace is insignificant.

_OPERATOR_CHARS = {"&", "|", "!", "(", ")"}


def _tokenize(expr: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch in _OPERATOR_CHARS:
            tokens.append(ch)
            i += 1
            continue
        if ch.isalnum() or ch == "_":
            j = i
            while j < n and (expr[j].isalnum() or expr[j] == "_"):
                j += 1
            tokens.append(expr[i:j])
            i = j
            continue
        raise ValueError(f"unexpected character {ch!r} in gate expression")
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _next(self) -> str:
        tok = self._peek()
        if tok is None:
            raise ValueError("unexpected end of gate expression")
        self._pos += 1
        return tok

    def parse(self) -> Gate:
        gate = self._parse_or()
        if self._peek() is not None:
            raise ValueError(
                f"unexpected trailing token {self._peek()!r} in gate expression"
            )
        return gate

    def _parse_or(self) -> Gate:
        operands = [self._parse_and()]
        while self._peek() == "|":
            self._next()
            operands.append(self._parse_and())
        return operands[0] if len(operands) == 1 else Or(tuple(operands))

    def _parse_and(self) -> Gate:
        operands = [self._parse_not()]
        while self._peek() == "&":
            self._next()
            operands.append(self._parse_not())
        return operands[0] if len(operands) == 1 else And(tuple(operands))

    def _parse_not(self) -> Gate:
        if self._peek() == "!":
            self._next()
            return Not(self._parse_not())
        return self._parse_atom()

    def _parse_atom(self) -> Gate:
        tok = self._next()
        if tok == "(":
            inner = self._parse_or()
            closing = self._next()
            if closing != ")":
                raise ValueError(
                    f"expected ')' in gate expression, got {closing!r}"
                )
            return inner
        if tok in _OPERATOR_CHARS:
            raise ValueError(
                f"unexpected operator {tok!r} where a component id was expected"
            )
        return Component(tok)


def parse_gate(expr: str) -> Gate:
    """Parse a gate-DSL string into a :data:`Gate` AST.

    Operators, low to high precedence: ``|`` (or), ``&`` (and), ``!``
    (not). Parentheses group. Identifiers are component ids. Raises
    ``ValueError`` on malformed input.

        >>> parse_gate("obsidian & (thunderbird | outlook)")
        And((Component('obsidian'), Or((Component('thunderbird'), Component('outlook')))))
    """
    tokens = _tokenize(expr)
    if not tokens:
        raise ValueError("empty gate expression")
    return _Parser(tokens).parse()
