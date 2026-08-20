"""Base Strategy-Pattern infrastructure for MHES's Excel export.

Every team's export builds an Excel workbook from the same Preview
Category -> Task -> Activity data (``ExportContext.categories``), but
*how* that workbook gets built differs per team: Bamawl Team and KiKan
Team each populate their own single official template
(``services/bamawl_export_builder.py``, ``services/kikan_export_builder.py``),
while every other team gets a workbook built from scratch, column by
column (``services/export_workbook_service.py``). Before this refactor,
``routes/export.py::export_excel`` picked between these three
free-function code paths directly with an if/elif/else chain -- this
module formalizes that same choice as a Strategy Pattern: one common
interface (``BaseExportService.build``), one concrete strategy class
per team-specific builder, each simply delegating to that team's
already-existing, unchanged builder function -- this refactor is
structural only, no export's actual output changes.

See ``services/export_strategies.py`` for the concrete strategies
(Bamawl, KiKan, Default) and the registry that selects between them.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExportContext:
    """Everything a concrete export strategy might need to build one
    workbook.

    Not every field is used by every strategy -- e.g. only the
    team-specific strategies use ``column_mapping``/``template_path``,
    only ``DefaultExportStrategy`` uses ``template_config`` -- unused
    fields are simply left at their default. ``routes/export.py``
    populates only the fields the strategy it selected actually needs,
    exactly mirroring what each of the three code paths already
    received before this refactor.
    """

    filepath: str
    categories: list[dict[str, Any]]
    project_name: str
    created_by: str
    column_mapping: dict[str, Any] | None = None
    template_path: str | None = None
    template_config: dict[str, Any] | None = None
    project_remark: str = ""
    # Edited phase percentages from Preview (list of {"label", "coef"}),
    # written into the export template's coefficient row so a formula
    # team's export reflects the user's adjusted percentages rather than
    # the template's originals. None/empty keeps the template defaults.
    phase_coefficients: list[dict[str, Any]] | None = None


class BaseExportService(ABC):
    """Common interface and shared functionality every concrete export
    strategy (one per team, or the shared default) implements.

    Subclasses only need to implement ``build`` -- everything else here
    is genuinely shared behavior, not duplicated per strategy.
    """

    #: Human-readable name for logging -- overridden by each concrete
    #: strategy (e.g. "Bamawl Team", "KiKan Team").
    team_name: str = "Default"

    @abstractmethod
    def build(self, context: ExportContext) -> None:
        """Populate and save a workbook for ``context`` to
        ``context.filepath``.

        Raises whatever team-specific error the underlying builder
        raises (e.g. ``BamawlExportError``, ``KikanExportError``) if
        the data doesn't fit that team's template -- callers are
        expected to catch those explicitly, same as before this
        refactor.
        """

    @staticmethod
    def _flatten_tasks(categories: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        """Return every (category name, task) pair across ``categories``,
        in order -- the same flattening both Bamawl's and KiKan's own
        builders already do internally. Exposed here as shared
        functionality for any strategy (including a future SGL/SSD
        one) that needs a flat task list rather than the nested
        Category -> Task structure.
        """
        return [
            (cat.get("category", ""), task)
            for cat in categories
            for task in cat.get("tasks", [])
        ]

    def run(self, context: ExportContext) -> None:
        """Build the workbook, then log one consistent, team-agnostic
        success line -- the one piece of behavior every strategy gets
        uniformly, on top of whatever it already logs internally.
        """
        self.build(context)
        logger.info(
            "%s: export workbook built for project=%r -> %s",
            self.team_name, context.project_name, context.filepath,
        )