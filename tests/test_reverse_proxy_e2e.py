"""反向代理子路径部署端到端测试

用真实 uvicorn 进程 + 模拟反代服务器覆盖以下场景：

1. 上游构建产物（绝对路径引用）经子路径反代：资源落到站点根，
   复现控制台 MIME 报错与 favicon 404（上游子路径故障现象）
2. 分支构建产物（相对路径引用）经子路径反代：JS/CSS/favicon/docs 全部可用，
   API 登录与鉴权请求正常
3. 根路径部署回归：资源与 API 与上游行为一致
4. 反代保留路径前缀 + PATH_PREFIX：路由按去前缀后的路径匹配
5. 不带末尾斜杠的访问：页面携带末尾斜杠规整脚本
6. 浏览器级验证（playwright-core）：复现上游 MIME 报错白屏，确认分支下渲染与登录正常

运行方式（项目根目录、使用项目虚拟环境）：

    python tests/test_reverse_proxy_e2e.py
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent

# 上游构建产物（提交 bcb264b）：使用绝对路径引用
PRE_FIX_COMMIT = "bcb264b"

PANEL_PASSWORD = "e2e-test-password"
PROXY_PREFIX = "/AGY2API"
READY_TIMEOUT_SECONDS = 60

IGNORE_NAMES = shutil.ignore_patterns(
    ".git", "node_modules", "creds", "data", "__pycache__", "*.pyc"
)


class HttpResult:
    def __init__(self, url, status, headers, body):
        self.url = url
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def content_type(self):
        return self.headers.get("Content-Type", "")

    @property
    def text(self):
        return self.body.decode("utf-8")


def http_request(method, url, json_body=None, headers=None, timeout=60):
    data = None
    request_headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResult(url, response.status, response.headers, response.read())
    except HTTPError as error:
        return HttpResult(url, error.code, error.headers, error.read())


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SubPathProxyHandler(BaseHTTPRequestHandler):
    """模拟把 /AGY2API/ 反代到后端的服务器"""

    backend_url = "http://127.0.0.1:1"
    prefix = PROXY_PREFIX
    # preserve_prefix 为 True 时原样转发前缀（对应 proxy_pass 不带 URI 的反代），
    # 为 False 时剥掉前缀再转发（对应 proxy_pass 带 URI 的反代）
    preserve_prefix = False

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def _proxy(self):
        path = self.path
        if path == self.prefix:
            # location /AGY2API（无尾斜杠）反代：精确命中映射到后端根路径
            target_path = path if self.preserve_prefix else "/"
        elif path.startswith(self.prefix + "/"):
            target_path = path if self.preserve_prefix else path[len(self.prefix):]
        else:
            # 反代端口上的其他服务：面板引用绝对路径时请求会落到这里。
            # 按现场情况模拟——资源路径返回 HTML（SPA catch-all，复现模块脚本
            # MIME 类型报错），favicon 返回 404（复现 favicon 404 报错）
            if self.path.rstrip("/").endswith("/favicon.ico"):
                self._send(404, "text/html", b"<html><body>404 Not Found</body></html>")
            else:
                body = (
                    b"<html><head><title>Other Service</title></head>"
                    b"<body>other service</body></html>"
                )
                self._send(200, "text/html", body)
            return

        body = None
        length = self.headers.get("Content-Length")
        if length:
            body = self.rfile.read(int(length))
        forward_headers = {}
        for name in ("Content-Type", "Authorization", "Accept"):
            value = self.headers.get(name)
            if value:
                forward_headers[name] = value

        request = Request(
            self.backend_url + target_path,
            data=body,
            method=self.command,
            headers=forward_headers,
        )
        try:
            with urlopen(request, timeout=60) as response:
                self._send(
                    response.status,
                    response.headers.get("Content-Type", "application/octet-stream"),
                    response.read(),
                )
        except HTTPError as error:
            self._send(
                error.code,
                error.headers.get("Content-Type", "application/octet-stream"),
                error.read(),
            )

    def _send(self, status, content_type, payload):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_proxy(backend_port, preserve_prefix=False):
    port = free_port()

    class Handler(SubPathProxyHandler):
        pass

    Handler.backend_url = f"http://127.0.0.1:{backend_port}"
    Handler.preserve_prefix = preserve_prefix
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def start_backend(tree, path_prefix=""):
    port = free_port()
    env = dict(os.environ)
    env["HOST"] = "127.0.0.1"
    env["PORT"] = str(port)
    env["PANEL_PASSWORD"] = PANEL_PASSWORD
    if path_prefix:
        env["PATH_PREFIX"] = path_prefix
    process = subprocess.Popen(
        [sys.executable, "web.py"],
        cwd=str(tree),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_ready(port)
    return process, port


def _wait_ready(port):
    deadline = time.time() + READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            result = http_request("GET", f"http://127.0.0.1:{port}/", timeout=5)
            if result.status == 200:
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"后端在 {READY_TIMEOUT_SECONDS} 秒内未就绪: 127.0.0.1:{port}")


def extract_upstream_tree(target):
    """从修复前提交导出完整代码树（含当时的绝对路径构建产物）"""
    target.mkdir(parents=True, exist_ok=True)
    archive_path = target.parent / "upstream.tar"
    with open(archive_path, "wb") as archive_file:
        subprocess.run(
            ["git", "archive", PRE_FIX_COMMIT],
            cwd=str(REPO_ROOT),
            stdout=archive_file,
            check=True,
        )
    with tarfile.open(archive_path) as tar:
        tar.extractall(path=str(target), filter="data")
    archive_path.unlink()


class ReverseProxySubPathE2ETest(unittest.TestCase):
    """子路径反代端到端场景"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="agy2api-e2e-"))
        cls.processes = []
        cls.servers = []

        try:
            # 上游代码树（修复前）
            cls.upstream_tree = cls.tmp / "upstream"
            extract_upstream_tree(cls.upstream_tree)

            # 分支代码树：当前工作区副本（node_modules 等运行时无关内容除外）
            cls.branch_tree = cls.tmp / "branch"
            shutil.copytree(REPO_ROOT, cls.branch_tree, ignore=IGNORE_NAMES)
            cls.branch_rootpath_tree = cls.tmp / "branch-rootpath"
            shutil.copytree(REPO_ROOT, cls.branch_rootpath_tree, ignore=IGNORE_NAMES)

            # 场景一：上游构建 + 前缀剥离反代（复现故障）
            cls.upstream_process, cls.upstream_port = start_backend(cls.upstream_tree)
            cls.processes.append(cls.upstream_process)
            cls.upstream_proxy, cls.upstream_proxy_port = start_proxy(cls.upstream_port)
            cls.servers.append(cls.upstream_proxy)

            # 场景二：分支构建 + 前缀剥离反代
            cls.branch_process, cls.branch_port = start_backend(cls.branch_tree)
            cls.processes.append(cls.branch_process)
            cls.branch_proxy, cls.branch_proxy_port = start_proxy(cls.branch_port)
            cls.servers.append(cls.branch_proxy)

            # 场景四：分支构建 + 前缀保留反代 + PATH_PREFIX
            cls.rootpath_process, cls.rootpath_port = start_backend(
                cls.branch_rootpath_tree, path_prefix=PROXY_PREFIX
            )
            cls.processes.append(cls.rootpath_process)
            cls.rootpath_proxy, cls.rootpath_proxy_port = start_proxy(
                cls.rootpath_port, preserve_prefix=True
            )
            cls.servers.append(cls.rootpath_proxy)
        except Exception:
            cls._teardown()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._teardown()

    @classmethod
    def _teardown(cls):
        for process in getattr(cls, "processes", []):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        for server in getattr(cls, "servers", []):
            server.shutdown()
            server.server_close()
        if getattr(cls, "tmp", None) and cls.tmp.exists():
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------- 场景一：上游构建产物在子路径反代下故障 ----------

    def test_upstream_absolute_assets_fall_back_to_site_root(self):
        page = http_request(
            "GET", f"http://127.0.0.1:{self.upstream_proxy_port}{PROXY_PREFIX}/"
        )
        self.assertEqual(page.status, 200)
        self.assertIn("text/html", page.content_type)

        script_src = re.search(r'<script[^>]*src="([^"]+)"', page.text).group(1)
        self.assertTrue(
            script_src.startswith("/assets/"),
            "上游构建产物应以绝对路径引用模块脚本",
        )

        # 浏览器将绝对路径解析到站点根，该路径在反代端口上不属于本应用
        asset = http_request(
            "GET", f"http://127.0.0.1:{self.upstream_proxy_port}{script_src}"
        )
        self.assertIn(
            "text/html",
            asset.content_type,
            "上游场景下脚本请求应返回 HTML（复现模块脚本 MIME 报错）",
        )

        favicon = http_request(
            "GET", f"http://127.0.0.1:{self.upstream_proxy_port}/favicon.ico"
        )
        self.assertEqual(favicon.status, 404, "上游场景下 favicon 请求应 404")

    # ---------- 场景二：分支构建产物在子路径反代下可用 ----------

    def test_branch_assets_load_under_subpath_proxy(self):
        page = http_request(
            "GET", f"http://127.0.0.1:{self.branch_proxy_port}{PROXY_PREFIX}/"
        )
        self.assertEqual(page.status, 200)
        self.assertIn("text/html", page.content_type)

        for script_src in re.findall(r'<script[^>]*src="([^"]+)"', page.text):
            self.assertTrue(script_src.startswith("./"))
            asset = http_request("GET", urljoin(page.url, script_src))
            self.assertEqual(asset.status, 200)
            self.assertIn("javascript", asset.content_type)
            self.assertNotIn("text/html", asset.content_type)
            expected = (self.branch_tree / "dist" / script_src[2:]).read_bytes()
            self.assertEqual(asset.body, expected, f"{script_src} 内容应与后端一致")

        for css_href in re.findall(r'<link[^>]*href="([^"]+)"', page.text):
            self.assertTrue(css_href.startswith("./"))
            asset = http_request("GET", urljoin(page.url, css_href))
            self.assertEqual(asset.status, 200)
            if css_href.endswith(".css"):
                self.assertIn("text/css", asset.content_type)
            else:
                self.assertIn("image", asset.content_type)

        favicon = http_request("GET", urljoin(page.url, "./favicon.ico"))
        self.assertEqual(favicon.status, 200)
        self.assertIn("image/x-icon", favicon.content_type)

        docs_image = http_request(
            "GET",
            f"http://127.0.0.1:{self.branch_proxy_port}{PROXY_PREFIX}/docs/qq-group.jpg",
        )
        self.assertEqual(docs_image.status, 200)
        self.assertIn("image", docs_image.content_type)

    def test_branch_api_calls_under_subpath_proxy(self):
        base = f"http://127.0.0.1:{self.branch_proxy_port}{PROXY_PREFIX}"

        login = http_request(
            "POST", f"{base}/auth/login", json_body={"password": PANEL_PASSWORD}
        )
        self.assertEqual(login.status, 200)
        token = json.loads(login.text)["token"]

        wrong = http_request(
            "POST", f"{base}/auth/login", json_body={"password": "wrong-password"}
        )
        self.assertEqual(wrong.status, 401)

        version = http_request("GET", f"{base}/version/info")
        self.assertEqual(version.status, 200)
        self.assertTrue(json.loads(version.text)["success"])

        config = http_request(
            "GET", f"{base}/config/get", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(config.status, 200)

    # ---------- 场景三：根路径部署回归 ----------

    def test_branch_root_deployment_regression(self):
        base = f"http://127.0.0.1:{self.branch_port}"
        page = http_request("GET", f"{base}/")
        self.assertEqual(page.status, 200)

        for script_src in re.findall(r'<script[^>]*src="([^"]+)"', page.text):
            asset = http_request("GET", urljoin(page.url, script_src))
            self.assertEqual(asset.status, 200)
            self.assertIn("javascript", asset.content_type)

        favicon = http_request("GET", f"{base}/favicon.ico")
        self.assertEqual(favicon.status, 200)

        login = http_request(
            "POST", f"{base}/auth/login", json_body={"password": PANEL_PASSWORD}
        )
        self.assertEqual(login.status, 200)

    # ---------- 场景四：反代保留前缀 + PATH_PREFIX ----------

    def test_branch_prefix_preserving_proxy_with_path_prefix(self):
        base = f"http://127.0.0.1:{self.rootpath_proxy_port}{PROXY_PREFIX}"
        page = http_request("GET", f"{base}/")
        self.assertEqual(page.status, 200)
        self.assertIn("text/html", page.content_type)

        for script_src in re.findall(r'<script[^>]*src="([^"]+)"', page.text):
            asset = http_request("GET", urljoin(page.url, script_src))
            self.assertEqual(asset.status, 200)
            self.assertIn("javascript", asset.content_type)

        favicon = http_request("GET", urljoin(page.url, "./favicon.ico"))
        self.assertEqual(favicon.status, 200)

        login = http_request(
            "POST", f"{base}/auth/login", json_body={"password": PANEL_PASSWORD}
        )
        self.assertEqual(login.status, 200)

    # ---------- 场景五：不带末尾斜杠的访问 ----------

    def test_no_trailing_slash_url_serves_normalization_script(self):
        page = http_request(
            "GET", f"http://127.0.0.1:{self.branch_proxy_port}{PROXY_PREFIX}"
        )
        self.assertEqual(page.status, 200)

        inline_index = page.text.find("location.pathname.endsWith")
        module_index = re.search(r'<script[^>]*type="module"', page.text).start()
        self.assertNotEqual(inline_index, -1, "页面应包含末尾斜杠规整脚本")
        self.assertLess(
            inline_index,
            module_index,
            "规整脚本应先于模块脚本，保证模块加载失败时仍能跳转",
        )

    # ---------- 场景六：浏览器级验证（控制台报错与渲染） ----------

    def test_browser_console_and_rendering(self):
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            self.skipTest("未找到 node，跳过浏览器级验证")
        result = subprocess.run(
            [
                node,
                str(REPO_ROOT / "tests" / "frontend" / "browser_subpath_check.mjs"),
                "--branch-subpath",
                f"http://127.0.0.1:{self.branch_proxy_port}{PROXY_PREFIX}/",
                "--upstream-subpath",
                f"http://127.0.0.1:{self.upstream_proxy_port}{PROXY_PREFIX}/",
                "--branch-no-slash",
                f"http://127.0.0.1:{self.branch_proxy_port}{PROXY_PREFIX}",
                "--password",
                PANEL_PASSWORD,
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        sys.stdout.write(result.stdout)
        if "SKIP:" in result.stdout:
            self.skipTest("playwright-core 不可用，跳过浏览器级验证")
        self.assertEqual(
            result.returncode,
            0,
            f"浏览器验证失败:\n{result.stdout}\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
