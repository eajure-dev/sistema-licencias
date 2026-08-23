import os, sqlite3, secrets, smtplib, hashlib, uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import Flask, request, redirect, url_for, render_template, flash, jsonify, session, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from transbank.common.options import WebpayOptions
from transbank.common.integration_type import IntegrationType
from transbank.webpay.webpay_plus.transaction import Transaction

BASE = os.path.abspath(os.path.dirname(__file__))
DATA = os.path.join(BASE, "data")
UPLOADS = os.path.join(BASE, "uploads")
os.makedirs(DATA, exist_ok=True)
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("APP_SECRET", "CAMBIAR-ESTA-CLAVE-" + secrets.token_hex(16))
DB = os.path.join(DATA, "licencias.db")

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def q(sql, params=(), one=False):
    db = conn()
    cur = db.execute(sql, params)
    rows = cur.fetchall()
    db.commit()
    db.close()
    return (rows[0] if rows else None) if one else rows

def execute(sql, params=()):
    db = conn()
    cur = db.execute(sql, params)
    db.commit()
    ident = cur.lastrowid
    db.close()
    return ident


def setting(key, default=""):
    row = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return row["value"] if row and row["value"] is not None else default

def get_webpay_transaction():
    environment = setting("webpay_environment", "integration").lower()
    if environment == "production":
        env = IntegrationType.LIVE
    else:
        env = IntegrationType.TEST
    commerce_code = setting("webpay_commerce_code")
    api_key = setting("webpay_api_key")
    if not commerce_code or not api_key:
        raise RuntimeError("Webpay no está configurado. Ingresa código de comercio y API Key.")
    return Transaction(WebpayOptions(commerce_code, api_key, env))

def public_base_url():
    return setting("public_base_url", request.url_root.rstrip("/")).rstrip("/")

def now():
    return datetime.utcnow()

def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def generate_code():
    return "POS-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))

def init_db():
    db = conn()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        name TEXT,
        business TEXT,
        phone TEXT,
        rut TEXT,
        address TEXT,
        role TEXT DEFAULT 'client',
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS plans(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price INTEGER DEFAULT 0,
        duration_days INTEGER DEFAULT 30,
        machines INTEGER DEFAULT 1,
        updates INTEGER DEFAULT 1,
        features TEXT DEFAULT '',
        active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS licenses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan_id INTEGER,
        code TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'pending',
        starts_at TEXT,
        expires_at TEXT,
        price INTEGER DEFAULT 0,
        machines_allowed INTEGER DEFAULT 1,
        updates_allowed INTEGER DEFAULT 1,
        version_limit TEXT DEFAULT '',
        machine_id TEXT,
        created_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        license_id INTEGER,
        amount INTEGER,
        provider TEXT,
        reference TEXT UNIQUE,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        paid_at TEXT
    );
    CREATE TABLE IF NOT EXISTS versions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version TEXT NOT NULL,
        channel TEXT DEFAULT 'stable',
        notes TEXT,
        filename TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    db.commit()
    db.close()
    if not q("SELECT id FROM users WHERE role='admin'", one=True):
        execute("INSERT INTO users(email,password,name,role,created_at) VALUES(?,?,?,?,?)",
                ("admin@local", generate_password_hash("admin123"), "Administrador", "admin", iso(now())))
    if not q("SELECT id FROM plans", one=True):
        execute("INSERT INTO plans(name,price,duration_days,machines,updates,features) VALUES(?,?,?,?,?,?)",
                ("Mensual", 9990, 30, 1, 1, "POS,Inventario,Reportes"))

class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.email = row["email"]
        self.role = row["role"]
        self.name = row["name"] or ""
    @property
    def is_admin(self):
        return self.role == "admin"

@login_manager.user_loader
def load_user(uid):
    row = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return User(row) if row else None

def admin_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Acceso solo para administrador.", "danger")
            return redirect(url_for("dashboard"))
        return f(*a, **kw)
    return wrapped

