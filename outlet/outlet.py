from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
import os
from datetime import datetime, date
import secrets
import smtplib
from email.mime.text import MIMEText
from flask_socketio import SocketIO, join_room, leave_room, emit

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "your_secret_key")
socketio = SocketIO(app, cors_allowed_origins="*")  # SocketIO for WebRTC signaling


LOG_FILE = "user_activity.log"
DB_FILE = "electricity.db"

#  ADD THIS HERE (GLOBAL)
video_rooms = {}

# Database Initialization
def init_db():
    # Users DB
    with sqlite3.connect("users.db") as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reset_token TEXT
            )
        """)

    # Electricity DB
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                reading REAL NOT NULL,
                usage REAL,
                unit_price REAL NOT NULL,
                standing_charge REAL NOT NULL,
                total_cost REAL,
                UNIQUE(user_id, date),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

    # Parameters DB
    with sqlite3.connect('eparameter.db') as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS const (
                company TEXT,
                date TEXT,
                unit_price REAL NOT NULL,
                standing_charge REAL NOT NULL,
                PRIMARY KEY (company, date)
            )
        """)

    print("Database initialized")

init_db()

# ---------- Utility Functions ----------

def log_action(action, username=None):
    time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{time}] {action}"
    if username:
        entry += f" - User: {username}"
    with open(LOG_FILE, "a") as f:
        f.write(entry + "\n")

def send_reset_email(email, token):
    subject = "Password Reset Link"
    body = f"Your password reset token is: {token}"
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = "your@email.com"
    msg['To'] = email

    try:
        with smtplib.SMTP('localhost') as server:
            server.send_message(msg)
        print("Reset email sent to", email)
    except Exception as e:
        print("Failed to send email:", str(e))

def get_user_id(username):
    with sqlite3.connect("users.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username=?", (username,))
        row = cursor.fetchone()
        return row[0] if row else None

def get_user_records(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT date, reading, usage, unit_price, standing_charge, total_cost
        FROM readings
        WHERE user_id = ?
        ORDER BY date
    """, (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def insert_user_record(user_id, record):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO readings (user_id, date, reading, usage, unit_price, standing_charge, total_cost)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, *record))
    conn.commit()
    conn.close()

def get_latest_user_reading(user_id, date_val):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT reading FROM readings
        WHERE user_id = ? AND date < ?
        ORDER BY date DESC LIMIT 1
    """, (user_id, date_val))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

# ---------- Routes ----------

