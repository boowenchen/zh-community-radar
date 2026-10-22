import copy

import pytest

from radar.editor import analyze, ask, build_digest, validate_assessment
from radar.events import list_events, rebuild
from radar.llm import ModelRunner, quota_config
from radar.models import Item, Scan
from radar.store import Store


def seed(
    db,
    text="I tested v2.1 with 8 GB RAM. It works. The test used the same local inference workload and the same machine as the previous release. This is one person's experience, not a broad benchmark. Reproduction steps and limitations are described here.",
    published="2026-10-21T12:00:00Z",
    fetched="2026-10-21T13:00:00Z",
):
    item = Item(
        "test",
        "1",
        "https://example.com/test",
        "AI inference v2.1",
        text,
        published_at=published,
        fetched_at=fetched,
    )
    db.save_scan(Scan("test", items=[item], complete=True, observed_at=fetched), "seed")
    rebuild(db)
    return item, list_events(db, cutoff="2026-10-22T00:00:00Z")[0]


def assessment(item):
    return {
        "decision": "include",
        "relevance": "practical_ai",
        "reading_action": "inspect_test",
        "claims": [
            {
                "text": "作者實測 v2.1，使用 8 GB RAM。",
                "kind": "first_hand",
                "evidence": [
                    {
                        "item_id": item.item_id,
                        "revision": 1,
                        "content_hash": item.content_hash,
                        "field": "text",
                        "quote": "I tested v2.1 with 8 GB RAM.",
                    }
                ],
            }
        ],
        "reactions": [],
        "limitations": ["sample_only"],
    }


class Provider:
    name = "fixture"
    model = "fixture-editor"

    def __init__(self, item):
        self.item = item
        self.calls = 0

    def generate(self, system, payload, schema, max_output_tokens):
        self.calls += 1
        return {
            "content": model_assessment(self.item, payload),
            "usage": {"input_tokens": 50, "output_tokens": 60, "basis": "observed"},
        }


def model_assessment(item, payload):
    value = assessment(item)
    handle = next(
        unit["evidence_id"]
        for row in payload["evidence"]
        if row["title"] == item.title
        for unit in row["units"]
        if "I tested v2.1 with 8 GB RAM." in unit["quote"]
    )
    for claim in value["claims"]:
        for ref in claim["evidence"]:
            for key in ("item_id", "revision", "content_hash", "field", "quote"):
                del ref[key]
            ref["evidence_id"] = handle
    return value


def test_short_references_bind_only_supplied_immutable_evidence(tmp_path):
    from radar.editor import _payload, _resolve_model_references
    from radar.events import event_evidence

    with Store(tmp_path) as db:
        item, event = seed(db)
        payload = _payload(event, event_evidence(db, event), "recent_publication")
        wire = model_assessment(item, payload)
        resolved = _resolve_model_references(wire, payload)
        checked = validate_assessment(db, event, resolved, "2026-10-22T00:00:00Z")
        ref = checked["claims"][0]["evidence"][0]
        assert (ref["item_id"], ref["revision"], ref["content_hash"]) == (item.item_id, 1, item.content_hash)
        assert "evidence_id" in wire["claims"][0]["evidence"][0]  # input stays immutable
        bad = copy.deepcopy(wire)
        bad["claims"][0]["evidence"][0]["evidence_id"] = "e999"
        with pytest.raises(ValueError, match="unknown_evidence_id"):
            _resolve_model_references(bad, payload)
        bad = copy.deepcopy(wire)
        bad["claims"][0]["evidence"][0]["item_id"] = "injected-id"
        with pytest.raises(ValueError, match="assessment_schema"):
            _resolve_model_references(bad, payload)
        changed = copy.deepcopy(payload)
        changed["evidence"][0]["units"] = []
        with pytest.raises(ValueError, match="unknown_evidence_id"):
            _resolve_model_references(wire, changed)
        changed = copy.deepcopy(payload)
        changed["evidence"][0]["content_hash"] = "wrong"
        with pytest.raises(ValueError, match="evidence_version_mismatch"):
            validate_assessment(db, event, _resolve_model_references(wire, changed), "2026-10-22T00:00:00Z")


