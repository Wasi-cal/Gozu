# Copyright (c) 2026 Calfus Inc.
# Author: Prakrit Mohanty

"""Local Semgrep field collection for the init wizard / `gozu config edit`."""

from cli.prompts import prompt_text
from cli.status import error


def prompt_project_label(default: str = "") -> str:
    """
    A display label only, stored in the same `project_key` column
    SonarQube configs use - Semgrep has no server-registered project to
    reference. The actual scan target is whatever path `gozu run -p <path>`
    is given at run time, not anything chosen here.
    """
    while True:
        label = prompt_text("Project label (for your own reference, e.g. a repo name):", default=default).strip()
        if label:
            return label
        error("Project label can't be empty.")
