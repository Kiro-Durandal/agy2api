import { test } from "node:test";
import assert from "node:assert/strict";
import { getBasePath } from "../../frontend/src/basePath.js";

function withLocation(pathname, fn) {
  const previous = globalThis.window;
  globalThis.window = {
    location: {
      pathname,
      href: `http://panel.example${pathname}`,
    },
  };
  try {
    fn();
  } finally {
    globalThis.window = previous;
  }
}

test("getBasePath 面板挂在根路径", () => {
  withLocation("/", () => {
    assert.equal(getBasePath(), "/");
  });
});

test("getBasePath 面板挂在子路径且带末尾斜杠", () => {
  withLocation("/AGY2API/", () => {
    assert.equal(getBasePath(), "/AGY2API/");
  });
});

test("getBasePath 面板挂在子路径但不带末尾斜杠", () => {
  withLocation("/AGY2API", () => {
    assert.equal(getBasePath(), "/AGY2API/");
  });
});

test("getBasePath 多级嵌套路径", () => {
  withLocation("/api/v1/panel/", () => {
    assert.equal(getBasePath(), "/api/v1/panel/");
  });
  withLocation("/api/v1/panel", () => {
    assert.equal(getBasePath(), "/api/v1/panel/");
  });
});

// api.js 在模块加载时读取 window.location 计算 baseURL，
// 每个用例通过查询串导入独立模块实例，避免实例间串扰
async function importApi(pathname) {
  globalThis.window = {
    location: {
      pathname,
      href: `http://panel.example${pathname}`,
    },
  };
  return import(`../../frontend/src/api.js?case=${encodeURIComponent(pathname)}`);
}

const URL_COMPOSITION_CASES = [
  { pathname: "/", url: "/auth/login", expected: "/auth/login" },
  { pathname: "/AGY2API/", url: "/auth/login", expected: "/AGY2API/auth/login" },
  { pathname: "/AGY2API", url: "/auth/login", expected: "/AGY2API/auth/login" },
  { pathname: "/api/v1/panel/", url: "/version/info", expected: "/api/v1/panel/version/info" },
  { pathname: "/AGY2API/", url: "/usage-logs/list", expected: "/AGY2API/usage-logs/list" },
];

for (const testCase of URL_COMPOSITION_CASES) {
  test(`api URL 组合 页面 ${testCase.pathname} 请求 ${testCase.url}`, async () => {
    const api = (await importApi(testCase.pathname)).default;
    assert.equal(
      api.getUri({ url: testCase.url }),
      testCase.expected,
      "请求 URL 应与页面所在基径拼合",
    );
  });
}

test("api 401 响应后跳转到面板基径而非站点根", async () => {
  const api = (await importApi("/AGY2API/")).default;
  globalThis.localStorage = {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  };
  api.defaults.adapter = async () => {
    const error = new Error("Request failed with status code 401");
    error.response = { status: 401, data: { detail: "密码错误" } };
    throw error;
  };

  await assert.rejects(api.post("/auth/login", { password: "wrong" }));
  assert.equal(globalThis.window.location.href, "/AGY2API/");
});

test("api 401 响应后根路径部署跳转到根", async () => {
  const api = (await importApi("/")).default;
  globalThis.localStorage = {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  };
  api.defaults.adapter = async () => {
    const error = new Error("Request failed with status code 401");
    error.response = { status: 401, data: { detail: "密码错误" } };
    throw error;
  };

  await assert.rejects(api.get("/config/get"));
  assert.equal(globalThis.window.location.href, "/");
});

test("api 请求拦截器附加登录 token", async () => {
  const api = (await importApi("/AGY2API/")).default;
  globalThis.localStorage = {
    getItem: (key) => (key === "panelToken" ? "test-token" : null),
    setItem: () => {},
    removeItem: () => {},
  };
  let capturedConfig = null;
  api.defaults.adapter = async (config) => {
    capturedConfig = config;
    return { data: {}, status: 200, statusText: "OK", headers: {}, config };
  };

  await api.get("/version/info");
  assert.equal(capturedConfig.headers.Authorization, "Bearer test-token");
});
