"""Leg-pair timeliness constant shared by every measurement path.

There is no Level-1 direct measurement path: direct measurement was gated on a
channel role that the contract no longer defines.  What survives here is the one
definition of how far apart the two legs of a declared double-pole pair may be
sampled before the pair stops describing a single instant.

Two legs of one double-pole circuit are usable only *as a pair*.  A leg is not a
measurement of the circuit at half its value: with the partner missing the sum
is one leg's power, and with the legs polled a cycle apart the sum mixes two
different instants.  Both cases are reported as reasons and never repaired by
averaging, by taking the newer leg, or by falling back to one leg.
"""

from __future__ import annotations

#: How far apart in time the two legs of a declared double-pole pair may be
#: before the pair stops describing one instant.  Two legs of one circuit are
#: sampled within a cycle of each other (the reference install's Refoss panel
#: polls at 15.7 s and its legs arrive ~2 s apart); a larger skew means one leg
#: is a leftover from an earlier poll, so the pair is a mix of two instants and
#: neither leg may be used.
#:
#: This is the *single* definition of the limit.  The solver path
#: (``DisaggregationEngine._exclude_partial_legs``) reads this constant, so the
#: engine default and the documented rule can never disagree.
DEFAULT_MAX_LEG_SKEW_S = 20.0
