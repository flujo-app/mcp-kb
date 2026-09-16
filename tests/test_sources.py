import pytest

from mcp_school.config import Config, FileSource
from mcp_school.sources import SourceError, materialise, materialise_all


def test_a_file_source_is_served_in_place(tmp_path):
    source = FileSource(name="kubed", url=f"file://{tmp_path}")
    assert materialise(source, tmp_path / "cache") == tmp_path
    assert not (tmp_path / "cache").exists()


def test_a_missing_directory_is_a_source_error(tmp_path):
    source = FileSource(name="kubed", url=f"file://{tmp_path / 'nope'}")
    with pytest.raises(SourceError, match="nope"):
        materialise(source, tmp_path / "cache")


def test_materialise_all_skips_a_broken_source_and_keeps_the_rest(tmp_path):
    (tmp_path / "good").mkdir()
    config = Config.model_validate(
        {
            "sources": [
                {"name": "good", "url": f"file://{tmp_path / 'good'}"},
                {"name": "bad", "url": f"file://{tmp_path / 'bad'}"},
            ]
        }
    )
    got = materialise_all(config, tmp_path / "cache")
    assert got["good"] == tmp_path / "good"
    assert isinstance(got["bad"], SourceError)
