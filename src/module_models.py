"""
Shared result type returned by every module runner.

Each runner (`run_dictionaries`, `run_pd`, `run_sales_op`) returns a
``ModuleOutcome`` describing what it loaded and whether it succeeded.
``main`` combines the outcome with the module's static post-action config
(``ModuleEntry``) into a ``ModuleRun`` and dispatches post-actions uniformly.

Keeping this in a tiny standalone module (rather than inside ``main``) means
the runners do not import from the CLI entry point — a prerequisite for the
module registry that lets a fifth module be added without touching ``main``'s
dispatch.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy import Table


@dataclass(frozen=True, slots=True)
class ModuleOutcome:
    """
    What a module runner produced for one run.

    ``tables_to_grant`` and ``relation_references_to_grant`` are carried by the
    runner (not looked up in ``main``) so each module owns the construction of
    the relations its post-actions grant on — PD builds them from
    ``build_pd_tables`` / ``pd_view_reference``, the dictionary modules from
    their loaded tables, sales_op from its loaded tables plus its view.
    """

    has_errors: bool
    has_warnings: bool
    has_format_changes: bool
    tables_to_grant: tuple[Table, ...]
    relation_references_to_grant: tuple[str, ...]
