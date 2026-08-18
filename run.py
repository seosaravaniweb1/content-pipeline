"""نقطه ورود CLI ابزار استخراج و تحلیل خوراک محتوایی.

نمونه‌ها::

    python -m content_pipeline.run init-db -c config.yaml
    python -m content_pipeline.run start   -c config.yaml
    python -m content_pipeline.run start   -c config.yaml --resume-from 3
    python -m content_pipeline.run status  -c config.yaml
    python -m content_pipeline.run normalize "دانلود جزوه ریاضی ۱۴۰۴ PDF"

هزینه‌ی API این ابزار صفر است؛ هیچ سرویس پولی استفاده نمی‌شود.
"""

from __future__ import annotations

# اجرای مستقیم از داخل خود پوشه (``python run.py panel -c config.yaml``) با
# import های نسبی کار نمی‌کند. به‌جای خطای گیج‌کننده، پوشه‌ی والد را به مسیر
# اضافه می‌کنیم و همان فایل را این بار به‌عنوان ماژول بسته اجرا می‌کنیم.
if __name__ == "__main__" and not __package__:  # pragma: no cover
    import pathlib
    import runpy
    import sys as _sys

    _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    runpy.run_module("content_pipeline.run", run_name="__main__", alter_sys=True)
    raise SystemExit(0)

import sqlite3
import sys
from dataclasses import dataclass
from typing import Optional

import typer

# روی ویندوز، وقتی خروجی به فایل یا لوله هدایت شود پایتون از کدگذاری محلی
# (cp1252/cp437) استفاده می‌کند و چاپ متن فارسی با UnicodeEncodeError می‌افتد.
# کنسول خودش مشکلی ندارد، ولی این خط هر دو حالت را امن می‌کند.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover — استریم غیرعادی
        pass

from .core import db, normalizer, pipeline
from .core.config import Config, ConfigError, load_config
from .output import exporter

app = typer.Typer(
    add_completion=False,
    help="پایپ‌لاین موضوع‌محور استخراج و تحلیل خوراک محتوایی (بدون هزینه‌ی API)",
)

PHASE_NAMES = pipeline.PHASE_NAMES
LAST_PHASE = pipeline.LAST_PHASE


@dataclass
class Context:
    config: Config
    conn: sqlite3.Connection
    run_id: str

    def close(self) -> None:
        self.conn.close()


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _open(
    config_path: Optional[str],
    topic: Optional[str] = None,
    run_id: Optional[str] = None,
    create: bool = True,
) -> Context:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _fail(str(exc))
        raise
    conn = db.connect(config.db_path)

    target_topic = topic or config.target_topic
    if run_id:
        if db.get_run(conn, run_id) is None:
            _fail(f"run_id پیدا نشد: {run_id}")
        resolved = run_id
    elif create:
        if not target_topic:
            _fail(
                "موضوع هدف مشخص نیست: --topic بدهید یا run.target_topic را در config پر کنید."
            )
        resolved = db.create_run(conn, target_topic)
    else:
        row = db.latest_run(conn, target_topic or None)
        if row is None:
            _fail("هیچ اجرای قبلی پیدا نشد. اول `start` را اجرا کنید یا --run-id بدهید.")
        resolved = row["run_id"]
    return Context(config=config, conn=conn, run_id=resolved)


# ---------------------------------------------------------------------------
# فازها — پیاده‌سازی مشترک با پنل وب در core/pipeline.py است
# ---------------------------------------------------------------------------


def _run_phase(context: Context, phase: int) -> None:
    if phase not in PHASE_NAMES:
        _fail(f"شماره فاز نامعتبر: {phase}")
    typer.secho(f"\n▶ فاز {phase} — {PHASE_NAMES[phase]}", fg=typer.colors.CYAN, bold=True)
    summary = pipeline.run_phase(
        context.conn,
        context.run_id,
        context.config,
        phase,
        log=lambda line: typer.echo(f"  {line}"),
    )
    typer.echo(summary)


# ---------------------------------------------------------------------------
# دستورها
# ---------------------------------------------------------------------------


@app.command("init-db")
def init_db(config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    """ساخت دیتابیس و اسکیما."""
    configuration = load_config(config)
    conn = db.connect(configuration.db_path)
    conn.close()
    typer.secho(f"دیتابیس آماده است: {configuration.db_path}", fg=typer.colors.GREEN)


@app.command("start")
def start(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="مسیر config.yaml"),
    topic: Optional[str] = typer.Option(None, "--topic", "-t", help="موضوع هدف"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="ادامه‌ی یک اجرای موجود"),
    resume_from: int = typer.Option(1, "--resume-from", min=1, max=LAST_PHASE),
    until: int = typer.Option(LAST_PHASE, "--until", min=1, max=LAST_PHASE),
    phases: Optional[str] = typer.Option(None, "--phases", help="فهرست دلخواه، مثل 1,2"),
) -> None:
    """اجرای پایپ‌لاین. فازها مستقل‌اند و از طریق دیتابیس با هم حرف می‌زنند."""
    context = _open(
        config, topic=topic, run_id=run_id, create=resume_from == 1 and run_id is None
    )
    if phases:
        try:
            selected = [int(p) for p in phases.replace(" ", "").split(",") if p]
        except ValueError:
            _fail("قالب --phases نامعتبر است. نمونه: --phases 1,2")
            return
    else:
        selected = list(range(resume_from, until + 1))

    typer.secho(f"run_id: {context.run_id}", fg=typer.colors.BRIGHT_BLACK)
    try:
        for phase in selected:
            _run_phase(context, phase)
    except Exception as exc:
        db.set_run_status(context.conn, context.run_id, db.FAILED)
        typer.secho(f"\nاجرا متوقف شد: {exc}", fg=typer.colors.RED, err=True)
        typer.echo("داده‌های فازهای قبلی در دیتابیس محفوظ است؛ با --resume-from ادامه دهید.")
        context.close()
        raise typer.Exit(code=1)

    if LAST_PHASE in selected:
        db.set_run_status(context.conn, context.run_id, db.COMPLETED, LAST_PHASE)
    context.close()


