#!/usr/bin/env python3
"""Compatibility entry point for the reusable PPTD exporter/importer."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pptd_engine.pptx import *  # retain previous probe imports
if __name__ == "__main__": main()
