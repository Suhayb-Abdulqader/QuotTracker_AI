"""QuotTracker FastAPI application."""

import io
import logging
import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

import auth
import db
from ai_helper import AIServiceError, answer_search, extract_from_document, extract_text_from_pdf
from sync import run_sync


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("quottracker")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024
ALLOWED_UPLOAD_TYPES = {".xlsx", ".xls", ".csv", ".pdf"}
ALLOWED_HOSTS_SETTING = os.getenv("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [
    host.strip()
    for host in (
        ALLOWED_HOSTS_SETTING or "localhost,127.0.0.1,testserver"
    ).split(",")
    if host.strip()
]
if auth.APP_ENV == "production" and (
    not ALLOWED_HOSTS_SETTING.strip()
    or not ALLOWED_HOSTS
    or "*" in ALLOWED_HOSTS
    or "testserver" in ALLOWED_HOSTS
):
    raise RuntimeError("Set ALLOWED_HOSTS to explicit public hostnames in production.")

@asynccontextmanager
async def lifespan(application: FastAPI):
    db.init_db()
    application.state.database_ready = True
    try:
        yield
    finally:
        application.state.database_ready = False


app = FastAPI(
    title="QuotTracker",
    version="3.1",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.SESSION_SECRET,
    session_cookie=(
        "__Host-quottracker_session" if auth.COOKIE_SECURE else "quottracker_session"
    ),
    max_age=auth.SESSION_MAX_AGE,
    same_site="strict",
    https_only=auth.COOKIE_SECURE,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = None
    if request.url.path == "/api/upload" and request.method == "POST":
        length = request.headers.get("content-length")
        if length:
            try:
                too_large = int(length) > MAX_REQUEST_BYTES
            except ValueError:
                response = JSONResponse(
                    status_code=400, content={"detail": "Invalid Content-Length."}
                )
            else:
                if too_large:
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": "Upload request is too large."},
                    )
    if response is None:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        f"script-src 'self' 'nonce-{getattr(request.state, 'csp_nonce', '')}'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    if auth.COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def authenticated() -> Depends:
    return Depends(auth.require_authenticated)


def csrf_protected() -> Depends:
    return Depends(auth.require_csrf)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


@app.get("/", response_class=HTMLResponse)
def serve_frontend(request: Request):
    nonce = secrets.token_urlsafe(18)
    request.state.csp_nonce = nonce
    html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace("__CSP_NONCE__", nonce)
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/auth/status")
def auth_status(request: Request):
    if not request.session.get("authenticated"):
        return {"authenticated": False}
    token = request.session.get("csrf_token")
    if not token:
        token = auth.new_csrf_token()
        request.session["csrf_token"] = token
    return {"authenticated": True, "csrf_token": token}


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request):
    auth.ensure_login_not_limited(request)
    if not auth.credentials_match(body.username, body.password):
        auth.record_login_failure(request)
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    auth.clear_login_failures(request)
    request.session.clear()
    request.session["authenticated"] = True
    request.session["csrf_token"] = auth.new_csrf_token()
    return {
        "authenticated": True,
        "csrf_token": request.session["csrf_token"],
    }


@app.post("/api/auth/logout", dependencies=[authenticated(), csrf_protected()])
def logout(request: Request):
    request.session.clear()
    return {"authenticated": False}


@app.get("/api/health", include_in_schema=False)
def health(request: Request):
    if not getattr(request.app.state, "database_ready", False):
        raise HTTPException(status_code=503, detail="Database is not ready.")
    conn = db.get_db()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/api/stats", dependencies=[authenticated()])
def stats():
    conn = db.get_db()
    try:
        requests = db.get_all_requests(conn)
        tracker = db.get_all_tracker(conn)
        month_start = datetime.now().replace(day=1).strftime("%Y-%m-%d")
        synced = db.count_audit_actions_since(conn, month_start, ("INSERT", "UPDATE"))
    finally:
        conn.close()

    tracker_nos = {row["request_no"] for row in tracker}
    missing = sum(1 for row in requests if row["request_no"] not in tracker_nos)
    incomplete = sum(
        1 for row in tracker if not row.get("vendor_po") or row.get("amount") is None
    )
    total_value = sum(float(row.get("amount") or 0) for row in tracker)
    return {
        "open_requests": len(requests),
        "need_attention": missing + incomplete,
        "synced_this_month": synced,
        "estimated_value": total_value,
    }


