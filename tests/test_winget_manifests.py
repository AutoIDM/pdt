import importlib.util
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "render_winget_manifests.py"
spec = importlib.util.spec_from_file_location("render_winget_manifests", SCRIPT)
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)

SHA = "ab" * 32


def test_renders_three_manifests_with_no_placeholder_left(tmp_path):
    written = renderer.render("9.8.7", SHA, tmp_path, "2026-01-02")
    assert sorted(path.name for path in written) == [
        "AutoIDM.pdt.installer.yaml",
        "AutoIDM.pdt.locale.en-US.yaml",
        "AutoIDM.pdt.yaml",
    ]
    manifests = {}
    for path in written:
        text = path.read_text()
        assert "{" not in text
        manifests[path.name] = yaml.safe_load(text)
    for manifest in manifests.values():
        assert manifest["PackageIdentifier"] == "AutoIDM.pdt"
        assert manifest["PackageVersion"] == "9.8.7"
        assert manifest["ManifestVersion"] == "1.10.0"

    installer = manifests["AutoIDM.pdt.installer.yaml"]
    assert installer["InstallerType"] == "zip"
    assert installer["NestedInstallerType"] == "portable"
    assert installer["NestedInstallerFiles"] == [
        {"RelativeFilePath": "pdt.exe", "PortableCommandAlias": "pdt"}]
    assert installer["Dependencies"]["PackageDependencies"] == [
        {"PackageIdentifier": "astral-sh.uv"}]
    assert installer["Installers"] == [{
        "Architecture": "x64",
        "InstallerUrl": "https://github.com/AutoIDM/pdt/releases/download/v9.8.7/pdt-windows-x64.zip",
        "InstallerSha256": SHA.upper(),
    }]

    locale = manifests["AutoIDM.pdt.locale.en-US.yaml"]
    assert locale["Publisher"] == "AutoIDM"
    assert locale["PackageName"] == "pdt"
    assert locale["License"] == "MIT"
    assert manifests["AutoIDM.pdt.yaml"]["ManifestType"] == "version"
