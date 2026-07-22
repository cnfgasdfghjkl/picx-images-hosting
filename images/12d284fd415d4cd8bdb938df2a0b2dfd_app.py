from flask import Flask, request, jsonify, render_template_string, session, send_file
import sqlite3
import hashlib
from datetime import datetime, timedelta
import os
import re
import base64
import json
from io import BytesIO
import uuid
import threading
import time
import requests
import shutil

app = Flask(__name__)
app.secret_key = '6d5f55329de8'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

DB_FILE = 'chat.db'
AI_API_KEY = 'ffffb668be484180a2854043980a2c32.LUDga4uaBLn4qWA7'
AI_MODEL = 'glm-4-flash'

# ========== 图片自动清理 ==========
def clean_old_images():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        cutoff = (datetime.now() - timedelta(days=3)).isoformat()
        c.execute('UPDATE messages SET image_data = NULL WHERE message_type = "image" AND timestamp < ?', (cutoff,))
        c.execute('UPDATE group_messages SET image_data = NULL WHERE message_type = "image" AND timestamp < ?', (cutoff,))
        conn.commit()
        affected = conn.total_changes
        conn.close()
        if affected > 0:
            print(f"🧹 已清理 {affected} 条旧图片")
        return affected
    except Exception as e:
        print(f"❌ 清理图片失败: {e}")
        return 0

def daily_cleanup():
    while True:
        now = datetime.now()
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()
        print(f"⏰ 下次图片清理: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
        time.sleep(wait_seconds)
        clean_old_images()

if not os.environ.get('WERKZEUG_RUN_MAIN'):
    cleanup_thread = threading.Thread(target=daily_cleanup, daemon=True)
    cleanup_thread.start()

# ========== 数据库迁移 ==========
def migrate_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in c.fetchall()]
    if 'ai_tokens' not in columns:
        c.execute('ALTER TABLE users ADD COLUMN ai_tokens INTEGER DEFAULT 0')

    c.execute("PRAGMA table_info(messages)")
    msg_columns = [col[1] for col in c.fetchall()]
    if 'message_type' not in msg_columns:
        c.execute('ALTER TABLE messages ADD COLUMN message_type TEXT DEFAULT "text"')
    if 'image_data' not in msg_columns:
        c.execute('ALTER TABLE messages ADD COLUMN image_data TEXT')

    c.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            message TEXT,
            type TEXT DEFAULT 'transfer'
        )
    ''')

    c.execute("PRAGMA table_info(transactions)")
    trans_columns = [col[1] for col in c.fetchall()]
    if 'type' not in trans_columns:
        c.execute('ALTER TABLE transactions ADD COLUMN type TEXT DEFAULT "transfer"')

    c.execute('''
        CREATE TABLE IF NOT EXISTS token_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fb_amount INTEGER NOT NULL,
            tokens INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            UNIQUE(group_id, user_id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            message_type TEXT DEFAULT 'text',
            image_data TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS group_message_reads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            read_at TEXT NOT NULL,
            UNIQUE(message_id, user_id)
        )
    ''')

    c.execute("SELECT id FROM users WHERE username = 'admin'")
    admin = c.fetchone()
    if admin:
        c.execute('UPDATE users SET ai_tokens = 999999 WHERE username = "admin"')

    conn.commit()
    conn.close()
    print("✅ 数据库迁移完成")

migrate_db()

# ========== 初始化数据库 ==========
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TEXT NOT NULL,
            fb_balance INTEGER DEFAULT 3,
            ai_tokens INTEGER DEFAULT 0
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            friend_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            UNIQUE(user_id, friend_id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            message_type TEXT DEFAULT 'text',
            image_data TEXT
        )
    ''')

    admin_pw = hashlib.sha256('9sFg7$kP2qRz8'.encode()).hexdigest()
    c.execute('INSERT OR IGNORE INTO users (username, password, created_at, fb_balance, ai_tokens) VALUES (?, ?, ?, ?, ?)',
              ('admin', admin_pw, datetime.now().isoformat(), 999999, 999999))

    conn.commit()
    conn.close()
    print("✅ 数据库初始化完成")

init_db()

# ========== AI相关 ==========
def call_ai_api(prompt):
    headers = {
        'Authorization': f'Bearer {AI_API_KEY}',
        'Content-Type': 'application/json'
    }
    data = {
        'model': AI_MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.7,
        'max_tokens': 1000
    }
    try:
        response = requests.post(
            'https://open.bigmodel.cn/api/paas/v4/chat/completions',
            headers=headers,
            json=data,
            timeout=30
        )
        if response.status_code == 200:
            result = response.json()
            return True, result['choices'][0]['message']['content']
        else:
            return False, f'AI服务异常: {response.status_code}'
    except Exception as e:
        return False, f'AI服务连接失败: {str(e)}'

