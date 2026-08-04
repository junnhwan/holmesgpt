"""Tests for the HolmesCustomSkills mirror sync.

The mirror deletes rows based on what loaded from disk, which makes the empty case load
bearing: deleting your last custom skill must clear the mirror, but an unreadable
ConfigMap mount must not. These tests pin the decision down at the layer that makes it.
"""

from pathlib import Path
from unittest.mock import Mock

from holmes.utils.holmes_sync_skills import holmes_sync_skills_status

SKILL_BODY = "---\ndescription: Test skill\n---\n## Goal\nTest\n"


def _write_skill(dir_path: Path, name: str) -> None:
    (dir_path / name).mkdir(parents=True, exist_ok=True)
    (dir_path / name / "SKILL.md").write_text(SKILL_BODY)


def _dal() -> Mock:
    dal = Mock()
    dal.account_id = "acct-1"
    return dal


def _config(paths) -> Mock:
    config = Mock()
    config.cluster_name = "c1"
    config.custom_skill_paths = paths
    return config


def test_clean_load_syncs_with_prune_enabled(tmp_path: Path):
    _write_skill(tmp_path, "alpha")
    dal = _dal()

    holmes_sync_skills_status(dal, _config([tmp_path]))

    rows, cluster = dal.sync_skills.call_args[0]
    assert cluster == "c1"
    assert [r["skill_name"] for r in rows] == ["alpha"]
    assert dal.sync_skills.call_args[1]["prune"] is True


def test_last_skill_deleted_still_prunes(tmp_path: Path):
    """The reported gap: an empty but READABLE directory must prune the mirror.

    Previously this returned early without calling sync_skills at all, so the row for the
    deleted skill stayed in HolmesCustomSkills forever and the UI kept listing it.
    """
    empty = tmp_path / "skills"
    empty.mkdir()
    dal = _dal()

    holmes_sync_skills_status(dal, _config([empty]))

    dal.sync_skills.assert_called_once()
    rows, _ = dal.sync_skills.call_args[0]
    assert rows == []
    assert dal.sync_skills.call_args[1]["prune"] is True


def test_unreadable_source_does_not_prune(tmp_path: Path):
    """An unmounted ConfigMap looks like an empty one, so it must not wipe the mirror."""
    dal = _dal()

    holmes_sync_skills_status(dal, _config([tmp_path / "not-mounted-yet"]))

    rows, _ = dal.sync_skills.call_args[0]
    assert rows == []
    assert dal.sync_skills.call_args[1]["prune"] is False


def test_partial_failure_upserts_but_does_not_prune(tmp_path: Path):
    """Conservative rule: one bad path suppresses the prune for the whole sync, so the
    skills the failed path would have provided are not deleted from the mirror."""
    good = tmp_path / "good"
    _write_skill(good, "alpha")
    dal = _dal()

    holmes_sync_skills_status(dal, _config([good, tmp_path / "missing"]))

    rows, _ = dal.sync_skills.call_args[0]
    assert [r["skill_name"] for r in rows] == ["alpha"]
    assert dal.sync_skills.call_args[1]["prune"] is False


def test_missing_cluster_name_skips_entirely(tmp_path: Path):
    _write_skill(tmp_path, "alpha")
    dal = _dal()
    config = _config([tmp_path])
    config.cluster_name = None

    holmes_sync_skills_status(dal, config)

    dal.sync_skills.assert_not_called()


def test_sync_failure_never_raises(tmp_path: Path):
    """A display-only mirror must never prevent Holmes from starting."""
    dal = _dal()
    dal.sync_skills.side_effect = RuntimeError("boom")

    _write_skill(tmp_path, "alpha")
    holmes_sync_skills_status(dal, _config([tmp_path]))

    # Assert the call happened, otherwise the side_effect never fires and this passes
    # vacuously -- it would still be green if the sync were skipped entirely, proving
    # nothing about suppression.
    dal.sync_skills.assert_called_once()


def test_loader_failure_never_raises_and_skips_the_write(monkeypatch, tmp_path: Path):
    """The loader runs BEFORE the rows are built, so its failure is a separate path.

    Must not raise, and must not reach sync_skills at all -- with no loaded skills and no
    health signal there is nothing to upsert and pruning would be a guess.
    """
    dal = _dal()

    def boom(*_args, **_kwargs):
        raise RuntimeError("loader exploded")

    monkeypatch.setattr(
        "holmes.utils.holmes_sync_skills.load_filesystem_skills", boom
    )

    holmes_sync_skills_status(dal, _config([tmp_path]))

    dal.sync_skills.assert_not_called()
