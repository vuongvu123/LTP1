from flask import Flask, render_template, request, redirect, url_for, session, flash
import pymysql
from pymysql.err import InternalError
from datetime import datetime

# Socket.IO for realtime
from flask_socketio import SocketIO, join_room, leave_room, emit

app = Flask(__name__)
app.secret_key = "secret_key_cua_ban"

# initialize SocketIO (will choose best async mode available)
socketio = SocketIO(app, cors_allowed_origins="*")

# Giá tiền 5k / 1 giờ
COST_PER_HOUR = 5000
COST_PER_SECOND = COST_PER_HOUR / 3600.0


def seconds_to_hms(sec):
    try:
        sec = int(sec)
    except Exception:
        sec = 0
    if sec <= 0:
        return "00:00:00"
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ============================
# KẾT NỐI DATABASE
# ============================
def get_db_connection():
    conn = pymysql.connect(
        host="localhost",
        user="root",
        password="123456",
        database="netcafe",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )
    return conn


# Ensure DB schema has expected columns (run-once safe)
def ensure_db_schema():
    conn = get_db_connection()
    with conn.cursor() as cur:
        # Ensure topup_requests has user_notified column
        try:
            cur.execute("SELECT user_notified FROM topup_requests LIMIT 1")
        except InternalError:
            cur.execute("ALTER TABLE topup_requests ADD COLUMN user_notified TINYINT(1) DEFAULT 0")


# call schema ensure at startup
ensure_db_schema()

# map user_id -> set of socket ids for connected clients
active_user_sids = {}
# map admin socket id -> current target user id
admin_targets = {}
# map user_id -> background task handle (to avoid multiple tasks per user)
user_time_tasks = {}