def send_email(to, subject, body):
    host = q("SELECT value FROM settings WHERE key='smtp_host'", one=True)
    if not host:
        return False
    port = q("SELECT value FROM settings WHERE key='smtp_port'", one=True)
    user = q("SELECT value FROM settings WHERE key='smtp_user'", one=True)
    password = q("SELECT value FROM settings WHERE key='smtp_password'", one=True)
    sender = q("SELECT value FROM settings WHERE key='smtp_from'", one=True)
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender["value"] if sender else user["value"]
        msg["To"] = to
        msg.set_content(body)
        with smtplib.SMTP(host["value"], int(port["value"] if port else 587)) as s:
            s.starttls()
            if user and password:
                s.login(user["value"], password["value"])
            s.send_message(msg)
        return True
    except Exception as e:
        print("EMAIL ERROR:", e)
        return False

def create_license(user_id, plan_id, activate=False):
    plan = q("SELECT * FROM plans WHERE id=?", (plan_id,), one=True)
    if not plan:
        raise ValueError("Plan inexistente")
    start = now()
    expires = start + timedelta(days=int(plan["duration_days"]))
    code = generate_code()
    while q("SELECT id FROM licenses WHERE code=?", (code,), one=True):
        code = generate_code()
    lid = execute("""INSERT INTO licenses
        (user_id,plan_id,code,status,starts_at,expires_at,price,machines_allowed,updates_allowed,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (user_id, plan_id, code, "active" if activate else "pending",
         iso(start), iso(expires), plan["price"], plan["machines"], plan["updates"], iso(now())))
    return q("SELECT * FROM licenses WHERE id=?", (lid,), one=True)

def activate_payment(reference, provider="manual"):
    pay = q("SELECT * FROM payments WHERE reference=?", (reference,), one=True)
    if not pay or pay["status"] == "paid":
        return False
    execute("UPDATE payments SET status='paid', paid_at=? WHERE id=?", (iso(now()), pay["id"]))
    if pay["license_id"]:
        lic = q("SELECT * FROM licenses WHERE id=?", (pay["license_id"],), one=True)
        plan = q("SELECT * FROM plans WHERE id=?", (lic["plan_id"],), one=True)
        base = now()
        if lic["expires_at"]:
            old = datetime.strptime(lic["expires_at"], "%Y-%m-%d %H:%M:%S")
            if old > base: base = old
        execute("UPDATE licenses SET status='active', starts_at=?, expires_at=? WHERE id=?",
                (iso(now()), iso(base + timedelta(days=int(plan["duration_days"]))), lic["id"]))
    return True

@app.route("/")
def index():
    plans = q("SELECT * FROM plans WHERE active=1")
    return render_template("index.html", plans=plans)

@app.route("/register/<int:plan_id>", methods=["GET","POST"])
def register(plan_id):
    plan = q("SELECT * FROM plans WHERE id=? AND active=1", (plan_id,), one=True)
    if not plan:
        flash("Plan no disponible.", "danger"); return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        if q("SELECT id FROM users WHERE email=?", (email,), one=True):
            flash("Ese correo ya está registrado. Inicia sesión para renovar.", "warning")
            return redirect(url_for("login"))
        uid = execute("""INSERT INTO users(email,password,name,business,phone,rut,address,role,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
            (email, generate_password_hash(request.form["password"]), request.form["name"],
             request.form["business"], request.form["phone"], request.form.get("rut",""),
             request.form.get("address",""), "client", iso(now())))
        lic = create_license(uid, plan_id, activate=False)
        ref = "PAY-" + uuid.uuid4().hex[:12].upper()
        execute("INSERT INTO payments(user_id,license_id,amount,provider,reference,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (uid, lic["id"], plan["price"], "pending", ref, "pending", iso(now())))
        session["pending_reference"] = ref
        session["pending_email"] = email
        return redirect(url_for("checkout", reference=ref))
    return render_template("register.html", plan=plan)

@app.route("/checkout/<reference>")
def checkout(reference):
    pay = q("""SELECT p.*, pl.name plan_name, u.name,u.email,l.code FROM payments p
             JOIN licenses l ON l.id=p.license_id JOIN plans pl ON pl.id=l.plan_id
             JOIN users u ON u.id=p.user_id WHERE p.reference=?""", (reference,), one=True)
    if not pay: return "Pago no encontrado", 404
    # Si Webpay está configurado, se crea la transacción real.
    try:
        tx = get_webpay_transaction()
        buy_order = ("LIC" + str(pay["id"]) + str(int(now().timestamp())))[-26:]
        return_url = public_base_url() + url_for("webpay_commit")
        response = tx.create(buy_order, pay["reference"], int(pay["amount"]), return_url)
        execute("UPDATE payments SET provider=?, reference=? WHERE id=?",
                ("webpay", pay["reference"], pay["id"]))
        return render_template("checkout.html", payment=pay, webpay_url=response["url"],
                               token=response["token"], buy_order=buy_order, webpay_ready=True)
    except Exception as e:
        return render_template("checkout.html", payment=pay, webpay_ready=False,
                               webpay_error=str(e))


@app.route("/webpay/commit", methods=["GET", "POST"])
def webpay_commit():
    # Transbank retorna token_ws; si el pago fue anulado puede llegar TBK_TOKEN.
    token = request.values.get("token_ws")
    if not token:
        return render_template("payment_result.html", ok=False,
                               message="El pago fue cancelado o no se recibió un token válido.")
    try:
        tx = get_webpay_transaction()
        response = tx.commit(token)
        buy_order = response.get("buy_order")
        session_id = response.get("session_id")
        amount = int(response.get("amount", 0))
        response_code = response.get("response_code")

        pay = q("SELECT * FROM payments WHERE reference=?", (session_id,), one=True)
        if not pay:
            return render_template("payment_result.html", ok=False,
                                   message="No encontramos la orden de pago.")

        # Se activa únicamente después de confirmar directamente con Transbank.
        if response_code == 0 and amount == int(pay["amount"]):
            activate_payment(session_id, "webpay")
            user = q("SELECT * FROM users WHERE id=?", (pay["user_id"],), one=True)
            lic = q("SELECT * FROM licenses WHERE id=?", (pay["license_id"],), one=True)
            body = f"""Hola {user['name']},

Tu pago con Webpay fue confirmado correctamente.

Código de activación: {lic['code']}
Licencia válida hasta: {lic['expires_at']}

Instrucciones:
1. Descarga e instala el programa.
2. Ábrelo y selecciona Activar licencia.
3. Ingresa el código anterior.
4. Mantén conexión a internet durante la primera activación.

También puedes ingresar a tu portal de cliente para revisar tu licencia y futuras actualizaciones.
"""
            send_email(user["email"], "Pago confirmado y licencia activada", body)
            return render_template("payment_result.html", ok=True, payment=pay, license=lic)
        return render_template("payment_result.html", ok=False,
                               message=f"El pago no fue autorizado. Código de respuesta: {response_code}")
    except Exception as e:
        return render_template("payment_result.html", ok=False,
                               message=f"No fue posible confirmar el pago: {e}")

@app.route("/demo/pay/<reference>", methods=["POST"])
def demo_pay(reference):
    if activate_payment(reference, "demo"):
        pay = q("SELECT * FROM payments WHERE reference=?", (reference,), one=True)
        user = q("SELECT * FROM users WHERE id=?", (pay["user_id"],), one=True)
        lic = q("SELECT * FROM licenses WHERE id=?", (pay["license_id"],), one=True)
        body = f"""Hola {user['name']},

Tu pago fue confirmado correctamente.

Código de activación: {lic['code']}
Licencia válida hasta: {lic['expires_at']}

Instrucciones:
1. Instala o abre el programa.
2. Selecciona Activar licencia.
3. Ingresa el código.
4. Conecta el equipo a internet durante la activación.

Guarda este correo para futuras consultas.
"""
        send_email(user["email"], "Pago confirmado y código de activación", body)
    return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        row = q("SELECT * FROM users WHERE email=?", (request.form["email"].strip().lower(),), one=True)
        if row and check_password_hash(row["password"], request.form["password"]):
            login_user(User(row))
            return redirect(url_for("dashboard"))
        flash("Correo o contraseña incorrectos.", "danger")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user(); return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.is_admin:
        stats = {
            "clients": q("SELECT COUNT(*) n FROM users WHERE role='client'", one=True)["n"],
            "active": q("SELECT COUNT(*) n FROM licenses WHERE status='active' AND expires_at>?", (iso(now()),), one=True)["n"],
            "payments": q("SELECT COUNT(*) n FROM payments WHERE status='paid'", one=True)["n"],
        }
        licenses = q("""SELECT l.*,u.business,u.name,u.email,pl.name plan FROM licenses l
                      JOIN users u ON u.id=l.user_id LEFT JOIN plans pl ON pl.id=l.plan_id
                      ORDER BY l.id DESC LIMIT 20""")
        return render_template("admin_dashboard.html", stats=stats, licenses=licenses)
    licenses = q("""SELECT l.*,pl.name plan FROM licenses l LEFT JOIN plans pl ON pl.id=l.plan_id
                  WHERE l.user_id=? ORDER BY l.id DESC""", (current_user.id,))
    versions = q("SELECT * FROM versions ORDER BY id DESC LIMIT 10")
    return render_template("client_dashboard.html", licenses=licenses, versions=versions)

@app.route("/admin/plans", methods=["GET","POST"])
@login_required
@admin_required
def admin_plans():
    if request.method == "POST":
        execute("INSERT INTO plans(name,price,duration_days,machines,updates,features,active) VALUES(?,?,?,?,?,?,1)",
                (request.form["name"], int(request.form["price"]), int(request.form["duration"]),
                 int(request.form["machines"]), 1 if request.form.get("updates") else 0, request.form.get("features","")))
        flash("Plan creado.", "success")
    return render_template("plans.html", plans=q("SELECT * FROM plans ORDER BY id DESC"))


@app.route("/admin/invite", methods=["GET", "POST"])
@login_required
@admin_required
def admin_invite():
    plans = q("SELECT * FROM plans WHERE active=1 ORDER BY name")
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        name = request.form["name"].strip()
        business = request.form.get("business", "").strip()
        plan_id = int(request.form["plan_id"])
        free_days = max(1, int(request.form["free_days"]))

        user = q("SELECT * FROM users WHERE email=?", (email,), one=True)
        if user:
            uid = user["id"]
        else:
            temp_password = secrets.token_urlsafe(12)
            uid = execute("""INSERT INTO users(email,password,name,business,phone,role,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                          (email, generate_password_hash(temp_password), name, business, "", "client", iso(now())))

        plan = q("SELECT * FROM plans WHERE id=?", (plan_id,), one=True)
        code = generate_code()
        while q("SELECT id FROM licenses WHERE code=?", (code,), one=True):
            code = generate_code()

        start = now()
        expires = start + timedelta(days=free_days)
        lid = execute("""INSERT INTO licenses
            (user_id,plan_id,code,status,starts_at,expires_at,price,machines_allowed,updates_allowed,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (uid, plan_id, code, "active", iso(start), iso(expires), 0,
             plan["machines"], plan["updates"], iso(now())))

        download_url = public_base_url() + url_for("client_download_program")
        body = f"""Hola {name},

¡Has sido invitado a utilizar Ventas POS gratis!

Negocio: {business or "Cliente"}
Plan: {plan["name"]}
Duración gratuita: {free_days} días
Válida hasta: {iso(expires)}

Tu código de activación es:
{code}

DESCARGA DEL PROGRAMA:
{download_url}

Instrucciones:
1. Descarga el programa desde el enlace anterior.
2. Instálalo o ejecútalo.
3. Abre Ventas POS.
4. Ingresa tu código de activación.
5. El programa quedará asociado al equipo autorizado.

Disfruta tu período gratuito.
"""
        sent = send_email(email, "🎁 Invitación gratuita a Ventas POS", body)
        flash("Invitación creada. " + ("Correo enviado correctamente." if sent else "La licencia fue creada, pero configura SMTP para enviar el correo."), "success")
        return redirect(url_for("admin_license", license_id=lid))
    return render_template("invite.html", plans=plans)

@app.route("/admin/clients")
@login_required
@admin_required
def admin_clients():
    clients = q("""SELECT u.*,l.id AS license_id,l.code,l.status,l.expires_at,l.price,pl.name plan FROM users u
                 LEFT JOIN licenses l ON l.id=(SELECT id FROM licenses WHERE user_id=u.id ORDER BY id DESC LIMIT 1)
                 LEFT JOIN plans pl ON pl.id=l.plan_id WHERE u.role='client' ORDER BY u.id DESC""")
    return render_template("clients.html", clients=clients)

@app.route("/admin/license/<int:license_id>", methods=["GET","POST"])
@login_required
@admin_required
def admin_license(license_id):
    lic = q("""SELECT l.*,u.name,u.email,u.business,pl.name plan FROM licenses l
             JOIN users u ON u.id=l.user_id LEFT JOIN plans pl ON pl.id=l.plan_id WHERE l.id=?""", (license_id,), one=True)
    if not lic: return "Licencia no encontrada", 404
    if request.method == "POST":
        expires = request.form["expires_at"].replace("T"," ") + ":00" if "T" in request.form["expires_at"] else request.form["expires_at"]
        execute("""UPDATE licenses SET status=?,price=?,expires_at=?,machines_allowed=?,updates_allowed=?,version_limit=?
                 WHERE id=?""",
                (request.form["status"], int(request.form["price"]), expires, int(request.form["machines"]),
                 1 if request.form.get("updates") else 0, request.form.get("version_limit",""), license_id))
        flash("Licencia actualizada.", "success")
        return redirect(url_for("admin_license", license_id=license_id))
    return render_template("license_edit.html", lic=lic)


@app.route("/admin/license/<int:license_id>/add-days", methods=["POST"])
@login_required
@admin_required
def admin_add_days(license_id):
    lic = q("SELECT * FROM licenses WHERE id=?", (license_id,), one=True)
    if not lic:
        return "Licencia no encontrada", 404
    days = max(1, int(request.form["days"]))
    base = now()
    if lic["expires_at"]:
        current_expiry = datetime.strptime(lic["expires_at"], "%Y-%m-%d %H:%M:%S")
        if current_expiry > base:
            base = current_expiry
    new_expiry = base + timedelta(days=days)
    execute("UPDATE licenses SET expires_at=?, status='active' WHERE id=?",
            (iso(new_expiry), license_id))
    flash(f"Se agregaron {days} días. Nuevo vencimiento: {iso(new_expiry)}", "success")
    return redirect(url_for("admin_license", license_id=license_id))

@app.route("/admin/versions", methods=["GET","POST"])
@login_required
@admin_required
def admin_versions():
    if request.method == "POST":
        f = request.files.get("file")
        filename = ""
        if f and f.filename:
            filename = secure_filename(f.filename)
            f.save(os.path.join(UPLOADS, filename))
        execute("INSERT INTO versions(version,channel,notes,filename,created_at) VALUES(?,?,?,?,?)",
                (request.form["version"], request.form["channel"], request.form.get("notes",""), filename, iso(now())))
        if request.form.get("set_as_program") and filename:
            execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("program_filename", filename))
        flash("Archivo publicado.", "success")
    return render_template("versions.html", versions=q("SELECT * FROM versions ORDER BY id DESC"))


@app.route("/programa")
@login_required
def client_download_program():
    lic = q("""SELECT * FROM licenses WHERE user_id=? AND status='active'
               ORDER BY expires_at DESC LIMIT 1""", (current_user.id,), one=True)
    if not lic or datetime.strptime(lic["expires_at"], "%Y-%m-%d %H:%M:%S") < now():
        flash("No tienes una licencia activa para descargar el programa.", "danger")
        return redirect(url_for("dashboard"))

    filename = setting("program_filename")
    if not filename or not os.path.isfile(os.path.join(UPLOADS, filename)):
        flash("El programa todavía no ha sido publicado para descarga.", "warning")
        return redirect(url_for("dashboard"))
    return send_from_directory(UPLOADS, filename, as_attachment=True)

@app.route("/download/<path:filename>")
@login_required
def download(filename):
    return send_from_directory(UPLOADS, filename, as_attachment=True)

@app.route("/admin/settings", methods=["GET","POST"])
@login_required
@admin_required
def admin_settings():
    if request.method == "POST":
        for k in ["smtp_host","smtp_port","smtp_user","smtp_password","smtp_from","webpay_environment","webpay_commerce_code","webpay_api_key","public_base_url"]:
            execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, request.form.get(k,"")))
        flash("Configuración guardada.", "success")
    values = {r["key"]:r["value"] for r in q("SELECT * FROM settings")}
    return render_template("settings.html", values=values)

