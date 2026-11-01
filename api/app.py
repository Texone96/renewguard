import os, re, time, uuid
from datetime import date

import redis
from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

import core

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = core.MAX_BYTES + 64 * 1024
CORS(app, expose_headers=["Content-Disposition"])
VERSION = os.getenv("APP_VERSION", "dev")
r = core.get_redis()
store = core.get_storage()

WS_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

REQS = Counter("renewguard_requests_total", "HTTP requests", ["endpoint", "status"])
LAT = Histogram("renewguard_request_seconds", "Request latency", ["endpoint"])
UPLOADS = Counter("renewguard_uploads_total", "Document uploads", ["result"])
ENH = Histogram("renewguard_enhance_seconds", "Scan clean-up time",
                buckets=(.05, .1, .2, .5, 1, 2, 5))
NOTIFS = Counter("renewguard_notifications_total", "Manual reminders", ["channel", "result"])
DOCS = Gauge("renewguard_documents", "Stored documents by status", ["status"])


@app.before_request
def _start():
    request._t = time.time()


@app.after_request
def _record(resp):
    if request.path != "/metrics":
        ep = "/api/documents/:id" if request.path.startswith("/api/documents/") else request.path
        LAT.labels(ep).observe(time.time() - request._t)
        REQS.labels(ep, str(resp.status_code)).inc()
    return resp


def ws_id():
    w = request.headers.get("X-Workspace") or request.args.get("ws", "")
    return w if WS_RE.match(w) else None


def owned(doc_id):
    ws = ws_id()
    if not ws:
        return None, (jsonify(error="missing workspace"), 400)
    doc = core.load_doc(r, doc_id)
    if not doc or doc["ws"] != ws:
        return None, (jsonify(error="not found"), 404)
    return doc, None


@app.route("/health")
def health():
    return jsonify(status="ok", version=VERSION, pod=os.getenv("HOSTNAME", "local"))


@app.route("/api/enhance", methods=["POST"])
def enhance_only():
    """Clean up an uploaded scan and return it. Stores nothing (also used for load tests)."""
    f = request.files.get("file")
    data = f.read() if f else b""
    if core.sniff(data) not in ("image/jpeg", "image/png"):
        return jsonify(error="JPEG or PNG image required"), 400
    t = time.time()
    try:
        out = core.enhance(data)
    except Exception:
        return jsonify(error="could not read image"), 400
    ENH.observe(time.time() - t)
    return Response(out, mimetype="image/jpeg")


@app.route("/api/documents", methods=["POST"])
def upload():
    ws = ws_id()
    if not ws:
        return jsonify(error="missing workspace"), 400
    form, f = request.form, request.files.get("file")
    try:
        title = form["title"].strip()[:60]
        dtype = form.get("type", "Other")
        expiry = date.fromisoformat(form["expiry"]).isoformat()
        remind = int(form.get("remind_days", 30))
        email = form.get("email", "").strip()[:120]
        assert title and dtype in core.TYPES and 1 <= remind <= 365 and f is not None
        assert not email or EMAIL_RE.match(email)
    except (KeyError, ValueError, AssertionError):
        UPLOADS.labels("invalid").inc()
        return jsonify(error="invalid form data"), 400
    data = f.read()
    mime = core.sniff(data)
    if mime is None:
        UPLOADS.labels("invalid").inc()
        return jsonify(error="PDF, JPEG or PNG files only"), 400
    name = re.sub(r"[^\w. -]", "", f.filename or "document")[:80] or "document"
    enhanced = False
    if form.get("enhance") == "1" and mime != "application/pdf":
        try:
            t = time.time()
            data, mime, enhanced = core.enhance(data), "image/jpeg", True
            ENH.observe(time.time() - t)
            name = os.path.splitext(name)[0] + ".jpg"
        except Exception:
            UPLOADS.labels("invalid").inc()
            return jsonify(error="could not read image"), 400
    doc_id = uuid.uuid4().hex[:12]
    doc = {"id": doc_id, "ws": ws, "title": title, "type": dtype, "filename": name, "mime": mime,
           "size": len(data), "expiry": expiry, "remind_days": remind, "email": email,
           "enhanced": "1" if enhanced else "0", "last_stage": ""}
    try:
        store.put(f"{ws}/{doc_id}", data, mime)
        core.save_doc(r, doc)
    except Exception:
        app.logger.exception("upload failed")
        UPLOADS.labels("error").inc()
        return jsonify(error="storage unavailable"), 503
    UPLOADS.labels("ok").inc()
    return jsonify(core.public(doc)), 201


@app.route("/api/documents")
def list_documents():
    ws = ws_id()
    if not ws:
        return jsonify(error="missing workspace"), 400
    try:
        return jsonify([core.public(d) for d in core.list_docs(r, ws)])
    except redis.RedisError:
        return jsonify(error="store unavailable"), 503


@app.route("/api/documents/<doc_id>/file")
def download(doc_id):
    doc, err = owned(doc_id)
    if err:
        return err
    try:
        data = store.get(f"{doc['ws']}/{doc['id']}")
    except Exception:
        return jsonify(error="file missing"), 404
    return Response(data, mimetype=doc["mime"],
                    headers={"Content-Disposition": f'inline; filename="{doc["filename"]}"'})


@app.route("/api/documents/<doc_id>", methods=["DELETE"])
def remove(doc_id):
    doc, err = owned(doc_id)
    if err:
        return err
    store.delete(f"{doc['ws']}/{doc['id']}")
    core.delete_doc(r, doc)
    return jsonify(ok=True)


@app.route("/api/documents/<doc_id>/renew", methods=["POST"])
def renew(doc_id):
    doc, err = owned(doc_id)
    if err:
        return err
    try:
        expiry = date.fromisoformat((request.get_json(silent=True) or {})["expiry"]).isoformat()
    except (KeyError, ValueError, TypeError):
        return jsonify(error="invalid date"), 400
    r.hset(f"doc:{doc_id}", mapping={"expiry": expiry, "last_stage": ""})
    return jsonify(core.public(core.load_doc(r, doc_id)))


@app.route("/api/documents/<doc_id>/remind", methods=["POST"])
def remind_now(doc_id):
    doc, err = owned(doc_id)
    if err:
        return err
    days, _ = core.status_of(doc["expiry"], int(doc["remind_days"]))
    results = core.notify(doc, days)
    for ch, ok in results.items():
        NOTIFS.labels(ch, "sent" if ok else "failed").inc()
    return jsonify(sent=results, configured=bool(results))


def refresh_gauge():
    counts = {"ok": 0, "soon": 0, "expired": 0}
    try:
        for i in r.smembers("docs:all"):
            d = core.load_doc(r, i)
            if d:
                counts[core.status_of(d["expiry"], int(d["remind_days"]))[1]] += 1
    except redis.RedisError:
        return
    for k, v in counts.items():
        DOCS.labels(k).set(v)


@app.route("/metrics")
def metrics():
    refresh_gauge()
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
