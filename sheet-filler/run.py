"""نقطه ورود «تکمیل‌کننده‌ی گوگل‌شیت».

نمونه‌ها::

    python -m sheet_filler.run panel   -c config.yaml          # پنل مدیریت در مرورگر
    python -m sheet_filler.run inspect -c config.yaml          # شیت چه ستون‌هایی دارد؟
    python -m sheet_filler.run fill    -c config.yaml          # یک بار پر کن
    python -m sheet_filler.run fill    -c config.yaml --auto   # خودکار، تا وقتی نبندیدش
    python -m sheet_filler.run links   -c config.yaml --file sources.csv
    python -m sheet_filler.run status  -c config.yaml

این برنامه مستقل است و به هیچ پروژه‌ی دیگری وابسته نیست. هزینه‌ی API هم
ندارد: همه‌چیز از HTML صفحه‌های منبع درمی‌آید.
"""

from __future__ import annotations

# اجرای مستقیم از داخل خود پوشه (``python run.py panel``) با import نسبی کار
# نمی‌کند. به‌جای خطای گیج‌کننده، پوشه‌ی والد را به مسیر اضافه می‌کنیم و همین
# فایل را این بار به‌عنوان ماژول بسته اجرا می‌کنیم.
if __name__ == "__main__" and not __package__:  # pragma: no cover
    import pathlib
    import runpy
    import sys as _sys

    _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    runpy.run_module("sheet_filler.run", run_name="__main__", alter_sys=True)
    raise SystemExit(0)

import sqlite3
import sys
from dataclasses import dataclass
from typing import Optional

import typer

# روی ویندوز، وقتی خروجی به فایل یا لوله هدایت شود پایتون از کدگذاری محلی
# استفاده می‌کند و چاپ متن فارسی با UnicodeEncodeError می‌افتد.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover — استریم غیرعادی
        pass

from .core import db, filler, gsheet, http, normalizer, sources
from .core.config import Config, ConfigError, load_config

app = typer.Typer(
    add_completion=False,
    help="تکمیل خودکار ستون‌های گوگل‌شیت محصولات از روی سایت‌های منبع (بدون هزینه‌ی API)",
)


@dataclass
class Context:
    config: Config
    conn: sqlite3.Connection

    def close(self) -> None:
        self.conn.close()


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _open(config_path: Optional[str]) -> Context:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _fail(str(exc))
        raise
    return Context(config=config, conn=db.connect(config.db_path))


def _options(context: Context, overrides: dict) -> filler.FillOptions:
    stored = db.get_setting(context.conn, "settings", {}) or {}
    merged = {**stored, **{k: v for k, v in overrides.items() if v not in (None, "")}}
    return filler.options_from_config(context.config, merged)


# ---------------------------------------------------------------------------
# دستورها
# ---------------------------------------------------------------------------


@app.command("inspect")
def inspect_command(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    sheet: Optional[str] = typer.Option(None, "--sheet", help="آدرس کامل گوگل‌شیت"),
    tab: Optional[str] = typer.Option(None, "--tab"),
    file: Optional[str] = typer.Option(None, "--file", help="به‌جای گوگل‌شیت، فایل csv/xlsx"),
) -> None:
    """شیت را می‌خواند و می‌گوید هر ستون چطور پر می‌شود — بدون نوشتن چیزی."""
    context = _open(config)
    options = _options(context, {"sheet_url": sheet, "tab": tab, "file": file})
    try:
        plan, rows, lists = filler.inspect(options)
    except gsheet.SheetError as exc:
        context.close()
        _fail(str(exc))
        return
    writable = {spec.key for spec in options.writable(plan)}
    typer.secho(f"{len(rows)} ردیف، {len(plan.specs)} ستون", bold=True)
    for spec in plan.specs:
        mark = "✓" if spec.key in writable else "—"
        kind = filler.KIND_LABELS.get(spec.kind, spec.kind)
        extra = f" | {len(spec.options)} گزینه" if spec.options else ""
        typer.echo(f"  {mark} {spec.column}: {kind}{extra}")
        typer.secho(f"      برچسب‌ها: {'، '.join(spec.search_labels())}", fg=typer.colors.BRIGHT_BLACK)
    if lists:
        typer.echo("لیست‌ها: " + "، ".join(f"{k} ({len(v)})" for k, v in lists.items()))
    context.close()


