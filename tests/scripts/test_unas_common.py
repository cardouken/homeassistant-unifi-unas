from pathlib import Path

import unas_common


def test_missing_or_broken_config_reads_as_empty(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(unas_common, "CONFIG_FILE", path)
    assert unas_common.load_config() == {}
    path.write_text("{broken")
    assert unas_common.load_config() == {}
    path.write_text("[1, 2]")
    assert unas_common.load_config() == {}


def test_config_is_read(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"root": "unas/abc", "mqtt": {"host": "192.0.2.2"}}')
    monkeypatch.setattr(unas_common, "CONFIG_FILE", path)
    assert unas_common.load_config()["root"] == "unas/abc"


def test_vendored_paho_is_the_one_imported():
    import paho.mqtt.client as mqtt

    assert Path(mqtt.__file__).resolve().is_relative_to(unas_common.BUNDLE_DIR / "vendor")
