import pytest

from radar.llm import ModelError, ModelRunner, quota_config
from radar.store import Store


class FakeProvider:
    name = "fixture"
    model = "fixture-1"

    def __init__(self, content=None, error=False):
        self.calls = 0
        self.content = content or {"answer": "ok"}
        self.error = error

    def generate(self, system, payload, schema, max_output_tokens):
        self.calls += 1
        if self.error:
            raise ModelError("model_timeout")
        return {
            "content": self.content,
            "usage": {"input_tokens": 100, "output_tokens": 25, "basis": "observed"},
        }


def test_cache_reuses_result_and_does_not_charge_again(tmp_path):
    with Store(tmp_path) as db:
        provider = FakeProvider()
        model = ModelRunner(db, provider, quota_config(), "run1")
        first = model.request("system", {"untrusted_text": "hi"}, {})
        second = model.request("system", {"untrusted_text": "hi"}, {})
        assert not first["cache_hit"] and second["cache_hit"]
        assert provider.calls == 1
        assert model.usage()["calls"] == 1
        assert model.usage()["input_tokens"] == 100
        assert model.usage()["billing_usd"] is None


@pytest.mark.parametrize("scope", ["run", "day", "month"])
def test_budget_blocks_before_provider_and_accounts_other_runs(tmp_path, scope):
    with Store(tmp_path) as db:
        provider = FakeProvider()
        config = quota_config()
        config["limits"][scope]["calls"] = 1
        first = ModelRunner(db, provider, config, "run1")
        first.request("system", {"a": 1}, {})
        another = ModelRunner(db, provider, config, "run1" if scope == "run" else "run2")
        with pytest.raises(ModelError, match=f"budget_{scope}_calls"):
            another.request("system", {"a": 2}, {})
        assert provider.calls == 1


def test_unknown_timeout_charged_conservatively_and_not_free(tmp_path):
    with Store(tmp_path) as db:
        runner = ModelRunner(db, FakeProvider(error=True), quota_config(), "r")
        with pytest.raises(ModelError, match="model_timeout"):
            runner.request("system", {"a": 1}, {})
        usage = runner.usage()
        assert usage["unknown_calls"] == 1
        assert usage["input_tokens"] is None
        assert usage["reserved_input_tokens"] > 0
        assert usage["billing_usd"] is None


def test_cache_survives_new_process_and_unfinished_call_not_resent(tmp_path):
    with Store(tmp_path) as db:
        provider = FakeProvider()
        runner = ModelRunner(db, provider, quota_config(), "r")
        result = runner.request("system", {"a": 1}, {})
        with db.conn:
            db.conn.execute(
                "UPDATE model_calls SET status='reserved',response=NULL WHERE call_id=?", (result["call_id"],)
            )
    with Store(tmp_path) as db:
        runner = ModelRunner(db, provider, quota_config(), "next")
        with pytest.raises(ModelError, match="model_call_outcome_unknown"):
            runner.request("system", {"a": 1}, {})
        assert provider.calls == 1


def test_day_budget_uses_taipei_date(tmp_path):
    with Store(tmp_path) as db:
        config = quota_config()
        config["limits"]["day"]["calls"] = 1
        provider = FakeProvider()
        ModelRunner(db, provider, config, "r1", now="2026-01-01T15:59:00Z").request("s", {"a": 1}, {})
        ModelRunner(db, provider, config, "r2", now="2026-01-01T16:01:00Z").request("s", {"a": 2}, {})
        assert provider.calls == 2


def test_one_day_validation_budget_expires_without_calling_provider(tmp_path):
    with Store(tmp_path) as db:
        config = quota_config() | {"validation_local_date": "2026-01-01"}
        provider = FakeProvider()
        ModelRunner(db, provider, config, "valid", now="2026-01-01T15:59:00Z").request("s", {"a": 1}, {})
        with pytest.raises(ModelError, match="budget_validation_date_expired"):
            ModelRunner(db, provider, config, "expired", now="2026-01-01T16:01:00Z").request(
                "s", {"a": 2}, {}
            )
        assert provider.calls == 1
        assert db.conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 1


def test_claude_command_disables_tools_plugins_and_secret_environment(monkeypatch, tmp_path):
    from radar.llm import ClaudeCLI

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-secret")
    monkeypatch.setenv("EXAMPLE_SERVICE_TOKEN", "fixture-secret")
    provider = ClaudeCLI("claude", "sonnet", timeout_seconds=20)
    command, env = provider.command("system", 1000)
    assert command[command.index("--tools") + 1] == ""
    assert "--safe-mode" in command and "--restricted" in command
    assert "--strict-mcp-config" in command and "--disable-slash-commands" in command
    assert "--no-session-persistence" in command
    assert "--bare" not in command
    assert "ANTHROPIC_API_KEY" not in env and "EXAMPLE_SERVICE_TOKEN" not in env
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "1000"


