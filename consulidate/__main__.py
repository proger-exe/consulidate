from __future__ import annotations

import asyncio
import sys

from consulidate.app import run
from consulidate.config import get_config

if __name__ == "__main__":
    if "--check" in sys.argv:
        config = get_config()
        print(
            f"Consulidate config OK: organization={config.file.organization.code}, "
            f"roles={len(config.roles)}, provider={config.runtime.model_provider}"
        )
    else:
        asyncio.run(run())
