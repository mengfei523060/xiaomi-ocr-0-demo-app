"""Vendored copies of the repository modules the App depends on.

Provenance (copied 2026-11-27, logic unchanged):
  otsl.py          ← postprocess/otsl.py
  document.py      ← postprocess/document.py      (import adjusted to relative .otsl)
  region.py        ← postprocess/region.py
  cropping.py      ← evaluation_pipeline/cropping.py (trailing dead comment removed)
  ollama_backend.py← pipeline/ollama_backend.py
  layout_regions.py← evaluation_pipeline/layout_detect_paddlex.py (region helpers only)

Keeping these copies lets app/ run as a standalone folder with only
Pillow, pypdfium2 and numpy installed — see app/core/requirements.txt.
"""
