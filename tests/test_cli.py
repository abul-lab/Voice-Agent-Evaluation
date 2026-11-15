"""Command-line interface and report writing: exercised in-process on temporary files."""

from __future__ import annotations

import json
from pathlib import Path

from voiceshop import cli
from voiceshop.evaluation.judge import Completion


def test_scenarios_and_check_config_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VOICESHOP_KEYS_FILE", str(tmp_path / "keys.json"))
    assert cli.main(["scenarios"]) == 0
    assert "store_recommend_tent_two_people" in capsys.readouterr().out

    assert cli.main(["check-config"]) == 0
    assert "configuration OK" in capsys.readouterr().out

    monkeypatch.setenv("VOICESHOP_LOG_LEVEL", "INVALID")
    assert cli.main(["check-config"]) == 2
    assert "PROBLEM:" in capsys.readouterr().out


def test_keys_create_list_and_revoke_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VOICESHOP_KEYS_FILE", str(tmp_path / "keys.json"))
    assert cli.main(["keys", "create", "--name", "qe-lead", "--role", "admin"]) == 0
    out = capsys.readouterr().out
    assert "created qe-lead (admin)" in out

    # Duplicate key name is refused until revoked.
    assert cli.main(["keys", "create", "--name", "qe-lead", "--role", "viewer"]) == 2
    capsys.readouterr()

    assert cli.main(["keys", "list"]) == 0
    assert "qe-lead" in capsys.readouterr().out

    assert cli.main(["keys", "revoke", "--name", "qe-lead"]) == 0
    assert cli.main(["keys", "revoke", "--name", "qe-lead"]) == 2
    capsys.readouterr()


def test_eval_baseline_regress_and_prune_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VOICESHOP_KEYS_FILE", str(tmp_path / "keys.json"))
    db = tmp_path / "cli.db"
    out_dir = tmp_path / "smoke-out"
    base_file = tmp_path / "baseline.json"

    assert cli.main(["--db", str(db), "eval", "--tags", "smoke", "--out", str(out_dir)]) == 0
    assert (out_dir / "report.json").is_file()
    assert (out_dir / "report.md").is_file()
    md = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "# Evaluation report:" in md and "## Release gates" in md

    assert cli.main(["baseline", "--report", str(out_dir / "report.json"), "--out", str(base_file)]) == 0
    assert cli.main(["regress", "--baseline", str(base_file), "--report", str(out_dir / "report.json")]) == 0
    assert cli.main(["regress", "--baseline", str(base_file), "--report", str(tmp_path / "missing.json")]) == 2

    assert cli.main(["--db", str(db), "prune", "--older-than-days", "30", "--dry-run"]) == 0
    assert "would remove" in capsys.readouterr().out


def test_judge_calibrate_and_drift_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICESHOP_KEYS_FILE", str(tmp_path / "keys.json"))
    good = json.dumps({"accuracy": 5, "helpfulness": 5, "tone": 5, "invented_facts": False, "reason": "ok"})

    class _StubBackend:
        name = "ollama"
        model = "stub-1"

        def complete(self, prompt: str) -> Completion:
            return Completion(text=good, prompt_tokens=8, output_tokens=4, latency_ms=1.0)

    monkeypatch.setattr("voiceshop.evaluation.judge.make_backend", lambda name, model: _StubBackend())

    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "runs": [{
            "scenario_id": "store_recommend_tent_two_people",
            "conversation": [
                {"seq": 1, "speaker": "customer", "text_spoken": "Hi", "text_heard": "Hi"},
                {"seq": 2, "speaker": "agent", "text_spoken": "Welcome", "text_heard": "Welcome", "agent": "router"},
            ],
        }],
    }), encoding="utf-8")
    judged_path = tmp_path / "judged.json"
    assert cli.main(["judge", "--report", str(report_path), "--out", str(judged_path)]) == 0
    assert json.loads(judged_path.read_text(encoding="utf-8"))["summary"]["turns_judged"] == 1

    items_path = tmp_path / "items.json"
    items_path.write_text(json.dumps({
        "items": [{"heard": "Hi", "reply": "Welcome",
                   "human": {"accuracy": 5, "helpfulness": 5, "tone": 5, "invented_facts": False}}],
    }), encoding="utf-8")
    cal_path = tmp_path / "cal.json"
    assert cli.main(["calibrate", "--items", str(items_path), "--out", str(cal_path)]) == 0
    assert cli.main(["judge-drift", "--reference", str(cal_path), "--current", str(cal_path)]) == 0

    drifted = tmp_path / "drifted.json"
    drifted.write_text(json.dumps({
        "judge": {"backend": "ollama", "model": "other-model"},
        "items": 1,
        "agreement": 0.0,
        "failures": 0,
    }), encoding="utf-8")
    assert cli.main(["judge-drift", "--reference", str(cal_path), "--current", str(drifted)]) == 1
    assert (Path(cal_path)).is_file()