def test_bounded_sentence_sampling_reaches_full_source_caveats(tmp_path):
    from radar.editor import _excerpt_units, _payload
    from radar.events import event_evidence

    text = "I tested this AI tool in my daily workflow. " + "Ordinary feature description. " * 200
    text += "The cost is significant. Each run re-reads the conversation. The tool failed under load."
    with Store(tmp_path) as db:
        item, event = seed(db, text=text)
        evidence = event_evidence(db, event, max_chars=5500)
        assert "significant" not in evidence[0]["text"]  # old leading excerpt misses the caveat
        payload = _payload(event, evidence, "recent_publication", db=db)
        units = payload["evidence"][0]["units"]
        assert any("significant" in unit["quote"] for unit in units)
        assert all(unit["quote"] in (item.text if unit["field"] == "text" else item.title) for unit in units)
        assert sum(len(unit["quote"]) for unit in units if unit["field"] == "text") <= 2600
        assert max(len(unit["quote"]) for unit in units) <= 500
    # A long unpunctuated comment still contributes a bounded exact excerpt.
    comment = "a" * 600
    snippets = _excerpt_units(comment, 180)
    assert snippets and sum(len(value) for _, value in snippets) <= 180


def test_payload_budget_counts_utf8_schema_and_only_retains_resolvable_units(tmp_path):
    from radar.editor import MODEL_ASSESSMENT_SCHEMA, SYSTEM_PROMPT, _fit_model_payload, _payload
    from radar.events import event_evidence
    from radar.models import json_text

    with Store(tmp_path) as db:
        item, event = seed(db, text="人工智慧實測的詳細觀察。" * 400 + "成本很高而且有失敗風險。")
        original = _payload(event, event_evidence(db, event), "recent_publication", db=db)
        local, wire = _fit_model_payload(original, 10000)
        encoded = SYSTEM_PROMPT + json_text(wire) + json_text(MODEL_ASSESSMENT_SCHEMA)
        assert len(encoded.encode()) <= 10000
        assert [r["units"] for r in local["evidence"]] == [r["units"] for r in wire["evidence"]]
        assert all(
            u["quote"] in (item.title if u["field"] == "title" else item.text)
            for r in local["evidence"]
            for u in r["units"]
        )
        assert "item_id" not in wire["evidence"][0] and "item_id" in local["evidence"][0]


@pytest.mark.parametrize(
    "metadata,note",
    [
        ({"comments_total": 2}, "來源標示 2 則留言，本次未取得留言正文"),
        ({"comments_available": False}, "此資料來源未提供留言；本卡只描述作者文章"),
    ],
)
def test_digest_discloses_uncaptured_comments_and_actual_sampling(tmp_path, metadata, note):
    with Store(tmp_path) as db:
        item = Item(
            "test",
            "1",
            "https://example.com/test",
            "AI inference v2.1",
            seed.__defaults__[0],
            published_at="2026-10-21T12:00:00Z",
            fetched_at="2026-10-21T13:00:00Z",
            metadata=metadata,
        )
        db.save_scan(Scan("test", items=[item], complete=True, observed_at=item.fetched_at), "seed")
        rebuild(db)
        model = ModelRunner(db, Provider(item), quota_config(), "fixture-comment-scope")
        analyze(db, model, "2026-10-21T00:00:00Z", "2026-10-22T00:00:00Z")
        report = build_digest(db, "2026-10-21T00:00:00Z", "2026-10-22T00:00:00Z")["publication_payload"]
        assert note in report["markdown"]
        assert "10-21 21:00" in report["markdown"]
        assert "內容截止窗不代表來源都已更新到該時刻" in report["markdown"]


