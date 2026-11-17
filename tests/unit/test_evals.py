from evals.run_evals import main


def test_offline_eval_harness_passes_thresholds(capsys):
    assert main(["--offline"]) == 0
    assert "all thresholds met" in capsys.readouterr().out


def test_eval_fails_when_threshold_unreachable(capsys):
    assert main(["--offline", "--min-precision", "1.1"]) == 1
