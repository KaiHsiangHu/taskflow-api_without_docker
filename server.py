"""A dependency-free REST API and static file server for a small task app."""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import date
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv


load_dotenv()


# 0.0.0.0 allows cloud platforms such as Render to reach the service.
# It also works for local development via http://127.0.0.1:8000.
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "taskflow123")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

STATIC_DIR = Path(__file__).with_name("frontend")
ZODIAC_FILE = Path(__file__).with_name("data") / "zodiac.json"
ZODIAC_SIGNS = json.loads(ZODIAC_FILE.read_text(encoding="utf-8"))
ZODIAC_BY_ID = {sign["id"]: sign for sign in ZODIAC_SIGNS}
DEMO_STATE_LOCK = threading.Lock()
DEMO_STATE = {
    "version": 0,
    "active_tab": "tasks",
    "selected_signs": [],
    "month": "",
    "day": "",
    "focus": "",
    "refresh_tasks": False,
    "generate_ai": False,
}


def zodiac_for_date(month: int, day: int) -> dict:
    """Return a zodiac sign using the common tropical-zodiac date ranges."""
    date(2000, month, day)  # Validate the month/day, including February 29.
    cutoffs = {
        1: (19, "capricorn", "aquarius"), 2: (18, "aquarius", "pisces"),
        3: (20, "pisces", "aries"), 4: (19, "aries", "taurus"),
        5: (20, "taurus", "gemini"), 6: (20, "gemini", "cancer"),
        7: (22, "cancer", "leo"), 8: (22, "leo", "virgo"),
        9: (22, "virgo", "libra"), 10: (22, "libra", "scorpio"),
        11: (21, "scorpio", "sagittarius"), 12: (21, "sagittarius", "capricorn"),
    }
    cutoff, before, after = cutoffs[month]
    return ZODIAC_BY_ID[before if day <= cutoff else after]


