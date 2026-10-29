import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_evals_offline_from_cassette():
    """CI gate: evals replay the committed cassette, need no key, cost nothing."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(ROOT),
        "OPENROUTER_API_KEY": "",
        "LEAK_CONFIG": str(ROOT / "config.example.toml"),  # not the developer's config.toml
    }
    out = subprocess.run(
        [sys.executable, "evals/run_evals.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr + out.stdout
    assert "cost this run: $0.0000" in out.stdout
    import json

    res = json.loads((ROOT / "evals" / "results.json").read_text())
    assert res["extraction"]["overall"]["f1"] >= 0.9
    assert res["ledger"]["missed_merges"] == 0
    assert res["detection"]["recall"] == 1.0 and res["detection"]["precision"] >= 0.9