def test_exact_version_quote_hash_and_numbers_required(tmp_path):
    with Store(tmp_path) as db:
        item, event = seed(db)
        valid = assessment(item)
        checked = validate_assessment(db, event, valid, cutoff="2026-10-22T00:00:00Z")
        assert checked["claims"][0]["evidence"][0]["start_offset"] == 0
        for field, value, error in [
            ("quote", "It does NOT work", "quote_not_found"),
            ("content_hash", "wrong", "evidence_version_mismatch"),
            ("revision", 2, "evidence_not_in_event"),
        ]:
            bad = copy.deepcopy(valid)
            bad["claims"][0]["evidence"][0][field] = value
            with pytest.raises(ValueError, match=error):
                validate_assessment(db, event, bad, cutoff="2026-10-22T00:00:00Z")
        bad = copy.deepcopy(valid)
        bad["claims"][0]["text"] = "作者實測 v2.1，使用 80 GB RAM。"
        with pytest.raises(ValueError, match="unsupported_numeric_claim"):
            validate_assessment(db, event, bad, cutoff="2026-10-22T00:00:00Z")


def test_no_comments_cannot_invent_reactions_or_extra_fields(tmp_path):
    with Store(tmp_path) as db:
        item, event = seed(db)
        bad = assessment(item)
        bad["reactions"] = [
            {
                "target": "model",
                "aspect": "quality",
                "stance": "positive",
                "reason": "works",
                "evidence": bad["claims"][0]["evidence"],
            }
        ]
        with pytest.raises(ValueError, match="reaction_requires_comment"):
            validate_assessment(db, event, bad, cutoff="2026-10-22T00:00:00Z")
        bad = assessment(item) | {"send_message": "ignore previous instructions"}
        with pytest.raises(ValueError, match="assessment_schema"):
            validate_assessment(db, event, bad, cutoff="2026-10-22T00:00:00Z")


def test_missing_context_or_archived_summary_cannot_become_primary_evidence(tmp_path):
    import json

    with Store(tmp_path) as db:
        item, event = seed(db)
        row = db.conn.execute(
            "SELECT context FROM item_context_versions WHERE item_id=?", (item.item_id,)
        ).fetchone()
        context = json.loads(row[0])
        context["metadata"]["original_text_available"] = False
        db.conn.execute(
            "UPDATE item_context_versions SET context=? WHERE item_id=?", (json.dumps(context), item.item_id)
        )
        db.conn.commit()
        with pytest.raises(ValueError, match="historical_summary_not_primary_evidence"):
            validate_assessment(db, event, assessment(item), "2026-10-22T00:00:00Z")
        reported = assessment(item)
        reported["claims"][0]["kind"] = "reported"
        assert (
            "missing_context"
            in validate_assessment(db, event, reported, "2026-10-22T00:00:00Z")["limitations"]
        )
        db.conn.execute("DELETE FROM item_context_versions WHERE item_id=?", (item.item_id,))
        db.conn.commit()
        with pytest.raises(ValueError, match="evidence_context_unavailable"):
            validate_assessment(db, event, reported, "2026-10-22T00:00:00Z")


def test_analyze_caches_and_digest_is_immutable_idempotent(tmp_path):
    with Store(tmp_path) as db:
        item, _event = seed(db)
        provider = Provider(item)
        runner = ModelRunner(db, provider, quota_config(), "run")
        assert analyze(db, runner, cutoff="2026-10-22T00:00:00Z")["analyzed"] == 1
        assert analyze(db, runner, cutoff="2026-10-22T00:00:00Z")["model_calls"] == 0
        one = build_digest(db, since="2026-10-21T00:00:00Z", cutoff="2026-10-22T00:00:00Z")
        two = build_digest(db, since="2026-10-21T00:00:00Z", cutoff="2026-10-22T00:00:00Z")
        assert one["report_id"] == two["report_id"]
        assert len(one["cards"]) == 1 and not two["changed"]
        assert "https://example.com/test" in one["markdown"]
        assert provider.calls == 1


def test_unknown_published_and_old_bootstrap_not_called_new(tmp_path):
    with Store(tmp_path) as db:
        item, _event = seed(db, published="2025-01-01T00:00:00Z")
        runner = ModelRunner(db, Provider(item), quota_config(), "r")
        result = analyze(db, runner, since="2026-10-21T00:00:00Z", cutoff="2026-10-22T00:00:00Z")
        assert result["analyzed"] == 0 and result["model_calls"] == 0
        report = build_digest(db, since="2026-10-21T00:00:00Z", cutoff="2026-10-22T00:00:00Z")
        assert report["cards"] == [] and not report["notify"]
        answer = ask(db, "最近有哪些 AI 工具", since="2026-10-21T00:00:00Z", cutoff="2026-10-22T00:00:00Z")
        assert answer["status"] == "stale_data"