def test_external_review_usage_is_idempotent_and_unknown_is_not_zero(tmp_path):
    from radar.llm import record_external_usage

    with Store(tmp_path) as db:
        values = {
            "external_id": "job-fixture",
            "run_id": "r",
            "category": "review",
            "provider": "external-reviewer",
            "model": "fixture",
            "started_at": "2026-10-21T19:03:03Z",
            "input_tokens": 20785,
            "output_tokens": 6012,
        }
        assert record_external_usage(db, **values)["changed"]
        assert not record_external_usage(db, **values)["changed"]
        values.update(external_id="unknown", input_tokens=None, output_tokens=None)
        with pytest.raises(ModelError, match="unknown_usage_reservation_required"):
            record_external_usage(db, **values)
        record_external_usage(db, **values, reserved_input=30000, reserved_output=8000, status="failed")
        usage = ModelRunner(db, FakeProvider(), quota_config(), "r").usage()
        assert usage["input_tokens"] is None and usage["observed_output_tokens"] == 6012
        assert usage["reserved_input_tokens"] == 30000


def test_content_databases_share_budget_and_cache_ledger(tmp_path):
    with Store(tmp_path / "one") as one, Store(tmp_path / "two") as two, Store(tmp_path / "ledger") as ledger:
        config = quota_config()
        config["limits"]["day"]["calls"] = 1
        provider = FakeProvider()
        ModelRunner(one, provider, config, "one", ledger_db=ledger).request("s", {"a": 1}, {})
        second = ModelRunner(two, provider, config, "two", ledger_db=ledger)
        assert second.request("s", {"a": 1}, {})["cache_hit"]
        with pytest.raises(ModelError, match="budget_day_calls"):
            second.request("s", {"a": 2}, {})
        assert provider.calls == 1
        assert second.usage("day")["calls"] == 1


def test_proposal_only_model_configuration_cannot_be_activated(tmp_path):
    import json

    from radar.llm import load_config

    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(quota_config() | {"proposal_only": True}), encoding="utf-8")
    with pytest.raises(ModelError, match="model_config_proposal_only"):
        load_config(path)


def external_reservation_values(**changes):
    return {
        "external_id": "review-fixture",
        "run_id": "r",
        "category": "review",
        "provider": "external-reviewer",
        "model": "fixture-reviewer",
        "config": quota_config(),
        "reserved_input": 1000,
        "reserved_output": 500,
        "now": "2026-10-22T00:00:00Z",
    } | changes


def test_external_reservation_is_once_only_across_restart(tmp_path):
    from radar.llm import finish_external_usage, reserve_external_usage

    values = external_reservation_values()
    with Store(tmp_path) as db:
        first = reserve_external_usage(db, **values)
        assert first["dispatch_allowed"] and first["changed"]
    with Store(tmp_path) as db:
        again = reserve_external_usage(db, **values)
        assert again["call_id"] == first["call_id"]
        assert not again["dispatch_allowed"] and not again["changed"]
        with pytest.raises(ModelError, match="external_usage_conflict"):
            reserve_external_usage(db, **(values | {"model": "different-reviewer"}))
        receipt = finish_external_usage(
            db,
            external_id=values["external_id"],
            input_tokens=120,
            output_tokens=30,
            receipt_ref="validation/review-fixture.json",
        )
        assert receipt["changed"] and not receipt["dispatch_allowed"]
        assert not reserve_external_usage(db, **values)["dispatch_allowed"]
        assert not finish_external_usage(
            db,
            external_id=values["external_id"],
            input_tokens=120,
            output_tokens=30,
            receipt_ref="validation/review-fixture.json",
        )["changed"]
        with pytest.raises(ModelError, match="external_usage_conflict"):
            finish_external_usage(db, external_id=values["external_id"], input_tokens=121, output_tokens=30)
        assert db.conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 1