def generate_ai_reading(sign: dict, focus: str) -> str:
    """Generate an entertainment-only reading with the Gemini REST API."""
    if not GEMINI_API_KEY:
        raise RuntimeError("尚未設定 GEMINI_API_KEY")
    prompt = f"""你是語氣溫暖、謹慎的繁體中文星座內容助手。
使用者的星座已由固定日期規則判定為：{sign['name']}（{sign['english']}，{sign['dates']}）。
參考特質：{'、'.join(sign['traits'])}。
使用者想了解：{focus or '整體性格與近期自我成長方向'}。

請寫 250 至 400 字的個人化解讀，分成「你的特質」、「可以發揮的方向」、「溫柔提醒」三個短段落。
避免預言具體事件、健康、投資或重大人生決策；不要假裝知道使用者未提供的資料。
最後明確提醒內容僅供娛樂與自我探索。"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 700},
    }).encode("utf-8")
    request = Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
    )
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    parts = result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "\n".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise RuntimeError("Gemini 沒有回傳文字")
    return text


class TaskStore:
    """In-memory task storage used by the example API."""

    def __init__(self) -> None:
        self._next_id = 3
        self._tasks = [
            {"id": 1, "title": "閱讀 API 文件", "completed": True},
            {"id": 2, "title": "新增第一個待辦事項", "completed": False},
        ]

    def list(self) -> list[dict]:
        return [task.copy() for task in self._tasks]

    def create(self, title: str) -> dict:
        task = {"id": self._next_id, "title": title, "completed": False}
        self._next_id += 1
        self._tasks.append(task)
        return task.copy()

    def update(self, task_id: int, values: dict) -> dict | None:
        for task in self._tasks:
            if task["id"] == task_id:
                task.update(values)
                return task.copy()
        return None

    def delete(self, task_id: int) -> bool:
        for index, task in enumerate(self._tasks):
            if task["id"] == task_id:
                del self._tasks[index]
                return True
        return False


STORE = TaskStore()
SESSIONS: set[str] = set()


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "TaskApi/1.0"

    def _json(
        self,
        data: object,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length))
            return value if isinstance(value, dict) else None
        except (ValueError, json.JSONDecodeError):
            return None

    def _task_id(self, path: str) -> int | None:
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "tasks"]:
            return None
        try:
            return int(parts[2])
        except ValueError:
            return None

    def _session_token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookie.get("taskflow_session")
        return session.value if session else None

    def _is_authenticated(self) -> bool:
        token = self._session_token()
        return token is not None and token in SESSIONS

    def _require_auth(self) -> bool:
        if self._is_authenticated():
            return True
        self._json({"error": "請先登入"}, HTTPStatus.UNAUTHORIZED)
        return False

    def _cookie_header(self, token: str, max_age: int | None = None) -> str:
        parts = [f"taskflow_session={token}", "Path=/", "HttpOnly", "SameSite=Strict"]
        if self.headers.get("X-Forwarded-Proto") == "https":
            parts.append("Secure")
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        return "; ".join(parts)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/tasks":
            if not self._require_auth():
                return
            self._json(STORE.list())
            return
        if path == "/api/zodiac":
            if not self._require_auth():
                return
            query = parse_qs(parsed.query).get("q", [""])[0].strip().casefold()
            signs = ZODIAC_SIGNS
            if query:
                signs = [
                    sign for sign in signs
                    if query in sign["name"].casefold()
                    or query in sign["english"].casefold()
                    or query in sign["element"].casefold()
                ]
            self._json(signs)
            return
        if path == "/api/demo-state":
            if not self._require_auth():
                return
            with DEMO_STATE_LOCK:
                self._json(DEMO_STATE.copy())
            return
        if path == "/api/auth":
            self._json({"authenticated": self._is_authenticated()})
            return
        if path == "/api/health":
            self._json({"status": "ok"})
            return
        self._serve_static(path)

    def do_HEAD(self) -> None:
        """Support Render's port and health probes without returning a body."""
        path = urlparse(self.path).path
        if path in {"/", "/api/health"}:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/login":
            data = self._read_json()
            password = data.get("password", "") if data else ""
            if not isinstance(password, str) or not secrets.compare_digest(password, APP_PASSWORD):
                self._json({"error": "密碼錯誤"}, HTTPStatus.UNAUTHORIZED)
                return
            token = secrets.token_urlsafe(32)
            SESSIONS.add(token)
            self._json(
                {"authenticated": True},
                headers={"Set-Cookie": self._cookie_header(token)},
            )
            return
        if path == "/api/logout":
            token = self._session_token()
            if token:
                SESSIONS.discard(token)
            self._json(
                {"authenticated": False},
                headers={"Set-Cookie": self._cookie_header("", max_age=0)},
            )
            return
        if path == "/api/ai-reading":
            if not self._require_auth():
                return
            data = self._read_json()
            try:
                month = int(data.get("month"))
                day = int(data.get("day"))
                focus = data.get("focus", "").strip()
                if not isinstance(data.get("focus", ""), str) or len(focus) > 500:
                    raise ValueError
                sign = zodiac_for_date(month, day)
            except (AttributeError, TypeError, ValueError):
                self._json({"error": "請輸入有效日期，想了解的內容不可超過 500 字"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                reading = generate_ai_reading(sign, focus)
            except RuntimeError as error:
                self._json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            except (HTTPError, URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError):
                self._json({"error": "AI 服務暫時無法使用，請稍後再試"}, HTTPStatus.BAD_GATEWAY)
                return
            self._json({"zodiac": sign, "reading": reading, "model": GEMINI_MODEL})
            return
        if path == "/api/demo-state":
            if not self._require_auth():
                return
            data = self._read_json()
            try:
                active_tab = data.get("active_tab", "tasks")
                selected_signs = data.get("selected_signs", [])
                month = data.get("month", "")
                day = data.get("day", "")
                focus = data.get("focus", "")
                if active_tab not in {"tasks", "zodiac", "ai"}:
                    raise ValueError
                if not isinstance(selected_signs, list) or any(
                    sign not in ZODIAC_BY_ID for sign in selected_signs
                ):
                    raise ValueError
                if not isinstance(focus, str) or len(focus) > 500:
                    raise ValueError
                if month != "" or day != "":
                    zodiac_for_date(int(month), int(day))
            except (AttributeError, TypeError, ValueError):
                self._json({"error": "測試操作資料格式錯誤"}, HTTPStatus.BAD_REQUEST)
                return
            with DEMO_STATE_LOCK:
                DEMO_STATE.update({
                    "version": DEMO_STATE["version"] + 1,
                    "active_tab": active_tab,
                    "selected_signs": selected_signs,
                    "month": month,
                    "day": day,
                    "focus": focus,
                    "refresh_tasks": bool(data.get("refresh_tasks", False)),
                    "generate_ai": bool(data.get("generate_ai", False)),
                })
                result = DEMO_STATE.copy()
            self._json(result)
            return
        if path != "/api/tasks":
            self._json({"error": "找不到 API"}, HTTPStatus.NOT_FOUND)
            return
        if not self._require_auth():
            return
        data = self._read_json()
        title = data.get("title", "").strip() if data else ""
        if not title:
            self._json({"error": "title 為必填欄位"}, HTTPStatus.BAD_REQUEST)
            return
        self._json(STORE.create(title), HTTPStatus.CREATED)

    def do_PATCH(self) -> None:
        if not self._require_auth():
            return
        task_id = self._task_id(urlparse(self.path).path)
        data = self._read_json()
        if task_id is None or data is None:
            self._json({"error": "請求格式錯誤"}, HTTPStatus.BAD_REQUEST)
            return
        values = {}
        if "title" in data:
            if not isinstance(data["title"], str) or not data["title"].strip():
                self._json({"error": "title 不可為空"}, HTTPStatus.BAD_REQUEST)
                return
            values["title"] = data["title"].strip()
        if "completed" in data:
            if not isinstance(data["completed"], bool):
                self._json({"error": "completed 必須是布林值"}, HTTPStatus.BAD_REQUEST)
                return
            values["completed"] = data["completed"]
        task = STORE.update(task_id, values)
        self._json(task if task else {"error": "找不到待辦事項"},
                   HTTPStatus.OK if task else HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        if not self._require_auth():
            return
        task_id = self._task_id(urlparse(self.path).path)
        if task_id is None:
            self._json({"error": "請求格式錯誤"}, HTTPStatus.BAD_REQUEST)
            return
        if not STORE.delete(task_id):
            self._json({"error": "找不到待辦事項"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def _serve_static(self, path: str) -> None:
        files = {
            "/": "index.html",
            "/app.js": "app.js",
            "/style.css": "style.css",
            "/logo.svg": "logo.svg",
        }
        filename = files.get(path)
        if not filename:
            self._json({"error": "找不到頁面"}, HTTPStatus.NOT_FOUND)
            return
        body = (STATIC_DIR / filename).read_bytes()
        content_type = {
            ".html": "text/html",
            ".js": "text/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
        }[Path(filename).suffix]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(host: str = HOST, port: int = PORT) -> None:
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"Task API running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
