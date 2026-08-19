import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import server


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ApiHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        server.STORE = server.TaskStore()
        server.SESSIONS.clear()
        server.DEMO_STATE.update({
            "version": 0,
            "active_tab": "tasks",
            "selected_signs": [],
            "month": "",
            "day": "",
            "focus": "",
            "refresh_tasks": False,
            "generate_ai": False,
        })
        self.cookie = None

    def request(self, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", self.port)
        encoded = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection.request(method, path, encoded, headers)
        response = connection.getresponse()
        data = response.read()
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        connection.close()
        return response.status, json.loads(data) if data else None

    def login(self, password=None):
        return self.request(
            "POST",
            "/api/login",
            {"password": password or server.APP_PASSWORD},
        )

    def test_task_crud(self):
        status, auth = self.login()
        self.assertEqual(status, 200)
        self.assertTrue(auth["authenticated"])

        status, tasks = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(tasks), 2)

        status, task = self.request("POST", "/api/tasks", {"title": "測試 API"})
        self.assertEqual(status, 201)
        self.assertEqual(task["title"], "測試 API")

        status, task = self.request("PATCH", f"/api/tasks/{task['id']}", {"completed": True})
        self.assertEqual(status, 200)
        self.assertTrue(task["completed"])

        status, _ = self.request("DELETE", f"/api/tasks/{task['id']}")
        self.assertEqual(status, 204)

    def test_validation(self):
        self.login()
        status, error = self.request("POST", "/api/tasks", {"title": "  "})
        self.assertEqual(status, 400)
        self.assertIn("error", error)

    def test_authentication_required(self):
        status, error = self.request("GET", "/api/tasks")
        self.assertEqual(status, 401)
        self.assertEqual(error["error"], "請先登入")

        status, error = self.login("wrong-password")
        self.assertEqual(status, 401)
        self.assertEqual(error["error"], "密碼錯誤")

    def test_render_head_probe(self):
        status, body = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertIsNone(body)

        status, body = self.request("HEAD", "/api/health")
        self.assertEqual(status, 200)
        self.assertIsNone(body)

    def test_logout(self):
        self.login()
        status, _ = self.request("POST", "/api/logout", {})
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/api/tasks")
        self.assertEqual(status, 401)

    def test_zodiac_search(self):
        status, _ = self.request("GET", "/api/zodiac")
        self.assertEqual(status, 401)

        self.login()
        status, signs = self.request("GET", "/api/zodiac")
        self.assertEqual(status, 200)
        self.assertEqual(len(signs), 12)

        status, signs = self.request("GET", "/api/zodiac?q=Leo")
        self.assertEqual(status, 200)
        self.assertEqual([sign["id"] for sign in signs], ["leo"])

    def test_ai_reading_uses_deterministic_zodiac(self):
        self.login()
        with patch("server.generate_ai_reading", return_value="這是測試解讀") as generate:
            status, result = self.request(
                "POST",
                "/api/ai-reading",
                {"month": 8, "day": 7, "focus": "團隊合作"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(result["zodiac"]["id"], "leo")
        self.assertEqual(result["reading"], "這是測試解讀")
        self.assertEqual(generate.call_args.args[1], "團隊合作")

    def test_ai_reading_rejects_invalid_date(self):
        self.login()
        status, error = self.request(
            "POST",
            "/api/ai-reading",
            {"month": 2, "day": 31, "focus": ""},
        )
        self.assertEqual(status, 400)
        self.assertIn("有效日期", error["error"])

    def test_demo_state_sync(self):
        self.login()
        status, state = self.request("POST", "/api/demo-state", {
            "active_tab": "zodiac",
            "selected_signs": ["capricorn", "virgo"],
            "month": 12,
            "day": 24,
            "focus": "星座的人格特質",
            "refresh_tasks": True,
            "generate_ai": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(state["version"], 1)

        status, state = self.request("GET", "/api/demo-state")
        self.assertEqual(status, 200)
        self.assertEqual(state["selected_signs"], ["capricorn", "virgo"])
        self.assertEqual((state["month"], state["day"]), (12, 24))
        self.assertTrue(state["generate_ai"])


if __name__ == "__main__":
    unittest.main()
