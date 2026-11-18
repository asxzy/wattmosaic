"""HB §21 mandatory acceptance tests (K07).

Each ``test_atNN_*.py`` module owns exactly one acceptance requirement from
``handbook.md`` §21.  All 20 are marked ``xfail(strict=True)`` with a reason that
names the single task which closes them; that task removes exactly its marker.
Strictness means an accidental pass fails the run, so progress stays visible.

See ``README.md`` in this directory for the file → closing-task map.
"""