@app.command("fill")
def fill_command(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    sheet: Optional[str] = typer.Option(
        None, "--sheet", help="آدرس کامل گوگل‌شیت (شناسه‌اش خودکار درمی‌آید)"
    ),
    tab: Optional[str] = typer.Option(None, "--tab"),
    file: Optional[str] = typer.Option(None, "--file", help="به‌جای گوگل‌شیت، csv/xlsx محلی"),
    limit: int = typer.Option(0, "--limit", help="فقط این تعداد ردیف (۰ = تا آخر شیت)"),
    auto: bool = typer.Option(
        False, "--auto", help="حلقه‌ی خودکار: پر کن، بخواب، دوباره شیت را بخوان"
    ),
    every: int = typer.Option(0, "--every", help="فاصله‌ی دورهای خودکار به دقیقه"),
    save: bool = typer.Option(False, "--save", help="این تنظیمات برای دفعه‌ی بعد ذخیره شود"),
) -> None:
    """پر کردن ستون‌های شیت از روی سایت‌های منبع."""
    context = _open(config)
    overrides = {
        "sheet_url": sheet,
        "tab": tab,
        "file": file,
        "limit": limit or None,
        "auto": auto or None,
        "auto_every_minutes": every or None,
    }
    if save:
        stored = db.get_setting(context.conn, "settings", {}) or {}
        stored.update({k: v for k, v in overrides.items() if v not in (None, "")})
        db.set_setting(context.conn, "settings", stored)
        typer.secho("تنظیمات ذخیره شد.", fg=typer.colors.BRIGHT_BLACK)

    options = _options(context, overrides)
    if not options.ready:
        context.close()
        _fail(
            "نه آدرس گوگل‌شیت داده شده و نه فایل ورودی.\n"
            "  نمونه: python -m sheet_filler.run fill -c config.yaml"
            " --sheet https://docs.google.com/spreadsheets/d/..."
        )
        return
    try:
        if options.auto:
            typer.secho(
                f"حالت خودکار: هر {options.auto_every_minutes} دقیقه یک‌بار."
                " برای توقف Ctrl+C بزنید.",
                fg=typer.colors.CYAN,
            )
            stats = filler.run_auto(
                context.conn,
                context.config,
                options,
                lambda: http.fetcher_from_config(context.config),
                log=lambda line: typer.echo(f"  {line}"),
            )
        else:
            with http.fetcher_from_config(context.config) as fetcher:
                stats = filler.run(
                    context.conn,
                    context.config,
                    options,
                    fetcher,
                    log=lambda line: typer.echo(f"  {line}"),
                )
        typer.echo(stats.render())
    except gsheet.SheetError as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        typer.secho("\nمتوقف شد؛ ردیف‌های پرشده محفوظ‌اند.", fg=typer.colors.YELLOW)
    finally:
        context.close()


@app.command("links")
def links_command(
    file: str = typer.Option(..., "--file", help="csv/xlsx با ستون عنوان و ستون آدرس"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """ایمپورت نگاشت «عنوان → آدرس منبع».

    اگر از جای دیگری آدرس صفحه‌ی محصول‌ها را دارید، با این دستور واردشان کنید
    تا برنامه به‌جای جستجو، مستقیم سراغ همان صفحه‌ها برود.
    """
    context = _open(config)
    norm_config = normalizer.config_from_mapping(context.config.get("normalizer", {}))
    try:
        titles, links = sources.import_links(context.conn, file, norm_config)
    except ValueError as exc:
        context.close()
        _fail(str(exc))
        return
    typer.secho(f"{titles} عنوان و {links} آدرس ثبت شد.", fg=typer.colors.GREEN)
    typer.echo(f"مجموع آدرس‌های ذخیره‌شده: {db.link_count(context.conn)}")
    context.close()


@app.command("status")
def status_command(config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    """وضعیت شیت‌هایی که تا حالا پر شده‌اند."""
    context = _open(config)
    rows = db.list_sheets(context.conn)
    if not rows:
        typer.echo("هنوز هیچ شیتی پر نشده است.")
    for sheet in rows:
        counts = db.counts_of(context.conn, sheet["sheet_key"])
        typer.secho(f"{sheet['title'] or sheet['sheet_key']}", bold=True)
        typer.echo(
            f"  ردیف: {counts['total']} | کامل: {counts['done']} | ناقص: {counts['partial']}"
            f" | در صف: {counts['pending']} | نوشته‌شده در شیت: {counts['pushed']}"
        )
    typer.echo(f"آدرس‌های ذخیره‌شده: {db.link_count(context.conn)}")
    context.close()


@app.command("panel")
def panel_command(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="مسیر config.yaml"),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="پیش‌فرض ۸۰۵۰"),
    host: str = typer.Option("127.0.0.1", "--host", help="فقط لوکال؛ عوض کردنش ریسک دارد"),
    open_browser: bool = typer.Option(True, "--browser/--no-browser"),
) -> None:
    """بالا آوردن پنل مدیریت این برنامه در مرورگر."""
    import webbrowser

    from .web import server as web_server

    try:
        configuration = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
        return
    db.connect(configuration.db_path).close()

    candidates = [port] if port is not None else list(range(8050, 8060))
    httpd = state = None
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            httpd, state = web_server.build_server(configuration, config, host=host, port=candidate)
            break
        except OSError as exc:
            last_error = exc
    if httpd is None or state is None:
        _fail(f"پورت در دسترس نیست ({last_error}). با --port پورت دیگری بدهید.")
        return

    url = web_server.url_for(httpd, state.token)
    typer.secho("پنل تکمیل گوگل‌شیت بالا آمد:", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  {url}")
    typer.echo("  این لینک توکن نشست را دارد؛ همین را باز کنید. برای بستن: Ctrl+C")
    if host != "127.0.0.1":
        typer.secho("  ⚠ پنل روی شبکه باز است.", fg=typer.colors.YELLOW)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        typer.secho("\nپنل بسته شد.", fg=typer.colors.YELLOW)
    finally:
        httpd.server_close()


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        typer.secho("\nمتوقف شد.", fg=typer.colors.YELLOW)
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
