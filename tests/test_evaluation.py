import pytest

from radar.evaluation import alert_cases, binary_metrics, freeze


def test_metrics_require_complete_labels_and_do_not_invent_denominators():
    with pytest.raises(ValueError, match="evaluation_set_mismatch"):
        binary_metrics({"a": True, "b": False}, {"a": True})
    result = binary_metrics(
        {"a": True, "b": True, "c": False, "d": False}, {"a": True, "b": False, "c": True, "d": False}
    )
    assert result["precision"] == 0.5 and result["recall"] == 0.5
    assert binary_metrics({"a": False}, {"a": False})["recall"] is None


def test_frozen_packets_cannot_be_silently_replaced(tmp_path):
    path = tmp_path / "alerts.json"
    cases = alert_cases()
    packet = freeze(path, cases, "independent_alert_labels")
    assert len(cases) == 30 and all(c["synthetic"] for c in cases)
    assert packet["review_status"] == "unlabeled"
    with pytest.raises(FileExistsError):
        freeze(path, cases, "changed")
