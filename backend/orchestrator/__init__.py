import os
import sys

if sys.version_info[0] < 3:
    sys.stderr.write(
        "Orchestrator requires Python 3.6+ (f-strings used). "
        "You are running Python %s.\nRun with: python3 -m orchestrator\n" % sys.version.split()[0]
    )
    sys.stderr.flush()
    os._exit(1)

from . import config, state, agents, notifier
from .core import main

__all__ = ["main", "config", "state", "agents", "notifier"]
