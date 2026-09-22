#!/usr/bin/env python3
"""Compatibility entry point; the staged runtime owns the implementation."""
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sandbox.runtime import run_grill as implementation

if __name__ == '__main__':
    raise SystemExit(implementation.main())
sys.modules[__name__] = implementation