@app.command("phase")
def phase_command(
    number: int = typer.Argument(..., min=1, max=LAST_PHASE),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """اجرای فقط یک فاز روی یک اجرای موجود."""
    context = _open(config, run_id=run_id, create=False)
    typer.secho(f"run_id: {context.run_id}", fg=typer.colors.BRIGHT_BLACK)
    try:
        _run_phase(context, number)
    finally:
        context.close()


@app.command("export")
def export_command(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
    no_sheets: bool = typer.Option(False, "--no-sheets", help="فقط فایل محلی"),
) -> None:
    """ساخت خروجی نهایی (سه شیت)."""
    context = _open(config, run_id=run_id, create=False)
    stats = exporter.run(
        context.conn, context.run_id, context.config, push_to_sheets=not no_sheets
    )
    typer.echo(stats.render())
    context.close()


@app.command("status")
def status_command(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """وضعیت یک اجرا."""
    context = _open(config, run_id=run_id, create=False)
    conn, rid = context.conn, context.run_id
    row = db.get_run(conn, rid)
    typer.secho(f"run_id: {rid}", bold=True)
    typer.echo(
        f"موضوع: {row['target_topic']}  |  وضعیت: {row['status']}  |  فاز: {row['current_phase']}"
    )

    labels = {
        "raw": "عنوان خام",
        "relevant": "مرتبط",
        "borderline": "مرزی (شیت C)",
        "canonical": "محصول یکپارچه",
        "needs_review": "نیازمند بازبینی",
        "has_suggest": "has_suggest",
        "no_suggest": "no_suggest",
        "pending_suggest": "در صف ساجست",
        "keywords": "کلمه‌ی کلیدی",
    }
    counts = pipeline.counters(conn, rid)
    for key, label in labels.items():
        typer.echo(f"  {label}: {counts[key]}")
    context.close()


@app.command("runs")
def runs_command(config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    """فهرست اجراهای قبلی."""
    configuration = load_config(config)
    conn = db.connect(configuration.db_path)
    rows = db.list_runs(conn)
    if not rows:
        typer.echo("هنوز هیچ اجرایی ثبت نشده است.")
    for row in rows:
        typer.echo(
            f"{row['run_id']}  |  {row['target_topic']}  |  {row['status']}"
            f"  |  فاز {row['current_phase']}"
        )
    conn.close()


@app.command("panel")
def panel_command(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="مسیر config.yaml"),
    port: Optional[int] = typer.Option(
        None, "--port", "-p", help="پورت پنل (پیش‌فرض ۸۰۰۰؛ اگر اشغال بود، پورت بعدی)"
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="فقط لوکال؛ عوض کردنش ریسک دارد"),
    open_browser: bool = typer.Option(True, "--browser/--no-browser", help="باز کردن مرورگر"),
) -> None:
    """بالا آوردن پنل مدیریت در مرورگر."""
    import webbrowser

    from .web import server as web_server

    try:
        configuration = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
        return
    db.connect(configuration.db_path).close()  # اطمینان از وجود دیتابیس و اسکیما

    # پورت صریح یعنی همان پورت؛ بدون آن، اگر ۸۰۰۰ اشغال بود سراغ بعدی می‌رویم
    # تا اجرای دو-کلیکی به خطای پورت نخورد.
    candidates = [port] if port is not None else list(range(8000, 8010))
    httpd = state = None
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            httpd, state = web_server.build_server(
                configuration, config, host=host, port=candidate
            )
            break
        except OSError as exc:
            last_error = exc
    if httpd is None or state is None:
        if port is not None:
            _fail(f"پورت {port} در دسترس نیست ({last_error}). با --port پورت دیگری بدهید.")
        else:
            _fail(
                f"پورت‌های ۸۰۰۰ تا ۸۰۰۹ همه اشغال‌اند ({last_error})."
                " با --port پورت دیگری بدهید."
            )
        return

    url = web_server.url_for(httpd, state.token)
    typer.secho("پنل مدیریت بالا آمد:", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  {url}")
    typer.echo("  این لینک توکن نشست را دارد؛ همین را باز کنید. برای بستن: Ctrl+C")
    if host != "127.0.0.1":
        typer.secho(
            "  ⚠ پنل روی شبکه باز است. هرکسی که به این پورت برسد می‌تواند کراول راه بیندازد.",
            fg=typer.colors.YELLOW,
        )
    if open_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        typer.secho("\nپنل بسته شد.", fg=typer.colors.YELLOW)
    finally:
        httpd.server_close()


@app.command("normalize")
def normalize_command(
    text: str = typer.Argument(...),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """ابزار کمکی برای دیدن خروجی فاز ۰ روی یک متن."""
    configuration = load_config(config)
    norm_config = normalizer.config_from_mapping(configuration.normalizer)
    typer.echo(f"ورودی      : {text}")
    typer.echo(f"نمایشی     : {normalizer.normalize_display(text, norm_config)}")
    typer.echo(f"تطبیق      : {normalizer.normalize(text, norm_config)}")
    typer.echo(f"توکن‌ها     : {normalizer.meaningful_tokens(text, norm_config)}")


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        typer.secho("\nمتوقف شد.", fg=typer.colors.YELLOW)
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