def test_historical_query_no_future_text_and_missing_data_is_explicit(tmp_path):
    with Store(tmp_path) as db:
        _item, _event = seed(db)
        result = ask(db, "最近有哪些 AI 工具", since="2026-10-20T00:00:00Z", cutoff="2026-10-21T12:30:00Z")
        assert result["status"] == "no_data"
        assert not result["cards"] and result["model_calls"] == 0
        result = ask(db, "不存在的 FoobarXYZ", since="2026-10-21T00:00:00Z", cutoff="2026-10-22T00:00:00Z")
        assert result["status"] == "no_matching_data"


def test_following_preview_keeps_unsent_evidence_available(tmp_path):
    with Store(tmp_path) as db:
        item, _event = seed(db)
        runner = ModelRunner(db, Provider(item), quota_config(), "r")
        analyze(db, runner, cutoff="2026-10-22T00:00:00Z")
        one = build_digest(db, since="2026-10-21T00:00:00Z", cutoff="2026-10-22T00:00:00Z")
        two = build_digest(db, since="2026-10-21T00:00:00Z", cutoff="2026-10-22T01:00:00Z")
        assert len(one["cards"]) == 1
        assert len(two["cards"]) == 1


def test_number_next_to_chinese_cannot_evade_literal_check(tmp_path):
    with Store(tmp_path) as db:
        item, event = seed(db)
        bad = assessment(item)
        bad["claims"][0]["text"] = "作者使用80GB RAM。"
        with pytest.raises(ValueError, match="unsupported_numeric_claim"):
            validate_assessment(db, event, bad, cutoff="2026-10-22T00:00:00Z")


def test_invalid_assessment_gets_only_one_accounted_repair(tmp_path):
    class RepairProvider(Provider):
        def generate(self, system, payload, schema, max_output_tokens):
            result = super().generate(system, payload, schema, max_output_tokens)
            if "repair" not in payload:
                result["content"]["claims"][0]["text"] = "作者使用80GB RAM。"
            return result

    with Store(tmp_path) as db:
        item, _event = seed(db)
        provider = RepairProvider(item)
        runner = ModelRunner(db, provider, quota_config(), "r")
        result = analyze(db, runner, cutoff="2026-10-22T00:00:00Z")
        assert result["analyzed"] == 1 and result["model_calls"] == 2
        assert provider.calls == 2
        assert db.conn.execute("SELECT count(*) FROM model_calls WHERE category='repair'").fetchone()[0] == 1
        assert analyze(db, runner, cutoff="2026-10-22T00:00:00Z")["model_calls"] == 0


def test_explicit_version_question_can_read_first_party_release_without_daily_card(tmp_path):
    with Store(tmp_path) as db:
        release = Item(
            "github",
            "v2",
            "https://github.com/a/exampleai/releases/tag/v2",
            "ExampleAI v2 release",
            "v2 changes: improved local inference memory use. Migration: change the config format. " * 4,
            kind="release",
            published_at="2026-10-21T12:00:00Z",
            fetched_at="2026-10-21T13:00:00Z",
        )
        db.save_scan(
            Scan("github", items=[release], complete=True, observed_at=release.fetched_at), "release"
        )
        rebuild(db)
        answer = ask(
            db, "ExampleAI 版本改了什麼？", since="2026-10-21T00:00:00Z", cutoff="2026-10-22T00:00:00Z"
        )
        assert answer["status"] == "available" and answer["intent"] == "version_changes"
        assert "v2 changes" in answer["cards"][0]["quote"]
        assert answer["cards"][0]["evidence_type"] == "first_party_unverified"
        assert build_digest(db, since="2026-10-21T00:00:00Z", cutoff="2026-10-22T00:00:00Z")["cards"] == []


