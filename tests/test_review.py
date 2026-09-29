"""The owner's call log page: filters, times, and marking verdicts wrong."""

from helpers import SALES, SPAMMER, post, run_call


def page(client, **qs):
    q = "&".join(f"{k}={v}" for k, v in qs.items())
    return client.get(f"/calls?token=admin&{q}")


def test_calls_page_shows_reason_and_confidence(make):
    client, _ = make(SALES)
    run_call(client, "solar panels for your home today")
    r = page(client)
    assert "Solar panel offer." in r.text      # the summary column
    assert "0.90" in r.text                     # Twilio confidence
    assert "mark wrong" in r.text


def test_calls_page_shows_pacific_time(make):
    client, _ = make(SALES)
    run_call(client, "solar panels")
    client.app.state.callguard.calls.upsert("CA1", ts=1790000000)  # 2026-09-21 14:13 UTC
    assert "Sep 21 07:13 PDT" in page(client).text


def test_calls_page_filters_by_decision(make):
    client, _ = make(SALES)
    run_call(client, "solar panels")
    assert "solar panels" in page(client, decision="block").text
    assert "solar panels" not in page(client, decision="allow").text


def test_calls_page_filters_test_numbers(make):
    client, _ = make(SALES)
    client.app.state.callguard.settings.test_numbers = ("+14085550100",)
    post(client, "/voice", CallSid="CA1", From=SPAMMER)
    post(client, "/voice", CallSid="CA2", From="+14085550100")
    assert SPAMMER not in page(client, source="test").text
    assert "+14085550100" in page(client, source="test").text
    real = page(client, source="real").text
    assert SPAMMER in real and "+14085550100" not in real
    assert "<span class=tag>test</span>" in page(client).text


def test_real_filter_without_test_numbers_shows_everything(make):
    client, _ = make(SALES)
    post(client, "/voice", CallSid="CA1", From=SPAMMER)
    assert SPAMMER in page(client, source="real").text
    assert SPAMMER not in page(client, source="test").text


def test_flag_keeps_page_filters(make):
    client, _ = make(SALES)
    run_call(client, "solar panels")
    r = client.post("/flag", data={"token": "admin", "sid": "CA1", "wrong": "1",
                                   "back": "decision=block&source=real"}, follow_redirects=False)
    assert r.headers["location"].endswith("&decision=block&source=real")


def test_flag_requires_admin_token(make):
    client, _ = make(SALES)
    run_call(client, "solar panels")
    assert client.post("/flag", data={"token": "nope", "sid": "CA1", "wrong": "1"}).status_code == 404


def test_flag_marks_and_unmarks_a_verdict(make):
    client, _ = make(SALES)
    run_call(client, "solar panels")
    r = client.post("/flag", data={"token": "admin", "sid": "CA1", "wrong": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.app.state.callguard.calls.get("CA1")["wrong"] == 1
    assert "1 marked wrong" in page(client).text
    client.post("/flag", data={"token": "admin", "sid": "CA1", "wrong": "0"},
                follow_redirects=False)
    assert client.app.state.callguard.calls.get("CA1")["wrong"] == 0


def test_low_confidence_is_highlighted(make):
    client, _ = make(SALES)
    post(client, "/voice", CallSid="CA1", From=SPAMMER)
    post(client, "/screen", CallSid="CA1", From=SPAMMER, SpeechResult="mumble", Confidence="0.31")
    post(client, "/decide", CallSid="CA1", From=SPAMMER)
    assert 'class="lo">0.31' in page(client).text


def test_wrong_column_added_to_existing_db(tmp_path):
    # A database created before the review flag existed must still open.
    import sqlite3
    from callguard.calllog import CallLog
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE calls (sid TEXT PRIMARY KEY, ts REAL, from_number TEXT, "
                "transcript TEXT, confidence REAL, decision TEXT, category TEXT, caller TEXT, "
                "summary TEXT, mode TEXT, error TEXT, outcome TEXT, recording_url TEXT)")
    old.execute("INSERT INTO calls (sid, decision) VALUES ('OLD1', 'block')")
    old.commit(); old.close()
    log = CallLog(str(db))
    assert log.get("OLD1")["wrong"] == 0
    assert log.counts()["block"] == 1
