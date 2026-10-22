"""Frozen evaluation packages and strict metrics; no model calls or auto-labels."""

import hashlib
import json
from pathlib import Path

from .models import json_text
from .recovery import atomic_write_json


def freeze(path, cases, purpose):
    path = Path(path)
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_evaluation_id")
    packet = {"schema_version": 1, "purpose": purpose, "review_status": "unlabeled", "cases": cases}
    packet["sha256"] = hashlib.sha256(json_text(packet).encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, packet, overwrite=False)
    return packet


def binary_metrics(predictions, labels):
    """Require a complete independently labeled set. Missing labels never become negatives."""
    if not predictions or set(predictions) != set(labels):
        raise ValueError("evaluation_set_mismatch")
    if any(type(v) is not bool for v in (*predictions.values(), *labels.values())):
        raise ValueError("boolean_labels_required")
    tp = sum(predictions[k] and labels[k] for k in labels)
    fp = sum(predictions[k] and not labels[k] for k in labels)
    fn = sum(not predictions[k] and labels[k] for k in labels)
    tn = len(labels) - tp - fp - fn
    return {
        "count": len(labels),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "positive_labels": tp + fn,
        "negative_labels": tn + fp,
    }


def alert_cases():
    """Synthetic data only. Reviewer must label independently; facts are stated scenario conditions."""
    base = {
        "relevant": True,
        "new_evidence": True,
        "verified": True,
        "user_affected": True,
        "review_passed": True,
        "independent_origins": 1,
        "waiting_cost": "等待日報可能讓目前工作繼續受損",
        "version_matches": True,
        "reproducible": True,
    }
    scenarios = [
        ("正在用的資料庫版本可重現寫入遺失", "critical_regression", {}),
        ("正在用的同步程式確認會刪除遠端檔案", "critical_regression", {}),
        ("部署中的工具明確洩漏權杖並有重現步驟", "security_impact", {}),
        (
            "維護者確認現用版本有遠端命令執行風險",
            "security_impact",
            {"reproducible": False, "maintainer_confirmed": True},
        ),
        ("剛建議的升級會破壞使用者正在用的格式，須撤回", "correction", {"previous_action_invalidated": True}),
        ("剛建議的參數會覆寫工作檔案，須立即更正", "correction", {"previous_action_invalidated": True}),
        (
            "目前阻塞的轉換流程有可重現修正，等待會錯過交付",
            "blocking_work_improvement",
            {"work_currently_blocked": True},
        ),
        ("正在受阻的推論服務有同硬體驗證修復", "blocking_work_improvement", {"work_currently_blocked": True}),
        ("相同已用版本的記憶體錯誤可重現並持續破壞結果", "critical_regression", {"independent_origins": 2}),
        ("現用套件惡意更新的雜湊與版本已確認", "security_impact", {"independent_origins": 2}),
        (
            "先前報告誤認支援的GPU，使用者正依建議採購，須更正",
            "correction",
            {"previous_action_invalidated": True},
        ),
        (
            "在用CLI更新後會送出私人檔案，維護者確認影響範圍",
            "security_impact",
            {"maintainer_confirmed": True},
        ),
        ("watchlist 發布一般修正版本", "release", {}),
        ("熱門專案只有新tag，尚無使用者測試", "release", {"verified": False}),
        ("HN 討論暴增但沒有新實證", "popularity", {"new_evidence": False}),
        ("社群傳聞晶片公司遭收購", "financial_rumor", {"verified": False}),
        ("多個帳號複製同一多頭傳聞", "financial_rumor", {"independent_origins": 0}),
        ("有人回報回歸但不是使用者目前版本", "critical_regression", {"version_matches": False}),
        ("故障沒有可重現步驟也無維護者確認", "critical_regression", {"reproducible": False}),
        ("安全新聞與使用者環境無關", "security_impact", {"user_affected": False}),
        ("嚴重故障但來源引文尚無法核對", "critical_regression", {"verified": False}),
        ("同一證據已通知，沒有新變化", "critical_regression", {"new_evidence": False}),
        ("內容判斷仍待獨立外審", "security_impact", {"review_passed": False}),
        ("只說很重要卻沒說等日報有什麼代價", "critical_regression", {"waiting_cost": ""}),
        (
            "新模型測試很強但沒有正在受阻的工作",
            "blocking_work_improvement",
            {"work_currently_blocked": False},
        ),
        (
            "新模型宣稱能解卡，但沒有可重現證據",
            "blocking_work_improvement",
            {"work_currently_blocked": True, "reproducible": False},
        ),
        ("報告有小措辭修正，既有建議不受影響", "correction", {"previous_action_invalidated": False}),
        ("來源403應走系統健康，不是社群內容警報", "source_failure", {}),
        ("已過去的資料洩漏，使用者早已修補", "security_impact", {"user_affected": False}),
        ("AI 產品業配宣稱突破，缺乏原始證據", "popularity", {"verified": False, "independent_origins": 0}),
    ]
    return [
        {
            "id": f"alert-{i:02}",
            "synthetic": True,
            "scenario": description,
            "facts": base | {"kind": kind} | delta,
        }
        for i, (description, kind, delta) in enumerate(scenarios, 1)
    ]


def read_labels(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not value.get("reviewer_receipt") or not value.get("independent_session"):
        raise ValueError("independent_label_provenance_required")
    return value["labels"]