@app.get("/api/activity", dependencies=[authenticated()])
def activity():
    today = date.today()
    first_day = today - timedelta(days=6)
    conn = db.get_db()
    try:
        counts = db.get_activity_counts(conn, first_day.isoformat())
    finally:
        conn.close()
    return [
        {
            "day": (first_day + timedelta(days=offset)).strftime("%a"),
            "count": counts.get(
                (first_day + timedelta(days=offset)).isoformat(), 0
            ),
        }
        for offset in range(7)
    ]


@app.get("/api/attention", dependencies=[authenticated()])
def attention():
    conn = db.get_db()
    try:
        requests = db.get_all_requests(conn)
        tracker = db.get_all_tracker(conn)
    finally:
        conn.close()

    tracker_nos = {row["request_no"] for row in tracker}
    missing_po = sum(1 for row in tracker if not row.get("vendor_po"))
    not_in_tracker = sum(
        1 for row in requests if row["request_no"] not in tracker_nos
    )
    total = missing_po + not_in_tracker
    items = []
    if total:
        items.append({
            "type": "missing_po",
            "title": f"{total} request{'s' if total != 1 else ''} need attention",
            "description": "Requests are missing a vendor PO or tracker entry.",
            "count": total,
        })
    return {"items": items, "total": total}


@app.get("/api/requests", dependencies=[authenticated()])
def requests_list(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    search: str = Query(default="", max_length=200),
):
    conn = db.get_db()
    try:
        tracker = db.get_all_tracker(conn)
        requests = db.get_all_requests(conn)
        logs = db.get_audit_log(conn, limit=5000)
    finally:
        conn.close()

    log_map: dict[str, dict] = {}
    for log in logs:
        request_no = log.get("request_no")
        if request_no and request_no not in log_map:
            log_map[request_no] = log

    combined = []
    tracker_nos = {row["request_no"] for row in tracker}
    for row in tracker:
        request_no = row.get("request_no", "")
        log = log_map.get(request_no, {})
        raw_updated = log.get("created_at") or row.get("updated_at", "")
        combined.append({
            **row,
            "vendor": row.get("brand") or "—",
            "updated": _format_updated(raw_updated),
            "source": _display_source(log.get("source", "sync")),
            "_sort_updated": raw_updated,
        })
    for row in requests:
        if row["request_no"] not in tracker_nos:
            raw_updated = row.get("updated_at") or ""
            combined.append({
                **row,
                "vendor": row.get("brand") or "—",
                "updated": _format_updated(raw_updated),
                "amount": None,
                "status": "Missing",
                "source": _display_source(row.get("source", "sheet")),
                "_sort_updated": raw_updated,
            })

    if search:
        query = search.casefold()
        combined = [
            row for row in combined
            if any(query in str(value).casefold() for key, value in row.items()
                   if value and not key.startswith("_"))
        ]
    combined.sort(
        key=lambda row: (
            row.get("status") != "Missing",
            row.get("_sort_updated", ""),
        )
    )
    total = len(combined)
    page = combined[offset:offset + limit]
    for row in page:
        row.pop("_sort_updated", None)
    return {"total": total, "items": page}


def _format_updated(raw: str) -> str:
    try:
        stamp = datetime.fromisoformat(raw[:19])
    except (TypeError, ValueError):
        return (raw or "")[:10]
    today = datetime.now().date()
    if stamp.date() == today:
        return f"Today, {stamp.strftime('%H:%M')}"
    if stamp.date() == today - timedelta(days=1):
        return "Yesterday"
    return stamp.strftime("%b %d, %Y")


def _display_source(source: str) -> str:
    return {"pdf": "PDF", "email": "Email", "manual": "Manual"}.get(
        source, "Sheet"
    )


@app.get("/api/requests/{request_no}", dependencies=[authenticated()])
def request_detail(request_no: str):
    conn = db.get_db()
    try:
        row = db.get_tracker_row(conn, request_no)
        logs = db.get_audit_log(conn, request_no=request_no)
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    return {"request": row, "audit_log": logs}