@app.route('/')
def home():
    return render_template('index.html', user=session.get('user'))

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    message = None
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect("users.db") as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO users (username, email, password, created_at)
                    VALUES (?, ?, ?, ?)
                """, (username, email, password, created_at))
                conn.commit()
                log_action("User signed up", username)
                flash("Signup successful! Please login.")
                return redirect(url_for('login'))
            except sqlite3.IntegrityError:
                message = "Username or Email already exists!"
    return render_template('signup.html', error=message)

@app.route('/login', methods=['GET', 'POST'])
def login():
    message = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        with sqlite3.connect("users.db") as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
            user = cursor.fetchone()
            if user:
                session['user'] = username
                log_action("User logged in", username)
                return redirect(url_for('home'))
            else:
                message = "Invalid credentials!"
    return render_template('login.html', error=message)

@app.route('/logout')
def logout():
    user = session.get('user')
    session.pop('user', None)
    log_action("User logged out", user)
    return redirect(url_for('home'))

@app.route('/reset', methods=['GET', 'POST'])
def reset():
    message = None
    if request.method == 'POST':
        email = request.form['email']
        token = secrets.token_hex(8)
        with sqlite3.connect("users.db") as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE email=?", (email,))
            user = cursor.fetchone()
            if user:
                cursor.execute("UPDATE users SET reset_token=? WHERE email=?", (token, email))
                conn.commit()
                send_reset_email(email, token)
                log_action("Reset token sent", user[1])
                message = "Reset token sent to your email."
            else:
                message = "Email not found."
    return render_template('reset.html', message=message)

@app.route('/electricity', methods=['GET', 'POST'])
def electricity():
    if 'user' not in session:
        flash("Please login to access this page.")
        return redirect(url_for('login'))

    username = session['user']
    user_id = get_user_id(username)
    message = None
    daily_total = None

    if request.method == 'POST':
        date_str = request.form['date']
        reading = float(request.form['reading'])
        unit_price = float(request.form['unit_price'])
        standing_charge = float(request.form['standing_charge'])

        # Check if record exists
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT 1 FROM readings WHERE user_id = ? AND date = ?", (user_id, date_str))
        exists = c.fetchone()
        conn.close()

        if exists:
            message = f"A reading already exists for {date_str}."
        else:
            prev_reading = get_latest_user_reading(user_id, date_str)
            usage = round(reading - prev_reading, 2)
            if prev_reading == 0:
                usage = 0 
            total_cost = round((usage * unit_price) + standing_charge, 2)
            insert_user_record(user_id, (date_str, reading, usage, unit_price, standing_charge, total_cost))
            daily_total = f"{total_cost:.2f}"

    raw_records = get_user_records(user_id)
    records = [{
        'date': r[0],
        'reading': r[1],
        'usage': r[2],
        'unit_price': f"{r[3]:.2f}",
        'standing_charge': f"{r[4]:.2f}",
        'total_cost': f"{r[5]:.2f}"
    } for r in raw_records]

    return render_template('electricity.html',
                           records  =records,
                           message=message,
                           daily_total=daily_total,
                           current_date=date.today().isoformat())
                           

@app.route('/notepad')
def notepad():
    if 'user' in session:
        return "Notepad Page (Protected)"
    else:
        flash("Please login to access this page.")
        return redirect(url_for('login'))

@app.route('/messages')
def messages():
    if 'user' in session:
        return "Messages Page (Protected)"
    else:
        flash("Please login to access this page.")
        return redirect(url_for('login'))

@app.route('/videochat-lobby')
def videochat_lobby():
    if 'user' not in session:
        return redirect(url_for('login'))
    print(os.listdir('templates'))

    return render_template(
        'videochat_lobby.html',
        current_user=session['user'],
        rooms=video_rooms
    )


@app.route('/videochat/create', methods=['POST'])
def create_video_room():
    if 'user' not in session:
        return redirect(url_for('login'))

    username = session['user']
    room_name = request.form.get('room_name', '').strip()  # <-- fetch from form

    if not room_name:
        flash("Room name is required!")
        return redirect(url_for('videochat_lobby'))

    if room_name in video_rooms:
        flash("Room already exists!")
        return redirect(url_for('videochat_lobby'))

    # Create room
    video_rooms[room_name] = {
        "host": username,
        "participants": [username]
    }

    return redirect(url_for('videochat_room', room_name=room_name))


@app.route('/videochat/room/<room_name>')
def videochat_room(room_name):
    if 'user' not in session:
        return redirect(url_for('login'))

    username = session['user']

    if room_name not in video_rooms:
        flash("Room no longer exists")
        return redirect(url_for('videochat_lobby'))

    if username not in video_rooms[room_name]['participants']:
        video_rooms[room_name]['participants'].append(username)

    return render_template(
        'videochat.html',
        username=username,
        room=room_name
    )


    
# ---------------- SocketIO events ----------------
@socketio.on('join')
def handle_join(room):
    join_room(room)
    user = session.get('user')

    # notify host if a new participant joins
    if room in video_rooms:
        host = video_rooms[room]['host']
        if host != user:
            emit('new_participant', room=room, to=host)



@socketio.on('offer')
def handle_offer(data):
    emit('offer', data, room=data['room'], include_self=False)

@socketio.on('answer')
def handle_answer(data):
    emit('answer', data, room=data['room'], include_self=False)

@socketio.on('ice_candidate')
def handle_ice(data):
    emit('ice_candidate', data, room=data['room'], include_self=False)

@socketio.on('leave')
def handle_leave(room):
    leave_room(room)

    user = session.get('user')
    if room in video_rooms and user in video_rooms[room]['participants']:
        video_rooms[room]['participants'].remove(user)

        # delete room if empty
        if not video_rooms[room]['participants']:
            del video_rooms[room]

    emit('user_left', room=room, broadcast=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(
        app,
        host="0.0.0.0",
        port=port
    )

