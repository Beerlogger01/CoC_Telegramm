from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]


def test_addon_metadata_matches_published_image():
    config = yaml.safe_load((ROOT / "coc_telegram" / "config.yaml").read_text())
    repository = yaml.safe_load((ROOT / "repository.yaml").read_text())

    assert config["arch"] == ["aarch64", "amd64"]
    assert config["image"] == "ghcr.io/beerlogger01/coc-telegram"
    assert config["version"] == "0.3.0"
    assert repository["url"] == config["url"]


def test_addon_does_not_publish_ports():
    config = yaml.safe_load((ROOT / "coc_telegram" / "config.yaml").read_text())

    assert "ports" not in config
    assert config["host_network"] is False
