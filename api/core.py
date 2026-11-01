import io, json, os, smtplib
from datetime import date, datetime, timezone
from email.message import EmailMessage
from urllib import request as urlrequest

import redis
from PIL import Image, ImageFilter, ImageOps

Image.MAX_IMAGE_PIXELS = 30_000_000          # refuse decompression bombs
TYPES = ["License", "Insurance", "Subscription", "Passport", "Warranty", "Other"]
MAX_BYTES = 8 * 1024 * 1024
SIGNATURES = {b"%PDF": "application/pdf", b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg"}


def get_redis():
    return redis.Redis(host=os.getenv("REDIS_HOST", "redis"), port=6379,
                       decode_responses=True, socket_timeout=2)


def today():
    return datetime.now(timezone.utc).date()


# ---------- file storage: S3 in the cloud, a folder on your laptop ----------
class LocalStorage:
    def __init__(self, root):
        self.root = root

    def _path(self, key):
        path = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def put(self, key, data, content_type):
        with open(self._path(key), "wb") as f:
            f.write(data)

    def get(self, key):
        with open(self._path(key), "rb") as f:
            return f.read()

    def delete(self, key):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass


class S3Storage:
    def __init__(self, bucket):
        import boto3
        self.bucket = bucket
        self.s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "ap-south-1"))

    def put(self, key, data, content_type):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)


def get_storage():
    bucket = os.getenv("DOCS_BUCKET")
    return S3Storage(bucket) if bucket else LocalStorage(os.getenv("LOCAL_DIR", "/data"))


# ---------- documents ----------
def sniff(data):
    for sig, mime in SIGNATURES.items():
        if data.startswith(sig):
            return mime
    return None


def status_of(expiry, remind_days):
    days = (date.fromisoformat(expiry) - today()).days
    if days < 0:
        return days, "expired"
    return days, ("soon" if days <= remind_days else "ok")


def public(doc):
    days, status = status_of(doc["expiry"], int(doc["remind_days"]))
    return {"id": doc["id"], "title": doc["title"], "type": doc["type"],
            "filename": doc["filename"], "size": int(doc["size"]), "expiry": doc["expiry"],
            "remind_days": int(doc["remind_days"]), "days_left": days, "status": status,
            "enhanced": doc["enhanced"] == "1", "has_email": bool(doc.get("email"))}


def save_doc(r, doc):
    pipe = r.pipeline()
    pipe.hset(f"doc:{doc['id']}", mapping={k: str(v) for k, v in doc.items()})
    pipe.sadd(f"ws:{doc['ws']}", doc["id"])
    pipe.sadd("docs:all", doc["id"])
    pipe.execute()


def load_doc(r, doc_id):
    return r.hgetall(f"doc:{doc_id}") or None


def delete_doc(r, doc):
    pipe = r.pipeline()
    pipe.delete(f"doc:{doc['id']}")
    pipe.srem(f"ws:{doc['ws']}", doc["id"])
    pipe.srem("docs:all", doc["id"])
    pipe.execute()


def list_docs(r, ws):
    docs = [load_doc(r, i) for i in r.smembers(f"ws:{ws}")]
    return sorted((d for d in docs if d), key=lambda d: d["expiry"])


# ---------- the CPU-heavy part: scan clean-up ----------
def enhance(data):
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((1600, 1600), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)                         # fix faded scans
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


# ---------- reminders ----------
def reminder_stage(days, remind_days):
    """Which reminder is due now? One per threshold, so nobody gets daily spam."""
    if days < 0:
        return "expired"
    for t in sorted({remind_days, 7, 1, 0}):
        if t <= remind_days and days <= t:
            return str(t)
    return None


def describe(doc, days):
    if days < 0:
        when = f"expired {-days} day{'s' if days != -1 else ''} ago (on {doc['expiry']})"
    elif days == 0:
        when = f"expires today ({doc['expiry']})"
    else:
        when = f"expires in {days} day{'s' if days != 1 else ''} (on {doc['expiry']})"
    return f"{doc['title']} ({doc['type']}) {when}. Please renew it."


def _post_slack(hook, text):
    body = json.dumps({"text": text}).encode()
    req = urlrequest.Request(hook, body, {"Content-Type": "application/json"})
    try:
        urlrequest.urlopen(req, timeout=5).read()
        return True
    except Exception:
        return False


def _send_email(to, subject, text):
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER", "renewguard@localhost")
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(text)
    try:
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=10) as s:
            s.starttls()
            if os.getenv("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
        return True
    except Exception:
        return False


def notify(doc, days):
    """Send on every configured channel. Returns {channel: succeeded}."""
    text = describe(doc, days)
    results = {}
    if os.getenv("SLACK_WEBHOOK_URL"):
        results["slack"] = _post_slack(os.environ["SLACK_WEBHOOK_URL"], f":bell: *RenewGuard*: {text}")
    if doc.get("email") and os.getenv("SMTP_HOST"):
        results["email"] = _send_email(doc["email"], f"Renewal reminder: {doc['title']}", text)
    return results
