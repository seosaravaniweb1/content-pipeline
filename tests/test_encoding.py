"""تست‌های فارسی‌ماندنِ متن: رمزگشایی صفحه، BOM، و خروجی کنسول."""

from __future__ import annotations

import subprocess
import sys

import pytest

from content_pipeline import launch
from content_pipeline.core import extract
from content_pipeline.core.config import load_config

TITLE = "رمان شب سرد الناز"


# --------------------------------------------------------- رمزگشایی صفحه


def test_utf8_page_without_any_hint():
    body = f"<html><head><title>{TITLE}</title></head></html>".encode("utf-8")
    assert TITLE in extract.decode_html(body)


def test_windows_1256_page_without_any_hint():
    """صفحات قدیمی فارسی هدر charset ندارند؛ با utf-8 خوانده شوند خراب می‌شوند."""
    body = f"<html><head><title>{TITLE}</title></head></html>".encode("windows-1256")
    assert TITLE in extract.decode_html(body)


def test_meta_charset_inside_the_page_is_used():
    body = f'<html><meta charset="windows-1256"><title>{TITLE}</title>'.encode("windows-1256")
    assert TITLE in extract.decode_html(body)


def test_http_header_charset_wins():
    body = TITLE.encode("windows-1256")
    assert TITLE in extract.decode_html(body, "text/html; charset=windows-1256")


def test_bom_is_stripped_not_kept_in_the_title():
    body = b"\xef\xbb\xbf" + TITLE.encode("utf-8")
    assert extract.decode_html(body) == TITLE


def test_broken_charset_name_does_not_crash():
    body = TITLE.encode("utf-8")
    assert TITLE in extract.decode_html(body, "text/html; charset=iso-8859-bogus")


def test_empty_body():
    assert extract.decode_html(b"") == ""


def test_title_survives_the_whole_extract_path():
    body = f"<html><head><title>{TITLE}</title></head><body><h1>{TITLE}</h1></body></html>".encode(
        "windows-1256"
    )
    assert extract.extract_title(extract.decode_html(body)) == TITLE


# ------------------------------------------------------------------- BOM


def test_config_saved_by_notepad_with_bom_still_loads(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes("﻿".encode("utf-8") + 'run:\n  target_topic: "رمان"\n'.encode("utf-8"))
    assert load_config(path).target_topic == "رمان"


# -------------------------------------------------- خروجی کنسول ویندوز


def test_persian_output_survives_being_redirected_to_a_file(tmp_path):
    """روی ویندوز، خروجی هدایت‌شده کدگذاری محلی می‌گیرد و بدون تنظیم
    صریح، چاپ فارسی با UnicodeEncodeError می‌افتد."""
    target = tmp_path / "out.txt"
    with target.open("wb") as handle:
        result = subprocess.run(
            [sys.executable, "-m", "content_pipeline.run", "normalize", "جزوه ریاضی"],
            stdout=handle,
            stderr=subprocess.PIPE,
        )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "جزوه" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------- راه‌انداز


def test_launcher_creates_config_next_to_the_project(tmp_path, monkeypatch):
    root = tmp_path / "content_pipeline"
    root.mkdir()
    (root / "config.example.yaml").write_text('run:\n  target_topic: "رمان"\n', encoding="utf-8")
    monkeypatch.setattr(launch, "ROOT", root)
    monkeypatch.setattr(launch, "WORKDIR", tmp_path)

    created = launch.ensure_config()

    assert created == tmp_path / "config.yaml"
    assert "رمان" in created.read_text(encoding="utf-8")


def test_launcher_keeps_an_existing_config(tmp_path, monkeypatch):
    root = tmp_path / "content_pipeline"
    root.mkdir()
    (root / "config.example.yaml").write_text("run: {}\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text('run:\n  target_topic: "دست‌نخورده"\n', encoding="utf-8")
    monkeypatch.setattr(launch, "ROOT", root)
    monkeypatch.setattr(launch, "WORKDIR", tmp_path)

    assert "دست‌نخورده" in launch.ensure_config().read_text(encoding="utf-8")


def test_launcher_reports_missing_packages():
    assert launch.missing_packages() == []  # در محیط تست هر دو نصب‌اند


@pytest.mark.parametrize("name", ["start-panel.bat", "start-panel.sh"])
def test_launcher_scripts_ship_with_the_project(name):
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / name
    assert path.is_file()


def test_bat_file_stays_ascii_only():
    """cmd ویندوز متن غیرانگلیسی داخل bat را تکه‌تکه اجرا می‌کند؛
    پیام‌های فارسی باید در launch.py بمانند، نه اینجا."""
    from pathlib import Path

    body = (Path(__file__).resolve().parent.parent / "start-panel.bat").read_bytes()
    assert all(byte < 128 for byte in body), "فایل bat باید فقط ASCII باشد"
    assert b"chcp" not in body, "chcp وسط فایل bat، پردازش خط‌ها را خراب می‌کند"
