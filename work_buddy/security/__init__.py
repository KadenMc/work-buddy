"""Security boundaries shared by Work Buddy surfaces.

The package is intentionally small.  In particular, local browser identity is
not general account authentication and must not be described as such.
"""

from work_buddy.security.actors import ActorRef, InvalidActorReference
from work_buddy.security.local_identity import (
    BootstrapGrant,
    BoundaryRequest,
    HumanAuthorityContext,
    LocalIdentityAuthority,
    LocalIdentityError,
    LocalIdentityPolicy,
    LocalPrincipal,
    SessionGrant,
    get_default_authority,
)

__all__ = [
    "ActorRef",
    "BootstrapGrant",
    "BoundaryRequest",
    "HumanAuthorityContext",
    "InvalidActorReference",
    "LocalIdentityAuthority",
    "LocalIdentityError",
    "LocalIdentityPolicy",
    "LocalPrincipal",
    "SessionGrant",
    "get_default_authority",
]