def _start_user_time_task(user_id):
    """Start a background task that updates a user's time every second and emits updates.
    Uses socketio.start_background_task to avoid raw threads.
    """
    if user_id in user_time_tasks:
        return

    def task():
        # run until user is offline or removed from tracking
        try:
            while True:
                # check if user still has connected sids or is online
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute("SELECT is_online, balance, last_active FROM users WHERE id=%s", (user_id,))
                    row = cur.fetchone()
                if not row or row.get('is_online') == 0:
                    break

                # update time and balance
                res = update_user_time(user_id)

                # fetch latest balance and compute seconds left
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute("SELECT balance FROM users WHERE id=%s", (user_id,))
                    r = cur.fetchone()
                bal = float(r.get('balance') or 0)
                seconds_left = int(bal / COST_PER_SECOND) if COST_PER_SECOND else 0

                payload = {
                    'user_id': user_id,
                    'balance': bal,
                    'seconds_left': seconds_left,
                    'status': res
                }
                try:
                    socketio.emit('time_update', payload, room=f"user_{user_id}")
                except Exception:
                    pass

                if res == 'OUT':
                    # user ran out of money; stop task
                    break

                socketio.sleep(1)
        finally:
            # cleanup
            user_time_tasks.pop(user_id, None)

    # start and store a handle (not required by flask-socketio but keep presence)
    th = socketio.start_background_task(task)
    user_time_tasks[user_id] = th
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
                # QUAN TRỌNG: Reset last_active = NOW() để bắt đầu đếm giờ lại từ đầu
                # Bỏ qua khoảng thời gian offline trước đó
                cur.execute(
                    "UPDATE users SET is_online=1, last_active=%s WHERE id=%s",
                    (datetime.now(), user["id"])
                )

                # Báo cho admin biết user online
                try:
                    socketio.emit('user_status', {'user_id': user['id'], 'is_online': 1}, room='admins')
                except Exception:
                    pass
            
            # Kích hoạt task đếm giờ ngay lập tức
            if user["role"] == "user":
                 _start_user_time_task(user["id"])

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
        # 1. Cập nhật tiền lần cuối cùng trước khi logout
        update_user_time(user_id)

        conn = get_db_connection()
        with conn.cursor() as cur:
            # 2. Set trạng thái về Offline
            cur.execute("UPDATE users SET is_online=0 WHERE id=%s", (user_id,))
        
        # ... (Phần code xóa chat và dọn dẹp giữ nguyên như cũ) ...
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM messages WHERE from_user_id=%s OR to_user_id=%s", (user_id, user_id))
        except Exception:
            pass

        try:
            socketio.emit('user_status', {'user_id': user_id, 'is_online': 0}, room='admins')
            socketio.emit('clear_chat', {'user_id': user_id}, room='admins')
            socketio.emit('clear_chat', {'user_id': user_id}, room=f"user_{user_id}")
            
            # Gửi tín hiệu PAUSE về client để đồng hồ dừng ngay lập tức
            socketio.emit('time_update', {'user_id': user_id, 'status': 'PAUSED'}, room=f"user_{user_id}")
        except Exception:
            pass

        # Dừng background task
        try:
            user_time_tasks.pop(user_id, None)
        except Exception:
            pass

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
    conn = get_db_connection()

    # XỬ LÝ TRƯỜNG HỢP RELOAD TRANG:
    # Nếu DB đang ghi nhận là offline (do socket disconnect khi reload), ta phải set lại Online
    # và reset last_active để tránh trừ tiền oan trong tích tắc reload.
    with conn.cursor() as cur:
        cur.execute("SELECT is_online, last_active FROM users WHERE id=%s", (user_id,))
        u_status = cur.fetchone()
        
        if u_status and u_status['is_online'] == 0:
            # User đang reload trang -> Set lại Online và Resume thời gian
            cur.execute("UPDATE users SET is_online=1, last_active=%s WHERE id=%s", 
                        (datetime.now(), user_id))
            # Khởi động lại task đếm giờ
            _start_user_time_task(user_id)

    # Auto trừ tiền (Bình thường)
    result = update_user_time(user_id)
    if result == "OUT":
        session.clear()
        flash("Bạn đã hết tiền! Vui lòng nạp thêm.", "danger")
        return redirect(url_for("login"))

    # ... (Phần còn lại giữ nguyên) ...
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        user = cur.fetchone()
        
        # ... code lấy requests_list ...
        cur.execute("SELECT * FROM topup_requests WHERE user_id=%s ORDER BY created_at DESC", (user_id,))
        requests_list = cur.fetchall()
        
        # Compute seconds left
        bal = float(user['balance'] or 0)
        seconds_left = int(bal / COST_PER_SECOND)

    return render_template("user_dashboard.html", user=user, requests_list=requests_list, seconds_left=seconds_left)


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
    # enforce minimum topup amount
    if amount < 5000:
        flash("Số tiền tối thiểu để nạp là 5000 đ", "danger")
        return redirect(url_for("user_dashboard"))
    user_id = session["user_id"]

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO topup_requests (user_id, amount, status, user_notified) "
                    "VALUES (%s, %s, 'pending', 0)", (user_id, amount))
        # notify admin clients in real-time about new topup request
        try:
            req_id = cur.lastrowid
            cur.execute("SELECT tr.id, tr.user_id, tr.amount, tr.status, tr.created_at, u.username FROM topup_requests tr JOIN users u ON tr.user_id=u.id WHERE tr.id=%s", (req_id,))
            new_req = cur.fetchone()
            if new_req and isinstance(new_req.get('created_at'), datetime):
                new_req['created_at'] = str(new_req['created_at'])
            socketio.emit('new_topup_request', new_req, room='admins')
        except Exception:
            pass

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
        # compute seconds_left for each user (balance -> seconds)
        for u in users:
            try:
                bal = float(u.get('balance') or 0)
            except Exception:
                bal = 0.0
            u['seconds_left'] = int(bal / COST_PER_SECOND)

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

        # mark request approved and ensure user_notified is 0 so user will be notified on next dashboard load
        cur.execute("UPDATE topup_requests SET status='approved', user_notified=0 WHERE id=%s", (req_id,))

        # insert a message from admin to the user to notify them immediately in chat
        cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
        admin_row = cur.fetchone()
        admin_id = admin_row['id'] if admin_row else None
        if admin_id:
            content = f"Yêu cầu nạp {int(req['amount'])} đ của bạn đã được duyệt và nạp vào tài khoản."
            cur.execute("INSERT INTO messages (from_user_id, to_user_id, content) VALUES (%s, %s, %s)",
                        (admin_id, req['user_id'], content))
            # emit real-time notification to the user's room
            try:
                socketio.emit('new_message', {
                    'from_user_id': admin_id,
                    'to_user_id': req['user_id'],
                    'content': content
                }, room=f"user_{req['user_id']}")
            except Exception:
                pass
        # notify admin dashboards that this request was updated
        try:
            socketio.emit('topup_request_updated', {'id': req_id, 'status': 'approved'}, room='admins')
        except Exception:
            pass

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
    # Prepare participants
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
        admin_row = cur.fetchone()
        admin_id = admin_row['id'] if admin_row else None

    # ADMIN: optionally choose a user to chat with via query param or form
    target_user_id = None
    if session['role'] == 'admin':
        # get selected user id from querystring or form
        target_user_id = request.args.get('user_id') or request.form.get('target_user_id')
        if target_user_id:
            try:
                target_user_id = int(target_user_id)
            except ValueError:
                target_user_id = None

    # POST handling: insert message with from_user_id/to_user_id
    if request.method == "POST":
        content = request.form.get("content")
        if session['role'] == 'user':
            # user -> admin
            to_id = admin_id
            from_id = user_id
        else:
            # admin -> target user (must provide target_user_id)
            from_id = user_id
            to_id = target_user_id

        if from_id and to_id and content:
            with conn.cursor() as cur:
                    # if to_id not provided (user sending), find an admin id
                    if not to_id:
                        cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
                        arow = cur.fetchone()
                        to_id = arow['id'] if arow else None
                    if not to_id:
                        # try to find an admin if sender is a user
                        cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
                        arow = cur.fetchone()
                        to_id = arow['id'] if arow else None
                    if not to_id:
                        # reject sending messages with no recipient (admin forgot to select user)
                        try:
                            emit('error', {'message': 'No recipient selected'}, room=request.sid)
                        except Exception:
                            pass
                        print(f"[socket] message rejected: from={from_id} to=None content={content}")
                        return
                    # log resolved recipient
                    print(f"[socket] resolved to_id={to_id}")
                    cur.execute("INSERT INTO messages (from_user_id, to_user_id, content) VALUES (%s, %s, %s)",
                                (from_id, to_id, content))

    # Fetch messages for display
    with conn.cursor() as cur:
        if session['role'] == 'user':
            # show conversation between this user and admin
            cur.execute(
                "SELECT m.*, u.username AS sender_name, u.role AS sender_role "
                "FROM messages m JOIN users u ON m.from_user_id = u.id "
                "WHERE (m.from_user_id=%s AND m.to_user_id=%s) OR (m.from_user_id=%s AND m.to_user_id=%s) "
                "ORDER BY m.created_at ASC",
                (user_id, admin_id, admin_id, user_id)
            )
            messages = cur.fetchall()
            # no user list needed for normal user
            users = []
        else:
            # admin: show either conversation with selected user or an empty list
            # get list of all users for dropdown
            cur.execute("SELECT id, username FROM users WHERE role='user'")
            users = cur.fetchall()

            if not target_user_id and users:
                # default to first user in list
                target_user_id = users[0]['id']

            if target_user_id:
                cur.execute(
                    "SELECT m.*, u.username AS sender_name, u.role AS sender_role "
                    "FROM messages m JOIN users u ON m.from_user_id = u.id "
                    "WHERE (m.from_user_id=%s AND m.to_user_id=%s) OR (m.from_user_id=%s AND m.to_user_id=%s) "
                    "ORDER BY m.created_at ASC",
                    (user_id, target_user_id, target_user_id, user_id)
                )
                messages = cur.fetchall()
            else:
                messages = []

    return render_template("chat.html", messages=messages, username=username, role=role, users=users, target_user_id=target_user_id, user_id=user_id)


