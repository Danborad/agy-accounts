/*
 * Antigravity UI 汉化注入脚本
 * 由 agy-accounts 的 Nginx 注入层加载，词库外置在 /__agy-i18n-zh.json。
 *
 * 只翻译精确匹配的界面文案；代码块、终端、输入框、会话正文、
 * 对话标题和项目名不会被改写。网址加 ?no-i18n=1 可临时关闭。
 */
(function () {
  "use strict";

  if (window.location.pathname.indexOf("/__agy/sidecar/") === 0) return;

  var params = new URLSearchParams(window.location.search);
  var STORAGE_KEY = "agy-i18n-enabled";

  try {
    if (params.get("no-i18n") === "1") {
      localStorage.setItem(STORAGE_KEY, "0");
      return;
    }
    if (params.get("i18n") === "1") {
      localStorage.setItem(STORAGE_KEY, "1");
    }
    if (localStorage.getItem(STORAGE_KEY) === "0") return;
  } catch (e) {
    // ignore storage failures
  }

  var dict = null;
  var rules = [
    [/^Welcome back, (.+)\. Where should we pick up\?$/, function (m) {
      return "欢迎回来，" + m[1] + "。要从哪里继续？";
    }],
    [/^Your Plan: (.+)$/, function (m) { return "你的套餐：" + m[1]; }],
    [/^Resets in (.+)$/, function (m) { return duration(m[1]) + " 后重置"; }],
    [/^(\d+(?:\.\d+)?)% left$/, function (m) { return "剩余 " + m[1] + "%"; }],
    [/^Hey (.+), what should we build today\?$/, function (m) {
      return "嗨，" + m[1] + "，今天想构建什么？";
    }],
    [/^Good to see you, (.+)\. What's next\?$/, function (m) {
      return "很高兴见到你，" + m[1] + "。接下来做什么？";
    }],
    [/^Select model, current: (.+)$/, function (m) { return "选择模型，当前：" + m[1]; }],
    [/^Select project, current: (.+)$/, function (m) { return "选择项目，当前：" + m[1]; }],
    [/^Agent permissions preset selector, current: (.+)$/, function (m) {
      return "代理权限预设，当前：" + m[1];
    }],
    [/^Remove shortcut (.+) for (.+)$/, function (m) {
      return "移除“" + m[2] + "”的快捷键：" + m[1];
    }],
    [/^Shortcut (.+) for (.+)$/, function (m) {
      return "“" + m[2] + "”的快捷键：" + m[1];
    }],
    [/^Add shortcut for (.+)$/, function (m) { return "为“" + m[1] + "”添加快捷键"; }],
    [/^Show (\d+) breakdown$/, function (m) { return "展开 " + m[1] + " 项明细"; }],
    [/^(\d+(?:\.\d+)?)\s*\/\s*([\d,]+) tokens \(([\d.]+)%\)$/, function (m) {
      return m[1] + " / " + m[2] + " tokens（" + m[3] + "%）";
    }],
    [/^\(([\d,]+) tokens\) ([\d.]+)%$/, function (m) {
      return "（" + m[1] + " tokens）" + m[2] + "%";
    }],
    [/^Rules: ([\d,]+) tokens$/, function (m) { return "规则：" + m[1] + " tokens"; }],
    [/^Skills: ([\d,]+) tokens$/, function (m) { return "技能：" + m[1] + " tokens"; }],
    [/^(\d+)\s*mo$/, function (m) { return m[1] + " 个月前"; }],
    [/^(\d+)\s*d$/, function (m) { return m[1] + " 天前"; }],
    [/^(\d+)\s*h$/, function (m) { return m[1] + " 小时前"; }],
    [/^(\d+)\s*m$/, function (m) { return m[1] + " 分钟前"; }],
    [/^(\d+)\s*s$/, function (m) { return m[1] + " 秒前"; }],
    [/^(\d+) days?$/, function (m) { return m[1] + " 天"; }],
    [/^(\d+) hours?$/, function (m) { return m[1] + " 小时"; }],
    [/^(\d+) minutes?$/, function (m) { return m[1] + " 分钟"; }]
  ];

  var SKIP_TEXT = [
    "script", "style", "pre", "code", "kbd", "samp", "textarea",
    "[contenteditable='true']", "[contenteditable='']",
    ".monaco-editor", ".cm-editor", ".xterm", ".terminal",
    ".markdown-body", ".prose", "article", "[data-message-author-role]",
    "a[href^='/c/']", "a[href*='project=']"
  ].join(",");

  var SKIP_ATTR = [
    "script", "style", "pre", "code", ".monaco-editor", ".cm-editor",
    ".xterm", ".terminal", ".markdown-body", ".prose", "article",
    "[data-message-author-role]"
  ].join(",");

  function duration(text) {
    return String(text)
      .replace(/(\d+(?:\.\d+)?)\s*d\b/g, "$1 天")
      .replace(/(\d+(?:\.\d+)?)\s*h\b/g, "$1 小时")
      .replace(/(\d+(?:\.\d+)?)\s*m\b/g, "$1 分钟");
  }

  function translate(raw) {
    if (!dict || typeof raw !== "string") return null;
    var text = raw.trim();
    if (!text || text.length > 240) return null;
    if (Object.prototype.hasOwnProperty.call(dict, text)) {
      return raw.replace(text, dict[text]);
    }
    for (var i = 0; i < rules.length; i += 1) {
      var match = text.match(rules[i][0]);
      if (match) return raw.replace(text, rules[i][1](match));
    }
    return null;
  }

  function shouldSkip(element) {
    return Boolean(element && element.closest(SKIP_TEXT));
  }

  function translateTextNode(node) {
    if (!node || node.nodeType !== 3) return;
    if (shouldSkip(node.parentElement)) return;
    var next = translate(node.nodeValue);
    if (next !== null && next !== node.nodeValue) node.nodeValue = next;
  }

  function translateAttributes(element) {
    if (!element || element.nodeType !== 1) return;
    if (element.closest(SKIP_ATTR)) return;
    ["placeholder", "title", "aria-label"].forEach(function (name) {
      var value = element.getAttribute(name);
      if (!value) return;
      var next = translate(value);
      if (next !== null && next !== value) element.setAttribute(name, next);
    });
  }

  function scan(root) {
    if (!root || !dict) return;
    if (root.nodeType === 3) {
      translateTextNode(root);
      return;
    }
    if (root.nodeType !== 1 && root.nodeType !== 9) return;
    if (root.nodeType === 1) translateAttributes(root);
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) translateTextNode(node);
    if (root.nodeType === 1 || root.nodeType === 9) {
      var elements = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (var i = 0; i < elements.length; i += 1) translateAttributes(elements[i]);
    }
  }

  var pending = new Set();
  var scheduled = false;

  function flush() {
    scheduled = false;
    if (!dict) return;
    var nodes = Array.from(pending);
    pending.clear();
    nodes.forEach(function (node) {
      if (!node.isConnected) return;
      if (node.nodeType === 3) translateTextNode(node);
      else if (node.nodeType === 1) scan(node);
    });
  }

  function schedule(node) {
    pending.add(node);
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(flush);
  }

  function start() {
    if (!document.body) {
      document.addEventListener("DOMContentLoaded", start, { once: true });
      return;
    }
    scan(document.body);
    var observer = new MutationObserver(function (mutations) {
      mutations.forEach(function (mutation) {
        if (mutation.type === "childList") {
          mutation.addedNodes.forEach(function (node) {
            if (node.nodeType === 1 || node.nodeType === 3) schedule(node);
          });
        } else if (mutation.type === "characterData") {
          schedule(mutation.target);
        } else if (mutation.type === "attributes") {
          translateAttributes(mutation.target);
        }
      });
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["placeholder", "title", "aria-label"]
    });
  }

  window
    .fetch("/__agy-i18n-zh.json", { cache: "no-store" })
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(function (data) {
      delete data._meta;
      dict = data;
      window.__agyI18n = { dict: dict, translate: translate, enabled: true };
      start();
    })
    .catch(function (error) {
      console.warn("[agy-i18n] 词库加载失败：", error);
    });
})();
