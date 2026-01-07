import os
import sqlite3
import uuid
from datetime import datetime

from flask import Flask, g, jsonify, render_template, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "marketplace.db")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH", str(10 * 1024 * 1024)))


def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    db = sqlite3.connect(DB_PATH)
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY
            )
            """
        )
        db.commit()

        def applied_versions():
            rows = db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            return {r[0] for r in rows}

        def apply(version: int, fn):
            if version in applied_versions():
                return
            fn()
            db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
            db.commit()

        def migration_v1():
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

        def migration_v2():
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS sellers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    whatsapp TEXT NOT NULL,
                    pin_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sellers_whatsapp
                ON sellers(whatsapp)
                """
            )

        def migration_v3():
            cols = db.execute("PRAGMA table_info(products)").fetchall()
            colnames = {c[1] for c in cols}
            if "seller_id" not in colnames:
                db.execute("ALTER TABLE products ADD COLUMN seller_id INTEGER")
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_products_seller_id
                ON products(seller_id)
                """
            )

        def migration_v4():
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS product_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_product_images_product_id
                ON product_images(product_id)
                """
            )

        apply(1, migration_v1)
        apply(2, migration_v2)
        apply(3, migration_v3)
        apply(4, migration_v4)

    finally:
        db.close()


def now_utc():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def normalize_whatsapp(s: str) -> str:
    s = (s or "").strip().replace(" ", "")
    # Keep leading + if present, otherwise digits only.
    if s.startswith("+"):
        rest = "".join(ch for ch in s[1:] if ch.isdigit())
        return "+" + rest
    return "".join(ch for ch in s if ch.isdigit())


def current_seller_id():
    sid = session.get("seller_id")
    return int(sid) if isinstance(sid, int) or (isinstance(sid, str) and sid.isdigit()) else None


def get_current_seller_row():
    sid = current_seller_id()
    if not sid:
        return None
    db = get_db()
    return db.execute(
        "SELECT id, name, whatsapp, created_at FROM sellers WHERE id=?",
        (sid,),
    ).fetchone()


ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _sniff_and_get_ext(file_storage):
    # Validate image with Pillow; returns safe extension.
    from PIL import Image

    file_storage.stream.seek(0)
    img = Image.open(file_storage.stream)
    img.verify()
    fmt = (img.format or "").upper()

    if fmt == "JPEG":
        ext = ".jpg"
    elif fmt == "PNG":
        ext = ".png"
    elif fmt == "WEBP":
        ext = ".webp"
    else:
        raise ValueError("Unsupported image format")

    file_storage.stream.seek(0)
    return ext


def save_upload_image(file_storage) -> str:
    ext = _sniff_and_get_ext(file_storage)
    if ext not in ALLOWED_IMAGE_EXTS:
        raise ValueError("Unsupported image type")

    filename = f"{uuid.uuid4().hex}{ext}"
    abs_path = os.path.join(UPLOAD_DIR, filename)

    # Ensure we never overwrite (extremely unlikely, but safe)
    while os.path.exists(abs_path):
        filename = f"{uuid.uuid4().hex}{ext}"
        abs_path = os.path.join(UPLOAD_DIR, filename)

    file_storage.save(abs_path)
    return filename


def product_to_dict(row, images_by_product_id=None):
    d = dict(row)

    seller = None
    if d.get("seller_id"):
        seller = {
            "id": d.get("seller_id"),
            "name": d.get("seller_name"),
            "whatsapp": d.get("seller_whatsapp"),
        }
    d["seller"] = seller

    pid = d.get("id")
    imgs = (images_by_product_id or {}).get(pid, [])
    d["images"] = [f"/uploads/{fn}" for fn in imgs]

    # remove join helper fields
    d.pop("seller_name", None)
    d.pop("seller_whatsapp", None)

    return d


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/api/sellers/me")
def sellers_me():
    row = get_current_seller_row()
    if not row:
        return jsonify({"error": "not logged in"}), 401
    return jsonify(dict(row))


