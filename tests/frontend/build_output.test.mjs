import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const repoRoot = fileURLToPath(new URL("../../", import.meta.url));
const distDir = path.join(repoRoot, "dist");
const distIndexPath = path.join(distDir, "index.html");

// 上游构建产物（提交 bcb264b）：使用绝对路径引用，
// 反代到子路径时浏览器会绕开子路径请求资源（控制台 MIME 报错、页面白屏）
const PRE_FIX_COMMIT = "bcb264b";

function readDistIndex() {
  return readFileSync(distIndexPath, "utf8");
}

function attrValues(html, tag, attr) {
  const pattern = new RegExp(`<${tag}[^>]*\\s${attr}="([^"]*)"`, "gi");
  return [...html.matchAll(pattern)].map((match) => match[1]);
}

test("构建产物中脚本与样式引用均为相对路径", () => {
  const html = readDistIndex();
  const refs = [
    ...attrValues(html, "script", "src"),
    ...attrValues(html, "link", "href"),
  ];
  assert.ok(refs.length >= 3, "应包含模块脚本、样式与 favicon 引用");
  for (const ref of refs) {
    assert.ok(
      ref.startsWith("./") && !ref.startsWith("/"),
      `引用应为相对路径，实际为 ${ref}`,
    );
    assert.ok(
      existsSync(path.join(distDir, ref)),
      `dist 中不存在被引用的文件 ${ref}`,
    );
  }
});

test("构建产物中不存在指向站点根的属性引用", () => {
  const html = readDistIndex();
  assert.doesNotMatch(
    html,
    /(?:src|href)="\/(?!\/)/,
    "不允许出现 /assets、/favicon.ico 等绝对路径引用",
  );
});

test("构建产物 bundle 包含页面基径计算逻辑", () => {
  const html = readDistIndex();
  const scriptSrc = attrValues(html, "script", "src")[0];
  const bundle = readFileSync(path.join(distDir, scriptSrc), "utf8");
  assert.match(bundle, /window\.location/);
  assert.match(bundle, /endsWith\("\/"\)/);
});

test("末尾斜杠规整脚本位于模块脚本之前", () => {
  const html = readDistIndex();
  const inlineIdx = html.indexOf("location.pathname.endsWith");
  const moduleIdx = html.search(/<script[^>]*type="module"/);
  assert.ok(inlineIdx !== -1, "应包含末尾斜杠规整脚本");
  assert.ok(moduleIdx !== -1, "应包含模块脚本");
  assert.ok(
    inlineIdx < moduleIdx,
    "规整脚本必须先于模块脚本，保证模块加载失败时仍能跳转",
  );
});

test("上游构建产物使用绝对路径引用（反代子路径故障的根因）", (t) => {
  let upstreamHtml;
  try {
    upstreamHtml = execFileSync(
      "git",
      ["show", `${PRE_FIX_COMMIT}:dist/index.html`],
      { cwd: repoRoot, encoding: "utf8" },
    );
  } catch {
    t.skip("当前环境无 git 历史，跳过上游产物对比");
    return;
  }
  assert.match(
    upstreamHtml,
    /<script[^>]*src="\/assets\//,
    "上游产物应以绝对路径引用模块脚本",
  );
  assert.match(
    upstreamHtml,
    /<link[^>]*href="\/favicon\.ico"/,
    "上游产物应以绝对路径引用 favicon",
  );
  assert.notEqual(
    upstreamHtml,
    readDistIndex(),
    "分支产物应与上游产物存在差异",
  );
});
