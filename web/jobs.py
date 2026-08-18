"""اجرای فازها در پس‌زمینه برای پنل وب.

قواعدی که اینجا رعایت شده و شکستنشان دردسر می‌سازد:

* در هر لحظه فقط **یک** کار اجرا می‌شود؛ دو کراول هم‌زمان روی یک دیتابیس
  یعنی دو برابر شدن نرخ درخواست به سایت‌های هدف.
* ترد کارگر اتصال sqlite خودش را باز می‌کند. اتصال sqlite بین تردها
  قابل اشتراک نیست و WAL خواندن هم‌زمان را پوشش می‌دهد.
* هر چیزی که فازها با ``print`` می‌نویسند به بافر لاگ همین کار می‌رود،
  نه به ترمینال؛ مسیریابی بر اساس شناسه‌ی ترد است تا چاپ‌های بقیه‌ی
  برنامه دست‌نخورده بماند.
* لغو، تعاونی است: بین دو فاز بررسی می‌شود. فاز نیمه‌کاره قطع نمی‌شود
  چون همان‌جا داده در حال نوشته‌شدن است.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, TextIO

from ..core import db, pipeline
from ..core.config import Config

MAX_LINES = 4000

IDLE = "idle"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


class JobBusy(RuntimeError):
    """یک کار در حال اجراست."""


class _StdoutRouter:
    """جایگزین ``sys.stdout`` که نوشته‌ها را بر اساس ترد مسیریابی می‌کند."""

    def __init__(self, original: TextIO) -> None:
        self.original = original
        self._sinks: dict[int, Callable[[str], None]] = {}

    def register(self, sink: Callable[[str], None]) -> None:
        self._sinks[threading.get_ident()] = sink

    def unregister(self) -> None:
        self._sinks.pop(threading.get_ident(), None)

    def write(self, text: str) -> int:
        sink = self._sinks.get(threading.get_ident())
        if sink is None:
            return self.original.write(text)
        sink(text)
        return len(text)

    def flush(self) -> None:
        self.original.flush()

    def isatty(self) -> bool:
        return False


def install_router() -> _StdoutRouter:
    """``sys.stdout`` را با مسیریاب جایگزین می‌کند (اگر قبلاً نشده باشد).

    این کار در لحظه‌ی شروع کار انجام می‌شود، نه در بالا آمدن سرور؛ چون
    ممکن است چیز دیگری (مثلاً pytest) بعداً ``sys.stdout`` را عوض کرده باشد.
    """
    stream = sys.stdout
    if isinstance(stream, _StdoutRouter):
        return stream
    router = _StdoutRouter(stream)
    sys.stdout = router  # type: ignore[assignment]
    return router


@dataclass
class JobState:
    status: str = IDLE
    run_id: str = ""
    phases: list[int] = field(default_factory=list)
    phase: int | None = None
    done_phases: list[int] = field(default_factory=list)
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "phases": list(self.phases),
            "phase": self.phase,
            "phase_name": pipeline.PHASE_NAMES.get(self.phase or 0, ""),
            "done_phases": list(self.done_phases),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round(
                (self.finished_at or time.time()) - self.started_at, 1
            )
            if self.started_at
            else 0.0,
        }


class JobRunner:
    """یک کار در پس‌زمینه، با لاگ قابل خواندن به‌صورت افزایشی."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._state = JobState()
        self._lines: list[str] = []
        self._offset = 0  # تعداد خطوطی که از ابتدای کار دور ریخته شده‌اند
        self._partial = ""
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ لاگ
    def _emit(self, text: str) -> None:
        with self._lock:
            self._partial += text
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                self._append(line.rstrip())

    def _append(self, line: str) -> None:
        self._lines.append(line)
        if len(self._lines) > MAX_LINES:
            drop = len(self._lines) - MAX_LINES
            del self._lines[:drop]
            self._offset += drop

    def log(self, line: str) -> None:
        with self._lock:
            self._append(str(line).rstrip())

    def tail(self, since: int = 0) -> tuple[int, list[str]]:
        """خطوط از شماره‌ی ``since`` به بعد، همراه با شماره‌ی خط بعدی."""
        with self._lock:
            start = max(0, since - self._offset)
            return self._offset + len(self._lines), list(self._lines[start:])

    # ----------------------------------------------------------------- حالت
    @property
    def running(self) -> bool:
        return self._state.status == RUNNING

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            state = self._state.as_dict()
        next_line, lines = self.tail(since)
        state["lines"] = lines
        state["next"] = next_line
        return state

    # ------------------------------------------------------------------ کار
    def start(self, run_id: str, phases: list[int], config: Config | None = None) -> None:
        phases = [int(p) for p in phases]
        bad = [p for p in phases if p not in pipeline.PHASE_NAMES]
        if not phases or bad:
            raise ValueError("فهرست فازها نامعتبر است.")
        with self._lock:
            if self._state.status == RUNNING:
                raise JobBusy("یک اجرا در جریان است؛ اول آن را تمام یا لغو کنید.")
            if config is not None:
                self.config = config
            self._state = JobState(
                status=RUNNING, run_id=run_id, phases=phases, started_at=time.time()
            )
            self._lines = []
            self._offset = 0
            self._partial = ""
            self._cancel.clear()
        self._thread = threading.Thread(
            target=self._worker, args=(run_id, phases), name="pipeline-job", daemon=True
        )
        self._thread.start()

    def cancel(self) -> bool:
        """لغو — بین دو واحد کار (هر صفحه، هر کوئری) بررسی می‌شود.

        صفحه‌ی در حال دانلود تمام می‌شود (چند ثانیه) ولی بعدش متوقف می‌شویم؛
        نه اینکه تا آخر فاز ادامه بدهد.
        """
        if not self.running:
            return False
        self._cancel.set()
        self.log("⏹ درخواست لغو ثبت شد؛ بعد از پایان کارِ در جریان متوقف می‌شود.")
        return True

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _finish(self, status: str, error: str = "") -> None:
        with self._lock:
            self._state.status = status
            self._state.error = error
            self._state.phase = None
            self._state.finished_at = time.time()

    def _worker(self, run_id: str, phases: list[int]) -> None:
        router = install_router()
        router.register(self._emit)
        conn = None
        try:
            conn = db.connect(self.config.db_path)
            for phase in phases:
                if self._cancel.is_set():
                    self.log("اجرا لغو شد.")
                    self._finish(CANCELLED)
                    return
                with self._lock:
                    self._state.phase = phase
                self.log(f"▶ فاز {phase} — {pipeline.PHASE_NAMES[phase]}")
                summary = pipeline.run_phase(
                    conn, run_id, self.config, phase, self.log, self._cancel.is_set
                )
                for line in str(summary).splitlines():
                    self.log(line)
                with self._lock:
                    self._state.done_phases.append(phase)
            if self._cancel.is_set():
                self.log("⏹ اجرا لغو شد؛ آنچه تا اینجا پیدا شد در دیتابیس محفوظ است.")
                self._finish(CANCELLED)
            else:
                self.log("✔ پایان.")
                self._finish(DONE)
        except Exception as exc:  # noqa: BLE001 — پیام خطا باید به پنل برسد
            self.log(f"✖ خطا: {exc}")
            for line in traceback.format_exc().splitlines()[-12:]:
                self.log(line)
            self.log("داده‌های فازهای قبلی در دیتابیس محفوظ است؛ از همان فاز ادامه دهید.")
            if conn is not None:
                try:
                    db.set_run_status(conn, run_id, db.FAILED)
                except Exception:  # pragma: no cover — دیتابیس هم از دست رفته
                    pass
            self._finish(FAILED, str(exc))
        finally:
            if conn is not None:
                conn.close()
            router.unregister()
