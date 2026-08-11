"""Registry of every team's ``TeamTemplateSpec`` (see
``services/team_template_validator.py``), keyed by team **name** --
the single place ``routes/upload.py`` looks up "does the current
user's team have its own strictly-validated template, and if so,
which one."

Bamawl Team, KiKan Team, and SGL Team have entries today. Adding SSD's
own template later means, in that team's own config module (mirroring
``utils/migrations/bamawl_import_export_config.py`` /
``utils/migrations/kikan_import_export_config.py`` /
``utils/migrations/sgl_import_export_config.py``):

1. Define its required worksheet list, header sheet/row, exact
   expected header row, column mapping, and (optionally) a sample
   template path -- the same ingredients Bamawl's config already has.
2. Build a ``TeamTemplateSpec`` from them.
3. Add one line to ``TEAM_TEMPLATE_SPECS`` below.

No changes to ``services/team_template_validator.py`` or
``routes/upload.py`` are needed -- both already operate generically
against whichever spec ``get_team_template_spec`` returns for the
current team, or fall back to the existing generic upload behavior
if it returns ``None``.
"""

from services.team_template_validator import TeamTemplateSpec


def _build_registry() -> dict[str, TeamTemplateSpec]:
    from utils.migrations.bamawl_import_export_config import _build_bamawl_template_spec
    from utils.migrations.kikan_import_export_config import _build_kikan_template_spec
    from utils.migrations.sgl_import_export_config import _build_sgl_template_spec

    return {
        "Bamawl Team": _build_bamawl_template_spec(),
        "KiKan Team": _build_kikan_template_spec(),
        "SGL Team": _build_sgl_template_spec(),
        # Future team-specific template, once its own config module
        # defines its own spec analogously to Bamawl's/KiKan's/SGL's:
        #   "SSD Team": _build_ssd_template_spec(),
    }


_REGISTRY_CACHE: dict[str, TeamTemplateSpec] | None = None


def get_team_template_spec(team_name: str) -> TeamTemplateSpec | None:
    """Return ``team_name``'s registered ``TeamTemplateSpec``, or None
    if that team has no strictly-validated template of its own.

    A ``None`` result means: this team's upload keeps using the
    existing generic, lenient, keyword-based column matching -- no
    structural validation is enforced for it, exactly as before any of
    this per-team template work existed.
    """
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = _build_registry()
    return _REGISTRY_CACHE.get(team_name)
