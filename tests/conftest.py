"""Load API-only modules without installing the complete Home Assistant core."""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).parents[1]
CUSTOM_COMPONENTS = ROOT / "custom_components"
PESC_PACKAGE = CUSTOM_COMPONENTS / "pesc"

# Importing ``custom_components.pesc.api`` normally executes the integration's
# Home Assistant entry point first. Unit tests for the standalone API client do
# not need HA, so namespace stubs point Python directly at the module files.
custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(CUSTOM_COMPONENTS)]
sys.modules.setdefault("custom_components", custom_components)

pesc = types.ModuleType("custom_components.pesc")
pesc.__path__ = [str(PESC_PACKAGE)]
sys.modules.setdefault("custom_components.pesc", pesc)
