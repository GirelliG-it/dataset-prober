"""AppPaths resolution tests.

These pin the resolution order and the answer-A semantics: --output-dir
and the env var name the output directory ITSELF (files land directly
inside it, no extra output/ level), while the marker-walk hangs output/
off the discovered root.
"""

import pytest

from dataset_prober.paths import AppPaths, OutputDirNotFoundError


def test_explicit_output_dir_is_used_directly(tmp_path):
    paths = AppPaths.resolve(output_dir=tmp_path / "results")
    assert paths.output_dir == tmp_path / "results"
    # Answer A: no extra output/ level, files land where the user pointed
    assert paths.probe_results_path == tmp_path / "results" / "probe_results.json"
    assert paths.env_file is None


def test_env_var_used_when_no_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("DATASET_PROBER_OUTPUT", str(tmp_path / "envdir"))
    paths = AppPaths.resolve()
    assert paths.output_dir == tmp_path / "envdir"
    assert paths.env_file is None


def test_flag_beats_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("DATASET_PROBER_OUTPUT", str(tmp_path / "envdir"))
    paths = AppPaths.resolve(output_dir=tmp_path / "flagdir")
    assert paths.output_dir == tmp_path / "flagdir"


def test_marker_walk_from_nested_cwd(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").touch()
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("DATASET_PROBER_OUTPUT", raising=False)
    paths = AppPaths.resolve()
    assert paths.output_dir == tmp_path / "output"
    assert paths.env_file == tmp_path / ".env"


def test_no_marker_no_flag_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # tmp_path has no pyproject.toml above it...
    monkeypatch.delenv("DATASET_PROBER_OUTPUT", raising=False)
    with pytest.raises(OutputDirNotFoundError):
        AppPaths.resolve()


def test_ensure_output_dir_creates_and_is_idempotent(tmp_path):
    paths = AppPaths.resolve(output_dir=tmp_path / "fresh" / "nested")
    assert not paths.output_dir.exists()  # properties have no side effects
    paths.ensure_output_dir()
    assert paths.output_dir.is_dir()
    paths.ensure_output_dir()  # second call must not raise