def test_salvage_requires_reviewed_whole_claims_and_drops_reactions_and_headline(tmp_path):
    from radar.editor import salvage_assessment

    with Store(tmp_path) as db:
        item, event = seed(db)
        original = assessment(item)
        bad = copy.deepcopy(original["claims"][0])
        bad["text"] = "作者使用80GB RAM。"
        original["claims"].insert(0, bad)
        saved = salvage_assessment(
            db,
            event,
            original,
            cutoff="2026-10-22T00:00:00Z",
            keep_claim_indexes=[1],
            source_call_id="fixture-original",
            model="fixture",
            checked_by="fixture-independent-reader",
            review_reason="Checked the entire remaining claim against its own source quote.",
        )
        assert saved["degraded"] and len(saved["result"]["claims"]) == 1
        assert saved["result"]["reactions"] == []
        assert len(original["claims"]) == 2
        report = build_digest(db, since="2026-10-21T00:00:00Z", cutoff="2026-10-22T00:00:00Z")
        assert report["cards"][0]["degraded"]
        assert "80GB" not in report["markdown"]
        assert report["cards"][0]["title"] != event["title"]
        fragment = assessment(item)
        fragment["claims"][0]["text"] = "例如"
        with pytest.raises(ValueError, match="salvage_fragment"):
            salvage_assessment(
                db,
                event,
                fragment,
                cutoff="2026-10-22T00:00:00Z",
                keep_claim_indexes=[0],
                source_call_id="fixture2",
                model="fixture",
                checked_by="reviewer",
                review_reason="check",
            )


def test_analyze_does_not_let_cached_top_candidate_starve_next_event(tmp_path):
    class EchoProvider:
        name, model = "fixture", "echo"

        def generate(self, system, payload, schema, max_output_tokens):
            ref = payload["evidence"][0]
            value = {
                "decision": "include",
                "relevance": "practical_ai",
                "reading_action": "read_discussion",
                "claims": [
                    {
                        "text": "作者描述了自己的本機推論使用經驗。",
                        "kind": "first_hand",
                        "evidence": [{"evidence_id": ref["units"][0]["evidence_id"]}],
                    }
                ],
                "reactions": [],
                "limitations": ["sample_only"],
            }
            return {"content": value, "usage": {"basis": "observed", "input_tokens": 50, "output_tokens": 40}}

    with Store(tmp_path) as db:
        for n in range(2):
            item = Item(
                "test",
                str(n),
                f"https://example.com/{n}",
                "AI inference",
                "I tested local inference. " * 10,
                published_at="2026-10-21T12:00:00Z",
                fetched_at="2026-10-21T13:00:00Z",
            )
            db.save_scan(Scan("test", items=[item], complete=True), f"seed{n}")
        rebuild(db)
        runner = ModelRunner(db, EchoProvider(), quota_config(), "r")
        first = analyze(db, runner, cutoff="2026-10-22T00:00:00Z", limit=1)
        second = analyze(db, runner, cutoff="2026-10-22T00:00:00Z", limit=1)
        assert first["analyzed"] == second["analyzed"] == 1
        assert first["assessment_ids"] != second["assessment_ids"]


def test_requested_event_beyond_top_forty_is_not_silently_ignored(tmp_path):
    with Store(tmp_path) as db:
        for n in range(45):
            item = Item(
                "test",
                str(n),
                f"https://example.com/{n}",
                f"AI inference {n}",
                "I tested local inference. " * 10,
                published_at="2026-10-21T12:00:00Z",
                fetched_at="2026-10-21T13:00:00Z",
            )
            db.save_scan(Scan("test", items=[item], complete=True), f"seed{n}")
        rebuild(db)
        target = list_events(db, cutoff="2026-10-22T00:00:00Z", limit=100)[-1]

        class OmitProvider:
            name, model = "fixture", "omit"

            def generate(self, system, payload, schema, max_output_tokens):
                return {
                    "content": {
                        "decision": "omit",
                        "relevance": "not_relevant",
                        "reading_action": "wait_for_evidence",
                        "claims": [],
                        "reactions": [],
                        "limitations": [],
                    },
                    "usage": {"basis": "observed", "input_tokens": 50, "output_tokens": 20},
                }

        result = analyze(
            db,
            ModelRunner(db, OmitProvider(), quota_config(), "r"),
            cutoff="2026-10-22T00:00:00Z",
            event_ids=[target["event_id"]],
        )
        assert result["analyzed"] == 1
