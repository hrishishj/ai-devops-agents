"""Locate data directories (prompts, runbooks, policies).

A source checkout has them next to the code; an installed wheel ships them inside the package
(`devopsagents/_data/<name>`), so the tool works from `pip install` with no repo checkout.
"""

from __future__ import annotations

from pathlib import Path

PACKAGED_ROOT = Path(__file__).resolve().parents[2] / "_data"


def resolve_data(base_dir: Path, configured: str, *, packaged_root: Path = PACKAGED_ROOT) -> Path:
    """Prefer `base_dir/configured` (a checkout or a user override); fall back to the packaged copy."""
    local = base_dir / configured
    if local.exists():
        return local
    name = Path(configured).parts[0] if Path(configured).parts else configured
    packaged = packaged_root / configured if (packaged_root / configured).exists() else packaged_root / name
    return packaged if packaged.exists() else local
