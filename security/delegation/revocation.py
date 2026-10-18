"""In-memory revocation state shared by the STS and the tool-side verifier (prototype A).

Two pieces live here:

  Denylist   revoked token ids (jti) and revoked grants (grant_id), each with the
             expiry after which the entry no longer matters. The STS writes to it;
             any verifier holding the same object reads from it. This is the
             in-process stand-in for the pushed deny list in DIAGRAMS.md section 4.
  Issued     the STS's record of every token it minted, enough to walk a token's
             descendants (children) and ancestors (parent_jti) when one is revoked.
  EscalationPolicy
             thresholds for tool-initiated reports: how many strikes inside a
             window before the STS widens a revocation from the presented token to
             the actor's delegation, then to the whole grant. The STS owns this;
             the reporting tool cannot change it.

Both are bounded by token lifetimes, not by traffic: a tool or handoff token's
exp never exceeds the user token it descends from (exp = min(now + ttl, subject
exp)), the user token lives 600 s and actor tokens 60 s, so every entry here is
pruned within 600 s of being written. Nothing is persisted; a restart forgets
all revocations and all issued tokens, which is acceptable only because a restart
also rotates the signing key and so invalidates every outstanding token.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Issued:
    """What the STS remembers about one token it signed."""
    jti: str
    kind: str  # user | actor | handoff | tool
    sub: str
    exp: int
    grant_id: str | None = None
    parent_jti: str | None = None
    client_id: str | None = None  # the client the token was issued to (actor) or that requested it (exchange)
    aud: tuple[str, ...] = ()
    actors: tuple[str, ...] = ()  # act chain, earliest first
    jkt: str | None = None  # cnf.jkt: the key the token is bound to, so revoke_key can find it
    children: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EscalationPolicy:
    """Strike thresholds for /revoke reports with reason anomaly or injection_suspected.

    Strikes are counted per (grant_id, actor chain) inside window_s. At the first
    strike only the presented token dies; at handoff_at the immediate actor's
    delegation (the handoff the token descends from, or every token that actor
    exchanged under the grant) dies; at grant_at the grant dies. cleanup never
    counts. Defaults: 10 minutes, 2, 3.
    """
    window_s: int = 600
    handoff_at: int = 2
    grant_at: int = 3


class Denylist:
    """Revoked jtis and grant_ids with expiries. Satisfies verify.RevocationCheck.

    An entry's expiry is the latest exp in the revoked family, so a check never
    misses a live descendant, and prune() drops entries as soon as every token they
    could match has expired on its own. The set is therefore bounded by the 600 s
    user token lifetime regardless of how many revocations arrive.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._jti: dict[str, int] = {}
        self._grant: dict[str, int] = {}

    def _now(self) -> int:
        return int(self.clock())

    def add_jti(self, jti: str, exp: int) -> None:
        self._jti[jti] = max(exp, self._jti.get(jti, 0))

    def add_grant(self, grant_id: str, exp: int) -> None:
        self._grant[grant_id] = max(exp, self._grant.get(grant_id, 0))

    def prune(self) -> None:
        now = self._now()
        for table in (self._jti, self._grant):
            for key in [k for k, exp in table.items() if exp <= now]:
                del table[key]

    def revoked(self, claims: dict) -> str | None:
        """Reason the token is revoked, or None. Checks jti, parent_jti and grant_id."""
        self.prune()
        jti = claims.get("jti")
        if jti and jti in self._jti:
            return f"token {jti[:8]} revoked"
        parent = claims.get("parent_jti")
        if parent and parent in self._jti:
            return f"parent token {parent[:8]} revoked"
        grant = claims.get("grant_id")
        if grant and grant in self._grant:
            return f"grant {grant[:8]} revoked"
        return None

    def __len__(self) -> int:
        self.prune()
        return len(self._jti) + len(self._grant)

    def __contains__(self, key: str) -> bool:
        self.prune()
        return key in self._jti or key in self._grant