def deduct_tokens(user_id, amount):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT ai_tokens FROM users WHERE id = ?', (user_id,))
    row = c.fetchone()
    if not row or row[0] < amount:
        conn.close()
        return False
    c.execute('UPDATE users SET ai_tokens = ai_tokens - ? WHERE id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    return True

def get_token_balance(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT ai_tokens FROM users WHERE id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def purchase_tokens(user_id, fb_amount):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT fb_balance FROM users WHERE id = ?', (user_id,))
    row = c.fetchone()
    if not row or row[0] < fb_amount:
        conn.close()
        return False, 'FB余额不足'
    tokens = fb_amount * 1000
    c.execute('UPDATE users SET fb_balance = fb_balance - ? WHERE id = ?', (fb_amount, user_id))
    c.execute('UPDATE users SET ai_tokens = ai_tokens + ? WHERE id = ?', (tokens, user_id))
    c.execute('INSERT INTO token_purchases (user_id, fb_amount, tokens, timestamp) VALUES (?, ?, ?, ?)',
              (user_id, fb_amount, tokens, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return True, tokens

# ========== 数据库操作 ==========
def get_user_by_username(username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, username, password, fb_balance, ai_tokens FROM users WHERE username = ?', (username,))
    row = c.fetchone()
    conn.close()
    return row

def get_user_by_id(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, username, fb_balance, ai_tokens FROM users WHERE id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, username, fb_balance, ai_tokens FROM users ORDER BY id ASC')
    rows = c.fetchall()
    conn.close()
    return [{'id': r[0], 'username': r[1], 'fb_balance': r[2], 'ai_tokens': r[3]} for r in rows]

def create_user(username, password):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    hashed = hashlib.sha256(password.encode()).hexdigest()
    try:
        c.execute('INSERT INTO users (username, password, created_at, fb_balance, ai_tokens) VALUES (?, ?, ?, ?, ?)',
                  (username, hashed, datetime.now().isoformat(), 3, 0))
        conn.commit()
        user_id = c.lastrowid
        conn.close()
        return user_id
    except sqlite3.IntegrityError:
        conn.close()
        return None

def delete_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM users WHERE id = ?', (user_id,))
    c.execute('DELETE FROM friends WHERE user_id = ? OR friend_id = ?', (user_id, user_id))
    c.execute('DELETE FROM messages WHERE sender_id = ? OR receiver_id = ?', (user_id, user_id))
    c.execute('DELETE FROM group_members WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def get_friend_requests(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT f.id, u.id, u.username, f.created_at
        FROM friends f
        JOIN users u ON u.id = f.user_id
        WHERE f.friend_id = ? AND f.status = 'pending'
    ''', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [{'request_id': r[0], 'user_id': r[1], 'username': r[2], 'created_at': r[3]} for r in rows]

def get_friends(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT u.id, u.username, u.fb_balance
        FROM friends f
        JOIN users u ON u.id = f.friend_id
        WHERE f.user_id = ? AND f.status = 'accepted'
        UNION
        SELECT u.id, u.username, u.fb_balance
        FROM friends f
        JOIN users u ON u.id = f.user_id
        WHERE f.friend_id = ? AND f.status = 'accepted'
    ''', (user_id, user_id))
    rows = c.fetchall()
    conn.close()
    return [{'id': r[0], 'username': r[1], 'fb_balance': r[2]} for r in rows]

def add_friend_request(user_id, friend_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO friends (user_id, friend_id, status, created_at) VALUES (?, ?, ?, ?)',
                  (user_id, friend_id, 'pending', datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True
    except:
        conn.close()
        return False

def accept_friend(request_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE friends SET status = "accepted" WHERE id = ? AND friend_id = ?', (request_id, user_id))
    conn.commit()
    conn.close()

def reject_friend(request_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM friends WHERE id = ? AND friend_id = ?', (request_id, user_id))
    conn.commit()
    conn.close()

def delete_friend(user_id, friend_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM friends WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)',
              (user_id, friend_id, friend_id, user_id))
    conn.commit()
    conn.close()

def delete_group(group_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT owner_id FROM groups WHERE id = ?', (group_id,))
    row = c.fetchone()
    if not row or row[0] != user_id:
        conn.close()
        return False
    c.execute('DELETE FROM groups WHERE id = ?', (group_id,))
    c.execute('DELETE FROM group_members WHERE group_id = ?', (group_id,))
    c.execute('DELETE FROM group_messages WHERE group_id = ?', (group_id,))
    conn.commit()
    conn.close()
    return True

def leave_group(group_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM group_members WHERE group_id = ? AND user_id = ?', (group_id, user_id))
    conn.commit()
    conn.close()

def save_message(sender_id, receiver_id, content, msg_type='text', image_data=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    timestamp = datetime.now().isoformat()
    c.execute('''
        INSERT INTO messages (sender_id, receiver_id, content, timestamp, message_type, image_data)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (sender_id, receiver_id, content, timestamp, msg_type, image_data))
    conn.commit()
    conn.close()

def get_messages(user_id, friend_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT id, sender_id, content, timestamp, message_type, image_data
        FROM messages
        WHERE (sender_id = ? AND receiver_id = ?)
           OR (sender_id = ? AND receiver_id = ?)
        ORDER BY id ASC LIMIT 200
    ''', (user_id, friend_id, friend_id, user_id))
    rows = c.fetchall()
    conn.close()
    return [{
        'id': r[0],
        'sender_id': r[1],
        'content': r[2],
        'timestamp': r[3],
        'message_type': r[4],
        'image_data': r[5] if r[5] else None
    } for r in rows]

def mark_read(user_id, friend_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE messages SET is_read = 1 WHERE sender_id = ? AND receiver_id = ?',
              (friend_id, user_id))
    conn.commit()
    conn.close()

def change_password(user_id, new_password):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    hashed = hashlib.sha256(new_password.encode()).hexdigest()
    c.execute('UPDATE users SET password = ? WHERE id = ?', (hashed, user_id))
    conn.commit()
    conn.close()

def is_admin(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT username FROM users WHERE id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row and row[0] == 'admin'

def get_fb_balance(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT fb_balance FROM users WHERE id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def get_user_transactions(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT t.id, t.from_user_id, t.to_user_id, t.amount, t.timestamp, t.message, t.type,
               u1.username as from_name, u2.username as to_name
        FROM transactions t
        JOIN users u1 ON u1.id = t.from_user_id
        JOIN users u2 ON u2.id = t.to_user_id
        WHERE t.from_user_id = ? OR t.to_user_id = ?
        ORDER BY t.id DESC LIMIT 50
    ''', (user_id, user_id))
    rows = c.fetchall()
    conn.close()
    return [{
        'id': r[0],
        'from_user': r[1],
        'to_user': r[2],
        'amount': r[3],
        'timestamp': r[4],
        'message': r[5] or '',
        'type': r[6],
        'from_name': r[7],
        'to_name': r[8]
    } for r in rows]

# ========== 群聊相关 ==========
def create_group(name, owner_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO groups (name, owner_id, created_at) VALUES (?, ?, ?)',
              (name, owner_id, datetime.now().isoformat()))
    group_id = c.lastrowid
    c.execute('INSERT INTO group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)',
              (group_id, owner_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return group_id

def get_user_groups(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT g.id, g.name, g.owner_id, u.username, g.created_at
        FROM groups g
        JOIN group_members gm ON gm.group_id = g.id
        JOIN users u ON u.id = g.owner_id
        WHERE gm.user_id = ?
        ORDER BY g.id ASC
    ''', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [{
        'id': r[0],
        'name': r[1],
        'owner_id': r[2],
        'owner_name': r[3],
        'created_at': r[4]
    } for r in rows]

def add_group_member(group_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)',
                  (group_id, user_id, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True
    except:
        conn.close()
        return False

def save_group_message(group_id, sender_id, content, msg_type='text', image_data=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    timestamp = datetime.now().isoformat()
    c.execute('''
        INSERT INTO group_messages (group_id, sender_id, content, timestamp, message_type, image_data)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (group_id, sender_id, content, timestamp, msg_type, image_data))
    msg_id = c.lastrowid
    conn.commit()
    conn.close()
    return msg_id

def get_group_messages(group_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT id, sender_id, content, timestamp, message_type, image_data
        FROM group_messages
        WHERE group_id = ?
        ORDER BY id ASC LIMIT 200
    ''', (group_id,))
    rows = c.fetchall()

    messages = []
    for r in rows:
        c.execute('SELECT COUNT(*) FROM group_message_reads WHERE message_id = ? AND user_id = ?',
                  (r[0], user_id))
        is_read = c.fetchone()[0] > 0
        c.execute('SELECT username FROM users WHERE id = ?', (r[1],))
        sender_name = c.fetchone()[0]
        messages.append({
            'id': r[0],
            'sender_id': r[1],
            'sender_name': sender_name,
            'content': r[2],
            'timestamp': r[3],
            'message_type': r[4],
            'image_data': r[5] if r[5] else None,
            'is_read': is_read
        })
    conn.close()
    return messages

def mark_group_message_read(message_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO group_message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)',
                  (message_id, user_id, datetime.now().isoformat()))
        conn.commit()
    except:
        pass
    conn.close()

def get_unread_group_messages_count(user_id, group_id=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if group_id:
        c.execute('''
            SELECT COUNT(*)
            FROM group_messages gm
            LEFT JOIN group_message_reads gmr ON gmr.message_id = gm.id AND gmr.user_id = ?
            WHERE gm.group_id = ? AND gmr.id IS NULL AND gm.sender_id != ?
        ''', (user_id, group_id, user_id))
        count = c.fetchone()[0]
        conn.close()
        return count
    else:
        c.execute('''
            SELECT gm.group_id, COUNT(*) as count
            FROM group_messages gm
            LEFT JOIN group_message_reads gmr ON gmr.message_id = gm.id AND gmr.user_id = ?
            WHERE gmr.id IS NULL AND gm.sender_id != ?
            GROUP BY gm.group_id
        ''', (user_id, user_id))
        rows = c.fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}

# ========== 路由 ==========
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, session=session)

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({'success': False, 'error': '请输入用户名和密码'})
    user = get_user_by_username(username)
    if not user:
        return jsonify({'success': False, 'error': '用户不存在'})
    if user[2] != hashlib.sha256(password.encode()).hexdigest():
        return jsonify({'success': False, 'error': '密码错误'})
    session['logged_in'] = True
    session['username'] = user[1]
    session['user_id'] = user[0]
    return jsonify({'success': True})

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({'success': False, 'error': '请输入用户名和密码'})
    if len(username) < 2:
        return jsonify({'success': False, 'error': '用户名至少2个字符'})
    user_id = create_user(username, password)
    if user_id:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '用户名已存在'})

@app.route('/logout')
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/fb_balance')
def api_fb_balance():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    balance = get_fb_balance(session['user_id'])
    return jsonify({'balance': balance})

@app.route('/api/token_balance')
def api_token_balance():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    tokens = get_token_balance(session['user_id'])
    return jsonify({'tokens': tokens})

@app.route('/api/purchase_tokens', methods=['POST'])
def api_purchase_tokens():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    fb_amount = data.get('fb_amount', 0)
    if fb_amount <= 0:
        return jsonify({'error': '请输入正确的FB数量'})
    success, result = purchase_tokens(session['user_id'], fb_amount)
    if success:
        return jsonify({'success': True, 'tokens': result, 'message': f'成功购买 {result} tokens'})
    return jsonify({'error': result})

@app.route('/api/ai_chat', methods=['POST'])
def api_ai_chat():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    prompt = data.get('prompt', '').strip()
    if not prompt:
        return jsonify({'error': '请输入问题'})
    estimated_tokens = len(prompt) * 2 + 100
    if not deduct_tokens(session['user_id'], estimated_tokens):
        return jsonify({'error': 'Token不足，请购买'})
    success, result = call_ai_api(prompt)
    if success:
        return jsonify({'success': True, 'response': result})
    return jsonify({'error': result})

@app.route('/api/friends')
def api_friends():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    friends = get_friends(session['user_id'])
    return jsonify({'friends': friends})

@app.route('/api/requests')
def api_requests():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    requests = get_friend_requests(session['user_id'])
    return jsonify({'requests': requests})

@app.route('/api/add_friend', methods=['POST'])
def api_add_friend():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    friend_id = data.get('friend_id')
    user_id = session['user_id']
    if friend_id == user_id:
        return jsonify({'error': '不能添加自己'})
    friend = get_user_by_id(friend_id)
    if not friend:
        return jsonify({'error': '用户不存在'})
    if add_friend_request(user_id, friend_id):
        return jsonify({'success': True, 'message': '好友请求已发送'})
    return jsonify({'error': '已发送过请求或已是好友'})

@app.route('/api/handle_request', methods=['POST'])
def api_handle_request():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    request_id = data.get('request_id')
    action = data.get('action')
    user_id = session['user_id']
    if action == 'accept':
        accept_friend(request_id, user_id)
    elif action == 'reject':
        reject_friend(request_id, user_id)
    return jsonify({'success': True})

@app.route('/api/delete_friend', methods=['POST'])
def api_delete_friend():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    friend_id = data.get('friend_id')
    if not friend_id:
        return jsonify({'error': '参数错误'})
    delete_friend(session['user_id'], friend_id)
    return jsonify({'success': True, 'message': '已删除好友'})

@app.route('/api/delete_group', methods=['POST'])
def api_delete_group():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    group_id = data.get('group_id')
    if not group_id:
        return jsonify({'error': '参数错误'})
    if delete_group(group_id, session['user_id']):
        return jsonify({'success': True, 'message': '已删除群聊'})
    return jsonify({'error': '只有群主可以删除群聊'})

@app.route('/api/leave_group', methods=['POST'])
def api_leave_group():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    group_id = data.get('group_id')
    if not group_id:
        return jsonify({'error': '参数错误'})
    leave_group(group_id, session['user_id'])
    return jsonify({'success': True, 'message': '已退出群聊'})

@app.route('/api/send', methods=['POST'])
def api_send():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    receiver_id = data.get('receiver_id')
    content = data.get('content', '').strip()
    msg_type = data.get('message_type', 'text')
    image_data = data.get('image_data')
    if not receiver_id or (not content and not image_data):
        return jsonify({'error': '参数错误'})
    save_message(session['user_id'], receiver_id, content, msg_type, image_data)
    return jsonify({'success': True})

@app.route('/api/messages')
def api_messages():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    friend_id = request.args.get('friend_id', type=int)
    if not friend_id:
        return jsonify({'error': '参数错误'})
    messages = get_messages(session['user_id'], friend_id)
    return jsonify({'messages': messages})

@app.route('/api/mark_read', methods=['POST'])
def api_mark_read():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    friend_id = data.get('friend_id')
    if not friend_id:
        return jsonify({'error': '参数错误'})
    mark_read(session['user_id'], friend_id)
    return jsonify({'success': True})

@app.route('/api/change_password', methods=['POST'])
def api_change_password():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    old_password = data.get('old_password', '').strip()
    new_password = data.get('new_password', '').strip()
    if not old_password or not new_password:
        return jsonify({'error': '请输入旧密码和新密码'})
    if len(new_password) < 4:
        return jsonify({'error': '新密码至少4个字符'})
    user_id = session['user_id']
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT password FROM users WHERE id = ?', (user_id,))
    row = c.fetchone()
    if not row or row[0] != hashlib.sha256(old_password.encode()).hexdigest():
        conn.close()
        return jsonify({'error': '旧密码错误'})
    change_password(user_id, new_password)
    conn.close()
    return jsonify({'success': True, 'message': '密码修改成功'})

@app.route('/api/transfer', methods=['POST'])
def api_transfer():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401

    data = request.json
    to_user_id = data.get('to_user_id')
    amount = data.get('amount', 0)
    message = data.get('message', '')

    if not to_user_id:
        return jsonify({'error': '请输入对方ID'})
    if amount <= 0:
        return jsonify({'error': '金额必须大于0'})
    if to_user_id == session['user_id']:
        return jsonify({'error': '不能给自己转账'})

    to_user = get_user_by_id(to_user_id)
    if not to_user:
        return jsonify({'error': '对方用户不存在'})

    from_balance = get_fb_balance(session['user_id'])
    if from_balance < amount:
        return jsonify({'error': f'余额不足，当前只有 {from_balance} FB'})

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('UPDATE users SET fb_balance = fb_balance - ? WHERE id = ?', (amount, session['user_id']))
        c.execute('UPDATE users SET fb_balance = fb_balance + ? WHERE id = ?', (amount, to_user_id))
        c.execute('''
            INSERT INTO transactions (from_user_id, to_user_id, amount, timestamp, message, type)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (session['user_id'], to_user_id, amount, datetime.now().isoformat(), message, 'transfer'))
        conn.commit()
        conn.close()
    except Exception as e:
        conn.close()
        return jsonify({'error': f'转账失败: {str(e)}'})

    save_message(0, to_user_id, f'💰 收到来自 {session["username"]} 的转账 {amount} FB' + (f' (备注: {message})' if message else ''), 'system')
    save_message(0, session['user_id'], f'💸 已转账 {amount} FB 给 {to_user[1]}' + (f' (备注: {message})' if message else ''), 'system')

    return jsonify({
        'success': True,
        'message': f'成功转账 {amount} FB 给 {to_user[1]}',
        'new_balance': get_fb_balance(session['user_id'])
    })

@app.route('/api/transactions')
def api_transactions():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    transactions = get_user_transactions(session['user_id'])
    return jsonify({'transactions': transactions})

@app.route('/api/groups')
def api_groups():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    groups = get_user_groups(session['user_id'])
    return jsonify({'groups': groups})

@app.route('/api/create_group', methods=['POST'])
def api_create_group():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    name = data.get('name', '').strip()
    member_ids = data.get('member_ids', [])
    if not name:
        return jsonify({'error': '请输入群名称'})
    group_id = create_group(name, session['user_id'])
    for member_id in member_ids:
        if member_id != session['user_id']:
            add_group_member(group_id, member_id)
    return jsonify({'success': True, 'group_id': group_id})

@app.route('/api/add_group_member', methods=['POST'])
def api_add_group_member():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    group_id = data.get('group_id')
    user_id = data.get('user_id')
    if not group_id or not user_id:
        return jsonify({'error': '参数错误'})
    if add_group_member(group_id, user_id):
        return jsonify({'success': True})
    return jsonify({'error': '添加失败'})

@app.route('/api/send_group_message', methods=['POST'])
def api_send_group_message():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    group_id = data.get('group_id')
    content = data.get('content', '').strip()
    msg_type = data.get('message_type', 'text')
    image_data = data.get('image_data')
    if not group_id or (not content and not image_data):
        return jsonify({'error': '参数错误'})
    msg_id = save_group_message(group_id, session['user_id'], content, msg_type, image_data)
    return jsonify({'success': True, 'message_id': msg_id})

@app.route('/api/group_messages')
def api_group_messages():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    group_id = request.args.get('group_id', type=int)
    if not group_id:
        return jsonify({'error': '参数错误'})
    messages = get_group_messages(group_id, session['user_id'])
    return jsonify({'messages': messages})

@app.route('/api/mark_group_read', methods=['POST'])
def api_mark_group_read():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    data = request.json
    message_ids = data.get('message_ids', [])
    for msg_id in message_ids:
        mark_group_message_read(msg_id, session['user_id'])
    return jsonify({'success': True})

@app.route('/api/group_unread_count')
def api_group_unread_count():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    counts = get_unread_group_messages_count(session['user_id'])
    return jsonify({'counts': counts})

@app.route('/api/admin/users', methods=['GET'])
def admin_users():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    if not is_admin(session['user_id']):
        return jsonify({'error': '权限不足'}), 403
    users = get_all_users()
    return jsonify({'users': users})

@app.route('/api/admin/delete_user', methods=['POST'])
def admin_delete_user():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401
    if not is_admin(session['user_id']):
        return jsonify({'error': '权限不足'}), 403
    data = request.json
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'error': '参数错误'})
    if user_id == session['user_id']:
        return jsonify({'error': '不能删除自己'})
    delete_user(user_id)
    return jsonify({'success': True, 'message': '用户已删除'})

# ========== HTML模板 ==========
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Hello Chat Pro</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        html, body { width:100%; height:100%; overflow:hidden; background:#0f0f1a; font-family:system-ui, -apple-system, sans-serif; -webkit-tap-highlight-color:transparent; touch-action:manipulation; }
        :root {
            --bg-primary: #1a1a2e;
            --bg-secondary: #0f0f22;
            --bg-sidebar: #121225;
            --bg-input: #0d0d1f;
            --bg-header: #16213e;
            --text-primary: #e0e0ff;
            --text-secondary: #7a7aaa;
            --text-muted: #444466;
            --accent: #3b82f6;
            --accent-purple: #8b5cf6;
            --accent-green: #4ade80;
            --accent-yellow: #fbbf24;
            --border-color: #2a2a4a;
            --radius-full: 9999px;
        }
        .app { display:flex; flex-direction:column; width:100%; height:100%; max-width:520px; margin:0 auto; background:var(--bg-primary); overflow:hidden; }
        .header { padding:12px 16px 10px; background:var(--bg-header); border-bottom:1px solid var(--border-color); flex-shrink:0; }
        .header h1 { color:var(--text-primary); font-size:17px; display:flex; justify-content:space-between; align-items:center; }
        .header h1 span { background:var(--accent); color:#fff; font-size:10px; padding:2px 12px; border-radius:var(--radius-full); font-weight:600; }
        .login-box { padding:30px 20px; flex:1; display:flex; flex-direction:column; justify-content:center; }
        .login-box h2 { color:var(--text-primary); text-align:center; margin-bottom:20px; font-size:20px; }
        .login-box input { width:100%; padding:14px 18px; margin-bottom:12px; background:var(--bg-input); border:1px solid var(--border-color); border-radius:var(--radius-full); color:var(--text-primary); font-size:16px; outline:none; -webkit-appearance:none; }
        .login-box input:focus { border-color:var(--accent); }
        .login-box button { width:100%; padding:14px; border:none; border-radius:var(--radius-full); background:var(--accent); color:#fff; font-size:16px; font-weight:600; cursor:pointer; touch-action:manipulation; }
        .login-box button:active { transform:scale(0.96); }
        .login-box .error { color:#f87171; text-align:center; margin-top:10px; font-size:14px; }
        .login-box .switch { color:var(--text-secondary); text-align:center; margin-top:12px; font-size:14px; cursor:pointer; }
        .top-bar { display:flex; justify-content:space-between; align-items:center; padding:6px 16px 4px; flex-shrink:0; flex-wrap:wrap; gap:4px; background:var(--bg-secondary); border-bottom:1px solid var(--border-color); }
        .top-bar .user { color:#4ade80; font-size:12px; }
        .top-bar .fb-info { color:var(--accent-yellow); font-size:12px; font-weight:600; cursor:pointer; padding:2px 8px; border:1px solid var(--accent-yellow); border-radius:var(--radius-full); }
        .top-bar .token-info { color:var(--accent-purple); font-size:12px; font-weight:600; cursor:pointer; padding:2px 8px; border:1px solid var(--accent-purple); border-radius:var(--radius-full); }
        .top-bar .actions { display:flex; gap:10px; align-items:center; }
        .top-bar .actions span { color:var(--text-secondary); font-size:12px; cursor:pointer; }
        .top-bar .logout { color:#f87171 !important; }
        .main { display:flex; flex:1; min-height:0; }
        .sidebar { width:44%; background:var(--bg-sidebar); border-right:1px solid var(--border-color); overflow-y:auto; padding:8px 8px; flex-shrink:0; -webkit-overflow-scrolling:touch; }
        .sidebar .section-title { color:var(--text-secondary); font-size:10px; margin:8px 0 4px; font-weight:600; letter-spacing:0.3px; }
        .sidebar .friend-item, .sidebar .group-item { display:flex; justify-content:space-between; align-items:center; padding:6px 8px; border-radius:8px; color:var(--text-primary); font-size:13px; cursor:pointer; margin-bottom:2px; touch-action:manipulation; position:relative; }
        .sidebar .friend-item:active, .sidebar .group-item:active { background:#1e1e3a; }
        .sidebar .friend-item.active, .sidebar .group-item.active { background:rgba(59,130,246,0.2); color:#8ab4ff; }
        .sidebar .friend-item .name, .sidebar .group-item .name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .sidebar .friend-item .btn-chat, .sidebar .group-item .btn-chat { background:var(--accent); color:#fff; border:none; border-radius:var(--radius-full); padding:2px 10px; font-size:10px; cursor:pointer; touch-action:manipulation; flex-shrink:0; margin-left:4px; }
        .sidebar .friend-item .btn-chat:active, .sidebar .group-item .btn-chat:active { transform:scale(0.92); }
        .sidebar .friend-item .unread-badge, .sidebar .group-item .unread-badge { background:#f87171; color:#fff; border-radius:50%; padding:1px 6px; font-size:9px; margin-left:4px; }
        .sidebar .friend-item .delete-btn, .sidebar .group-item .delete-btn {
            background:#f87171; color:#fff; border:none; border-radius:50%; width:20px; height:20px; font-size:10px; cursor:pointer; margin-left:4px; display:none; align-items:center; justify-content:center;
        }
        .sidebar .friend-item.show-delete .delete-btn, .sidebar .group-item.show-delete .delete-btn { display:flex; }
        .sidebar .friend-item .delete-btn:active, .sidebar .group-item .delete-btn:active { transform:scale(0.9); }
        .sidebar .request-item { display:flex; justify-content:space-between; align-items:center; padding:4px 6px; border-bottom:1px solid rgba(255,255,255,0.05); color:var(--text-primary); font-size:12px; }
        .sidebar .request-item .name { color:#f5d6b3; }
        .sidebar .request-item .btn-sm { border:none; border-radius:var(--radius-full); padding:2px 8px; font-size:10px; cursor:pointer; touch-action:manipulation; }
        .sidebar .request-item .btn-accept { background:#4ade80; color:#000; }
        .sidebar .request-item .btn-reject { background:#f87171; color:#fff; }
        .sidebar .request-item .btn-sm:active { transform:scale(0.92); }
        .sidebar .add-row { display:flex; gap:4px; margin:4px 0; flex-wrap:wrap; }
        .sidebar .add-row input { flex:1; padding:6px 10px; border-radius:var(--radius-full); border:1px solid var(--border-color); background:var(--bg-input); color:var(--text-primary); font-size:12px; outline:none; min-width:0; -webkit-appearance:none; }
        .sidebar .add-row input:focus { border-color:var(--accent); }
        .sidebar .add-row button { padding:6px 12px; border:none; border-radius:var(--radius-full); background:var(--accent-purple); color:#fff; font-size:11px; cursor:pointer; touch-action:manipulation; flex-shrink:0; }
        .sidebar .add-row button:active { transform:scale(0.92); }
        .sidebar .add-row .btn-group { background:var(--accent-green); color:#000; }
        .chat-area { flex:1; display:flex; flex-direction:column; background:var(--bg-secondary); min-width:0; }
        .chat-area .chat-header { padding:8px 12px; color:var(--text-primary); font-size:13px; border-bottom:1px solid var(--border-color); flex-shrink:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; background:var(--bg-sidebar); }
        .chat-area .chat-header .empty { color:var(--text-muted); }
        .messages { flex:1; padding:8px 10px; overflow-y:auto; display:flex; flex-direction:column; gap:4px; -webkit-overflow-scrolling:touch; }
        .msg { max-width:85%; padding:8px 12px; border-radius:14px; font-size:14px; line-height:1.5; word-break:break-word; animation:fadeIn 0.2s ease; }
        .msg.user { align-self:flex-end; background:var(--accent); color:#fff; border-bottom-right-radius:3px; }
        .msg.other { align-self:flex-start; background:#26264a; color:var(--text-primary); border-bottom-left-radius:3px; }
        .msg.system { align-self:center; background:rgba(255,255,255,0.05); color:var(--text-secondary); font-size:12px; padding:4px 12px; border-radius:12px; }
        .msg .time { display:block; text-align:right; font-size:9px; opacity:0.4; margin-top:2px; }
        .msg img { max-width:100%; border-radius:8px; cursor:pointer; margin-top:4px; }
        .msg-system { text-align:center; font-size:12px; color:var(--text-muted); padding:4px 0; }
        .input-area { padding:6px 10px 12px; background:var(--bg-sidebar); border-top:1px solid var(--border-color); display:flex; gap:6px; align-items:center; flex-shrink:0; padding-bottom:calc(12px + env(safe-area-inset-bottom, 0px)); z-index:10; position:relative; flex-wrap:wrap; }
        .input-area .wrap { flex:1; display:flex; align-items:center; background:var(--bg-input); border-radius:24px; border:1px solid var(--border-color); padding:0 16px; min-height:42px; transition:border 0.2s; cursor:text; touch-action:manipulation; min-width:100px; }
        .input-area .wrap:focus-within { border-color:var(--accent); }
        .input-area textarea { flex:1; padding:10px 0; border:none; background:transparent; color:var(--text-primary); font-size:16px; resize:none; font-family:inherit; outline:none; min-height:24px; max-height:100px; line-height:1.4; width:100%; -webkit-appearance:none; appearance:none; touch-action:manipulation; }
        .input-area textarea::placeholder { color:var(--text-muted); }
        .input-area textarea:disabled { opacity:0.35; }
        .input-area .action-btn { background:transparent; border:none; color:var(--text-secondary); font-size:20px; cursor:pointer; padding:4px 8px; touch-action:manipulation; flex-shrink:0; }
        .input-area .action-btn:active { transform:scale(0.92); }
        .input-area .action-btn:hover { color:var(--text-primary); }
        .input-area .send-btn { padding:10px 16px; border:none; border-radius:var(--radius-full); background:var(--accent); color:#fff; font-weight:600; font-size:14px; cursor:pointer; flex-shrink:0; min-height:42px; min-width:56px; touch-action:manipulation; }
        .input-area .send-btn:active { transform:scale(0.94); }
        .input-area .send-btn:disabled { opacity:0.35; }
        .refresh-btn { background:transparent; border:none; color:#4ade80; font-size:16px; cursor:pointer; padding:4px; touch-action:manipulation; }
        .refresh-btn:active { transform:scale(0.8); }
        #fileInput { display:none; }
        .modal { position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.7); display:none; justify-content:center; align-items:center; z-index:100; padding:20px; }
        .modal.show { display:flex !important; }
        .modal-content { background:var(--bg-primary); padding:24px; border-radius:20px; width:100%; max-width:400px; max-height:80vh; overflow-y:auto; border:1px solid var(--border-color); }
        .modal-content h3 { color:var(--text-primary); margin-bottom:16px; display:flex; justify-content:space-between; align-items:center; }
        .modal-content input, .modal-content select { width:100%; padding:12px 16px; margin-bottom:10px; background:var(--bg-input); border:1px solid var(--border-color); border-radius:var(--radius-full); color:var(--text-primary); font-size:14px; outline:none; -webkit-appearance:none; }
        .modal-content input:focus, .modal-content select:focus { border-color:var(--accent); }
        .modal-content button { width:100%; padding:12px; border:none; border-radius:var(--radius-full); background:var(--accent); color:#fff; font-size:15px; font-weight:600; cursor:pointer; touch-action:manipulation; }
        .modal-content button:active { transform:scale(0.96); }
        .modal-content .close { color:var(--text-secondary); cursor:pointer; font-size:20px; }
        .modal-content .btn-group { display:flex; gap:8px; }
        .modal-content .btn-group button { flex:1; }
        .image-modal { position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.9); display:none; justify-content:center; align-items:center; z-index:200; padding:20px; flex-direction:column; }
        .image-modal.show { display:flex; }
        .image-modal img { max-width:95%; max-height:75%; object-fit:contain; border-radius:8px; }
        .image-modal .close { position:absolute; top:20px; right:20px; color:#fff; font-size:30px; cursor:pointer; z-index:201; }
        .image-modal .download-btn { margin-top:16px; padding:10px 24px; background:#3b82f6; color:#fff; border:none; border-radius:var(--radius-full); font-size:14px; cursor:pointer; }
        .image-modal .download-btn:active { transform:scale(0.96); }
        .fb-transaction-item { padding:8px 0; border-bottom:1px solid var(--border-color); color:var(--text-secondary); font-size:13px; }
        .fb-transaction-item .amount { font-weight:600; }
        .fb-transaction-item .amount.positive { color:#4ade80; }
        .fb-transaction-item .amount.negative { color:#f87171; }
        @media (min-width: 521px) { .app { border-radius:28px; height:90vh; margin-top:5vh; box-shadow:0 25px 60px rgba(0,0,0,0.8); border:1px solid var(--border-color); } .sidebar { width:40%; } .input-area textarea { font-size:14px; } }
        @media (max-width: 520px) { .app { border-radius:0; height:100%; max-height:100%; } .sidebar { width:42%; padding:6px 6px; } .sidebar .friend-item, .sidebar .group-item { font-size:12px; padding:4px 6px; } .input-area { padding:4px 8px 10px; gap:4px; } .input-area .wrap { min-height:38px; padding:0 12px; } .input-area textarea { font-size:15px; min-height:22px; padding:8px 0; } .input-area .send-btn { min-height:38px; padding:8px 12px; font-size:13px; min-width:48px; } .input-area .action-btn { font-size:18px; padding:2px 4px; } .header h1 { font-size:15px; } .top-bar .user { font-size:11px; } .messages .msg { font-size:13px; padding:6px 10px; } .chat-area .chat-header { font-size:12px; padding:6px 10px; } }
        @media (max-width: 380px) { .sidebar { width:36%; padding:4px 4px; } .sidebar .friend-item, .sidebar .group-item { font-size:11px; padding:3px 4px; } .input-area textarea { font-size:14px; } .input-area .send-btn { font-size:12px; padding:6px 10px; min-height:34px; min-width:42px; } .input-area .wrap { min-height:34px; padding:0 10px; } .messages .msg { font-size:12px; padding:5px 8px; } }
        @supports (padding: max(0px)) { .app { padding-left:max(0px, env(safe-area-inset-left)); padding-right:max(0px, env(safe-area-inset-right)); } .input-area { padding-bottom:max(12px, env(safe-area-inset-bottom)); } }
        @keyframes fadeIn { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:translateY(0)} }
        /* 下拉刷新提示 */
        .pull-to-refresh { text-align:center; color:var(--text-muted); font-size:12px; padding:6px 0; display:none; }
        .pull-to-refresh.show { display:block; }
    </style>
</head>
<body>
<div class="app">
    <div class="header">
        <h1>🥓 Hello Chat Pro <span>v2.1</span></h1>
    </div>

    {% if not session.logged_in %}
    <div class="login-box">
        <h2>🔐 登录</h2>
        <input type="text" id="loginUser" placeholder="用户名" inputmode="text" autocomplete="username">
        <input type="password" id="loginPass" placeholder="密码" autocomplete="current-password">
        <button onclick="login()">登录</button>
        <div class="error" id="loginError"></div>
        <div class="switch" onclick="toggleRegister()">没有账号？点击注册 (送3FB)</div>
        <div id="registerBox" style="display:none;margin-top:16px;">
            <input type="text" id="regUser" placeholder="新用户名" inputmode="text" autocomplete="off">
            <input type="password" id="regPass" placeholder="新密码" autocomplete="new-password">
            <button onclick="register()" style="background:#8b5cf6;">注册 (送3FB)</button>
            <div class="error" id="regError"></div>
        </div>
    </div>
    {% else %}
    <div class="top-bar">
        <span class="user">✅ {{ session.username }} (ID:{{ session.user_id }})</span>
        <span class="fb-info" onclick="showFBInfo()">💰 FB: <span id="fbBalance">0</span></span>
        <span class="token-info" onclick="showTokenInfo()">🔮 <span id="tokenBalance">0</span></span>
        <div class="actions">
            <span onclick="openChangePassword()">🔑改密码</span>
            <span class="logout" onclick="logout()">退出</span>
        </div>
    </div>

    <div class="main">
        <div class="sidebar" id="sidebar">
            <div class="pull-to-refresh" id="pullHint">⬇️ 下拉刷新好友列表</div>
            <div class="section-title">➕ 添加好友/群聊</div>
            <div class="add-row">
                <input type="number" id="friendIdInput" placeholder="用户ID" inputmode="numeric" min="1">
                <button onclick="addFriend()">加好友</button>
                <button onclick="openGroupModal()" class="btn-group">建群</button>
                <button class="refresh-btn" onclick="fullRefresh()" title="刷新好友">🔄</button>
            </div>
            <div id="addResult" style="color:#7a7aaa;font-size:11px;margin-bottom:4px;"></div>

            <div class="section-title" id="requestTitle">📩 好友请求</div>
            <div id="requestList"></div>

            <div class="section-title">👥 好友 <span style="color:#f87171;font-size:10px;">(长按显示×删除)</span></div>
            <div id="friendList"></div>

            <div class="section-title">👥 群聊 <span style="color:#f87171;font-size:10px;">(长按显示×退出/删除)</span></div>
            <div id="groupList"></div>
        </div>

        <div class="chat-area">
            <div class="chat-header" id="chatHeader"><span class="empty">💬 选择好友或群聊开始聊天</span></div>
            <div class="messages" id="msgBox"><div class="msg-system">选择左侧开始聊天</div></div>

            <div class="input-area" id="inputArea">
                <div class="wrap" id="inputWrap" onclick="document.getElementById('msgInput').focus();">
                    <textarea id="msgInput" rows="1" placeholder="输入消息..." enterkeyhint="send" disabled></textarea>
                </div>
                <button class="action-btn" onclick="document.getElementById('fileInput').click();" title="发送图片">📎</button>
                <input type="file" id="fileInput" accept="image/*" onchange="sendImage(event)">
                <button class="action-btn" onclick="showTransferModal()" title="转账FB">💸</button>
                <button class="action-btn" onclick="showAIModal()" title="AI助手" style="color:#8b5cf6;">🤖</button>
                <button class="send-btn" id="sendBtn" disabled onclick="sendMsg()">发送</button>
            </div>
        </div>
    </div>
    {% endif %}
</div>

<!-- 图片预览 -->
<div class="image-modal" id="imageModal">
    <span class="close" onclick="closeImagePreview()">&times;</span>
    <img id="previewImage" src="" alt="图片预览">
    <button class="download-btn" onclick="downloadImage()">📥 下载图片</button>
</div>

<!-- 转账弹窗 -->
<div class="modal" id="transferModal">
    <div class="modal-content">
        <h3>💸 转账FB <span class="close" onclick="closeTransferModal()">✕</span></h3>
        <input type="number" id="transferUserId" placeholder="对方ID" inputmode="numeric" min="1">
        <input type="number" id="transferAmount" placeholder="金额" inputmode="numeric" min="1">
        <input type="text" id="transferMessage" placeholder="留言 (可选)">
        <button onclick="sendTransfer()" style="background:var(--accent-green);color:#000;font-weight:bold;">确认转账</button>
        <div id="transferResult" style="color:#f87171;text-align:center;margin-top:8px;font-size:13px;"></div>
    </div>
</div>

<!-- FB钱包弹窗 -->
<div class="modal" id="fbInfoModal">
    <div class="modal-content">
        <h3>💰 FB钱包 <span class="close" onclick="closeFBInfo()">✕</span></h3>
        <div style="color:var(--text-primary);font-size:18px;text-align:center;padding:12px;background:var(--bg-input);border-radius:12px;margin-bottom:16px;">
            余额: <span id="fbBalanceModal" style="color:var(--accent-yellow);font-weight:700;">0</span> FB
        </div>
        <div style="color:var(--text-secondary);font-size:13px;margin:12px 0 8px;">📜 交易记录</div>
        <div id="transactionHistory"></div>
    </div>
</div>

<!-- Token弹窗 -->
<div class="modal" id="tokenModal">
    <div class="modal-content">
        <h3>🔮 Token钱包 <span class="close" onclick="closeTokenModal()">✕</span></h3>
        <div style="color:var(--text-primary);font-size:18px;text-align:center;padding:12px;background:var(--bg-input);border-radius:12px;margin-bottom:16px;">
            Token: <span id="tokenBalanceModal" style="color:var(--accent-purple);font-weight:700;">0</span>
        </div>
        <div style="color:var(--text-secondary);font-size:13px;margin-bottom:8px;">💡 1 FB = 1000 Tokens (约1000字)</div>
        <input type="number" id="purchaseAmount" placeholder="购买FB数量" inputmode="numeric" min="1">
        <button onclick="purchaseTokens()" style="background:var(--accent-purple);">购买Tokens</button>
        <div id="purchaseResult" style="color:#f87171;text-align:center;margin-top:8px;font-size:13px;"></div>
    </div>
</div>

<!-- AI弹窗 -->
<div class="modal" id="aiModal">
    <div class="modal-content" style="max-width:450px;">
        <h3>🤖 AI助手 <span class="close" onclick="closeAIModal()">✕</span></h3>
        <div style="color:var(--text-secondary);font-size:12px;margin-bottom:8px;">消耗Token，1 Token≈1字</div>
        <textarea id="aiPrompt" rows="3" placeholder="输入你的问题..." style="width:100%;padding:12px;background:var(--bg-input);border:1px solid var(--border-color);border-radius:12px;color:var(--text-primary);font-size:14px;resize:vertical;min-height:80px;outline:none;"></textarea>
        <button onclick="askAI()" style="margin-top:10px;">发送给AI</button>
        <div id="aiResult" style="color:var(--text-primary);margin-top:12px;padding:12px;background:var(--bg-input);border-radius:12px;min-height:60px;white-space:pre-wrap;word-break:break-word;display:none;"></div>
    </div>
</div>

<!-- 群聊弹窗 -->
<div class="modal" id="groupModal">
    <div class="modal-content">
        <h3>👥 创建群聊 <span class="close" onclick="closeGroupModal()">✕</span></h3>
        <input type="text" id="groupNameInput" placeholder="群名称">
        <input type="text" id="groupMembersInput" placeholder="邀请用户ID (用逗号分隔)">
        <button onclick="createGroupSubmit()">创建群聊</button>
        <div id="groupResult" style="color:#f87171;text-align:center;margin-top:8px;font-size:13px;"></div>
    </div>
</div>

<!-- 修改密码弹窗 -->
<div class="modal" id="changePwdModal">
    <div class="modal-content">
        <h3>🔑 修改密码 <span class="close" onclick="closeChangePassword()">✕</span></h3>
        <input type="password" id="oldPwd" placeholder="旧密码">
        <input type="password" id="newPwd" placeholder="新密码（至少4位）">
        <input type="password" id="confirmPwd" placeholder="确认新密码">
        <button onclick="changePassword()">确认修改</button>
        <div id="pwdResult" style="color:#f87171;text-align:center;margin-top:8px;font-size:13px;"></div>
    </div>
</div>

<script>
{% if not session.logged_in %}
function login() {
    const user = document.getElementById('loginUser').value.trim();
    const pass = document.getElementById('loginPass').value.trim();
    if (!user || !pass) { document.getElementById('loginError').textContent = '请输入用户名和密码'; return; }
    fetch('/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:user, password:pass}) })
    .then(r=>r.json()).then(data=>{
        if (data.success) { location.reload(); }
        else { document.getElementById('loginError').textContent = data.error; }
    });
}
function register() {
    const user = document.getElementById('regUser').value.trim();
    const pass = document.getElementById('regPass').value.trim();
    if (!user || !pass) { document.getElementById('regError').textContent = '请输入用户名和密码'; return; }
    if (user.length < 2) { document.getElementById('regError').textContent = '用户名至少2个字符'; return; }
    fetch('/register', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:user, password:pass}) })
    .then(r=>r.json()).then(data=>{
        if (data.success) { document.getElementById('regError').style.color='#4ade80'; document.getElementById('regError').textContent='✅ 注册成功，请登录 (已赠送3FB)'; }
        else { document.getElementById('regError').style.color='#f87171'; document.getElementById('regError').textContent=data.error; }
    });
}
function toggleRegister() { const box=document.getElementById('registerBox'); box.style.display=box.style.display==='none'?'block':'none'; }
{% else %}
// ===== 全局状态 =====
let currentChatType = null;
let currentChatId = null;
let currentChatName = '';
let loading = false;
let currentImageData = null;
const myUserId = {{ session.user_id }};
const msgBox = document.getElementById('msgBox');
const msgInput = document.getElementById('msgInput');
const sendBtn = document.getElementById('sendBtn');
let renderedMessageIds = new Set();
let refreshTimer = null;

// ===== 强制刷新全部数据 =====
function fullRefresh() {
    const hint = document.getElementById('pullHint');
    hint.textContent = '🔄 刷新中...';
    hint.classList.add('show');
    setTimeout(() => {
        loadFriends();
        loadGroups();
        loadRequests();
        updateFBBalance();
        updateTokenBalance();
        hint.textContent = '✅ 已刷新';
        setTimeout(() => { hint.classList.remove('show'); }, 1000);
    }, 300);
}

// ===== 下拉刷新（手机专用） =====
let touchStartY = 0;
const sidebar = document.getElementById('sidebar');
sidebar.addEventListener('touchstart', function(e) {
    if (sidebar.scrollTop === 0) {
        touchStartY = e.touches[0].clientY;
    }
});
sidebar.addEventListener('touchmove', function(e) {
    if (sidebar.scrollTop === 0 && e.touches[0].clientY - touchStartY > 80) {
        fullRefresh();
        touchStartY = e.touches[0].clientY;
    }
});

// ===== 长按显示删除 =====
function setupLongPress(element, callback) {
    let timer = null;
    let isLongPress = false;

    element.addEventListener('touchstart', function(e) {
        isLongPress = false;
        timer = setTimeout(function() {
            isLongPress = true;
            callback(e);
        }, 800);
    });

    element.addEventListener('touchend', function(e) {
        clearTimeout(timer);
        if (!isLongPress) {
            const nameEl = element.querySelector('.name');
            if (nameEl && nameEl.onclick) {
                nameEl.onclick(e);
            }
        }
    });

    element.addEventListener('touchmove', function(e) {
        clearTimeout(timer);
    });

    element.addEventListener('contextmenu', function(e) {
        e.preventDefault();
        callback(e);
    });
}

// ===== 更新余额 =====
function updateFBBalance() {
    fetch('/api/fb_balance').then(r=>r.json()).then(data=>{
        if (data.balance !== undefined) {
            document.getElementById('fbBalance').textContent = data.balance;
        }
    });
}
function updateTokenBalance() {
    fetch('/api/token_balance').then(r=>r.json()).then(data=>{
        if (data.tokens !== undefined) {
            document.getElementById('tokenBalance').textContent = data.tokens;
        }
    });
}

// ===== 输入框处理 =====
document.getElementById('inputWrap').addEventListener('click', function() {
    msgInput.focus();
    setTimeout(()=>msgInput.focus(), 50);
});
msgInput.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 100) + 'px';
});

// ===== 消息渲染 =====
function addMsg(text, type, time, msgType='text', imageData=null, msgId=null) {
    if (msgId && renderedMessageIds.has(msgId)) {
        return;
    }
    if (msgId) {
        renderedMessageIds.add(msgId);
    }
    const d = document.createElement('div');
    d.className = 'msg ' + type;
    const t = time || new Date().toTimeString().slice(0,5);
    let content = text;
    if (msgType === 'image' && imageData) {
        content = text + `<br><img src="${imageData}" onclick="previewImage('${imageData}')" alt="图片">`;
    }
    d.innerHTML = content + `<span class="time">${t}</span>`;
    msgBox.appendChild(d);
    msgBox.scrollTop = msgBox.scrollHeight;
}
function addSys(text) {
    const d = document.createElement('div');
    d.className = 'msg-system';
    d.textContent = text;
    msgBox.appendChild(d);
    msgBox.scrollTop = msgBox.scrollHeight;
}

// ===== 图片预览和下载 =====
function previewImage(src) {
    currentImageData = src;
    document.getElementById('previewImage').src = src;
    document.getElementById('imageModal').classList.add('show');
}
function closeImagePreview() {
    document.getElementById('imageModal').classList.remove('show');
}
function downloadImage() {
    if (currentImageData) {
        const link = document.createElement('a');
        link.href = currentImageData;
        link.download = 'image_' + Date.now() + '.png';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }
}

// ===== 加载数据（修复版，确保显示） =====
function loadFriends() {
    fetch('/api/friends').then(r=>r.json()).then(data=>{
        const list = document.getElementById('friendList');
        let html = '';
        if (data.friends && data.friends.length>0) {
            data.friends.forEach(f=>{
                const active = (currentChatType==='friend' && currentChatId===f.id) ? ' active' : '';
                html += `
                    <div class="friend-item${active}">
                        <span class="name" onclick="openChat('friend',${f.id},'${f.username}')">${f.username} (ID:${f.id})</span>
                        <button class="btn-chat" onclick="openChat('friend',${f.id},'${f.username}')">💬</button>
                        <button class="delete-btn" onclick="deleteFriend(${f.id})" title="删除好友">×</button>
                    </div>
                `;
            });
        } else {
            html = '<div style="color:#444466;font-size:12px;padding:4px 0;">暂无好友</div>';
        }
        list.innerHTML = html;
        list.querySelectorAll('.friend-item').forEach(el => {
            setupLongPress(el, function(e) {
                el.classList.toggle('show-delete');
            });
        });
    }).catch(()=>{
        document.getElementById('friendList').innerHTML = '<div style="color:#f87171;font-size:12px;">⚠️ 加载好友失败</div>';
    });
}

function loadGroups() {
    fetch('/api/groups').then(r=>r.json()).then(data=>{
        const list = document.getElementById('groupList');
        let html = '';
        if (data.groups && data.groups.length>0) {
            fetch('/api/group_unread_count').then(r=>r.json()).then(unreadData=>{
                const counts = unreadData.counts || {};
                data.groups.forEach(g=>{
                    const active = (currentChatType==='group' && currentChatId===g.id) ? ' active' : '';
                    const unread = counts[g.id] || 0;
                    const isOwner = g.owner_id === myUserId;
                    html += `
                        <div class="group-item${active}">
                            <span class="name" onclick="openChat('group',${g.id},'${g.name}')">👥 ${g.name}</span>
                            ${unread>0 ? `<span class="unread-badge">${unread}</span>` : ''}
                            <button class="btn-chat" onclick="openChat('group',${g.id},'${g.name}')">💬</button>
                            <button class="delete-btn" onclick="${isOwner ? 'deleteGroup('+g.id+')' : 'leaveGroup('+g.id+')'}" title="${isOwner ? '删除群聊' : '退出群聊'}">×</button>
                        </div>
                    `;
                });
                list.innerHTML = html;
                list.querySelectorAll('.group-item').forEach(el => {
                    setupLongPress(el, function(e) {
                        el.classList.toggle('show-delete');
                    });
                });
            });
        } else {
            html = '<div style="color:#444466;font-size:12px;padding:4px 0;">暂无群聊</div>';
            list.innerHTML = html;
        }
    }).catch(()=>{
        document.getElementById('groupList').innerHTML = '<div style="color:#f87171;font-size:12px;">⚠️ 加载群聊失败</div>';
    });
}

function loadRequests() {
    fetch('/api/requests').then(r=>r.json()).then(data=>{
        const list = document.getElementById('requestList');
        if (data.requests && data.requests.length>0) {
            let html = '';
            data.requests.forEach(r=>{
                html += `<div class="request-item"><span class="name">${r.username} (ID:${r.user_id})</span>
                    <span><button class="btn-sm btn-accept" onclick="handleRequest(${r.request_id},'accept')">接受</button>
                    <button class="btn-sm btn-reject" onclick="handleRequest(${r.request_id},'reject')">拒绝</button></span></div>`;
            });
            list.innerHTML = html;
        } else {
            list.innerHTML = '<div style="color:#444466;font-size:11px;padding:4px 0;">暂无好友请求</div>';
        }
    });
}

// ===== 好友/群聊操作 =====
function handleRequest(requestId, action) {
    fetch('/api/handle_request', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({request_id:requestId, action:action}) })
    .then(()=>{ loadRequests(); loadFriends(); });
}

function addFriend() {
    const input = document.getElementById('friendIdInput');
    const friendId = input.value.trim();
    if (!friendId) { document.getElementById('addResult').textContent='⚠️ 请输入对方ID'; return; }
    fetch('/api/add_friend', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({friend_id:parseInt(friendId)}) })
    .then(r=>r.json()).then(data=>{
        const el = document.getElementById('addResult');
        if (data.success) { el.style.color='#4ade80'; el.textContent='✅ '+data.message; input.value=''; loadRequests(); }
        else { el.style.color='#f87171'; el.textContent='❌ '+data.error; }
    });
}

function deleteFriend(friendId) {
    if (!confirm('确定要删除这个好友吗？')) return;
    fetch('/api/delete_friend', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({friend_id:friendId}) })
    .then(()=>{
        loadFriends();
        if(currentChatType==='friend' && currentChatId===friendId) {
            currentChatId=null;
            currentChatName='';
            document.getElementById('chatHeader').innerHTML='<span class="empty">💬 选择好友开始聊天</span>';
            msgInput.disabled=true;
            sendBtn.disabled=true;
            msgBox.innerHTML='<div class="msg-system">选择左侧开始聊天</div>';
            renderedMessageIds.clear();
        }
    });
}

function deleteGroup(groupId) {
    if (!confirm('确定要删除这个群聊吗？（仅群主可删除）')) return;
    fetch('/api/delete_group', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({group_id:groupId}) })
    .then(r=>r.json()).then(data=>{
        if(data.success) { loadGroups(); if(currentChatType==='group' && currentChatId===groupId) { currentChatId=null; currentChatName=''; document.getElementById('chatHeader').innerHTML='<span class="empty">💬 选择群聊开始聊天</span>'; msgInput.disabled=true; sendBtn.disabled=true; msgBox.innerHTML='<div class="msg-system">选择左侧开始聊天</div>'; renderedMessageIds.clear(); } }
        else { alert(data.error); }
    });
}

function leaveGroup(groupId) {
    if (!confirm('确定要退出这个群聊吗？')) return;
    fetch('/api/leave_group', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({group_id:groupId}) })
    .then(()=>{ loadGroups(); if(currentChatType==='group' && currentChatId===groupId) { currentChatId=null; currentChatName=''; document.getElementById('chatHeader').innerHTML='<span class="empty">💬 选择群聊开始聊天</span>'; msgInput.disabled=true; sendBtn.disabled=true; msgBox.innerHTML='<div class="msg-system">选择左侧开始聊天</div>'; renderedMessageIds.clear(); } });
}

// ===== 打开聊天 =====
function openChat(type, id, name) {
    if (currentChatType === type && currentChatId === id) {
        return;
    }
    currentChatType = type;
    currentChatId = id;
    currentChatName = name;
    renderedMessageIds.clear();
    const header = document.getElementById('chatHeader');
    if (type === 'friend') {
        header.innerHTML = `<span>💬 与 ${name} (ID:${id}) 聊天</span>`;
    } else {
        header.innerHTML = `<span>👥 群聊: ${name}</span>`;
    }
    msgInput.disabled = false;
    sendBtn.disabled = false;
    msgInput.placeholder = type==='friend' ? `发给 ${name} ...` : `发到 ${name} ...`;
    setTimeout(()=>msgInput.focus(), 300);
    msgBox.innerHTML = '';

    const url = type === 'friend' ? `/api/messages?friend_id=${id}` : `/api/group_messages?group_id=${id}`;
    fetch(url).then(r=>r.json()).then(data=>{
        if (data.messages && data.messages.length>0) {
            data.messages.forEach(m=>{
                if (type === 'friend') {
                    const msgType = m.sender_id === myUserId ? 'user' : 'other';
                    const nameDisplay = msgType === 'user' ? '' : currentChatName + ': ';
                    addMsg(nameDisplay + m.content, msgType, m.timestamp.slice(11,16), m.message_type, m.image_data, m.id);
                } else {
                    const isMe = m.sender_id === myUserId;
                    const senderName = isMe ? '' : (m.sender_name || '用户' + m.sender_id) + ': ';
                    const msgType = isMe ? 'user' : 'other';
                    addMsg(senderName + m.content, msgType, m.timestamp.slice(11,16), m.message_type, m.image_data, m.id);
                }
            });
        } else {
            addSys('暂无消息，发送第一条吧');
        }
        msgBox.scrollTop = msgBox.scrollHeight;
    });

    if (type === 'friend') {
        fetch('/api/mark_read', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({friend_id:id}) });
    }

    document.querySelectorAll('.friend-item, .group-item').forEach(el=>el.classList.remove('active'));
    const selector = type === 'friend' ? '.friend-item' : '.group-item';
    document.querySelectorAll(selector).forEach(el=>{
        if (el.textContent.includes(name)) el.classList.add('active');
    });
    updateFBBalance();
    updateTokenBalance();
    loadGroups();
}

// ===== 发送消息 =====
function sendMsg() {
    if (loading || !currentChatId || !currentChatType) return;
    const text = msgInput.value.trim();
    if (!text) return;
    msgInput.value = '';
    msgInput.style.height = 'auto';
    const tempId = 'temp_' + Date.now();
    addMsg(text, 'user', null, 'text', null, tempId);
    loading = true;
    sendBtn.disabled = true;
    const url = currentChatType === 'friend' ? '/api/send' : '/api/send_group_message';
    const body = currentChatType === 'friend' ? { receiver_id: currentChatId, content: text } : { group_id: currentChatId, content: text };
    fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) })
    .then(r=>r.json()).then(data=>{
        loading = false;
        sendBtn.disabled = false;
        msgInput.focus();
        if (!data.success) addSys('❌ ' + (data.error||'发送失败'));
    }).catch(()=>{ loading=false; sendBtn.disabled=false; });
}

// ===== 发送图片 =====
function sendImage(event) {
    const file = event.target.files[0];
    if (!file) return;
    if (!currentChatId || !currentChatType) { addSys('⚠️ 请先选择聊天'); return; }
    const reader = new FileReader();
    reader.onload = function(e) {
        const dataUrl = e.target.result;
        const tempId = 'temp_img_' + Date.now();
        addMsg('📷 图片', 'user', null, 'image', dataUrl, tempId);
        const url = currentChatType === 'friend' ? '/api/send' : '/api/send_group_message';
        const body = currentChatType === 'friend' ?
            { receiver_id: currentChatId, content: '📷 图片', message_type: 'image', image_data: dataUrl } :
            { group_id: currentChatId, content: '📷 图片', message_type: 'image', image_data: dataUrl };
        fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
    };
    reader.readAsDataURL(file);
    event.target.value = '';
}

// ===== 转账 =====
function showTransferModal() {
    document.getElementById('transferModal').classList.add('show');
    document.getElementById('transferResult').textContent = '';
    document.getElementById('transferUserId').value = '';
    document.getElementById('transferAmount').value = '';
    document.getElementById('transferMessage').value = '';
    setTimeout(()=>document.getElementById('transferUserId').focus(), 100);
}
function closeTransferModal() {
    document.getElementById('transferModal').classList.remove('show');
}
function sendTransfer() {
    const to_user = document.getElementById('transferUserId').value.trim();
    const amount = parseInt(document.getElementById('transferAmount').value.trim());
    const message = document.getElementById('transferMessage').value.trim();

    if (!to_user) {
        document.getElementById('transferResult').textContent = '⚠️ 请输入对方ID';
        return;
    }
    if (!amount || amount<=0) {
        document.getElementById('transferResult').textContent = '⚠️ 请输入正确的金额';
        return;
    }

    document.getElementById('transferResult').textContent = '⏳ 处理中...';
    document.getElementById('transferResult').style.color = '#fbbf24';

    fetch('/api/transfer', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({to_user_id:parseInt(to_user), amount:amount, message:message})
    })
    .then(r=>r.json())
    .then(data=>{
        const el = document.getElementById('transferResult');
        if (data.success) {
            el.style.color='#4ade80';
            el.textContent='✅ ' + data.message;
            updateFBBalance();
            setTimeout(()=>{
                closeTransferModal();
                updateFBBalance();
            }, 1500);
        } else {
            el.style.color='#f87171';
            el.textContent='❌ ' + data.error;
        }
    })
    .catch(()=>{
        document.getElementById('transferResult').textContent = '❌ 网络错误，请重试';
        document.getElementById('transferResult').style.color = '#f87171';
    });
}

// ===== FB钱包 =====
function showFBInfo() {
    document.getElementById('fbInfoModal').classList.add('show');
    fetch('/api/fb_balance').then(r=>r.json()).then(data=>{
        document.getElementById('fbBalanceModal').textContent = data.balance || 0;
    });
    fetch('/api/transactions').then(r=>r.json()).then(data=>{
        const container = document.getElementById('transactionHistory');
        container.innerHTML = '';
        if (data.transactions && data.transactions.length>0) {
            data.transactions.forEach(t=>{
                const div = document.createElement('div');
                div.className = 'fb-transaction-item';
                const isFromMe = t.from_user === myUserId;
                const amountClass = isFromMe ? 'negative' : 'positive';
                const label = isFromMe ? `→ ${t.to_name}` : `← ${t.from_name}`;
                div.innerHTML = `
                    <span>${label} <span class="amount ${amountClass}">${isFromMe ? '-' : '+'}${t.amount}FB</span></span>
                    <span style="font-size:10px;color:#444466;">${t.timestamp.slice(5,16)} ${t.message ? '📝'+t.message : ''}</span>
                `;
                container.appendChild(div);
            });
        } else {
            container.innerHTML = '<div style="color:#444466;font-size:13px;padding:4px 0;">暂无交易记录</div>';
        }
    });
}
function closeFBInfo() {
    document.getElementById('fbInfoModal').classList.remove('show');
}

// ===== Token功能 =====
function showTokenInfo() {
    document.getElementById('tokenModal').classList.add('show');
    document.getElementById('purchaseResult').textContent = '';
    document.getElementById('purchaseAmount').value = '';
    fetch('/api/token_balance').then(r=>r.json()).then(data=>{
        document.getElementById('tokenBalanceModal').textContent = data.tokens || 0;
    });
}
function closeTokenModal() {
    document.getElementById('tokenModal').classList.remove('show');
}
function purchaseTokens() {
    const fb_amount = parseInt(document.getElementById('purchaseAmount').value.trim());
    if (!fb_amount || fb_amount<=0) {
        document.getElementById('purchaseResult').textContent = '⚠️ 请输入正确的FB数量';
        return;
    }
    fetch('/api/purchase_tokens', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({fb_amount:fb_amount})
    }).then(r=>r.json()).then(data=>{
        const el = document.getElementById('purchaseResult');
        if (data.success) {
            el.style.color='#4ade80';
            el.textContent='✅ '+data.message;
            updateTokenBalance();
            updateFBBalance();
            document.getElementById('tokenBalanceModal').textContent = data.tokens;
            setTimeout(()=>{ el.textContent=''; }, 3000);
        } else {
            el.style.color='#f87171';
            el.textContent='❌ '+data.error;
        }
    });
}

// ===== AI功能 =====
function showAIModal() {
    document.getElementById('aiModal').classList.add('show');
    document.getElementById('aiPrompt').value = '';
    document.getElementById('aiResult').style.display = 'none';
    document.getElementById('aiResult').textContent = '';
    setTimeout(()=>document.getElementById('aiPrompt').focus(), 100);
}
function closeAIModal() {
    document.getElementById('aiModal').classList.remove('show');
}
function askAI() {
    const prompt = document.getElementById('aiPrompt').value.trim();
    if (!prompt) { alert('请输入问题'); return; }
    const resultDiv = document.getElementById('aiResult');
    resultDiv.style.display = 'block';
    resultDiv.textContent = '⏳ AI思考中...';
    fetch('/api/ai_chat', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({prompt:prompt})
    }).then(r=>r.json()).then(data=>{
        if (data.success) {
            resultDiv.textContent = data.response;
            updateTokenBalance();
        } else {
            resultDiv.textContent = '❌ ' + data.error;
        }
    }).catch(()=>{
        resultDiv.textContent = '❌ 请求失败，请重试';
    });
}

// ===== 群聊 =====
function openGroupModal() {
    document.getElementById('groupModal').classList.add('show');
    document.getElementById('groupResult').textContent = '';
    document.getElementById('groupNameInput').value = '';
    document.getElementById('groupMembersInput').value = '';
    setTimeout(()=>document.getElementById('groupNameInput').focus(), 100);
}
function closeGroupModal() {
    document.getElementById('groupModal').classList.remove('show');
}
function createGroupSubmit() {
    const name = document.getElementById('groupNameInput').value.trim();
    const members = document.getElementById('groupMembersInput').value.trim();
    if (!name) {
        document.getElementById('groupResult').textContent = '⚠️ 请输入群名称';
        return;
    }
    const memberIds = members ? members.split(',').map(m=>parseInt(m.trim())).filter(m=>!isNaN(m) && m>0) : [];
    fetch('/api/create_group', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name, member_ids:memberIds}) })
    .then(r=>r.json()).then(data=>{
        if (data.success) {
            document.getElementById('groupResult').style.color='#4ade80';
            document.getElementById('groupResult').textContent='✅ 群聊创建成功！';
            setTimeout(closeGroupModal, 1000);
            loadGroups();
        } else {
            document.getElementById('groupResult').style.color='#f87171';
            document.getElementById('groupResult').textContent='❌ '+data.error;
        }
    });
}

// ===== 修改密码 =====
function openChangePassword() {
    document.getElementById('changePwdModal').classList.add('show');
    document.getElementById('pwdResult').textContent = '';
    document.getElementById('oldPwd').value = '';
    document.getElementById('newPwd').value = '';
    document.getElementById('confirmPwd').value = '';
    setTimeout(()=>document.getElementById('oldPwd').focus(), 100);
}
function closeChangePassword() {
    document.getElementById('changePwdModal').classList.remove('show');
}
function changePassword() {
    const old = document.getElementById('oldPwd').value.trim();
    const new1 = document.getElementById('newPwd').value.trim();
    const new2 = document.getElementById('confirmPwd').value.trim();
    if (!old || !new1 || !new2) { document.getElementById('pwdResult').textContent='⚠️ 请填写完整'; return; }
    if (new1.length < 4) { document.getElementById('pwdResult').textContent='⚠️ 新密码至少4位'; return; }
    if (new1 !== new2) { document.getElementById('pwdResult').textContent='⚠️ 两次密码不一致'; return; }
    fetch('/api/change_password', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({old_password:old, new_password:new1}) })
    .then(r=>r.json()).then(data=>{
        if (data.success) {
            document.getElementById('pwdResult').style.color='#4ade80';
            document.getElementById('pwdResult').textContent='✅ '+data.message;
            setTimeout(closeChangePassword, 1500);
        } else {
            document.getElementById('pwdResult').style.color='#f87171';
            document.getElementById('pwdResult').textContent='❌ '+data.error;
        }
    });
}

// ===== 退出 =====
function logout() {
    fetch('/logout').then(()=>location.reload());
}

// ===== 回车发送 =====
msgInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMsg();
    }
});
document.getElementById('aiPrompt').addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        askAI();
    }
});

// ===== 定时刷新 =====
setInterval(() => {
    loadFriends();
    loadGroups();
    loadRequests();
    updateFBBalance();
    updateTokenBalance();
}, 8000);

// ===== 初始化 =====
updateFBBalance();
updateTokenBalance();
loadFriends();
loadGroups();
loadRequests();
{% endif %}
</script>
</body>
</html>
'''

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)