@app.post("/api/sellers/register")
def sellers_register():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    whatsapp = normalize_whatsapp(data.get("whatsapp") or "")
    pin = (data.get("pin") or "").strip()

    if len(name) < 2 or len(name) > 60:
        return jsonify({"error": "Invalid name"}), 400
    if not whatsapp or len(whatsapp) < 10:
        return jsonify({"error": "Invalid WhatsApp"}), 400
    if not pin.isdigit() or not (4 <= len(pin) <= 8):
        return jsonify({"error": "PIN must be 4-8 digits"}), 400

    db = get_db()
    created_at = now_utc()
    pin_hash = generate_password_hash(pin)

    try:
        cur = db.execute(
            "INSERT INTO sellers (name, whatsapp, pin_hash, created_at) VALUES (?, ?, ?, ?)",
            (name, whatsapp, pin_hash, created_at),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "WhatsApp already registered"}), 409

    seller_id = cur.lastrowid
    session["seller_id"] = seller_id

    row = db.execute(
        "SELECT id, name, whatsapp, created_at FROM sellers WHERE id=?",
        (seller_id,),
    ).fetchone()
    return jsonify(dict(row)), 201


@app.post("/api/sellers/login")
def sellers_login():
    data = request.get_json(silent=True) or {}
    whatsapp = normalize_whatsapp(data.get("whatsapp") or "")
    pin = (data.get("pin") or "").strip()

    if not whatsapp or not pin:
        return jsonify({"error": "whatsapp and pin required"}), 400

    db = get_db()
    row = db.execute(
        "SELECT id, name, whatsapp, pin_hash, created_at FROM sellers WHERE whatsapp=?",
        (whatsapp,),
    ).fetchone()
    if not row:
        return jsonify({"error": "Invalid credentials"}), 401

    if not check_password_hash(row["pin_hash"], pin):
        return jsonify({"error": "Invalid credentials"}), 401

    session["seller_id"] = row["id"]
    return jsonify({"id": row["id"], "name": row["name"], "whatsapp": row["whatsapp"], "created_at": row["created_at"]})


@app.post("/api/sellers/logout")
def sellers_logout():
    session.pop("seller_id", None)
    return jsonify({"ok": True})


@app.get("/api/products")
def list_products():
    db = get_db()

    rows = db.execute(
        """
        SELECT
            p.id,
            p.name,
            p.price,
            p.details,
            p.created_at,
            p.seller_id,
            s.name AS seller_name,
            s.whatsapp AS seller_whatsapp
        FROM products p
        LEFT JOIN sellers s ON s.id = p.seller_id
        ORDER BY p.id DESC
        """
    ).fetchall()

    product_ids = [r["id"] for r in rows]
    images_by_pid = {}
    if product_ids:
        qmarks = ",".join(["?"] * len(product_ids))
        img_rows = db.execute(
            f"SELECT product_id, filename FROM product_images WHERE product_id IN ({qmarks}) ORDER BY sort_order ASC, id ASC",
            product_ids,
        ).fetchall()
        for ir in img_rows:
            images_by_pid.setdefault(ir["product_id"], []).append(ir["filename"])

    return jsonify([product_to_dict(r, images_by_pid) for r in rows])


