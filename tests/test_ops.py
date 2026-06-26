import os
import ops_backend


def test_targets_kolla_only(tmp_path, monkeypatch):
    kolla = tmp_path / "kolla"
    (kolla / "nova").mkdir(parents=True)
    (kolla / "neutron").mkdir()
    monkeypatch.setattr(ops_backend, "KOLLA_LOG_DIR", str(kolla))
    out = ops_backend.targets()
    assert set(out["kolla"]) == {"nova", "neutron"}
    assert "openstackit" not in out


def test_parse_line_extracts_req_id():
    rec = ops_backend._parse_line(
        "2026-06-25 14:30:01.123 INFO nova.api [req-abc12345-def6-7890-abcd-ef1234567890] hi",
        "kolla:nova")
    assert rec["ts"] == "2026-06-25 14:30:01.123"
    assert rec["level"] == "INFO"
    assert rec["id"].startswith("req-")
