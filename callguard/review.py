"""Call log page for the owner: /calls to review verdicts, /flag to mark one wrong.
Guarded by the admin token rather than a Twilio signature."""

import html
from datetime import datetime
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .state import State


def build_router(st: State) -> APIRouter:
    settings = st.settings
    tz = ZoneInfo(settings.timezone)
    router = APIRouter()

    @router.post("/flag")
    async def flag(request: Request):
        """Mark a verdict right or wrong from the review page. Browser form post, so this
        is guarded by the admin token rather than a Twilio signature."""
        form = await request.form()
        if str(form.get("token", "")) != settings.admin_token:
            raise HTTPException(status_code=404)
        st.calls.upsert(str(form.get("sid", "")),
                        wrong=1 if str(form.get("wrong")) == "1" else 0)
        back = f"/calls?token={quote(settings.admin_token)}"
        if form.get("back"):  # the page's filters, e.g. "decision=block&source=real"
            back += "&" + str(form.get("back"))
        return RedirectResponse(back, status_code=303)

    @router.get("/calls", response_class=HTMLResponse)
    async def calls_page(token: str = "", decision: str = "", source: str = ""):
        if token != settings.admin_token:
            raise HTTPException(status_code=404)
        esc = lambda v: html.escape("" if v is None else str(v))  # noqa: E731
        tok = quote(settings.admin_token)
        tests = settings.test_numbers
        n = st.calls.counts(source, tests)
        filters = urlencode({k: v for k, v in (("decision", decision), ("source", source)) if v})

        rows = []
        for c in st.calls.recent(decision=decision, source=source, test_numbers=tests):
            rec = (f'<a href="{esc(c["recording_url"])}.mp3">play</a>'
                   if c["recording_url"] else "")
            # Twilio's own confidence in the transcript: a block on a garbled one is the
            # case worth a second look, and it is free to spot.
            conf = c["confidence"] or 0
            conf_cell = (f'<span class="lo">{conf:.2f}</span>' if 0 < conf < 0.6
                         else (f"{conf:.2f}" if conf else ""))
            wrong = (c["wrong"] or 0) if "wrong" in c.keys() else 0
            btn = (f'<form method=post action="/flag"><input type=hidden name=token value="{esc(settings.admin_token)}">'
                   f'<input type=hidden name=sid value="{esc(c["sid"])}">'
                   f'<input type=hidden name=back value="{esc(filters)}">'
                   f'<input type=hidden name=wrong value="{0 if wrong else 1}">'
                   f'<button class="{"bad" if wrong else ""}">{"wrong &#10007;" if wrong else "mark wrong"}</button></form>')
            rows.append(
                f'<tr class="{"flagged" if wrong else ""}">' + "".join(f"<td>{x}</td>" for x in [
                    esc(datetime.fromtimestamp(c["ts"] or 0, tz).strftime("%b %d %H:%M %Z")),
                    esc(c["from_number"]) + (' <span class=tag>test</span>'
                                              if c["from_number"] in tests else ""),
                    f'<b>{esc(c["decision"])}</b>', esc(c["category"]),
                    esc(c["caller"]), esc(c["summary"]), esc(c["transcript"]), conf_cell,
                    esc(c["outcome"]), rec, esc(c["error"]), btn,
                ]) + "</tr>")

        def tab(label, key, val):
            cur = {"decision": decision, "source": source}
            on = " class=on" if cur[key] == val else ""
            q = urlencode({k: v for k, v in {**cur, key: val}.items() if v})
            return f'<a{on} href="/calls?token={tok}{"&" + q if q else ""}">{esc(label)}</a>'

        blocked, allowed = n.get("block", 0), n.get("allow", 0)
        return (
            "<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width'>"
            "<title>CallGuard log</title><style>body{font:14px system-ui;margin:16px}"
            "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;"
            "padding:6px;text-align:left;vertical-align:top}"
            "nav{margin:8px 0 16px}nav a{margin-right:12px;text-decoration:none;color:#2a5d8f}"
            "nav a.on{font-weight:700;text-decoration:underline}"
            ".lo{color:#b8412c;font-weight:600}tr.flagged{background:#fff4f0}"
            "button{font:12px system-ui;cursor:pointer}button.bad{color:#b8412c;font-weight:600}"
            "form{margin:0}.tag{font-size:11px;background:#e8eef5;color:#2a5d8f;"
            "border-radius:4px;padding:1px 5px}</style>"
            f"<h1>Screened calls</h1><p>Mode: <b>{esc(settings.mode)}</b> &middot; "
            f"{blocked} blocked ({n.get('block_wrong', 0)} marked wrong) &middot; "
            f"{allowed} allowed ({n.get('allow_wrong', 0)} marked wrong)</p>"
            f'<nav>{tab("All", "decision", "")}{tab("Blocked", "decision", "block")}'
            f'{tab("Allowed", "decision", "allow")}</nav>'
            f'<nav>{tab("All callers", "source", "")}{tab("Real calls", "source", "real")}'
            f'{tab("Test calls", "source", "test")}</nav>'
            "<table><tr>"
            "<th>When</th><th>From</th><th>Decision</th><th>Category</th><th>Caller</th>"
            "<th>Why</th><th>They said</th><th>Conf</th><th>Outcome</th><th>Voicemail</th>"
            "<th>Error</th><th>Review</th></tr>"
            + "".join(rows) + "</table>"
        )

    return router
