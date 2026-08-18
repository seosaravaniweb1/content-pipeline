"""راه‌انداز پنل — همان کاری که ``start-panel.bat`` صدا می‌زند.

چرا این فایل وجود دارد: ``cmd`` ویندوز نمی‌تواند فایل ``.bat`` با متن فارسی را
درست بخواند (کدپیج پیش‌فرضش فارسی نیست و با ``chcp`` وسط فایل، جای خواندنش
به‌هم می‌ریزد). ولی پایتون روی کنسول ویندوز با API یونیکد می‌نویسد و فارسی را
بی‌عیب نشان می‌دهد. پس فایل bat فقط یک خط ASCII است و همه‌ی پیام‌ها اینجاست.

این فایل عمداً هیچ import نسبی و هیچ وابستگی بیرونی ندارد تا حتی وقتی typer
نصب نیست هم اجرا شود و خودش نصبش کند.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # .../content_pipeline
WORKDIR = ROOT.parent                            # پوشه‌ی والد؛ مسیرهای config از اینجا حل می‌شوند
REQUIRED = (("typer", "typer"), ("yaml", "pyyaml"))


def _out(stream: object) -> None:
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover
        pass


def say(message: str = "") -> None:
    print(message, flush=True)


def missing_packages() -> list[str]:
    import importlib.util

    return [pip_name for module, pip_name in REQUIRED if importlib.util.find_spec(module) is None]


def install(packages: list[str]) -> bool:
    say(f"نصب وابستگی‌های لازم: {'، '.join(packages)}")
    say("این کار فقط بار اول انجام می‌شود و ممکن است یکی دو دقیقه طول بکشد.")
    say()
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *packages]
    )
    if result.returncode != 0:
        say()
        say("✖ نصب وابستگی‌ها موفق نبود. متن خطای بالا را نگاه کنید.")
        say("  اگر به اینترنت وصل نیستید یا فیلترشکن لازم است، اول آن را درست کنید.")
        return False
    say()
    return True


def ensure_config() -> Path | None:
    config = WORKDIR / "config.yaml"
    if config.exists():
        return config
    example = ROOT / "config.example.yaml"
    if not example.exists():  # pragma: no cover — فایل نمونه همیشه کنار پروژه است
        say(f"✖ فایل نمونه پیدا نشد: {example}")
        return None
    shutil.copyfile(example, config)
    say(f"+ فایل تنظیمات ساخته شد: {config}")
    say("  سایت‌های رقیب و موضوع هدف را از تب «تنظیمات» همان پنل پر کنید.")
    return config


def main() -> int:
    _out(sys.stdout)
    _out(sys.stderr)

    say()
    say("=" * 46)
    say("   پنل مدیریت خوراک محتوایی")
    say("=" * 46)
    say()

    if sys.version_info < (3, 9):  # pragma: no cover — نسخه‌ی خیلی قدیمی
        say(f"✖ نسخه‌ی پایتون شما {sys.version.split()[0]} است؛ حداقل ۳.۹ لازم است.")
        return 1

    packages = missing_packages()
    if packages and not install(packages):
        return 1

    config = ensure_config()
    if config is None:
        return 1

    say(f"+ تنظیمات: {config}")
    say("+ پنل در حال بالا آمدن است؛ مرورگر خودش باز می‌شود.")
    say("  برای بستن، در همین پنجره Ctrl+C بزنید.")
    say()

    sys.path.insert(0, str(WORKDIR))
    import os

    os.chdir(WORKDIR)
    from content_pipeline.run import app  # noqa: PLC0415 — بعد از نصب وابستگی‌ها

    try:
        app(["panel", "-c", str(config)], standalone_mode=False)
    except KeyboardInterrupt:
        say()
        say("پنل بسته شد.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
