from flask import Flask, render_template, request, redirect, url_for, session, flash
import pymysql
from datetime import datetime

app = Flask(__name__)
app.secret_key = "secret_key_cua_ban"

# Giá tiền 5k / 1 giờ
COST_PER_HOUR = 5000
COST_PER_SECOND = COST_PER_HOUR / 3600.0


# ============================
# KẾT NỐI DATABASE
# ============================
def get_db_connection():
    conn = pymysql.connect(
        host="localhost",
        user="root",
        password="",
        database="netcafe",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )
    return conn


# ============================
# HÀM UPDATE THỜI GIAN - TRỪ TIỀN
# ============================
def update_user_time(user_id):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT balance, last_active, is_online FROM users WHERE id=%s", (user_id,))
        u = cur.fetchone()

        if not u:
            return None

        if u["is_online"] == 0:
            return "OK"  # không online → không trừ tiền

        now = datetime.now()

        if u["last_active"] is None:
            cur.execute("UPDATE users SET last_active=%s WHERE id=%s", (now, user_id))
            return "OK"

        elapsed = (now - u["last_active"]).total_seconds()

        if elapsed <= 0:
            return "OK"

        money_lost = elapsed * COST_PER_SECOND
        new_balance = u["balance"] - money_lost

        if new_balance <= 0:
            cur.execute("UPDATE users SET balance=0, is_online=0 WHERE id=%s", (user_id,))
            return "OUT"

        cur.execute("UPDATE users SET balance=%s, last_active=%s WHERE id=%s",
                    (new_balance, now, user_id))

        return "OK"


# ============================
# LOGIN
# ============================
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username=%s AND password=%s",
                        (username, password))
            user = cur.fetchone()

        if user:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET is_online=1, last_active=%s WHERE id=%s",
                    (datetime.now(), user["id"])
                )

            if user["role"] == "admin":
                return redirect(url_for("admin_dashboard"))
            else:
                return redirect(url_for("user_dashboard"))

        flash("Sai tài khoản hoặc mật khẩu!", "danger")

    return render_template("login.html")


# ============================
# LOGOUT
# ============================
@app.route("/logout")
def logout():
    user_id = session.get("user_id")

    if user_id:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_online=0 WHERE id=%s", (user_id,))

    session.clear()

    return redirect(url_for("login"))


# ============================
# USER DASHBOARD
# ============================
@app.route("/user")
def user_dashboard():
    if "user_id" not in session or session["role"] != "user":
        return redirect(url_for("login"))

    user_id = session["user_id"]

    # Auto trừ tiền
    result = update_user_time(user_id)
    if result == "OUT":
        session.clear()
        flash("Bạn đã hết tiền! Vui lòng nạp thêm.", "danger")
        return redirect(url_for("login"))

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        user = cur.fetchone()

        cur.execute("SELECT * FROM topup_requests WHERE user_id=%s ORDER BY created_at DESC",
                    (user_id,))
        requests_list = cur.fetchall()

    return render_template("user_dashboard.html", user=user, requests_list=requests_list)


# ============================
# USER GỬI YÊU CẦU NẠP TIỀN
# ============================
@app.route("/user/request_topup", methods=["POST"])
def user_request_topup():
    if "user_id" not in session or session["role"] != "user":
        return redirect(url_for("login"))

    amount = request.form.get("amount")
    if not amount.isdigit():
        flash("Số tiền không hợp lệ!", "danger")
        return redirect(url_for("user_dashboard"))

    amount = int(amount)
    user_id = session["user_id"]

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO topup_requests (user_id, amount, status) "
                    "VALUES (%s, %s, 'pending')", (user_id, amount))

    flash("Đã gửi yêu cầu nạp tiền!", "success")
    return redirect(url_for("user_dashboard"))


# ============================
# ADMIN DASHBOARD
# ============================
@app.route("/admin")
def admin_dashboard():
    if "user_id" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, balance, is_online, last_active FROM users WHERE role='user'")
        users = cur.fetchall()

        cur.execute("""
            SELECT tr.*, u.username
            FROM topup_requests tr
            JOIN users u ON tr.user_id = u.id
            ORDER BY tr.created_at DESC
        """)
        requests_list = cur.fetchall()

    return render_template("admin_dashboard.html", users=users, requests_list=requests_list)


# ============================
# ADMIN NẠP TIỀN
# ============================
@app.route("/admin/topup/<int:user_id>", methods=["POST"])
def admin_topup(user_id):
    amount = request.form.get("amount")
    if not amount.isdigit():
        flash("Số tiền không hợp lệ!", "danger")
        return redirect(url_for("admin_dashboard"))

    amount = int(amount)

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET balance = balance + %s, last_active=%s WHERE id=%s",
                    (amount, datetime.now(), user_id))

    flash("Đã nạp tiền cho user!", "success")
    return redirect(url_for("admin_dashboard"))


# ============================
# ADMIN DUYỆT YÊU CẦU NẠP
# ============================
@app.route("/admin/approve_request/<int:req_id>")
def admin_approve_request(req_id):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM topup_requests WHERE id=%s", (req_id,))
        req = cur.fetchone()

        if not req or req["status"] != "pending":
            flash("Yêu cầu không hợp lệ!", "danger")
            return redirect(url_for("admin_dashboard"))

        cur.execute("UPDATE users SET balance = balance + %s, last_active=%s WHERE id=%s",
                    (req["amount"], datetime.now(), req["user_id"]))

        cur.execute("UPDATE topup_requests SET status='approved' WHERE id=%s", (req_id,))

    flash("Đã duyệt yêu cầu và nạp tiền!", "success")
    return redirect(url_for("admin_dashboard"))


# ============================
# ADMIN TẠO USER MỚI (FIX is_online)
# ============================
@app.route("/admin/create_user", methods=["POST"])
def admin_create_user():
    username = request.form.get("username")
    password = request.form.get("password")

    if not username or not password:
        flash("Không được bỏ trống!", "danger")
        return redirect(url_for("admin_dashboard"))

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username=%s", (username,))
        exists = cur.fetchone()

        if exists:
            flash("User đã tồn tại!", "danger")
            return redirect(url_for("admin_dashboard"))

        # Fix: user mới tạo luôn offline
        cur.execute("""
            INSERT INTO users (username, password, role, balance, is_online, last_active)
            VALUES (%s, %s, 'user', 0, 0, NULL)
        """, (username, password))

    flash("Tạo user thành công!", "success")
    return redirect(url_for("admin_dashboard"))


# ============================
# CHAT
# ============================
@app.route("/chat", methods=["GET", "POST"])
def chat():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]

    if session["role"] == "user":
        result = update_user_time(user_id)
        if result == "OUT":
            session.clear()
            flash("Bạn đã hết tiền!", "danger")
            return redirect(url_for("login"))

    username = session["username"]
    role = session["role"]

    conn = get_db_connection()

    if request.method == "POST":
        content = request.form.get("content")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (sender, role, content) VALUES (%s, %s, %s)",
                (username, role, content)
            )

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM messages ORDER BY created_at ASC")
        messages = cur.fetchall()

    return render_template("chat.html", messages=messages, username=username, role=role)


# ============================
# RUN
# ============================
if __name__ == "__main__":
    app.run(debug=True)
