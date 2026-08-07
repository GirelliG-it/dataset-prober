"""Application path resolution.

AppPaths owns runtime/user data locations: where output lands and where
.env is found. It does NOT own package data — profiles ship inside the
package and are resolved by config_loader (importlib.resources in C2).

Resolution order (decided 26 Jul; amended 31 Jul: the flag and env var
name the OUTPUT directory itself, not a root above it):
    1. --output-dir CLI flag        (explicit wins)
    2. DATASET_PROBER_OUTPUT env    (explicit, ambient)
    3. walk up from cwd for pyproject.toml; output/ hangs off that root
    4. raise with a clear message   (never guess)

env_file is only knowable in the marker-walk case — an installed copy
pointed at a bare output directory has no repo to find .env in. It is
None in that case; .env handling at the app boundary is C2's concern.

Path properties are side-effect free. Directory creation happens only
via ensure_output_dir(), called immediately before a write — never at
import, never on read-only invocations like --list-profiles.
"""

import os
from dataclasses import dataclass
from pathlib import Path

_ROOT_MARKER = "pyproject.toml"
_ENV_VAR = "DATASET_PROBER_OUTPUT"


class OutputDirNotFoundError(RuntimeError):
    """No usable output location: no flag, no env var, no marker above cwd."""


def _walk_up_for_marker(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / _ROOT_MARKER).is_file():
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class AppPaths:
    output_dir: Path
    env_file: Path | None = None

    # -- stable application artifacts (nobody else spells these) -----
    @property
    def probe_results_path(self) -> Path:
        return self.output_dir / "probe_results.json"

    @property
    def duckdb_path(self) -> Path:
        return self.output_dir / "datasets.duckdb"

    @property
    def analysis_summary_path(self) -> Path:
        return self.output_dir / "analysis_summary.txt"

    @property
    def agent_results_path(self) -> Path:
        return self.output_dir / "agent_results.json"

    # -- the one sanctioned side effect ------------------------------
    def ensure_output_dir(self) -> Path:
        """Create output_dir if needed. Call immediately before a write."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir

    # -- construction at the app boundary ----------------------------
    @classmethod
    def resolve(cls, output_dir: str | Path | None = None) -> "AppPaths":
        if output_dir is not None:
            return cls(output_dir=Path(output_dir).expanduser().resolve())
        env_value = os.environ.get(_ENV_VAR)
        if env_value:
            return cls(output_dir=Path(env_value).expanduser().resolve())
        root = _walk_up_for_marker(Path.cwd())
        if root is not None:
            return cls(output_dir=root / "output", env_file=root / ".env")
        raise OutputDirNotFoundError(
            f"Cannot determine where to write application data: no "
            f"--output-dir given, ${_ENV_VAR} is unset, and no "
            f"{_ROOT_MARKER} found in {Path.cwd()} or any parent. "
            f"Pass --output-dir or set ${_ENV_VAR}."
        )