@app.route("/api/license/activate", methods=["POST"])
def api_activate():
    d = request.get_json(force=True)
    code = d.get("code","").strip().upper()
    machine = d.get("machine_id","").strip()
    lic = q("SELECT * FROM licenses WHERE code=?", (code,), one=True)
    if not lic: return jsonify(ok=False, error="Código inválido"), 404
    if lic["status"] != "active" or datetime.strptime(lic["expires_at"], "%Y-%m-%d %H:%M:%S") < now():
        return jsonify(ok=False, error="Licencia inactiva o vencida"), 403
    if lic["machine_id"] and lic["machine_id"] != machine:
        return jsonify(ok=False, error="Licencia vinculada a otro equipo"), 403
    execute("UPDATE licenses SET machine_id=? WHERE id=?", (machine, lic["id"]))
    return jsonify(ok=True, message="Licencia activada", expires_at=lic["expires_at"])

@app.route("/api/license/check", methods=["POST"])
def api_check():
    d = request.get_json(force=True)
    lic = q("SELECT * FROM licenses WHERE code=?", (d.get("code","").strip().upper(),), one=True)
    if not lic: return jsonify(valid=False, reason="not_found"), 404
    valid = lic["status"]=="active" and datetime.strptime(lic["expires_at"], "%Y-%m-%d %H:%M:%S") >= now()
    if valid and lic["machine_id"] and lic["machine_id"] != d.get("machine_id",""):
        valid=False
    latest = q("SELECT * FROM versions ORDER BY id DESC LIMIT 1", one=True)
    return jsonify(valid=valid, status=lic["status"], expires_at=lic["expires_at"],
                   updates_allowed=bool(lic["updates_allowed"]),
                   version_limit=lic["version_limit"], latest_version=(latest["version"] if latest else None))

@app.route("/api/payments/webhook", methods=["POST"])
def payment_webhook():
    # Conectar aquí la verificación oficial de firma del proveedor elegido.
    d = request.get_json(silent=True) or request.form.to_dict()
    reference = d.get("reference")
    status = str(d.get("status","")).lower()
    if not reference: return jsonify(ok=False, error="missing_reference"), 400
    if status in ("paid","approved","success"):
        ok = activate_payment(reference, d.get("provider","webhook"))
        return jsonify(ok=ok)
    return jsonify(ok=True, ignored=True)

if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="127.0.0.1", port=5000)