# ============================
# RUN
# ============================
@socketio.on('join')
def on_join(data):
    # data: {user_id, role, target_user_id (optional)}
    user_id = data.get('user_id')
    role = data.get('role')
    target = data.get('target_user_id')
    print(f"[socket] join request: sid={request.sid} user_id={user_id} role={role} target={target}")
    # Always join the per-user room when a user_id is provided
    if user_id:
        room = f"user_{user_id}"
        try:
            join_room(room)
        except Exception:
            pass
        # track this sid for direct emits
        try:
            s = active_user_sids.get(user_id)
            if not s:
                active_user_sids[user_id] = set()
            active_user_sids[user_id].add(request.sid)
        except Exception:
            pass
        # start background time updater for this user if they're online
        try:
            # check DB online flag before starting
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT is_online FROM users WHERE id=%s", (user_id,))
                r = cur.fetchone()
            if r and r.get('is_online') == 1:
                _start_user_time_task(user_id)
        except Exception:
            pass
        # notify this client it joined its room
        try:
            emit('joined', {'room': room, 'user_id': user_id}, room=request.sid)
        except Exception:
            pass

    # If role indicates admin, also join admin room and optionally the target user's room
    if role == 'admin':
        try:
            join_room('admins')
        except Exception:
            pass
        # Do NOT auto-join admin to a user's room on connect — admin should explicitly switch users.
        # Only track the admin's current target if provided, but don't join the user room here.
        try:
            if target:
                admin_targets[request.sid] = target
        except Exception:
            pass
        try:
            emit('joined', {'room': 'admins', 'target': target, 'user_id': user_id}, room=request.sid)
        except Exception:
            pass


