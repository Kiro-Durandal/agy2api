/**
 * 浏览器级反代子路径验证脚本
 *
 * 用真实 Chromium 复现并验证控制台报错与白屏问题：
 *   1. 上游构建产物（绝对路径引用）+ 子路径反代：
 *      控制台出现模块脚本 MIME 类型报错，页面白屏（上游子路径故障现象）
 *   2. 分支构建产物（相对路径引用）+ 子路径反代：
 *      无控制台报错，面板正常渲染，登录流程可用
 *   3. 分支构建产物 + 不带末尾斜杠的子路径：
 *      自动补齐斜杠后正常渲染
 *
 * 依赖可选的 playwright-core，未安装或缺少浏览器时打印 SKIP 并以退出码 0 结束：
 *   cd frontend && npm i -D playwright-core && npx playwright-core install chromium
 *
 * 用法：
 *   node tests/frontend/browser_subpath_check.mjs \
 *     --branch-subpath http://127.0.0.1:23111/AGY2API/ \
 *     --upstream-subpath http://127.0.0.1:23112/AGY2API/ \
 *     --branch-no-slash http://127.0.0.1:23111/AGY2API \
 *     --password e2e-test-password
 */

import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 2) {
    args[argv[i].replace(/^--/, "")] = argv[i + 1];
  }
  return args;
}

const args = parseArgs(process.argv);
const PANEL_PASSWORD = args.password || "e2e-test-password";

let chromium;
try {
  ({ chromium } = require("playwright-core"));
} catch {
  console.log("SKIP: 未安装 playwright-core，跳过浏览器级验证");
  process.exit(0);
}

// 每个场景独立启动浏览器：故障页面可能导致浏览器进程崩溃，场景之间互相隔离
async function withBrowser(fn) {
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox"],
  });
  try {
    return await fn(browser);
  } finally {
    try {
      await browser.close();
    } catch {
      // 浏览器可能已崩溃，忽略关闭错误
    }
  }
}

function collectPageDiagnostics(page) {
  const consoleErrors = [];
  const requestFailures = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    consoleErrors.push(`pageerror: ${error.message}`);
  });
  page.on("requestfailed", (request) => {
    requestFailures.push(
      `${request.url()} ${request.failure()?.errorText || ""}`.trim(),
    );
  });
  return { consoleErrors, requestFailures };
}

async function waitForLoginUi(page, timeout = 20000) {
  await page.waitForSelector("text=登录控制面板", { timeout });
}

async function assertBranchPageHealthy(url, label) {
  return withBrowser(async (activeBrowser) => {
    const context = await activeBrowser.newContext();
    const page = await context.newPage();
    const { consoleErrors, requestFailures } = collectPageDiagnostics(page);
    await page.goto(url, { waitUntil: "load", timeout: 30000 });
    await waitForLoginUi(page);

    const problems = [];
    if (consoleErrors.length > 0) {
      problems.push(`控制台存在报错: ${consoleErrors.join(" | ")}`);
    }
    if (requestFailures.length > 0) {
      problems.push(`存在失败请求: ${requestFailures.join(" | ")}`);
    }

    if (problems.length === 0) {
      // 登录流程：验证子路径下的 API 请求与鉴权
      await page.fill('input[type="password"]', PANEL_PASSWORD);
      await page.click('button:has-text("登录")');
      await page.waitForSelector("text=OAuth 认证", { timeout: 20000 });

      // 项目信息页：API 端点展示与静态图片应基于同一子路径
      await page.click('button:has-text("项目信息")');
      await page.waitForSelector("text=API 端点（点击复制）", { timeout: 20000 });
      const endpointText = await page.locator("code").first().innerText();
      if (!endpointText.includes("/AGY2API/v1/chat/completions")) {
        problems.push(`API 端点展示未包含子路径: ${endpointText}`);
      }
      const imageLoaded = await page
        .waitForFunction(
          () => {
            const img = document.querySelector('img[alt="QQ 群二维码"]');
            return !!img && img.complete && img.naturalWidth > 0;
          },
          { timeout: 10000 },
        )
        .then(() => true)
        .catch(() => false);
      if (!imageLoaded) {
        problems.push("QQ 群二维码图片未加载");
      }
    }
    await context.close();
    if (problems.length > 0) {
      throw new Error(`${label} ${problems.join("；")}`);
    }
  });
}

async function checkUpstreamReproducesFailure() {
  return withBrowser(async (browser) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    const { consoleErrors } = collectPageDiagnostics(page);
    await page.goto(args["upstream-subpath"], {
      waitUntil: "load",
      timeout: 30000,
    });
    await page.waitForTimeout(3000);

    const problems = [];
    const mimeError = consoleErrors.find((text) =>
      /module script|MIME type/i.test(text),
    );
    if (!mimeError) {
      problems.push(
        `未复现模块脚本 MIME 报错，控制台输出: ${consoleErrors.join(" | ") || "(空)"}`,
      );
    }
    const loginVisible = await page
      .locator("text=登录控制面板")
      .count()
      .then((count) => count > 0);
    if (loginVisible) {
      problems.push("页面不应正常渲染登录界面");
    }
    await context.close();
    if (problems.length > 0) {
      throw new Error(`上游子路径场景 ${problems.join("；")}`);
    }
  });
}

async function checkNoTrailingSlash() {
  return withBrowser(async (browser) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(args["branch-no-slash"], {
      waitUntil: "load",
      timeout: 30000,
    });
    const problems = [];
    if (!page.url().endsWith("/AGY2API/")) {
      problems.push(`不带末尾斜杠的访问未补齐斜杠，最终地址: ${page.url()}`);
    }
    const { consoleErrors, requestFailures } = collectPageDiagnostics(page);
    await waitForLoginUi(page);
    if (consoleErrors.length > 0) {
      problems.push(`控制台存在报错: ${consoleErrors.join(" | ")}`);
    }
    if (requestFailures.length > 0) {
      problems.push(`存在失败请求: ${requestFailures.join(" | ")}`);
    }
    await context.close();
    if (problems.length > 0) {
      throw new Error(`分支无尾斜杠场景 ${problems.join("；")}`);
    }
  });
}

async function run() {
  const scenarios = [
    ["上游子路径场景（复现故障）", () => checkUpstreamReproducesFailure()],
    ["分支子路径场景", () => assertBranchPageHealthy(args["branch-subpath"], "分支子路径场景")],
    ["分支无尾斜杠场景", () => checkNoTrailingSlash()],
  ];

  const failures = [];
  for (const [name, scenario] of scenarios) {
    try {
      await scenario();
      console.log(`PASS: ${name}`);
    } catch (error) {
      failures.push(`${name}: ${error.message}`);
      console.log(`FAIL: ${name}: ${error.message}`);
    }
  }

  if (failures.length > 0) {
    process.exit(1);
  }
  console.log("PASS: 浏览器级反代子路径验证全部通过");
  process.exit(0);
}

run();
