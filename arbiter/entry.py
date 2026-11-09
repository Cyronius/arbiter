"""Absolute-path MCP entry point, also available in installed Python packages."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arbiter.cli import main

main()
