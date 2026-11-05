from types import SimpleNamespace

from pdt import run_output


def test_copy_run_copies_the_files_of_the_run_and_not_its_done_marker(monkeypatch, tmp_path):
    folder = "runs/20261105T182955Z-run1"
    listings = {
        ("storage", "report", "ls", "runs", "--json"): [{"name": folder + "/", "type": "directory"}],
        ("storage", "report", "ls", folder, "--recursive", "--json"): [
            {"name": f"{folder}/_done", "type": "file"},
            {"name": f"{folder}/accounts.csv", "type": "file"},
            {"name": f"{folder}/output", "type": "directory"},
            {"name": f"{folder}/output/_done", "type": "file"},
            {"name": f"{folder}/output/summary.txt", "type": "file"}],
    }
    monkeypatch.setattr(run_output, "pdt_json", lambda *args: listings[args])
    got = []
    monkeypatch.setattr(run_output.subprocess, "run",
                        lambda argv, **kwargs: got.append(argv[-2]) or SimpleNamespace(returncode=0))
    assert run_output.copy_run("report", "run1", tmp_path) == 2
    assert got == [f"{folder}/accounts.csv", f"{folder}/output/summary.txt"]
