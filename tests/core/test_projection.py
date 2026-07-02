from core.projection import full, summary, cap


class _Obj:
    def to_dict(self):
        return {"id": "1", "name": "vm", "status": "ACTIVE",
                "links": [{"x": 1}], "empty": None, "blank": ""}


def test_full_drops_noise_and_empties():
    out = full(_Obj())
    assert out == {"id": "1", "name": "vm", "status": "ACTIVE"}


def test_full_passes_through_plain_dict():
    assert full({"a": 1, "b": None}) == {"a": 1}


def test_summary_picks_fields():
    assert summary(_Obj(), ["id", "name"]) == {"id": "1", "name": "vm"}


def test_summary_falls_back_to_full_when_no_field_matches():
    assert summary(_Obj(), ["nonexistent"]) == full(_Obj())


def test_cap_limits_and_zero_means_all():
    assert cap([1, 2, 3, 4], 2) == [1, 2]
    assert cap([1, 2, 3], 0) == [1, 2, 3]
