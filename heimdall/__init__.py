"""HEIMDALL: an evidence-first investigation platform for SPHEREx observations.

The package holds the shared contracts, the data-source adapters (public IRSA
SPHEREx archive, local FITS), the scientific engine (``heimdall.engine``), the
integrated investigation pipeline, the sky-map data contract and the
programmatic API (``heimdall.api``) used by the CLI and the future UI.
"""

__version__ = "0.2.0"

# Version of the shared data contracts. The "provisional" suffix marks that
# several contracts must be revised after inspecting real data products.
SCHEMA_VERSION = "0.1.0-provisional"
