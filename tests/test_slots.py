import pytest

from harness.config import HarnessConfig, SlotConfig
from harness.slots import SlotPool


def pool(tmp_path, *slots):
    return SlotPool(HarnessConfig(state_dir=str(tmp_path), slots=list(slots)))


def test_github_slots_leased_before_local(tmp_path):
    p = pool(
        tmp_path, SlotConfig(name="local-01"), SlotConfig(name="slot-01", repo="org/project-01")
    )
    assert p.lease("r1").slot.name == "slot-01"
    assert p.lease("r2").slot.name == "local-01"  # fallback once the GitHub slot is busy


def test_cloud_runs_never_get_a_local_slot(tmp_path):
    p = pool(
        tmp_path, SlotConfig(name="local-01"), SlotConfig(name="slot-01", repo="org/project-01")
    )
    assert p.lease("r1", remote_only=True).slot.name == "slot-01"
    with pytest.raises(Exception, match="no free slot"):
        p.lease("r2", remote_only=True)
    with pytest.raises(ValueError, match="local-only"):
        p.candidates("local-01", remote_only=True)
    with pytest.raises(ValueError, match="need a GitHub slot"):
        pool(tmp_path / "x", SlotConfig(name="local-01")).candidates(remote_only=True)


def test_lease_records_the_home_name_and_keeps_it_when_a_token_is_recorded(tmp_path):
    import json

    p = pool(tmp_path, SlotConfig(name="local-01"))
    lease = p.lease("r1", home="abc123")
    assert lease.home == "abc123"
    lease.record_token("tok")
    data = json.loads(lease.path.read_text())
    assert data["home"] == "abc123" and data["agent_token"] == "tok"
    assert not list((tmp_path / "slots").glob(".*.tmp"))
    assert p.status()[0]["lease"]["home"] == "abc123"
