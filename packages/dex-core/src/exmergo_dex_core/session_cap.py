"""A host-set cap on the daily spend ceiling (issue #787).

``budget.session_ceiling`` bounds a day's cumulative spend, and it lives in
``.dex/config.yml``, which is a file the agent driving dex can write: the
one-time ask's answer flags, ``dex config set|unset``, and the engine's own
``session_ceiling`` and ``decline_session_ceiling`` arguments all land there, and
each takes effect for the rest of the UTC day before anyone reviews the commit.
The cap is the bound a host keeps out of that reach, selected the way
values-free mode is (#591): by the caller, through ``DexEngine`` or the
environment, and never by the repository.

It works by taking a minimum rather than by blocking writes. The ceiling in
force is the lower of the committed one and the cap, and the cap alone where the
file sets none or recorded a decline, so no write can raise spend above it and
nothing that writes has to be recognized for the cap to hold. A write that the
cap makes moot still runs, and says so (:func:`moot_write_warning`).

What it protects is narrower than the name suggests, and the docs say so: it is
a boundary where the host owns the engine process, and not against an agent
with a shell that can launch dex with an environment of its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .errors import ConfigurationError

#: The environment variable a host pins the cap with for every CLI process.
SESSION_CEILING_CAP_ENV = "DEX_SESSION_CEILING_CAP"

CapSource = Literal["environment", "host"]


@dataclass(frozen=True)
class SessionCeilingCap:
    """The cap and who set it: ``environment`` for ``DEX_SESSION_CEILING_CAP``,
    ``host`` for ``DexEngine(session_ceiling_cap=...)``."""

    value: float
    source: CapSource

    def named(self) -> str:
        """The cap as a refusal or a warning names it, so a reader knows both
        the number and the only place it can be changed."""

        where = (
            f"{SESSION_CEILING_CAP_ENV}={format_magnitude(self.value)}"
            if self.source == "environment"
            else f"DexEngine(session_ceiling_cap={format_magnitude(self.value)})"
        )
        return f"the host's cap ({where})"

    def payload(self, unit: str | None) -> dict[str, object]:
        """``data.session_ceiling_cap`` on an envelope."""

        return {"value": self.value, "unit": unit, "source": self.source}


def format_magnitude(value: float) -> str:
    """A magnitude as a person would type it: ``50000000000``, not ``5e+10``."""

    return str(int(value)) if float(value).is_integer() else repr(float(value))


def parse_session_ceiling_cap(
    raw: object, *, source: CapSource
) -> SessionCeilingCap | None:
    """The cap ``raw`` names, or ``None`` when nothing was set.

    Refuses anything that is not a positive, finite number rather than reading
    it as unset, for the reason an unknown ``DEX_RESULT_MODE`` refuses: a host
    that believes it pinned a bound and in fact pinned nothing has lost exactly
    the protection it came for.
    """

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, SessionCeilingCap):
        return raw
    named = (
        SESSION_CEILING_CAP_ENV
        if source == "environment"
        else "DexEngine(session_ceiling_cap=...)"
    )
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = float(raw.strip()) if isinstance(raw, str) else float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConfigurationError(
            f"{named} must be a positive number in the connector's unit (bytes on "
            f"BigQuery, seconds elsewhere), got {raw!r}; unset it to run without a "
            "host cap"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(
            f"{named} must be a positive number in the connector's unit, got "
            f"{raw!r}; unset it to run without a host cap"
        )
    return SessionCeilingCap(value=value, source=source)


def ceiling_in_force(
    session_ceiling: float | None, cap: SessionCeilingCap | None
) -> float | None:
    """The daily ceiling that binds: the committed one, the cap, or the lower of
    the two. A recorded decline never loosens a cap, which is why ``declined``
    is not an input here."""

    if cap is None:
        return session_ceiling
    if session_ceiling is None:
        return cap.value
    return min(session_ceiling, cap.value)


def ceiling_source(
    session_ceiling: float | None, cap: SessionCeilingCap | None
) -> Literal["config", "environment", "host"]:
    """Where the ceiling in force comes from. A tie goes to the cap: it is the
    bound no write can move, so it is the one a reader should be pointed at."""

    if cap is None:
        return "config"
    if session_ceiling is not None and session_ceiling < cap.value:
        return "config"
    return cap.source


def moot_write_warning(
    cap: SessionCeilingCap | None,
    *,
    before: float | None,
    after: float | None,
    what: str,
) -> list[str]:
    """The warning a ``budget.session_ceiling*`` write carries when the cap makes
    it change nothing in force.

    A warning, not a refusal: the minimum is what holds the bound, so refusing
    would only make the committed file unwritable under a cap for no protective
    gain. What the reviewer of the diff needs is to know the write is moot while
    the cap holds, and why.
    """

    if cap is None:
        return []
    if ceiling_in_force(before, cap) != ceiling_in_force(after, cap):
        return []
    return [
        f"{what} does not change the daily ceiling in force: {cap.named()} binds, "
        f"so the ceiling stays {format_magnitude(ceiling_in_force(after, cap))} "
        "whatever .dex/config.yml says above it. The change is still written, and "
        "takes effect only if the host lifts the cap"
    ]