@pytest.mark.parametrize("scope", ["run", "day", "month"])
@pytest.mark.parametrize("metric", ["calls", "input_tokens", "output_tokens"])
def test_external_reservation_uses_shared_model_budget(tmp_path, scope, metric):
    from radar.llm import reserve_external_usage

    with Store(tmp_path / "content") as content, Store(tmp_path / "ledger") as ledger:
        config = quota_config()
        config["limits"][scope][metric] = {"calls": 1, "input_tokens": 1000, "output_tokens": 500}[metric]
        reserve_external_usage(ledger, **external_reservation_values(config=config))
        provider = FakeProvider()
        runner = ModelRunner(
            content,
            provider,
            config,
            "r" if scope == "run" else "another",
            now="2026-10-22T00:01:00Z",
            ledger_db=ledger,
        )
        with pytest.raises(ModelError, match=f"budget_{scope}_{metric}"):
            runner.request("s", {"a": 1}, {})
        assert provider.calls == 0
        with pytest.raises(ModelError, match=f"budget_{scope}_{metric}"):
            reserve_external_usage(
                ledger,
                **external_reservation_values(
                    config=config, external_id="second", run_id="r" if scope == "run" else "another"
                ),
            )


def test_external_unknown_retains_reservation_until_explicit_observed_reconciliation(tmp_path):
    from radar.llm import finish_external_usage, reserve_external_usage

    with Store(tmp_path) as db:
        reserve_external_usage(db, **external_reservation_values())
        finish_external_usage(
            db, external_id="review-fixture", status="unknown", receipt_ref="validation/review-timeout.json"
        )
        runner = ModelRunner(db, FakeProvider(), quota_config(), "r", now="2026-10-22T00:01:00Z")
        usage = runner.usage()
        assert usage["unknown_calls"] == 1 and usage["input_tokens"] is None
        assert usage["reserved_input_tokens"] == 1000 and usage["reserved_output_tokens"] == 500
        assert not reserve_external_usage(db, **external_reservation_values())["dispatch_allowed"]
        with pytest.raises(ModelError, match="external_usage_conflict"):
            finish_external_usage(
                db,
                external_id="review-fixture",
                input_tokens=100,
                output_tokens=20,
                receipt_ref="validation/review-recovered.json",
            )
        with pytest.raises(ModelError, match="external_reconciliation_receipt_required"):
            finish_external_usage(
                db, external_id="review-fixture", input_tokens=100, output_tokens=20, reconcile_unknown=True
            )
        finish_external_usage(
            db,
            external_id="review-fixture",
            input_tokens=100,
            output_tokens=20,
            receipt_ref="validation/review-recovered.json",
            reconcile_unknown=True,
        )
        assert runner.usage()["input_tokens"] == 100
        assert runner.usage()["unknown_calls"] == 0


def test_external_actual_overrun_is_honest_and_blocks_next_reservation(tmp_path):
    from radar.llm import finish_external_usage, reserve_external_usage

    with Store(tmp_path) as db:
        config = quota_config()
        config["limits"]["day"]["output_tokens"] = 600
        reserve_external_usage(db, **external_reservation_values(config=config))
        finish_external_usage(
            db,
            external_id="review-fixture",
            input_tokens=100,
            output_tokens=900,
            receipt_ref="validation/review-overrun.json",
        )
        assert ModelRunner(db, FakeProvider(), config, "r").usage()["output_tokens"] == 900
        with pytest.raises(ModelError, match="budget_day_output_tokens"):
            reserve_external_usage(db, **external_reservation_values(config=config, external_id="second"))


def test_external_validation_date_proposal_and_missing_reservation_fail_closed(tmp_path):
    from radar.llm import finish_external_usage, reserve_external_usage

    with Store(tmp_path) as db:
        with pytest.raises(ModelError, match="budget_validation_date_expired"):
            reserve_external_usage(
                db,
                **external_reservation_values(
                    config=quota_config() | {"validation_local_date": "2026-10-21"}
                ),
            )
        with pytest.raises(ModelError, match="model_config_proposal_only"):
            reserve_external_usage(
                db, **external_reservation_values(config=quota_config() | {"proposal_only": True})
            )
        with pytest.raises(ModelError, match="external_reservation_missing"):
            finish_external_usage(db, external_id="missing", input_tokens=100, output_tokens=20)
        with pytest.raises(ModelError, match="unknown_usage_reservation_required"):
            reserve_external_usage(db, **external_reservation_values(reserved_output=0))
        assert db.conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0


def test_external_reservation_serializes_two_connections_without_overspending(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from radar.llm import initialize, reserve_external_usage

    with Store(tmp_path) as db:
        initialize(db)
    barrier = Barrier(2)
    config = quota_config()
    config["limits"]["day"]["calls"] = 1

    def reserve(index):
        with Store(tmp_path) as db:
            barrier.wait(timeout=10)
            try:
                return reserve_external_usage(
                    db,
                    **external_reservation_values(
                        config=config, external_id=f"concurrent-{index}", run_id=f"run-{index}"
                    ),
                )
            except ModelError as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(reserve, (1, 2)))
    assert sum(isinstance(value, dict) and value["dispatch_allowed"] for value in outcomes) == 1
    assert "budget_day_calls" in outcomes