@app.delete("/api/products/<int:product_id>")
def delete_product(product_id: int):
    seller_id = current_seller_id()
    if not seller_id:
        return jsonify({"error": "login required"}), 401

    db = get_db()
    row = db.execute(
        "SELECT id, seller_id FROM products WHERE id=?",
        (product_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    if row["seller_id"] != seller_id:
        return jsonify({"error": "forbidden"}), 403

    img_rows = db.execute(
        "SELECT filename FROM product_images WHERE product_id=?",
        (product_id,),
    ).fetchall()
    filenames = [r["filename"] for r in img_rows]

    db.execute("DELETE FROM products WHERE id=?", (product_id,))
    db.commit()

    # Best-effort file cleanup
    for fn in filenames:
        try:
            os.remove(os.path.join(UPLOAD_DIR, fn))
        except OSError:
            pass

    return jsonify({"ok": True})


@app.post("/api/products")
def create_product():
    seller_id = current_seller_id()
    if not seller_id:
        return jsonify({"error": "login required"}), 401

    # Multipart form expected for uploads
    name = (request.form.get("name") or "").strip()
    details = (request.form.get("details") or "").strip()

    price_raw = request.form.get("price")
    try:
        price = float(price_raw)
    except (TypeError, ValueError):
        price = None

    if not name or price is None or price < 0 or not details:
        return (
            jsonify(
                {
                    "error": "Invalid input",
                    "fields": {
                        "name": "required",
                        "price": "number >= 0",
                        "details": "required",
                    },
                }
            ),
            400,
        )

    files = request.files.getlist("images")
    if files and len(files) > 5:
        return jsonify({"error": "Max 5 images allowed"}), 400

    db = get_db()
    created_at = now_utc()

    cur = db.execute(
        "INSERT INTO products (name, price, details, created_at, seller_id) VALUES (?, ?, ?, ?, ?)",
        (name, price, details, created_at, seller_id),
    )
    db.commit()

    product_id = cur.lastrowid

    saved_filenames = []
    try:
        for i, f in enumerate(files or []):
            if not f or not getattr(f, "filename", ""):
                continue
            filename = save_upload_image(f)
            saved_filenames.append(filename)
            db.execute(
                "INSERT INTO product_images (product_id, filename, sort_order, created_at) VALUES (?, ?, ?, ?)",
                (product_id, filename, i, created_at),
            )
        db.commit()
    except ValueError as e:
        # cleanup product row + any saved files
        db.execute("DELETE FROM products WHERE id=?", (product_id,))
        db.commit()
        for fn in saved_filenames:
            try:
                os.remove(os.path.join(UPLOAD_DIR, fn))
            except OSError:
                pass
        return jsonify({"error": str(e)}), 400

    row = db.execute(
        """
        SELECT
            p.id,
            p.name,
            p.price,
            p.details,
            p.created_at,
            p.seller_id,
            s.name AS seller_name,
            s.whatsapp AS seller_whatsapp
        FROM products p
        LEFT JOIN sellers s ON s.id = p.seller_id
        WHERE p.id=?
        """,
        (product_id,),
    ).fetchone()

    images_by_pid = {product_id: saved_filenames}
    return jsonify(product_to_dict(row, images_by_pid)), 201


# Basic customer-care chatbot endpoint.
# By default uses a simple rule-based fallback.
# If you set ANTHROPIC_API_KEY in env, it will use Anthropic.
@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message required"}), 400

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        try:
            # Lazy import so app still works without the package.
            from anthropic import Anthropic

            client = Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
                max_tokens=300,
                temperature=0.3,
                system=(
                    "You are a professional customer support agent for an online marketplace. "
                    "Answer briefly and helpfully in Hinglish/Urdu if the user writes that way. "
                    "If asked to purchase, instruct them to use the Buy button to contact via WhatsApp."
                ),
                messages=[{"role": "user", "content": message}],
            )
            text = resp.content[0].text if resp.content else ""
            return jsonify({"reply": text or "Sorry, I couldn't generate a response."})
        except Exception:
            # Fall back silently
            pass

    # Fallback
    lower = message.lower()
    if "price" in lower or "rates" in lower:
        reply = "Aap product cards par price dekh sakte hain. Jo pasand aaye us par 'Buy on WhatsApp' dabayein."
    elif "delivery" in lower or "ship" in lower:
        reply = "Delivery details seller WhatsApp par confirm karega. Buy button se contact karein."
    elif "refund" in lower or "return" in lower:
        reply = "Return/Refund policy seller se WhatsApp par confirm hoti hai. Product details share kar dein."
    else:
        reply = "Ji bilkul—main help kar deta hoon. Aap apna sawal detail me batayein ya product select karke Buy on WhatsApp use karein."

    return jsonify({"reply": reply})


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
