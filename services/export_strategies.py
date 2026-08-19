"""Concrete export strategies (Strategy Pattern) -- one per team with
its own official template, plus the shared default every other team
uses.

Bamawl Team's own strategy class (``BamawlExportBuilder``) and KiKan
Team's own strategy class (``KikanExportBuilder``) each live in their
own dedicated builder module (``services/bamawl_export_builder.py``,
``services/kikan_export_builder.py``), not here -- each of those
modules is the single home for everything that team's export needs
(the low-level builder function, its config resolution, and its
Strategy Pattern wiring all together). This module only imports and
registers them, alongside the shared default's own strategy, so this
file stays genuinely team-agnostic, shared plumbing.

Each concrete strategy's ``build`` delegates to that team's
already-existing, unchanged builder function
(``services/bamawl_export_builder.py``, ``services/kikan_export_builder.py``,
``services/export_workbook_service.py``) -- this refactor only
reorganizes *how ``routes/export.py`` picks between them*, not what any
of them actually do. Every existing export's output is byte-for-byte
unchanged by this refactor.

Registering SSD Team's own future export builder means: build that
builder module the same way Bamawl's/KiKan's/SGL's already are (a
``build_ssd_workbook`` function plus its own ``*ExportError`` and
``BaseExportService`` subclass, all in their own dedicated module),
and add one line to ``EXPORT_STRATEGY_REGISTRY`` -- no changes needed
to ``services/base_export_service.py``, and only the new strategy's
own ``ExportContext`` fields need wiring up in ``routes/export.py``.
"""

from services.base_export_service import BaseExportService, ExportContext
from services.bamawl_export_builder import BamawlExportBuilder, BamawlExportError
from services.export_workbook_service import build_workbook
from services.kikan_export_builder import KikanExportBuilder, KikanExportError
from services.sgl_export_builder import SglExportBuilder, SglExportError
from services.ssd_export_builder import SsdExportBuilder, SsdExportError

__all__ = [
    "BamawlExportError",
    "KikanExportError",
    "SglExportError",
    "SsdExportError",
    "BamawlExportBuilder",
    "KikanExportBuilder",
    "SglExportBuilder",
    "SsdExportBuilder",
    "DefaultExportStrategy",
    "EXPORT_STRATEGY_REGISTRY",
    "get_export_strategy_class",
]


class DefaultExportStrategy(BaseExportService):
    """Every team without its own dedicated export builder (SSD Team,
    until it gets its own strategy below) -- delegates to
    ``services/export_workbook_service.py::build_workbook`` (unchanged),
    which builds a fresh workbook from scratch using that team's
    configured column layout (or the built-in default layout, for a
    team with no layout configured at all).
    """

    team_name = "Default"

    def build(self, context: ExportContext) -> None:
        build_workbook(
            context.filepath, context.project_name, context.created_by,
            context.categories, template_config=context.template_config,
            project_remark=context.project_remark,
        )


#: Maps a team's exact name (see ``utils/migrations/team_seed.py``) to
#: its own dedicated export strategy class. A team with no entry here
#: (every team today except Bamawl Team/KiKan Team/SGL Team) gets
#: ``DefaultExportStrategy`` -- see ``get_export_strategy_class``.
EXPORT_STRATEGY_REGISTRY: dict[str, type[BaseExportService]] = {
    "Bamawl Team": BamawlExportBuilder,
    "KiKan Team": KikanExportBuilder,
    "SGL Team": SglExportBuilder,
    "SSD Team": SsdExportBuilder,
}


def get_export_strategy_class(team_name: str | None) -> type[BaseExportService]:
    """Return the export strategy class registered for ``team_name``,
    or ``DefaultExportStrategy`` if that team has no dedicated export
    builder of its own.
    """
    return EXPORT_STRATEGY_REGISTRY.get(team_name, DefaultExportStrategy)