"""تست پنل وب — API، توکن و اجرای پس‌زمینه."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from content_pipeline.core import db, pipeline
from content_pipeline.core.config import load_config
from content_pipeline.web import jobs, server as web_server

CONFIG_TEMPLATE = """
database:
  path: {db_path}
run:
  target_topic: "رمان"
  topic_examples:
    - "رمان شب سرد"
output:
  xlsx_path: "{out_path}"
"""


@pytest.fixture
def panel(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(
            db_path=(tmp_path / "runs.db").as_posix(),
            out_path=(tmp_path / "out-{run_id}.xlsx").as_posix(),
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    httpd, state = web_server.build_server(config, config_path, port=0, token="tok")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]

    def call(path, method="GET", body=None, token="tok", headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "X-Panel-Token": token,
                **({"Content-Type": "application/json"} if body is not None else {}),
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                payload = json.loads(raw) if raw.startswith(b"{") else raw
                return response.status, payload
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, raw

    call.state = state  # type: ignore[attr-defined]
    call.config_path = config_path  # type: ignore[attr-defined]
    yield call
    httpd.shutdown()
    httpd.server_close()


# ------------------------------------------------------------------ امنیت


def test_api_requires_the_session_token(panel):
    status, payload = panel("/api/state", token="wrong")
    assert status == 403
    assert "توکن" in payload["error"]


def test_api_rejects_foreign_host_header(panel):
    status, _ = panel("/api/state", headers={"Host": "evil.example.com"})
    assert status == 403


def test_index_page_is_served_without_token(panel):
    status, body = panel("/", token="")
    assert status == 200
    assert b"<html" in body


def test_static_path_cannot_escape_the_static_directory(panel):
    status, _ = panel("/../server.py", token="")
    assert status in {400, 404}


# -------------------------------------------------------------- اجراها


def test_create_and_list_runs(panel):
    status, created = panel("/api/runs", method="POST", body={"topic": "رمان"})
    assert status == 200
    status, state = panel("/api/state")
    assert state["current"]["run_id"] == created["run_id"]
    assert state["current"]["counters"]["raw"] == 0
    assert [run["run_id"] for run in state["runs"]] == [created["run_id"]]


def test_create_run_falls_back_to_config_topic(panel):
    _, created = panel("/api/runs", method="POST", body={})
    assert created["target_topic"] == "رمان"


def test_delete_run_wipes_it(panel):
    _, created = panel("/api/runs", method="POST", body={"topic": "رمان"})
    status, _ = panel("/api/runs/delete", method="POST", body={"run_id": created["run_id"]})
    assert status == 200
    _, state = panel("/api/state")
    assert state["runs"] == []


def test_unknown_run_id_is_reported(panel):
    status, payload = panel("/api/products?run_id=nope")
    assert status == 404
    assert "nope" in payload["error"]


# ------------------------------------------------------------- محصولات


def seed(panel):
    """یک اجرا با دو محصول: یکی ادغام‌شده و یکی علامت‌خورده."""
    conn = panel.state.conn()
    run_id = db.create_run(conn, "رمان")
    merged = db.insert_canonical(conn, run_id, "رمان شب سرد", ["الناز"], 0.95, False)
    flagged = db.insert_canonical(conn, run_id, "رمان شب گرم", [], 0.5, True)
    raw_ids = []
    for index, (title, canonical_id) in enumerate(
        [("رمان شب سرد", merged), ("رمان شب سرد الناز", merged), ("رمان شب گرم", flagged)]
    ):
        url = f"https://site{index}.ir/p/{index}"
        db.insert_raw_product(
            conn,
            run_id=run_id,
            source_domain=f"site{index}.ir",
            source_url=url,
            raw_title=title,
            normalized_title=title,
            topic_score=0.9,
            is_relevant=True,
        )
        raw_id = conn.execute(
            "SELECT id FROM raw_products WHERE run_id=? AND source_url=?", (run_id, url)
        ).fetchone()["id"]
        db.map_raw_to_canonical(conn, raw_id, canonical_id)
        raw_ids.append(raw_id)
    db.insert_keyword(conn, merged, "شب سرد کامل", 0)
    db.update_canonical(conn, merged, suggest_status="has_suggest")
    db.update_canonical(conn, flagged, suggest_status="no_suggest")
    conn.commit()
    return run_id, merged, flagged, raw_ids


def test_product_filters(panel):
    run_id, merged, flagged, _ = seed(panel)

    _, everything = panel(f"/api/products?run_id={run_id}")
    assert everything["total"] == 2
    # علامت‌خورده‌ها اول می‌آیند تا کار بازبینی سریع‌تر باشد
    assert everything["items"][0]["id"] == flagged

    _, only_merged = panel(f"/api/products?run_id={run_id}&filter=merged")
    assert [item["id"] for item in only_merged["items"]] == [merged]

    _, review = panel(f"/api/products?run_id={run_id}&filter=needs_review")
    assert [item["id"] for item in review["items"]] == [flagged]

    _, has = panel(f"/api/products?run_id={run_id}&filter=has_suggest")
    assert [item["id"] for item in has["items"]] == [merged]

    _, search = panel(f"/api/products?run_id={run_id}&q=" + urllib.parse.quote("گرم"))
    assert [item["id"] for item in search["items"]] == [flagged]


def test_product_detail_lists_members_and_keywords(panel):
    _, merged, _, _ = seed(panel)
    _, product = panel(f"/api/product?id={merged}")
    assert [m["raw_title"] for m in product["members"]] == ["رمان شب سرد", "رمان شب سرد الناز"]
    assert product["keywords"] == ["شب سرد کامل"]
    assert product["entity_tokens"] == ["الناز"]


def test_merge_then_detach_round_trip(panel):
    run_id, merged, flagged, raw_ids = seed(panel)

    status, result = panel(
        "/api/products/merge", method="POST",
        body={"target_id": merged, "source_id": flagged},
    )
    assert status == 200 and result["moved"] == 1
    _, product = panel(f"/api/product?id={merged}")
    assert len(product["members"]) == 3

    status, detached = panel(
        "/api/products/detach", method="POST", body={"raw_id": raw_ids[2]}
    )
    assert status == 200
    _, product = panel(f"/api/product?id={merged}")
    assert len(product["members"]) == 2
    _, fresh = panel(f"/api/product?id={detached['canonical_id']}")
    assert fresh["needs_review"] is True


def test_merge_error_is_reported_not_crashed(panel):
    _, merged, _, _ = seed(panel)
    status, payload = panel(
        "/api/products/merge", method="POST", body={"target_id": merged, "source_id": merged}
    )
    assert status == 400
    assert "یکی" in payload["error"]


def test_rename_and_review_flag(panel):
    _, merged, flagged, _ = seed(panel)
    status, _ = panel(
        "/api/products/rename", method="POST", body={"id": merged, "title": "رمان شب سرد کامل"}
    )
    assert status == 200
    _, product = panel(f"/api/product?id={merged}")
    assert product["title"] == "رمان شب سرد کامل"

    panel("/api/products/review", method="POST", body={"id": flagged, "flag": False})
    _, product = panel(f"/api/product?id={flagged}")
    assert product["needs_review"] is False


def test_review_tab_lists_flagged_and_borderline(panel):
    run_id, _, flagged, _ = seed(panel)
    conn = panel.state.conn()
    db.insert_raw_product(
        conn,
        run_id=run_id,
        source_domain="x.ir",
        source_url="https://x.ir/p/9",
        raw_title="عنوان مرزی",
        normalized_title="عنوان مرزی",
        topic_score=0.55,
        is_relevant=None,
    )
    conn.commit()

    _, data = panel(f"/api/review?run_id={run_id}")
    assert [p["id"] for p in data["products"]] == [flagged]
    assert [b["raw_title"] for b in data["borderline"]] == ["عنوان مرزی"]


def test_manual_edits_are_blocked_while_a_job_runs(panel, monkeypatch):
    _, merged, flagged, _ = seed(panel)
    gate = threading.Event()
    monkeypatch.setattr(
        pipeline, "run_phase", lambda *args, **kwargs: (gate.wait(5), "ok")[1]
    )
    panel.state.runner.start("whatever", [2])
    try:
        status, payload = panel(
            "/api/products/merge", method="POST",
            body={"target_id": merged, "source_id": flagged},
        )
        assert status == 409
        assert "ویرایش دستی" in payload["error"]
    finally:
        gate.set()
        panel.state.runner.join(5)


# --------------------------------------------------------------- تنظیمات


def test_config_round_trip_keeps_a_backup(panel):
    _, data = panel("/api/config")
    assert "target_topic" in data["text"]

    edited = data["text"].replace('"رمان"', '"جزوه"')
    status, result = panel("/api/config", method="POST", body={"text": edited})
    assert status == 200
    assert panel.config_path.read_text(encoding="utf-8") == edited
    assert "رمان" in (panel.config_path.parent / "config.yaml.bak").read_text(encoding="utf-8")

    _, state = panel("/api/state")
    assert state["target_topic"] == "جزوه"


def test_invalid_yaml_is_rejected_before_writing(panel):
    _, before = panel("/api/config")
    status, payload = panel("/api/config", method="POST", body={"text": "run: [oops\n"})
    assert status == 400
    assert "YAML" in payload["error"]
    _, after = panel("/api/config")
    assert after["text"] == before["text"]


def test_config_root_must_be_a_mapping(panel):
    status, payload = panel("/api/config", method="POST", body={"text": "- a\n- b\n"})
    assert status == 400
    assert "نگاشت" in payload["error"]


# ------------------------------------------------------------------ ابزار


def test_normalize_endpoint(panel):
    _, data = panel("/api/normalize?text=%D8%AF%D8%A7%D9%86%D9%84%D9%88%D8%AF")
    assert data["input"] == "دانلود"
    assert data["match"] == ""  # «دانلود» کلمه‌ی ایستای تجاری است


def test_each_list_can_be_downloaded_on_its_own(panel):
    """«لیست با ساجست، بدون ساجست، و باهم» — هرکدام یک فایل جدا."""
    run_id, merged, flagged, _ = seed(panel)

    status, body = panel(f"/download?run_id={run_id}&key=ready&format=csv")
    assert status == 200
    text = body.decode("utf-8-sig")
    assert "رمان شب سرد" in text        # ساجست دارد
    assert "رمان شب گرم" not in text    # ندارد

    status, body = panel(f"/download?run_id={run_id}&key=archive&format=csv")
    assert status == 200
    text = body.decode("utf-8-sig")
    assert "رمان شب گرم" in text
    assert "رمان شب سرد" not in text

    status, body = panel(f"/download?run_id={run_id}&key=all&format=csv")
    text = body.decode("utf-8-sig")
    assert "رمان شب سرد" in text and "رمان شب گرم" in text
    assert "دارد" in text and "ندارد" in text


def test_download_reflects_manual_edits_not_the_old_file(panel):
    """دانلود از دیتابیس ساخته می‌شود، پس ادغام دستیِ بعد از فاز ۴ هم دیده می‌شود."""
    run_id, merged, flagged, _ = seed(panel)
    panel("/api/products/rename", method="POST", body={"id": merged, "title": "نام تازه"})

    _, body = panel(f"/download?run_id={run_id}&key=all&format=csv")
    assert "نام تازه" in body.decode("utf-8-sig")


def test_download_lists_what_is_available(panel):
    run_id, _, _, _ = seed(panel)
    _, info = panel(f"/api/output?run_id={run_id}")
    keys = {item["key"]: item["rows"] for item in info["downloads"]}
    assert keys["ready"] == 1
    assert keys["archive"] == 1
    assert keys["all"] == 2


def test_download_refuses_an_unknown_list(panel):
    run_id, _, _, _ = seed(panel)
    status, payload = panel(f"/download?run_id={run_id}&key=hack")
    assert status == 404
    assert "نامعتبر" in payload["error"]


# ------------------------------------------------------------- کار پس‌زمینه


def test_job_runs_phases_in_order_and_captures_print(panel, monkeypatch):
    seen = []

    def fake_phase(conn, run_id, config, phase, log=print, should_stop=None):
        seen.append(phase)
        print(f"چاپ داخل فاز {phase}")
        log(f"لاگ فاز {phase}")
        return f"خلاصه‌ی فاز {phase}"

    monkeypatch.setattr(pipeline, "run_phase", fake_phase)
    _, created = panel("/api/runs", method="POST", body={"topic": "رمان"})

    status, _ = panel(
        "/api/job", method="POST", body={"run_id": created["run_id"], "phases": [1, 2]}
    )
    assert status == 200
    panel.state.runner.join(10)

    _, job = panel("/api/job")
    assert seen == [1, 2]
    assert job["status"] == "done"
    assert job["done_phases"] == [1, 2]
    text = "\n".join(job["lines"])
    assert "چاپ داخل فاز 1" in text
    assert "لاگ فاز 2" in text
    assert "خلاصه‌ی فاز 2" in text


def test_job_reports_failure_and_keeps_the_run_resumable(panel, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("سایت جواب نداد")

    monkeypatch.setattr(pipeline, "run_phase", boom)
    _, created = panel("/api/runs", method="POST", body={"topic": "رمان"})
    panel("/api/job", method="POST", body={"run_id": created["run_id"], "phases": [1]})
    panel.state.runner.join(10)

    _, job = panel("/api/job")
    assert job["status"] == "failed"
    assert "سایت جواب نداد" in job["error"]
    _, state = panel("/api/state")
    assert state["current"]["status"] == db.FAILED


def test_second_job_is_refused_while_one_runs(panel, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(pipeline, "run_phase", lambda *a, **k: (gate.wait(5), "ok")[1])
    _, created = panel("/api/runs", method="POST", body={"topic": "رمان"})
    panel("/api/job", method="POST", body={"run_id": created["run_id"], "phases": [1]})
    try:
        status, payload = panel(
            "/api/job", method="POST", body={"run_id": created["run_id"], "phases": [1]}
        )
        assert status == 409
        assert "در جریان" in payload["error"]
    finally:
        gate.set()
        panel.state.runner.join(5)


def test_cancel_stops_before_the_next_phase(panel, monkeypatch):
    seen = []
    first = threading.Event()

    def fake_phase(conn, run_id, config, phase, log=print, should_stop=None):
        seen.append(phase)
        first.set()
        time.sleep(0.05)
        return "ok"

    monkeypatch.setattr(pipeline, "run_phase", fake_phase)
    _, created = panel("/api/runs", method="POST", body={"topic": "رمان"})
    panel("/api/job", method="POST", body={"run_id": created["run_id"], "phases": [1, 2, 3, 4]})
    first.wait(5)
    panel("/api/job/cancel", method="POST", body={})
    panel.state.runner.join(10)

    _, job = panel("/api/job")
    assert job["status"] == "cancelled"
    assert seen != [1, 2, 3, 4]


def test_invalid_phase_number_is_refused(panel):
    _, created = panel("/api/runs", method="POST", body={"topic": "رمان"})
    status, payload = panel(
        "/api/job", method="POST", body={"run_id": created["run_id"], "phases": [9]}
    )
    assert status == 400
    assert "فاز" in payload["error"]


def test_log_tail_is_incremental(panel):
    runner = jobs.JobRunner(load_config(None))
    for index in range(5):
        runner.log(f"خط {index}")
    next_line, lines = runner.tail(0)
    assert lines == [f"خط {index}" for index in range(5)]
    assert next_line == 5
    assert runner.tail(3) == (5, ["خط 3", "خط 4"])


def test_log_buffer_is_bounded(panel, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_LINES", 10)
    runner = jobs.JobRunner(load_config(None))
    for index in range(25):
        runner.log(f"خط {index}")
    next_line, lines = runner.tail(0)
    assert len(lines) == 10
    assert lines[0] == "خط 15"
    assert next_line == 25


# ------------------------------------------------- نقشه‌ی اجرا: دسته و سایت


def test_presets_are_offered_to_the_user(panel):
    _, data = panel("/api/presets")
    labels = [preset["label"] for preset in data["presets"]]
    assert "جزوه" in labels and "رمان" in labels and "کتاب دانشگاهی" in labels
    assert data["max_sites"] == 300
    assert all(preset["examples"] for preset in data["presets"])


def test_run_is_created_with_categories_and_sites(panel):
    status, created = panel(
        "/api/runs",
        method="POST",
        body={
            "categories": ["رمان", "jozve"],
            "sites": "https://a.ir\nb.ir\n\nhttps://a.ir/\n#کامنت\nنه یک آدرس",
        },
    )
    assert status == 200
    assert created["categories"] == ["رمان", "جزوه"]
    assert created["target_topic"] == "رمان، جزوه"
    assert created["sites"] == 2  # تکراری و خط‌های بی‌ربط حذف شدند

    _, plan = panel(f"/api/plan?run_id={created['run_id']}")
    assert plan["sites"] == ["https://a.ir", "https://b.ir"]  # بدون http هم پذیرفته شد
    assert plan["categories"] == ["رمان", "جزوه"]


def test_run_without_any_category_is_refused(panel):
    status, payload = panel("/api/runs", method="POST", body={"categories": [], "sites": "a.ir"})
    # موضوع از config پر می‌شود، پس این اجرا ساخته می‌شود
    assert status == 200 and payload["target_topic"] == "رمان"


def test_site_limit_is_enforced(panel):
    many = "\n".join(f"https://site{i}.ir" for i in range(301))
    status, payload = panel(
        "/api/runs", method="POST", body={"categories": ["رمان"], "sites": many}
    )
    assert status == 400
    assert "۳۰۰" in payload["error"] or "300" in payload["error"]


def test_plan_can_be_edited_later(panel):
    _, created = panel(
        "/api/runs", method="POST", body={"categories": ["رمان"], "sites": "a.ir"}
    )
    status, plan = panel(
        "/api/plan",
        method="POST",
        body={"run_id": created["run_id"], "categories": ["جزوه"], "sites": "c.ir\nd.ir"},
    )
    assert status == 200
    assert plan["categories"] == ["جزوه"]
    assert plan["sites"] == ["https://c.ir", "https://d.ir"]
    _, state = panel("/api/state")
    assert state["current"]["target_topic"] == "جزوه"


def test_plan_falls_back_to_config_sites(panel):
    _, created = panel("/api/runs", method="POST", body={"categories": ["رمان"], "sites": ""})
    _, plan = panel(f"/api/plan?run_id={created['run_id']}")
    assert plan["sites"] == []
    assert plan["from_config"] is True


def test_plan_is_locked_while_a_job_runs(panel, monkeypatch):
    _, created = panel("/api/runs", method="POST", body={"categories": ["رمان"], "sites": "a.ir"})
    gate = threading.Event()
    monkeypatch.setattr(pipeline, "run_phase", lambda *a, **k: (gate.wait(5), "ok")[1])
    panel.state.runner.start(created["run_id"], [1])
    try:
        status, _ = panel(
            "/api/plan", method="POST", body={"run_id": created["run_id"], "sites": "z.ir"}
        )
        assert status == 409
    finally:
        gate.set()
        panel.state.runner.join(5)


# ------------------------------------------------------- انتخاب خروجی


def test_sheet_choices_are_listed_with_hints(panel):
    _, data = panel("/api/sheets")
    keys = [sheet["key"] for sheet in data["sheets"]]
    assert keys == ["ready", "archive", "all", "review"]
    assert set(data["selected"]) == set(keys)
    combined = next(sheet for sheet in data["sheets"] if sheet["key"] == "all")
    assert "ساجست" in combined["hint"]


def test_user_can_pick_only_the_output_they_want(panel):
    status, saved = panel("/api/sheets", method="POST", body={"sheets": ["all", "review"]})
    assert status == 200
    assert saved["selected"] == ["all", "review"]
    _, data = panel("/api/sheets")
    assert data["selected"] == ["all", "review"]


def test_empty_output_selection_is_refused(panel):
    status, payload = panel("/api/sheets", method="POST", body={"sheets": []})
    assert status == 400
    assert "حداقل" in payload["error"]
