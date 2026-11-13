import csv
import hmac
import io
import os
import re
import secrets
import time
from decimal import Decimal, InvalidOperation
from functools import wraps

import psycopg2
import psycopg2.errors
import psycopg2.extras
from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ["SECRET_KEY"],
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=8 * 3600,
)

WRITE_DB_URL = os.environ["WRITE_DB_URL"]
READ_DB_URL = os.environ["READ_DB_URL"]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
IMAGE_RE = re.compile(r"^(/static/[\w./-]+|https?://\S+)$")

INTENTS = {
    "buy": "Сатып алуға ниетім бар",
    "recommend": "Достарыма ұсынамын",
    "info": "Қосымша ақпарат керек",
    "none": "Әзірге қызықпаймын",
}

SORTS = {
    "new": "created_at DESC",
    "price_asc": "price ASC NULLS LAST",
    "price_desc": "price DESC NULLS LAST",
    "name": "name ASC",
}

TRANS = {
    "а": "a", "ә": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d",
    "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "қ": "k", "л": "l", "м": "m", "н": "n", "ң": "n", "о": "o", "ө": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ұ": "u", "ү": "u",
    "ф": "f", "х": "kh", "һ": "h", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "і": "i", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


# ---------------------------------------------------------------- дерекқор

def run(url, sql, params=(), fetch=None):
    conn = psycopg2.connect(url, connect_timeout=4)
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return cur.rowcount
    finally:
        conn.close()


def W(sql, params=(), fetch=None):
    """Жазу және маңызды оқулар: PRIMARY сервер"""
    return run(WRITE_DB_URL, sql, params, fetch)


def R(sql, params=(), fetch=None):
    """Ашық оқу: STANDBY сервер"""
    return run(READ_DB_URL, sql, params, fetch)


# ---------------------------------------------------------------- қауіпсіздік

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


@app.context_processor
def inject_globals():
    return {
        "csrf_token": csrf_token,
        "github_url": os.environ.get("GITHUB_URL", "#"),
        "INTENTS": INTENTS,
    }


@app.template_filter("money")
def money(value):
    if value is None:
        return "Келісім бойынша"
    return f"{int(value):,}".replace(",", " ") + " ₸"


@app.template_filter("dt")
def fmt_dt(value):
    return value.strftime("%d.%m.%Y %H:%M") if value else ""


@app.template_filter("paragraphs")
def paragraphs(text):
    return [p.strip() for p in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n")) if p.strip()]


@app.template_filter("lines")
def lines(text):
    return [l.strip() for l in (text or "").replace("\r\n", "\n").split("\n") if l.strip()]


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(500)
def http_error(err):
    code = getattr(err, "code", 500)
    texts = {
        400: "Сұраныс қате немесе қауіпсіздік белгісі ескірген. Бетті жаңартып, қайталаңыз.",
        403: "Бұл бетке кіруге құқығыңыз жоқ.",
        404: "Бет табылмады.",
        500: "Сервер қатесі. Кейінірек қайталап көріңіз.",
    }
    return render_template("error.html", code=code, text=texts.get(code, "Қате")), code


# ---------------------------------------------------------------- көмекші

def slugify(text):
    out = "".join(TRANS.get(ch, ch) for ch in text.lower())
    return re.sub(r"[^a-z0-9]+", "-", out).strip("-")[:140]


def parse_decimal(value, lo, hi, places):
    try:
        d = Decimal(value.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None
    if not d.is_finite() or not (lo <= d <= hi):
        return None
    return d.quantize(Decimal(1).scaleb(-places))


def parse_product(form):
    f = {k: form.get(k, "").strip() for k in
         ("name", "slug", "summary", "price", "image_url", "audience", "price_model", "contact")}
    f["description"] = form.get("description", "").replace("\r\n", "\n").strip()
    f["features"] = form.get("features", "").replace("\r\n", "\n").strip()
    f["is_active"] = form.get("is_active") == "on"
    errors = []
    if not 2 <= len(f["name"]) <= 140:
        errors.append("Атауы 2–140 таңба болуы керек.")
    if not f["slug"]:
        f["slug"] = slugify(f["name"])
    if not SLUG_RE.match(f["slug"]):
        errors.append("Slug тек латын әріптері, сандар және сызықшадан тұруы керек.")
    if not 10 <= len(f["summary"]) <= 300:
        errors.append("Қысқаша сипаттама 10–300 таңба болуы керек.")
    if len(f["description"]) < 80:
        errors.append("Толық сипаттама кемінде 80 таңба болуы керек (екі абзац).")
    if f["price"]:
        price = parse_decimal(f["price"], Decimal(0), Decimal(10) ** 10 - 1, 2)
        if price is None:
            errors.append("Баға оң сан болуы керек.")
        f["price"] = price
    else:
        f["price"] = None
    if f["image_url"] and not IMAGE_RE.match(f["image_url"]):
        errors.append("Сурет жолы /static/... немесе http(s):// түрінде болуы керек.")
    if len(f["audience"]) > 240 or len(f["price_model"]) > 120 or len(f["contact"]) > 160:
        errors.append("Аудитория, баға моделі немесе байланыс өрісі тым ұзын.")
    return f, errors


def parse_office(form):
    f = {k: form.get(k, "").strip() for k in ("name", "address", "latitude", "longitude", "phone", "email")}
    errors = []
    if not 2 <= len(f["name"]) <= 120:
        errors.append("Кеңсе атауы 2–120 таңба болуы керек.")
    if not 3 <= len(f["address"]) <= 240:
        errors.append("Мекенжай 3–240 таңба болуы керек.")
    lat = parse_decimal(f["latitude"], Decimal(-90), Decimal(90), 6)
    lon = parse_decimal(f["longitude"], Decimal(-180), Decimal(180), 6)
    if lat is None:
        errors.append("Ендік -90 мен 90 аралығындағы сан болуы керек.")
    if lon is None:
        errors.append("Бойлық -180 мен 180 аралығындағы сан болуы керек.")
    if len(f["phone"]) > 40:
        errors.append("Телефон 40 таңбадан аспауы керек.")
    if f["email"] and (len(f["email"]) > 160 or not EMAIL_RE.match(f["email"])):
        errors.append("Email дұрыс емес.")
    f["latitude"], f["longitude"] = lat, lon
    f["phone"] = f["phone"] or None
    f["email"] = f["email"] or None
    return f, errors


def replica_confirms(product_id, name, active, tries=6):
    """Асинхронды репликация: өзгеріс standby-да көрінгенше қысқа күтеміз"""
    for _ in range(tries):
        try:
            row = R("SELECT name, is_active FROM products WHERE id = %s", (product_id,), "one")
        except psycopg2.Error:
            return None
        if row and row["name"] == name and row["is_active"] == active:
            return True
        time.sleep(0.3)
    return False


def flash_replication(product_id, name, active, verb):
    ok = replica_confirms(product_id, name, active)
    msg = f"Өнім {verb}: Primary серверінде расталды (id={product_id})."
    if ok is True:
        flash(msg + " Standby серверінде де көрінді.", "success")
    elif ok is False:
        flash(msg + " Standby әлі жаңарған жоқ (асинхронды репликация кідірісі), ашық бетте сәл кейін көрінеді.", "info")
    else:
        flash(msg + " Standby қолжетімсіз, ашық каталог уақытша жаңармайды.", "info")


# ---------------------------------------------------------------- ашық бөлім

@app.route("/")
def index():
    featured, db_error = [], False
    try:
        featured = R("SELECT slug, name, summary, price, image_url FROM products "
                     "WHERE is_active ORDER BY created_at DESC LIMIT 3", fetch="all")
    except psycopg2.Error:
        db_error = True
    return render_template("index.html", featured=featured, db_error=db_error)


@app.route("/register", methods=["GET", "POST"])
def register():
    form = {}
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        form = {"full_name": full_name, "email": email}
        errors = []
        if not 2 <= len(full_name) <= 120:
            errors.append("Аты-жөні 2–120 таңба болуы керек.")
        if len(email) > 160 or not EMAIL_RE.match(email):
            errors.append("Email дұрыс емес.")
        if not 8 <= len(password) <= 128:
            errors.append("Пароль 8–128 таңба болуы керек.")
        if password != confirm:
            errors.append("Парольдер сәйкес емес.")
        if not errors:
            try:
                # role формадан алынбайды: әдепкі 'user'
                W("INSERT INTO users (email, password_hash, full_name) VALUES (%s, %s, %s)",
                  (email, generate_password_hash(password), full_name))
                flash("Тіркелу сәтті өтті. Енді жүйеге кіріңіз.", "success")
                return redirect(url_for("login"))
            except psycopg2.errors.UniqueViolation:
                errors.append("Бұл email бұрын тіркелген.")
            except psycopg2.Error:
                errors.append("Primary серверіне қосылу мүмкін болмады. Кейінірек қайталаңыз.")
        for e in errors:
            flash(e, "error")
    return render_template("register.html", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        try:
            user = W("SELECT id, password_hash, full_name, role FROM users WHERE email = %s",
                     (email,), "one")
        except psycopg2.Error:
            flash("Primary серверіне қосылу мүмкін болмады.", "error")
            return render_template("login.html", email=email)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            return redirect(safe_next(request.args.get("next")))
        flash("Email немесе пароль қате", "error")
        return render_template("login.html", email=email)
    return render_template("login.html", email="")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    if request.method == "POST":
        session.clear()
        flash("Жүйеден шықтыңыз.", "success")
        return redirect(url_for("index"))
    return render_template("logout.html")


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    try:
        user = W("SELECT id, email, full_name, role, created_at FROM users WHERE id = %s",
                 (session["user_id"],), "one")
    except psycopg2.Error:
        flash("Primary серверіне қосылу мүмкін болмады.", "error")
        return redirect(url_for("index"))
    if user is None:
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        current = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        errors = []
        if not 2 <= len(full_name) <= 120:
            errors.append("Аты-жөні 2–120 таңба болуы керек.")
        new_hash = None
        if new_pw:
            if not 8 <= len(new_pw) <= 128:
                errors.append("Жаңа пароль 8–128 таңба болуы керек.")
            full = W("SELECT password_hash FROM users WHERE id = %s", (user["id"],), "one")
            if not check_password_hash(full["password_hash"], current):
                errors.append("Ағымдағы пароль қате.")
            new_hash = generate_password_hash(new_pw)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            try:
                if new_hash:
                    W("UPDATE users SET full_name = %s, password_hash = %s WHERE id = %s",
                      (full_name, new_hash, user["id"]))
                else:
                    W("UPDATE users SET full_name = %s WHERE id = %s", (full_name, user["id"]))
                session["full_name"] = full_name
                flash("Профиль Primary серверінде жаңартылды.", "success")
                return redirect(url_for("profile"))
            except psycopg2.Error:
                flash("Профильді сақтау мүмкін болмады.", "error")
        user["full_name"] = full_name
    return render_template("profile.html", user=user)


@app.route("/products")
def products():
    q = request.args.get("q", "").strip()[:80]
    sort = request.args.get("sort", "new")
    order = SORTS.get(sort, SORTS["new"])
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    items, db_error = [], False
    try:
        items = R(
            "SELECT id, slug, name, summary, price, image_url FROM products "
            "WHERE is_active AND (%s = '' OR name ILIKE %s OR summary ILIKE %s) "
            "ORDER BY " + order,  # order тек SORTS сөздігінен алынады
            (q, pattern, pattern), "all")
    except psycopg2.Error:
        db_error = True
    return render_template("products.html", products=items, q=q, sort=sort, db_error=db_error)


@app.route("/products/<slug>")
def product_detail(slug):
    try:
        product = R("SELECT id, slug, name, summary, description, price, image_url, features, "
                    "audience, price_model, contact FROM products WHERE slug = %s AND is_active",
                    (slug,), "one")
        stats = None
        if product:
            stats = R("SELECT COUNT(*) AS n, ROUND(AVG(rating), 1) AS avg FROM survey_responses "
                      "WHERE product_id = %s", (product["id"],), "one")
    except psycopg2.Error:
        return render_template("error.html", code=503,
                               text="Каталог уақытша қолжетімсіз: Standby серверіне қосылу мүмкін болмады."), 503
    if not product:
        abort(404)
    return render_template("product_detail.html", product=product, stats=stats)


@app.route("/survey", methods=["GET", "POST"])
def survey():
    if request.method == "POST":
        if "user_id" not in session:
            return redirect(url_for("login", next="/survey"))
        product_id = request.form.get("product_id", type=int)
        rating = request.form.get("rating", type=int)
        intent = request.form.get("intent", "")
        comment = request.form.get("comment", "").strip()
        errors = []
        if not product_id:
            errors.append("Өнімді таңдаңыз.")
        if rating is None or not 1 <= rating <= 5:
            errors.append("Баға 1 мен 5 аралығында болуы керек.")
        if intent not in INTENTS:
            errors.append("Ниетіңізді таңдаңыз.")
        if len(comment) > 1000:
            errors.append("Пікір 1000 таңбадан аспауы керек.")
        if not errors:
            try:
                W("INSERT INTO survey_responses (user_id, product_id, rating, intent, comment) "
                  "VALUES (%s, %s, %s, %s, %s)",
                  (session["user_id"], product_id, rating, intent, comment or None))
                flash("Сауалнама жауабы Primary серверінде сақталды. Рақмет!", "success")
                return redirect(url_for("survey"))
            except psycopg2.errors.ForeignKeyViolation:
                errors.append("Таңдалған өнім табылмады.")
            except psycopg2.Error:
                errors.append("Primary серверіне қосылу мүмкін болмады.")
        for e in errors:
            flash(e, "error")

    items, db_error = [], False
    try:
        items = R("SELECT id, slug, name FROM products WHERE is_active ORDER BY name", fetch="all")
    except psycopg2.Error:
        db_error = True
    selected = request.args.get("product", "")
    return render_template("survey.html", products=items, selected=selected, db_error=db_error)


@app.route("/contacts")
def contacts():
    offices, db_error = [], False
    try:
        offices = R("SELECT id, name, address, latitude, longitude, phone, email "
                    "FROM offices ORDER BY id", fetch="all")
    except psycopg2.Error:
        db_error = True
    selected = None
    wanted = request.args.get("office", type=int)
    for o in offices:
        if o["id"] == wanted:
            selected = o
    if selected is None and offices:
        selected = offices[0]
    map_src = None
    if selected:
        lat, lon = float(selected["latitude"]), float(selected["longitude"])
        map_src = ("https://www.openstreetmap.org/export/embed.html?bbox="
                   f"{lon - 0.012},{lat - 0.007},{lon + 0.012},{lat + 0.007}"
                   f"&layer=mapnik&marker={lat},{lon}")
    return render_template("contacts.html", offices=offices, selected=selected,
                           map_src=map_src, db_error=db_error)


@app.route("/health")
def health():
    out = {"primary": {"ok": False}, "standby": {"ok": False}}
    try:
        row = W("SELECT pg_is_in_recovery() AS in_recovery", fetch="one")
        out["primary"] = {"ok": True, "in_recovery": row["in_recovery"]}
        out["replication"] = [dict(r) for r in W(
            "SELECT state, sync_state, EXTRACT(EPOCH FROM replay_lag)::float AS replay_lag_s "
            "FROM pg_stat_replication", fetch="all")]
    except psycopg2.Error:
        pass
    try:
        row = R("SELECT pg_is_in_recovery() AS in_recovery", fetch="one")
        out["standby"] = {"ok": True, "in_recovery": row["in_recovery"]}
    except psycopg2.Error:
        pass
    code = 200 if out["primary"]["ok"] and out["standby"]["ok"] else 503
    return jsonify(out), code


# ---------------------------------------------------------------- админ бөлімі

@app.route("/admin")
@admin_required
def admin_home():
    stats = {}
    try:
        stats = W("SELECT (SELECT COUNT(*) FROM users) AS users, "
                  "(SELECT COUNT(*) FROM products) AS products, "
                  "(SELECT COUNT(*) FROM offices) AS offices, "
                  "(SELECT COUNT(*) FROM survey_responses) AS surveys", fetch="one")
    except psycopg2.Error:
        flash("Primary серверіне қосылу мүмкін болмады.", "error")
    return render_template("admin.html", stats=stats)


@app.route("/admin/products", methods=["GET", "POST"])
@admin_required
def admin_products():
    form, editing, data = {}, None, {}
    if request.method == "POST":
        action = request.form.get("action")
        pid = request.form.get("id", type=int)
        try:
            if action in ("create", "update"):
                data, errors = parse_product(request.form)
                if errors:
                    for e in errors:
                        flash(e, "error")
                    form, editing = data, pid
                else:
                    params = (data["slug"], data["name"], data["summary"], data["description"],
                              data["price"], data["image_url"] or None, data["features"],
                              data["audience"], data["price_model"], data["contact"],
                              data["is_active"])
                    if action == "create":
                        row = W("INSERT INTO products (slug, name, summary, description, price, "
                                "image_url, features, audience, price_model, contact, is_active) "
                                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                                params, "one")
                        verb = "жасалды"
                    else:
                        row = W("UPDATE products SET slug=%s, name=%s, summary=%s, description=%s, "
                                "price=%s, image_url=%s, features=%s, audience=%s, price_model=%s, "
                                "contact=%s, is_active=%s WHERE id=%s RETURNING id",
                                params + (pid,), "one")
                        verb = "жаңартылды"
                        if row is None:
                            abort(404)
                    flash_replication(row["id"], data["name"], data["is_active"], verb)
                    return redirect(url_for("admin_products"))
            elif action == "toggle":
                row = W("UPDATE products SET is_active = NOT is_active WHERE id = %s "
                        "RETURNING id, name, is_active", (pid,), "one")
                if row is None:
                    abort(404)
                flash_replication(row["id"], row["name"], row["is_active"],
                                  "жарияланды" if row["is_active"] else "жасырылды")
                return redirect(url_for("admin_products"))
            else:
                abort(400)
        except psycopg2.errors.UniqueViolation:
            flash("Бұл slug бұрыннан бар. Басқа slug таңдаңыз.", "error")
            form, editing = data, pid
        except psycopg2.Error:
            flash("Primary серверіне қосылу мүмкін болмады.", "error")
            form, editing = data, pid
    else:
        edit_id = request.args.get("edit", type=int)
        if edit_id:
            try:
                row = W("SELECT * FROM products WHERE id = %s", (edit_id,), "one")
                if row:
                    form, editing = row, edit_id
            except psycopg2.Error:
                flash("Primary серверіне қосылу мүмкін болмады.", "error")
    items = []
    try:
        items = W("SELECT id, slug, name, price, is_active, created_at FROM products "
                  "ORDER BY id DESC", fetch="all")
    except psycopg2.Error:
        flash("Өнімдер тізімін оқу мүмкін болмады.", "error")
    return render_template("admin_products.html", products=items, form=form, editing=editing)


@app.route("/admin/products/<int:pid>/delete", methods=["GET", "POST"])
@admin_required
def admin_product_delete(pid):
    try:
        product = W("SELECT id, name FROM products WHERE id = %s", (pid,), "one")
    except psycopg2.Error:
        flash("Primary серверіне қосылу мүмкін болмады.", "error")
        return redirect(url_for("admin_products"))
    if product is None:
        abort(404)
    if request.method == "POST":
        try:
            W("DELETE FROM products WHERE id = %s", (pid,))
            flash(f"Өнім «{product['name']}» Primary серверінен жойылды.", "success")
        except psycopg2.Error:
            flash("Жою мүмкін болмады.", "error")
        return redirect(url_for("admin_products"))
    return render_template(
        "admin_confirm.html", what="өнімді", title=product["name"],
        warning="Бұл өнімге байланысты барлық сауалнама жауаптары да жойылады.",
        action=url_for("admin_product_delete", pid=pid), back=url_for("admin_products"))


@app.route("/admin/offices", methods=["GET", "POST"])
@admin_required
def admin_offices():
    form, editing = {}, None
    if request.method == "POST":
        action = request.form.get("action")
        oid = request.form.get("id", type=int)
        if action not in ("create", "update"):
            abort(400)
        data, errors = parse_office(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            form, editing = data, oid
        else:
            params = (data["name"], data["address"], data["latitude"], data["longitude"],
                      data["phone"], data["email"])
            try:
                if action == "create":
                    W("INSERT INTO offices (name, address, latitude, longitude, phone, email) "
                      "VALUES (%s,%s,%s,%s,%s,%s)", params)
                    flash("Кеңсе Primary серверіне қосылды.", "success")
                else:
                    if W("UPDATE offices SET name=%s, address=%s, latitude=%s, longitude=%s, "
                         "phone=%s, email=%s WHERE id=%s", params + (oid,)) == 0:
                        abort(404)
                    flash("Кеңсе Primary серверінде жаңартылды.", "success")
                return redirect(url_for("admin_offices"))
            except psycopg2.Error:
                flash("Primary серверіне қосылу мүмкін болмады.", "error")
                form, editing = data, oid
    else:
        edit_id = request.args.get("edit", type=int)
        if edit_id:
            try:
                row = W("SELECT * FROM offices WHERE id = %s", (edit_id,), "one")
                if row:
                    form, editing = row, edit_id
            except psycopg2.Error:
                flash("Primary серверіне қосылу мүмкін болмады.", "error")
    items = []
    try:
        items = W("SELECT * FROM offices ORDER BY id", fetch="all")
    except psycopg2.Error:
        flash("Кеңселер тізімін оқу мүмкін болмады.", "error")
    return render_template("admin_offices.html", offices=items, form=form, editing=editing)


@app.route("/admin/offices/<int:oid>/delete", methods=["GET", "POST"])
@admin_required
def admin_office_delete(oid):
    try:
        office = W("SELECT id, name FROM offices WHERE id = %s", (oid,), "one")
    except psycopg2.Error:
        flash("Primary серверіне қосылу мүмкін болмады.", "error")
        return redirect(url_for("admin_offices"))
    if office is None:
        abort(404)
    if request.method == "POST":
        try:
            W("DELETE FROM offices WHERE id = %s", (oid,))
            flash(f"Кеңсе «{office['name']}» жойылды.", "success")
        except psycopg2.Error:
            flash("Жою мүмкін болмады.", "error")
        return redirect(url_for("admin_offices"))
    return render_template("admin_confirm.html", what="кеңсені", title=office["name"], warning="",
                           action=url_for("admin_office_delete", oid=oid),
                           back=url_for("admin_offices"))


@app.route("/admin/users")
@admin_required
def admin_users():
    """password_hash таңдалмайды, сондықтан беттен көрінбейді"""
    items = []
    try:
        items = W("SELECT u.id, u.email, u.full_name, u.role, u.created_at, "
                  "(SELECT COUNT(*) FROM survey_responses s WHERE s.user_id = u.id) AS surveys "
                  "FROM users u ORDER BY u.id", fetch="all")
    except psycopg2.Error:
        flash("Primary серверіне қосылу мүмкін болмады.", "error")
    return render_template("admin_users.html", users=items)


SURVEY_SQL = (
    "SELECT s.id, s.created_at, s.rating, s.intent, s.comment, p.name AS product, "
    "u.full_name AS user_name, u.email AS user_email "
    "FROM survey_responses s JOIN products p ON p.id = s.product_id "
    "LEFT JOIN users u ON u.id = s.user_id "
    "WHERE (%(pid)s::bigint IS NULL OR s.product_id = %(pid)s) "
    "AND (%(rating)s::int IS NULL OR s.rating = %(rating)s) "
    "ORDER BY s.id DESC"
)


def survey_filters():
    pid = request.args.get("product", type=int)
    rating = request.args.get("rating", type=int)
    if rating is not None and not 1 <= rating <= 5:
        rating = None
    return {"pid": pid, "rating": rating}


@app.route("/admin/surveys")
@admin_required
def admin_surveys():
    flt = survey_filters()
    rows, product_list, db_error = [], [], False
    try:
        rows = R(SURVEY_SQL, flt, "all")  # талдамалық оқу: STANDBY
        product_list = R("SELECT id, name FROM products ORDER BY name", fetch="all")
    except psycopg2.Error:
        db_error = True
    return render_template("admin_surveys.html", surveys=rows, product_list=product_list,
                           flt=flt, db_error=db_error)


@app.route("/admin/surveys/export.csv")
@admin_required
def admin_surveys_csv():
    try:
        rows = R(SURVEY_SQL, survey_filters(), "all")
    except psycopg2.Error:
        abort(503)

    def safe(value):
        text = "" if value is None else str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@") else text

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "created_at", "product", "user", "email", "rating", "intent", "comment"])
    for r in rows:
        writer.writerow([r["id"], r["created_at"], safe(r["product"]), safe(r["user_name"]),
                         safe(r["user_email"]), r["rating"], INTENTS.get(r["intent"], r["intent"]),
                         safe(r["comment"])])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=survey_responses.csv"})


@app.route("/admin/surveys/<int:sid>/delete", methods=["GET", "POST"])
@admin_required
def admin_survey_delete(sid):
    try:
        row = W("SELECT s.id, p.name AS product FROM survey_responses s "
                "JOIN products p ON p.id = s.product_id WHERE s.id = %s", (sid,), "one")
    except psycopg2.Error:
        flash("Primary серверіне қосылу мүмкін болмады.", "error")
        return redirect(url_for("admin_surveys"))
    if row is None:
        abort(404)
    if request.method == "POST":
        try:
            W("DELETE FROM survey_responses WHERE id = %s", (sid,))
            flash("Жауап Primary серверінен жойылды.", "success")
        except psycopg2.Error:
            flash("Жою мүмкін болмады.", "error")
        return redirect(url_for("admin_surveys"))
    return render_template("admin_confirm.html", what="сауалнама жауабын",
                           title=f"№{row['id']} ({row['product']})", warning="",
                           action=url_for("admin_survey_delete", sid=sid),
                           back=url_for("admin_surveys"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)