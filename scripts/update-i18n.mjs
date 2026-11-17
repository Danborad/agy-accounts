#!/usr/bin/env node
/*
 * 从上游 Antigravity-Chinese-Localization 提取 Web UI 汉化引擎。
 *
 * 上游 localize.js 同时包含 Electron app.asar 注入逻辑和一段自包含的
 * DOM 翻译脚本。这里只取出 DOM_TRANSLATOR_INJECTION，包上 agy-accounts
 * 的开关逻辑，写入 i18n/agy-i18n.js，不修改 Antigravity 安装文件。
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const UPSTREAM = 'https://raw.githubusercontent.com/antigravity-cn-community/Antigravity-Chinese-Localization/master/localize.js';
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const response = await fetch(UPSTREAM);
if (!response.ok) {
    throw new Error(`下载上游失败：HTTP ${response.status}`);
}
const source = await response.text();
const versionMatch = source.match(/const RELEASE_VERSION = '([^']+)'/);
const version = versionMatch ? versionMatch[1] : 'unknown';
const payloadMatch = source.match(/const DOM_TRANSLATOR_INJECTION = (`[\s\S]*?`);/);
if (!payloadMatch) {
    throw new Error('未找到 DOM_TRANSLATOR_INJECTION');
}
const payload = eval(payloadMatch[1]);
if (typeof payload !== 'string' || !payload.includes('MutationObserver')) {
    throw new Error('上游汉化脚本格式异常');
}

const header = `/*
 * Antigravity 中文界面注入脚本
 *
 * 汉化引擎来源：antigravity-cn-community/Antigravity-Chinese-Localization
 * https://github.com/antigravity-cn-community/Antigravity-Chinese-Localization
 * 上游版本：v${version}
 *
 * MIT License
 * Copyright (c) 2026 aabbWei
 * Copyright (c) 2026 antigravity-cn-community contributors
 *
 * 本文件仅作为 agy-accounts 的 Nginx 注入层使用，不修改 Antigravity
 * 安装目录或 app.asar。上游完整许可见 NOTICE。
 */
(function () {
  "use strict";
  try {
    var params = new URLSearchParams(window.location.search);
    var key = "agy-i18n-enabled";
    if (params.get("no-i18n") === "1") {
      localStorage.setItem(key, "0");
      return;
    }
    if (params.get("i18n") === "1") {
      localStorage.setItem(key, "1");
    }
    if (localStorage.getItem(key) === "0") return;
  } catch (e) {
    // localStorage 不可用时继续翻译
  }
`;

const output = header + payload + '\n})();\n';
// 语法自检，避免把损坏的上游内容写进注入层。
new Function(output);

await fs.writeFile(path.join(ROOT, 'i18n', 'agy-i18n.js'), output, 'utf8');
console.log(`已更新 i18n/agy-i18n.js（上游 v${version}，${Buffer.byteLength(output)} 字节）`);
