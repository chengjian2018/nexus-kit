"""Host configuration: locate local_config.yaml and inject its path into the
kernel settings (nexus.settings).

Only the host knows where the yaml lives; nexus/atoms read everything through
``nexus.settings``. Importing this package has the side effect of pointing the
kernel at ``host/config/local_config.yaml`` — host.main / host.cli import it
first thing.
"""

from pathlib import Path

from nexus import settings

settings.set_config_path(str(Path(__file__).resolve().parent / "local_config.yaml"))

# Host-level convenience re-exports (session/db wiring lives in the host)
from nexus.settings import (  # noqa: E402,F401
    get_knowledge_db_path,
    get_llm_config,
    get_session_compress_config,
    get_session_db_path,
    load_config,
)
