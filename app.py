import os
import time
import sqlite3
from flask import Flask, request, jsonify, session, render_template, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret_key_change_in_production")

# Initialize Gemini Client
gemini_api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=gemini_api_key) if gemini_api_key else None

# Define absolute path for the SQLite database file inside the project folder
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'defaultdb')

def init_db():
    """Automatically creates the database and required tables if they don't exist."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Create users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'student',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create chat_logs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                prompt TEXT NOT NULL,
                response TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        ''')

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error initializing database: {e}")

# Automatically create tables on application boot
init_db()

def get_db_connection():
    try:
        # Added timeout=10 to wait patiently if another write is happening
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # Enable Write-Ahead Logging (WAL) mode for high concurrency
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn
    except Exception as e:
        print(f"Database connection error: {e}")
        return None

# --- SECURITY DECORATORS (RBAC) ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if request.is_json:
                return jsonify({"error": "Unauthorized. Please log in."}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'admin':
            if request.is_json:
                return jsonify({"error": "Forbidden: Admin access required."}), 403
            return redirect(url_for('dashboard_page'))
        return f(*args, **kwargs)
    return decorated_function

# --- VIEW ROUTES ---
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('admin_page' if session.get('role') == 'admin' else 'dashboard_page'))
    return redirect(url_for('login_page'))

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/signup')
def signup_page():
    return render_template('signup.html')

@app.route('/dashboard')
@login_required
def dashboard_page():
    return render_template('dashboard.html', username=session.get('username'), role=session.get('role'))

@app.route('/admin')
@admin_required
def admin_page():
    return render_template('admin.html', username=session.get('username'))

# --- AUTHENTICATION API ---
@app.route('/api/signup', methods=['POST'])
def api_signup():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    if not username or not email or not password:
        return jsonify({"error": "All fields are required."}), 400

    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters long."}), 400

    hashed_password = generate_password_hash(password)
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed."}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ? OR username = ?", (email, username))
        if cursor.fetchone():
            return jsonify({"error": "Username or email already exists."}), 409

        cursor.execute(
            "INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, 'student')",
            (username, email, hashed_password)
        )
        conn.commit()
        return jsonify({"message": "Registration successful! Please log in."}), 201
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    if not email or not password:
        return jsonify({"error": "Email and password are required."}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed."}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()

        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            return jsonify({
                "message": "Login successful",
                "role": user['role'],
                "redirect": "/admin" if user['role'] == 'admin' else "/dashboard"
            }), 200
        else:
            return jsonify({"error": "Invalid email or password."}), 401
    except Exception as e:
        return jsonify({"error": f"Login failed: {str(e)}"}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({"message": "Logged out successfully"}), 200

# --- GEMINI AI INTEGRATION API ---
'''@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.get_json() or {}
    prompt = data.get('prompt', '').strip()

    if not prompt:
        return jsonify({"error": "Prompt cannot be empty."}), 400

    if not client:
        return jsonify({"error": "Gemini API client is not configured."}), 500

    system_instruction = (
        "You are NetGuide AI, an expert networking assistant for students. When users paste Cisco "
        "configuration errors, diagnose the root cause and provide corrected code blocks. For networking theory, "
        "explain core principles using structured bullet points and bold headers. Keep answers concise, highly organized, and "
        "optimized for low latency to ensure rapid, clean responses."
    )

    try:
        # Optimized configuration for fast response generation and performance
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.3,
            max_output_tokens=1024,
        )

        # Automatic retry mechanism for temporary 503 high-demand events
        response = None
        last_error = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model='gemini-3.8-flash',
                    contents=prompt,
                    config=config
                )
                break
            except Exception as api_err:
                last_error = api_err
                err_str = str(api_err)
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    if attempt < 2:
                        time.sleep(1.2)
                        continue
                raise api_err

        if not response:
            raise last_error

        reply = response.text

        # Non-blocking database chat logging
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO chat_logs (user_id, prompt, response) VALUES (?, ?, ?)",
                    (session['user_id'], prompt, reply)
                )
                conn.commit()
            except Exception as log_err:
                print(f"Warning: Failed to log chat to database: {log_err}")
            finally:
                conn.close()

        return jsonify({"response": reply}), 200
    except Exception as e:
        error_msg = str(e)
        if "503" in error_msg or "UNAVAILABLE" in error_msg:
            return jsonify({"error": "AI service is currently experiencing high demand. Please try again in a moment."}), 503
        return jsonify({"error": f"AI generation failed: {error_msg}"}), 500'''
# --- GEMINI AI INTEGRATION API ---
# --- GEMINI AI INTEGRATION API ---
@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.get_json() or {}
    prompt = data.get('prompt', '').strip()

    if not prompt:
        return jsonify({"error": "Prompt cannot be empty."}), 400

    if not client:
        return jsonify({"error": "Gemini API client is not configured."}), 500

    system_instruction = (
        "You are NetGuide AI, an expert networking assistant for students. "
        "When users paste Cisco configuration errors, diagnose the root cause and provide corrected code blocks. "
        "For networking theory, explain core principles using structured bullet points, bold headers, and markdown tables. "
        "Keep answers concise, highly organized, and optimized for low latency."
    )

    try:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.3,
            max_output_tokens=1500,
        )

        # Automatic retry mechanism for temporary 503 high-demand events
        response = None
        last_error = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model='gemini-3.8-flash',
                    contents=prompt,
                    config=config
                )
                break
            except Exception as api_err:
                last_error = api_err
                err_str = str(api_err)
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    if attempt < 2:
                        time.sleep(1.5)  # Wait 1.5 seconds before retrying
                        continue
                raise api_err

        if not response:
            raise last_error

        reply = response.text

        # Log complete response to database
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO chat_logs (user_id, prompt, response) VALUES (?, ?, ?)",
                    (session['user_id'], prompt, reply)
                )
                conn.commit()
            except Exception as log_err:
                print(f"Warning: Failed to log chat: {log_err}")
            finally:
                conn.close()

        return jsonify({"response": reply}), 200

    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
            return jsonify({"error": "Daily AI free-tier quota reached (20 requests/day limit). Please try again tomorrow or update your API key."}), 429
        if "503" in error_msg or "UNAVAILABLE" in error_msg:
            return jsonify({"error": "AI service is experiencing high demand. Please try again in a moment."}), 503
        return jsonify({"error": f"AI generation failed: {error_msg}"}), 500

# --- GEMINI AI live STREAMING INTEGRATION API for response ---
'''@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.get_json() or {}
    prompt = data.get('prompt', '').strip()

    if not prompt:
        return jsonify({"error": "Prompt cannot be empty."}), 400

    if not client:
        return jsonify({"error": "Gemini API client is not configured."}), 500

    system_instruction = (
        "You are NetGuide AI, an expert networking assistant for students. "
        "When users paste Cisco configuration errors, diagnose the root cause and provide corrected code blocks. "
        "For networking theory, explain core principles using structured bullet points, bold headers, and markdown tables. "
        "Keep answers concise, highly organized, and optimized for low latency."
    )

    def generate():
        full_reply = ""
        try:
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.3,
                max_output_tokens=1500,
            )

            response_stream = client.models.generate_content_stream(
                model='gemini-2.5-flash',
                contents=prompt,
                config=config
            )

            for chunk in response_stream:
                if chunk.text:
                    full_reply += chunk.text
                    yield chunk.text

        except Exception as stream_err:
            yield f"\n[Stream Error: {str(stream_err)}]"
            return

        # Save complete response to SQLite database after stream finishes
        if full_reply:
            conn = get_db_connection()
            if conn:
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO chat_logs (user_id, prompt, response) VALUES (?, ?, ?)",
                        (session['user_id'], prompt, full_reply)
                    )
                    conn.commit()
                except Exception as log_err:
                    print(f"Warning: Failed to log chat: {log_err}")
                finally:
                    conn.close()

    return Response(stream_with_context(generate()), mimetype='text/plain')'''

# --- ADMIN MANAGEMENT API ---
@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_get_users():
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed."}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, email, role, created_at FROM users ORDER BY created_at DESC")
        users = [dict(row) for row in cursor.fetchall()]

        cursor.execute("SELECT COUNT(*) as total_users FROM users")
        total_users = cursor.fetchone()['total_users']

        cursor.execute("SELECT COUNT(*) as total_chats FROM chat_logs")
        total_chats = cursor.fetchone()['total_chats']

        return jsonify({
            "users": users,
            "stats": {
                "total_users": total_users,
                "total_chats": total_chats
            }
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/update-role', methods=['POST'])
@admin_required
def admin_update_role():
    data = request.get_json() or {}
    target_user_id = data.get('user_id')
    new_role = data.get('role')

    if new_role not in ['student', 'admin']:
        return jsonify({"error": "Invalid role specified."}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed."}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, target_user_id))
        conn.commit()
        return jsonify({"message": f"User role successfully updated to {new_role}."}), 200
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/audit-logs', methods=['GET'])
@admin_required
def admin_audit_logs():
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed."}), 500

    try:
        cursor = conn.cursor()
        query = """
            SELECT chat_logs.id, users.username, chat_logs.prompt, chat_logs.response, chat_logs.created_at
            FROM chat_logs
            JOIN users ON chat_logs.user_id = users.id
            ORDER BY chat_logs.created_at DESC
            LIMIT 50
        """
        cursor.execute(query)
        logs = [dict(row) for row in cursor.fetchall()]
        return jsonify({"logs": logs}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/delete-user', methods=['POST'])
@admin_required
def admin_delete_user():
    data = request.get_json() or {}
    target_user_id = data.get('user_id')

    if target_user_id == session.get('user_id'):
        return jsonify({"error": "You cannot delete your own admin account."}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed."}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
        conn.commit()
        return jsonify({"message": "User and associated data successfully removed."}), 200
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

