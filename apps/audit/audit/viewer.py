"""利用料と入出力の履歴を検索するアプリ（変更仕様書 7 章、変更設計書 6 章）。経路はすべて /lab-audit/ の下。

- サインインは OpenID Connect（検証環境は Google、本番は Entra ID。設定だけで切り替える）
- 役割: 管理者（AUDIT_ADMINS）、監査担当（AUDIT_AUDITORS）、一般の利用者（自分の記録だけ）。絞り込みはサーバー側でかける
- 本文の閲覧は必ず記録する（記録を書けなければ本文を返さない）。監査担当が他人の本文を開くときは理由が必須
- 書き込みを伴う API（POST、PUT）は CSRF の確認用の値（ヘッダー X-CSRF-Token）が必須
"""

from __future__ import annotations

import os
import secrets
import socket
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .common import JST, Config, now_jst, validate_contracts
from .data import Store

BASE = "/lab-audit"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MAX_DAYS = 90


def _list(name: str) -> set[str]:
    return {x.strip().lower() for x in os.environ.get(name, "").replace(";", ",").split(",") if x.strip()}


def role_of(email: str, groups: list[str] | None = None) -> str:
    """管理者 > 監査担当 > 一般の利用者。メールアドレスか、ID トークンのグループ（AUDIT_ROLE_CLAIM）で決める。"""
    names = {email.lower(), *[g.lower() for g in (groups or [])]}
    if names & _list("AUDIT_ADMINS"):
        return "admin"
    if names & _list("AUDIT_AUDITORS"):
        return "auditor"
    return "user"


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=os.environ.get("AUDIT_SESSION_SECRET", "dev-only"),
                       session_cookie="lab_audit_session", max_age=8 * 3600, same_site="lax",
                       https_only=os.environ.get("AUDIT_COOKIE_SECURE", "false") == "true", path=BASE)
    app.state.store = store
    oauth = None
    issuer = os.environ.get("AUDIT_OIDC_ISSUER", "")
    if issuer and os.environ.get("AUDIT_OIDC_CLIENT_ID"):
        from authlib.integrations.starlette_client import OAuth
        oauth = OAuth()
        oauth.register("oidc", client_id=os.environ["AUDIT_OIDC_CLIENT_ID"],
                       client_secret=os.environ.get("AUDIT_OIDC_CLIENT_SECRET", ""),
                       server_metadata_url=issuer.rstrip("/") + "/.well-known/openid-configuration",
                       client_kwargs={"scope": "openid email profile"})

    def get_store() -> Store:
        if app.state.store is None:
            app.state.store = Store(Config.from_env(), pod=os.environ.get("HOSTNAME") or socket.gethostname())
        return app.state.store

    def current_user(request: Request) -> dict:
        user = request.session.get("user")
        if not user:
            raise HTTPException(401, "サインインしてください")
        return user

    def page_user(request: Request) -> dict | None:
        return request.session.get("user")

    def check_csrf(request: Request, user: dict = Depends(current_user)) -> dict:
        token = request.headers.get("x-csrf-token", "")
        if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
            raise HTTPException(403, "CSRF の確認用の値がありません")
        return user

    def scope_email(user: dict, requested: str | None) -> str | None:
        """一般の利用者は自分のメールアドレスに固定する（ほかの利用者を指定しても無視する）。"""
        if user["role"] == "user":
            return user["email"]
        return requested or None

    def need(user: dict, *roles: str) -> None:
        if user["role"] not in roles:
            raise HTTPException(403, "権限がありません")

    def parse_range(start: str | None, end: str | None) -> tuple[date, date]:
        today = now_jst().date()
        d1 = date.fromisoformat(end) if end else today
        d0 = date.fromisoformat(start) if start else d1 - timedelta(days=6)
        if d0 > d1:
            raise HTTPException(400, "期間の始まりが終わりより後です")
        if (d1 - d0).days >= MAX_DAYS:
            raise HTTPException(400, f"期間は最大 {MAX_DAYS} 日です")
        return d0, d1

    def day_bounds(d0: date, d1: date) -> tuple[datetime, datetime]:
        return (datetime(d0.year, d0.month, d0.day, tzinfo=JST),
                datetime(d1.year, d1.month, d1.day, 23, 59, 59, 999999, tzinfo=JST))

    # ---- サインイン ----
    @app.get(BASE + "/healthz", include_in_schema=False)
    def healthz():
        return PlainTextResponse("ok")

    @app.get(BASE, include_in_schema=False)
    def root_redirect():
        return RedirectResponse(BASE + "/")

    @app.get(BASE + "/login")
    async def login(request: Request):
        if oauth is None:
            raise HTTPException(503, "サインインの設定がありません")
        redirect = os.environ.get("AUDIT_BASE_URL", "").rstrip("/") + BASE + "/auth/callback"
        return await oauth.oidc.authorize_redirect(request, redirect)

    @app.get(BASE + "/auth/callback")
    async def callback(request: Request):
        token = await oauth.oidc.authorize_access_token(request)
        claims = token.get("userinfo") or {}
        email = (claims.get("email") or "").lower()
        if not email or claims.get("email_verified") is False:
            raise HTTPException(403, "メールアドレスを確かめられませんでした")
        claim = os.environ.get("AUDIT_ROLE_CLAIM", "")
        groups = claims.get(claim) if claim else None
        request.session.clear()
        request.session["user"] = {"email": email, "role": role_of(email, groups if isinstance(groups, list) else None)}
        request.session["csrf"] = secrets.token_urlsafe(24)
        return RedirectResponse(BASE + "/")

    @app.get(BASE + "/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse(BASE + "/signed-out")

    @app.get(BASE + "/signed-out", response_class=HTMLResponse)
    def signed_out(request: Request):
        return TEMPLATES.TemplateResponse(request, "signed_out.html", {"base": BASE})

    # ---- API ----
    @app.get(BASE + "/api/me")
    def me(user: dict = Depends(current_user)):
        return user

    @app.get(BASE + "/api/usage")
    def api_usage(request: Request, start: str | None = None, end: str | None = None, user_email: str | None = None,
                  group_by: str = "user_email", user: dict = Depends(current_user)):
        d0, d1 = parse_range(start, end)
        rows = get_store().usage(d0, d1, scope_email(user, user_email), group_by.split(","))
        return {"start": d0.isoformat(), "end": d1.isoformat(), "rows": rows}

    @app.get(BASE + "/api/usage.csv")
    def api_usage_csv(start: str | None = None, end: str | None = None, user_email: str | None = None,
                      group_by: str = "user_email", user: dict = Depends(current_user)):
        d0, d1 = parse_range(start, end)
        rows = get_store().usage(d0, d1, scope_email(user, user_email), group_by.split(","))
        return _csv(rows, f"usage-{d0}-{d1}.csv")

    @app.get(BASE + "/api/requests")
    def api_requests(start: str | None = None, end: str | None = None, user_email: str | None = None,
                     model_group: str | None = None, client: str | None = None, status: str | None = None,
                     blocked: bool = False, tool: str | None = None, page: int = 0,
                     user: dict = Depends(current_user)):
        d0, d1 = parse_range(start, end)
        t0, t1 = day_bounds(d0, d1)
        filters = {"model_group": model_group, "client": client, "status": status, "blocked": blocked, "tool": tool}
        rows, more = get_store().requests(t0, t1, scope_email(user, user_email), filters, max(0, page))
        return {"rows": rows, "page": page, "more": more}

    @app.get(BASE + "/api/requests/{call_id}")
    def api_request(call_id: str, at: str, user: dict = Depends(current_user)):
        row = get_store().request(call_id, datetime.fromisoformat(at), scope_email(user, None))
        if row is None:
            raise HTTPException(404, "見つかりません")
        return row

    @app.post(BASE + "/api/requests/{call_id}/body")
    async def api_body(call_id: str, request: Request, at: str, user: dict = Depends(check_csrf)):
        payload = await _json(request)
        store = get_store()
        row = store.request(call_id, datetime.fromisoformat(at), scope_email(user, None))
        if row is None or not row.get("body_key"):
            raise HTTPException(404, "見つかりません")
        reason = str(payload.get("reason") or "").strip()
        own = row.get("user_email") == user["email"]
        if not own and not reason:
            raise HTTPException(400, "他人の本文を開くときは理由を入れてください")
        # 閲覧の記録を先に書く。書けなければ本文を返さない
        try:
            store.write_access("view", {"viewer_email": user["email"], "viewer_role": user["role"],
                                        "subject_user_id": row.get("user_id"),
                                        "subject_user_email": row.get("user_email"), "call_id": call_id,
                                        "reason": reason or None,
                                        "client_ip": request.headers.get("x-forwarded-for") or
                                        (request.client.host if request.client else None)})
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"閲覧の記録を書けなかったので、本文は表示しません（{type(e).__name__}）")
        return store.body(row)

    @app.get(BASE + "/api/views")
    def api_views(start: str | None = None, end: str | None = None, viewer: str | None = None,
                  subject: str | None = None, user: dict = Depends(current_user)):
        need(user, "auditor", "admin")
        d0, d1 = parse_range(start, end)
        rows = get_store().read_access("view", d0, d1)
        rows = [r for r in rows if (not viewer or r.get("viewer_email") == viewer)
                and (not subject or r.get("subject_user_email") == subject)]
        return {"rows": rows}

    @app.get(BASE + "/api/admin-log")
    def api_admin_log(start: str | None = None, end: str | None = None, user: dict = Depends(current_user)):
        need(user, "admin")
        d0, d1 = parse_range(start, end)
        return {"rows": get_store().read_access("admin", d0, d1)}

    @app.get(BASE + "/api/settlement")
    def api_settlement(month: str | None = None, user: dict = Depends(current_user)):
        month = month or now_jst().strftime("%Y-%m")
        return {"month": month, "rows": get_store().settlement(month, scope_email(user, None))}

    @app.get(BASE + "/api/settlement.csv")
    def api_settlement_csv(month: str | None = None, user: dict = Depends(current_user)):
        month = month or now_jst().strftime("%Y-%m")
        return _csv(get_store().settlement(month, scope_email(user, None)), f"settlement-{month}.csv")

    @app.get(BASE + "/api/contracts")
    def api_contracts(user: dict = Depends(current_user)):
        version, doc = get_store().contracts()
        return {"version": version, **doc}

    @app.put(BASE + "/api/contracts")
    async def api_put_contracts(request: Request, user: dict = Depends(check_csrf)):
        need(user, "admin")
        payload = await _json(request)
        doc = {"schema": 1, "contracts": payload.get("contracts") or []}
        errors = validate_contracts(doc)
        if errors:
            raise HTTPException(400, " / ".join(errors))
        return _save(get_store(), "contracts", doc, payload.get("base_version"), user, "contract_update")

    @app.get(BASE + "/api/fx-rates")
    def api_fx(user: dict = Depends(current_user)):
        version, doc = get_store().fx_rates()
        return {"version": version, **doc}

    @app.put(BASE + "/api/fx-rates/{month}")
    async def api_put_fx(month: str, request: Request, user: dict = Depends(check_csrf)):
        need(user, "admin")
        payload = await _json(request)
        try:
            date.fromisoformat(month + "-01")
            rate = float(payload["usd_jpy"])
            assert rate > 0
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "月（YYYY-MM）とレート（正の数）を確かめてください")
        store = get_store()
        version, doc = store.fx_rates()
        doc = {"schema": 1, "usd_jpy": {**(doc.get("usd_jpy") or {}), month: rate}}
        return _save(store, "fx_rates", doc, payload.get("base_version", version), user, "fx_rate_update")

    @app.get(BASE + "/api/compactor")
    def api_compactor(user: dict = Depends(current_user)):
        need(user, "admin")
        return get_store().compactor_state()

    def _save(store: Store, kind: str, doc: dict, base_version: str | None, user: dict, action: str) -> dict:
        current, before = (store.contracts() if kind == "contracts" else store.fx_rates())
        key = store.save_versioned(kind, doc)
        conflict = store.previous_version(kind, key) != current or (base_version or None) != current
        store.write_access("admin", {"actor_email": user["email"], "action": action, "before": before,
                                     "after": doc, "base_version": base_version, "version": key,
                                     "conflict": conflict})
        return {"version": key, "conflict": conflict}

    # ---- 画面 ----
    def render(request: Request, name: str, user: dict, **ctx) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, {"base": BASE, "user": user, "csrf": request.session.get("csrf"),
                                                          "today": now_jst().date().isoformat(), **ctx})

    def pages(name: str, template: str, roles: tuple[str, ...] = ("user", "auditor", "admin")):
        @app.get(BASE + name, response_class=HTMLResponse, name=template)
        def _page(request: Request, user: dict | None = Depends(page_user)):
            if not user:
                return RedirectResponse(BASE + "/login")
            if user["role"] not in roles:
                raise HTTPException(403, "権限がありません")
            return render(request, template, user, query=dict(request.query_params))
        return _page

    pages("/", "usage.html")
    pages("/requests", "requests.html")
    pages("/request", "request.html")
    pages("/settlement", "settlement.html")
    pages("/views", "views.html", ("auditor", "admin"))
    pages("/admin", "admin.html", ("admin",))
    return app


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    return data if isinstance(data, dict) else {}


def _csv(rows: list[dict], filename: str) -> Response:
    import csv
    import io
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return Response(("﻿" + buf.getvalue()).encode(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run("audit.viewer:app", host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
