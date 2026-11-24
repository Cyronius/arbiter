"""Absolute-path MCP and service entry point, including from a source checkout."""
import os
import sys

# Do not put arbiter/operator.py ahead of the standard library's operator module.
package_dir = os.path.dirname(os.path.realpath(__file__))
sys.path = [os.path.dirname(package_dir), *(p for p in sys.path if p != package_dir)]
from arbiter.cli import main

main()
