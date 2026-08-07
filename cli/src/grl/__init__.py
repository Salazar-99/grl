"""GRL CLI."""

__version__ = "0.1.0"

from grl.config import GRLConfig, load_config
from grl.launcher import launch

__all__ = ["GRLConfig", "launch", "load_config"]
