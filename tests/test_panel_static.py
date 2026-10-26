"""面板静态资源路由测试（FastAPI TestClient）

覆盖控制面板首页、assets、favicon、docs 静态目录的响应内容与内容类型，
以及路径穿越防护。运行方式（项目根目录、使用项目虚拟环境）：

    python tests/test_panel_static.py
"""

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 在临时目录的代码树副本中运行：docs 静态目录挂载依赖仓库根目录作为工作目录，
# 应用启动还会在数据库目录写入运行时文件，避免污染仓库
TREE = Path(tempfile.mkdtemp(prefix="agy2api-static-")) / "tree"
shutil.copytree(
    REPO_ROOT,
    TREE,
    ignore=shutil.ignore_patterns(
        ".git", "node_modules", "creds", "data", "__pycache__", "*.pyc"
    ),
)
os.chdir(TREE)
sys.path.insert(0, str(TREE))

from fastapi.testclient import TestClient  # noqa: E402

from web import app  # noqa: E402


class PanelStaticRoutesTest(unittest.TestCase):
    """面板静态资源路由"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        # 进入上下文触发 lifespan：初始化配置缓存与数据库
        cls.client.__enter__()
        cls.response = cls.client.get("/")
        cls.index_html = cls.response.text

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_index_returns_html(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertIn("text/html", self.response.headers["content-type"])

    def test_index_assets_references_are_relative(self):
        refs = re.findall(r'(?:src|href)="([^"]+)"', self.index_html)
        asset_refs = [ref for ref in refs if "assets/" in ref or "favicon" in ref]
        self.assertTrue(asset_refs, "首页应引用构建产物资源")
        for ref in asset_refs:
            self.assertTrue(
                ref.startswith("./"),
                f"资源引用应为相对路径，实际为 {ref}",
            )

    def test_index_has_no_root_absolute_asset_reference(self):
        self.assertIsNone(
            re.search(r'(?:src|href)="/(?!/)', self.index_html),
            "首页不允许出现指向站点根的绝对资源引用",
        )

    def test_asset_script_served_with_javascript_mime(self):
        match = re.search(r'<script[^>]*src="([^"]+)"', self.index_html)
        self.assertIsNotNone(match)
        response = self.client.get(match.group(1))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("text/html", response.headers["content-type"])
        self.assertIn("javascript", response.headers["content-type"])

    def test_asset_stylesheet_served_with_css_mime(self):
        match = re.search(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', self.index_html)
        self.assertIsNotNone(match)
        response = self.client.get(match.group(1))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response.headers["content-type"])

    def test_favicon_served_with_icon_mime(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 200)
        self.assertIn("image/x-icon", response.headers["content-type"])

    def test_docs_static_file_served(self):
        response = self.client.get("/docs/qq-group.jpg")
        self.assertEqual(response.status_code, 200)
        self.assertIn("image", response.headers["content-type"])

    def test_asset_path_traversal_rejected(self):
        response = self.client.get("/assets/%2e%2e/web.py")
        self.assertEqual(response.status_code, 404)

    def test_missing_asset_returns_404(self):
        response = self.client.get("/assets/not-exist.js")
        self.assertEqual(response.status_code, 404)

    def test_login_rejects_wrong_password(self):
        response = self.client.post(
            "/auth/login", json={"password": "definitely-wrong"}
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TREE.parent, ignore_errors=True)
