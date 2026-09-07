# Copyright (c) 2026 Calfus Inc.
# Author: Wasiullah Rafeeq S

"""
`gozu run`'s end-of-run summary - printed once, after every activity
(fetch/create/reconcile, plus screenshot capture) has already completed,
for both a single-branch ScanToTicketWorkflow run and a multi-branch
config's MultiBranchScanWorkflow-aggregated result. Exactly one report
either way: MultiBranchScanWorkflow already combines every child's
TicketResult into one (temporal/workflows/multi_branch_scan.py) before
this ever sees it, so there is never a separate report per branch.

Reuses cli/status.py's SUCCESS/WARNING glyphs rather than inventing a
second visual vocabulary - this is the same "done/degraded" language
already used everywhere else in the CLI, just laid out as a table/panel
instead of plain echo lines.
"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cli.status import SUCCESS, WARNING
from core.models import TicketResult

# markup=False: none of this file's own styling uses rich markup tags (see
# Table's `style=` kwargs below instead) - disabling it outright means a
# dynamic value containing literal "[" / "]" (a branch name, a ticket key,
# a finding path) is never misparsed as a style tag and silently dropped.
# Confirmed live: without this, a title like
# "gozu run - x [main, release/2.0] (task y)" rendered with the entire
# "[main, release/2.0]" portion missing. Panel's own `title=` parses
# markup independently of this console setting though (confirmed live,
# still broken with markup=False alone) - that's why render_run_report()
# below wraps the title in a plain rich.text.Text (never markup-parsed at
# all), not a bare str.
_console = Console(markup=False)


def render_run_report(
    config_name: str,
    branches: list[str],
    ce_task_id: str,
    ticket_result: TicketResult,
    duration_seconds: float,
) -> None:
    branch_label = ", ".join(branches) if branches else "(no branch restriction)"

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="bold")
    table.add_column()

    table.add_row(f"{SUCCESS} Created", f"{len(ticket_result.created)} ticket(s)")
    for entry in ticket_result.created:
        table.add_row("", f"{entry.ticket_key} (finding {entry.finding_key})")

    table.add_row("Skipped", f"{len(ticket_result.skipped)} (already ticketed)")

    if ticket_result.deferred:
        rollup = f" -> rollup ticket {ticket_result.rollup_ticket}" if ticket_result.rollup_ticket else ""
        table.add_row(f"{WARNING} Deferred", f"{len(ticket_result.deferred)} (backlog cap){rollup}")

    if ticket_result.closed:
        table.add_row(f"{SUCCESS} Auto-closed", f"{len(ticket_result.closed)}")
        for ticket_key in ticket_result.closed:
            table.add_row("", ticket_key)

    table.add_row("Duration", f"{duration_seconds:.1f}s")

    _console.print(
        Panel(
            table,
            title=Text(f"gozu run - {config_name} [{branch_label}] (SonarQube task {ce_task_id})"),
            expand=False,
        )
    )