@socketio.on('switch_user')
def on_switch_user(data):
    # admin switches selected user to chat with
    prev = data.get('prev_user_id')
    new = data.get('new_user_id')
    sid = request.sid
    if prev:
        try:
            leave_room(f"user_{prev}")
        except Exception:
            pass
    if new:
        join_room(f"user_{new}")
        # remember this admin's current target
        try:
            admin_targets[sid] = new
        except Exception:
            pass
        # load conversation and emit back to this admin socket only
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT id, from_user_id, to_user_id, content, created_at FROM messages WHERE (from_user_id=%s AND to_user_id=%s) OR (from_user_id=%s AND to_user_id=%s) ORDER BY created_at ASC",
                        (session.get('user_id'), new, new, session.get('user_id')))
            msgs = cur.fetchall()
        # convert datetime to string for JSON serialization
        try:
            for m in msgs:
                if isinstance(m.get('created_at'), datetime):
                    m['created_at'] = str(m['created_at'])
        except Exception:
            pass
        emit('messages', {'messages': msgs}, room=sid)


@socketio.on('send_message')
def on_send_message(data):
    # data: {from_user_id, to_user_id, content}
    from_id = data.get('from_user_id')
    to_id = data.get('to_user_id')
    content = data.get('content')
    if not from_id or not content:
        return
    print(f"[socket] send_message from={from_id} to={to_id} content={content}")
    conn = get_db_connection()
    with conn.cursor() as cur:
        # if to_id not provided, decide based on sender role
        if not to_id:
            cur.execute("SELECT role FROM users WHERE id=%s", (from_id,))
            srow = cur.fetchone()
            sender_role = srow['role'] if srow else None
            print(f"[socket] sender_role={sender_role}")
            if sender_role == 'user':
                # user sending -> deliver to admin
                cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
                arow = cur.fetchone()
                to_id = arow['id'] if arow else None
            else:
                # admin must specify recipient explicitly; try to use admin_targets mapping
                try:
                    tgt = admin_targets.get(request.sid)
                except Exception:
                    tgt = None
                if tgt:
                    to_id = tgt
                else:
                    try:
                        emit('error', {'message': 'Admin must select a recipient'}, room=request.sid)
                    except Exception:
                        pass
                    print(f"[socket] message rejected: from={from_id} to=None content={content}")
                    return
        if not to_id:
            try:
                emit('error', {'message': 'No recipient available'}, room=request.sid)
            except Exception:
                pass
            print(f"[socket] message rejected: from={from_id} to=None content={content}")
            return
        # log resolved recipient
        print(f"[socket] resolved to_id={to_id}")
        # prevent rapid duplicate inserts: check for an existing identical message in last 2 seconds
        msg_id = None
        created_at = ''
        try:
            cur.execute(
                "SELECT id, created_at FROM messages WHERE from_user_id=%s AND to_user_id=%s AND content=%s AND created_at >= (NOW() - INTERVAL 2 SECOND) LIMIT 1",
                (from_id, to_id, content)
            )
            existing = cur.fetchone()
            if existing:
                msg_id = existing['id']
                created_at = existing['created_at']
                print(f"[socket] duplicate detected, reusing message id={msg_id}")
            else:
                cur.execute("INSERT INTO messages (from_user_id, to_user_id, content) VALUES (%s, %s, %s)",
                            (from_id, to_id, content))
                msg_id = cur.lastrowid
                cur.execute("SELECT created_at FROM messages WHERE id=%s", (msg_id,))
                crow = cur.fetchone()
                created_at = crow['created_at'] if crow else ''
            # get sender info for payload
            cur.execute("SELECT username, role FROM users WHERE id=%s", (from_id,))
            srow = cur.fetchone()
            sender_name = srow['username'] if srow else ''
            sender_role = srow['role'] if srow else ''
        except Exception as e:
            print('[socket] duplicate-check/insert error', e)
            # fallback: insert normally
            try:
                cur.execute("INSERT INTO messages (from_user_id, to_user_id, content) VALUES (%s, %s, %s)",
                            (from_id, to_id, content))
                msg_id = cur.lastrowid
                cur.execute("SELECT created_at FROM messages WHERE id=%s", (msg_id,))
                crow = cur.fetchone()
                created_at = crow['created_at'] if crow else ''
                cur.execute("SELECT username, role FROM users WHERE id=%s", (from_id,))
                srow = cur.fetchone()
                sender_name = srow['username'] if srow else ''
                sender_role = srow['role'] if srow else ''
            except Exception:
                msg_id = None
                created_at = ''

    payload = {
        'id': msg_id,
        'from_user_id': from_id,
        'to_user_id': to_id,
        'content': content,
        'sender_name': sender_name,
        'sender_role': sender_role,
        'created_at': str(created_at)
    }
    # emit to both participants' room so both see it
    try:
        socketio.emit('new_message', payload, room=f"user_{to_id}")
        socketio.emit('new_message', payload, room=f"user_{from_id}")
    except Exception:
        print('[socket] emit new_message failed', Exception)
        pass
    # ack back to sender socket if available
    try:
        emit('sent', payload, room=request.sid)
    except Exception:
        pass
    # also try direct emits to tracked socket ids for reliability
    try:
        sids = active_user_sids.get(to_id) or set()
        print(f"[socket] tracked sids for to_id={to_id}: {len(sids)}")
        for sid in sids:
            try:
                socketio.emit('new_message', payload, room=sid)
            except Exception:
                pass
    except Exception:
        pass
    try:
        sids = active_user_sids.get(from_id) or set()
        for sid in sids:
            try:
                socketio.emit('new_message', payload, room=sid)
            except Exception:
                pass
    except Exception:
        pass

    # notify admins (lightweight) that a user sent a message so admin can see who's active
    try:
        # only notify admins when the sender is a non-admin user
        if sender_role == 'user':
            notice = {
                'user_id': from_id,
                'username': sender_name,
                'snippet': (content[:80] + '...') if len(content) > 80 else content,
                'created_at': str(created_at)
            }
            socketio.emit('user_active', notice, room='admins')
    except Exception:
        pass


