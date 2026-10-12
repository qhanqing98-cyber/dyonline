"""用户与会话仓库；服务器管理员可用 python -m app.users 管理账号。"""
import argparse
import getpass
import hashlib
import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app import config

ITERATIONS = 600_000
USERNAME = re.compile(r"[a-zA-Z0-9_.-]{3,32}\Z")


def hash_password(password):
    """每个密码使用不同盐值，重复计算减慢离线猜测。"""
    if not 8 <= len(password) <= 256:
        raise ValueError("密码长度须为 8–256 个字符")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


def check_password(password, stored):
    try:
        algorithm, count, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(count))
        return secrets.compare_digest(got.hex(), expected)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class User:
    id: str
    username: str
    is_admin: bool


class UserStore:
    def __init__(self, db_path=None):
        path = Path(db_path or config.DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
        """)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()
        # 未知用户名也执行相同成本的密码校验。
        self.dummy_hash = hash_password(secrets.token_urlsafe(32))

    @staticmethod
    def _user(row):
        return User(row["id"], row["username"], bool(row["is_admin"])) if row else None

    def create(self, username, password, *, is_admin=False):
        if not USERNAME.fullmatch(username):
            raise ValueError("用户名须为 3–32 位字母、数字、下划线、点或连字符")
        digest = hash_password(password)
        user = User(uuid4().hex, username, is_admin)
        with self.lock, self.conn:
            try:
                self.conn.execute("INSERT INTO users VALUES (?, ?, ?, ?)",
                                  (user.id, username, digest, int(is_admin)))
            except sqlite3.IntegrityError:
                raise ValueError("用户名已存在") from None
        return user

    def bootstrap(self):
        """只在没有任何账号时创建管理员；以后修改环境不会重置密码。"""
        with self.lock:
            exists = self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if not exists and config.ADMIN_PASSWORD:
            self.create(config.ADMIN_USERNAME, config.ADMIN_PASSWORD, is_admin=True)
        with self.lock:
            row = self.conn.execute("SELECT * FROM users WHERE is_admin=1 ORDER BY rowid LIMIT 1").fetchone()
        if row is None:
            logging.getLogger(__name__).warning("尚无管理员，请使用账号管理命令创建", extra={"event": "admin_setup_required"})
        return self._user(row)

    def authenticate(self, username, password):
        with self.lock:
            row = self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        valid = check_password(password, row["password_hash"] if row else self.dummy_hash)
        return self._user(row) if row and valid else None

    def create_session(self, user_id):
        token = secrets.token_urlsafe(32)
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
            self.conn.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                              (hashlib.sha256(token.encode()).hexdigest(), user_id,
                               time.time() + config.COOKIE_MAX_AGE))
        return token

    def resolve_session(self, token):
        if not token or len(token) > 128:
            return None
        with self.lock:
            row = self.conn.execute("SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id "
                                    "WHERE token_hash=? AND expires_at>?",
                                    (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return self._user(row)

    def revoke_session(self, token):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM sessions WHERE token_hash=?",
                              (hashlib.sha256(token.encode()).hexdigest(),))

    def reset_password(self, username, password):
        digest = hash_password(password)
        with self.lock, self.conn:
            row = self.conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                raise ValueError("用户不存在")
            self.conn.execute("UPDATE users SET password_hash=? WHERE id=?", (digest, row["id"]))
            self.conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))

    def all(self):
        with self.lock:
            return [self._user(row) for row in self.conn.execute("SELECT * FROM users ORDER BY username")]

    def close(self):
        with self.lock:
            self.conn.close()


def main():
    parser = argparse.ArgumentParser(description="服务器账号管理（密码交互输入，不写入命令历史）")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("username")
    create.add_argument("--admin", action="store_true")
    reset = sub.add_parser("reset-password")
    reset.add_argument("username")
    sub.add_parser("list")
    args = parser.parse_args()
    store = UserStore()
    try:
        if args.command == "list":
            for user in store.all():
                print(user.username, "管理员" if user.is_admin else "用户")
        else:
            password = getpass.getpass("密码: ")
            if password != getpass.getpass("确认密码: "):
                raise ValueError("两次密码不一致")
            if args.command == "create":
                if not store.all() and not args.admin:
                    raise ValueError("首个账号必须带 --admin")
                store.create(args.username, password, is_admin=args.admin)
            else:
                store.reset_password(args.username, password)
            print("账号操作完成")
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        store.close()


if __name__ == "__main__":
    main()