@app.post(
    "/api/upload",
    dependencies=[authenticated(), csrf_protected()],
)
async def upload_file(file: UploadFile = File(...)):
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1][:200]
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Use .xlsx, .xls, .csv, or .pdf.",
        )

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
            temp_path = temp.name
            total_bytes = 0
            while chunk := await file.read(64 * 1024):
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="File is too large. The limit is 10 MB.",
                    )
                temp.write(chunk)

        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")

        if suffix == ".pdf":
            try:
                text = extract_text_from_pdf(temp_path)
                if not text.strip():
                    raise HTTPException(
                        status_code=422,
                        detail="This PDF contains no extractable text. OCR is not available.",
                    )
                extracted = extract_from_document(text, filename)
            except AIServiceError as exc:
                logger.warning("AI document extraction failed: %s", exc)
                raise HTTPException(
                    status_code=503,
                    detail="AI document extraction is unavailable. No data was saved.",
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            request_no = extracted.get("vendor_po") or extracted.get("quotation_no")
            if not request_no:
                raise HTTPException(
                    status_code=422,
                    detail="The document must contain a quotation number or vendor PO.",
                )
            request_no = request_no.strip()
            if len(request_no) > 128:
                raise HTTPException(
                    status_code=422,
                    detail="The extracted quotation/PO identifier is too long.",
                )

            request_row = {
                "request_no": request_no,
                "door_name": extracted.get("door_name") or filename,
                "quotation_no": extracted.get("quotation_no"),
                "vendor_po": extracted.get("vendor_po"),
                "brand": extracted.get("brand"),
                "request_date": extracted.get("request_date"),
                "installation_date": extracted.get("installation_date"),
            }
            tracker_row = {
                "Request No": request_no,
                "Door Name": request_row["door_name"],
                "Quotation No": request_row["quotation_no"],
                "Vendor PO": request_row["vendor_po"],
                "Brand": request_row["brand"],
                "Request Date": request_row["request_date"],
                "Installation Date": request_row["installation_date"],
                "Amount": extracted.get("amount"),
                "Status": "Pending",
            }
            conn = db.get_db()
            try:
                if db.get_tracker_row(conn, request_no):
                    raise HTTPException(
                        status_code=409,
                        detail="A tracker record with this quotation/PO already exists.",
                    )
                db.upsert_request(conn, request_row, source="pdf")
                if not db.insert_tracker_row(conn, tracker_row, source="pdf"):
                    raise HTTPException(
                        status_code=409, detail="The record already exists."
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return {
                "success": True,
                "type": "pdf",
                "message": f"PDF processed; extracted {len(extracted)} fields.",
                "extracted": extracted,
            }

        try:
            result = run_sync(temp_path)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Uploaded sheet processing failed")
            raise HTTPException(
                status_code=500,
                detail="The uploaded sheet could not be processed.",
            ) from exc
        return {"success": True, "type": "sheet", **result}
    finally:
        await file.close()
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


@app.get("/api/search", dependencies=[authenticated()])
def ai_search(q: str = Query(min_length=1, max_length=500)):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty.")
    conn = db.get_db()
    try:
        records = db.get_all_tracker(conn)
    finally:
        conn.close()
    if len(records) > 100:
        raise HTTPException(
            status_code=413,
            detail="AI Search is limited to 100 records at a time.",
        )
    try:
        result = answer_search(q, records)
    except AIServiceError as exc:
        logger.warning("AI search failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="AI search is unavailable. Please try again later.",
        ) from exc
    return {"query": q, "result": result}


@app.get("/api/export", dependencies=[authenticated()])
def export():
    conn = db.get_db()
    try:
        rows = db.get_all_tracker(conn)
    finally:
        conn.close()

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Tracker"
    sheet.append([
        "Request No", "Door Name", "Quotation No", "Vendor PO", "Brand",
        "Request Date", "Installation Date", "Amount", "Status",
        "SharePoint Folder",
    ])
    for row in rows:
        values = [
            row.get("request_no"), row.get("door_name"), row.get("quotation_no"),
            row.get("vendor_po"), row.get("brand"), row.get("request_date"),
            row.get("installation_date"), row.get("amount"), row.get("status"),
            row.get("sharepoint_folder"),
        ]
        sheet.append([_safe_spreadsheet_value(value) for value in values])

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": 'attachment; filename="tracker_export.xlsx"'},
    )


def _safe_spreadsheet_value(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value