@socketio.on('load_messages')
def on_load_messages(data):
    # data: {user_id, other_id}
    a = data.get('user_id')
    b = data.get('other_id')
    if not a or not b:
        return
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, from_user_id, to_user_id, content, created_at FROM messages WHERE (from_user_id=%s AND to_user_id=%s) OR (from_user_id=%s AND to_user_id=%s) ORDER BY created_at ASC",
                    (a, b, b, a))
        msgs = cur.fetchall()
    try:
        for m in msgs:
            if isinstance(m.get('created_at'), datetime):
                m['created_at'] = str(m['created_at'])
    except Exception:
        pass
    emit('messages', {'messages': msgs})


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    # ... (Phần code xóa sid giữ nguyên) ...
    try:
        to_remove = []
        for uid, sids in active_user_sids.items():
            if sid in sids:
                sids.discard(sid)
                if not sids:
                    to_remove.append(uid)
        for uid in to_remove:
            active_user_sids.pop(uid, None)
    except Exception:
        pass

    try:
        if sid in admin_targets:
            admin_targets.pop(sid, None)
    except Exception:
        pass

    # XỬ LÝ KHI USER MẤT KẾT NỐI HOÀN TOÀN
    try:
        to_stop = []
        for uid, sids in list(active_user_sids.items()):
            if not sids:
                to_stop.append(uid)
        for uid in to_stop:
            active_user_sids.pop(uid, None)
            
            # CHỐT SỔ LẦN CUỐI
            update_user_time(uid)

            try:
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET is_online=0 WHERE id=%s", (uid,))
                
                # Báo admin biết user này đã offline
                try:
                    socketio.emit('user_status', {'user_id': uid, 'is_online': 0}, room='admins')
                except Exception:
                    pass
            except Exception:
                pass
            
            user_time_tasks.pop(uid, None)
    except Exception:
        pass


if __name__ == "__main__":
    # run with socketio to enable realtime features
    socketio.run(app, debug=True)
