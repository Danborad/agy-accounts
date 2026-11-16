/*
 * Tauri → HTTP bridge for the agy-switch web frontend.
 *
 * The original frontend calls @tauri-apps/api `invoke(...)`. In a browser there
 * is no Tauri runtime, so this shim implements just enough of
 * window.__TAURI_INTERNALS__ to map account/quota commands onto the
 * agy-accounts Python HTTP API.
 *
 * This file is part of agy-accounts and is licensed CC BY-NC-SA 4.0.
 */
(function () {
  "use strict";

  var CONFIG_KEY = "agy-accounts-web-config";
  var ORDER_KEY = "agy-accounts-web-order";
  var THEME_KEY = "app-theme-preference";

  var callbacks = {};
  var nextCallback = 1;
  var eventListeners = new Map();
  var eventIds = new Map();
  var nextEventId = 1;
  var cache = { at: 0, status: null, quotas: null };
  var inflight = null;
  var quotaTimer = null;
  var quotaInflight = null;
  var quotaRefreshAt = 0;

  try { localStorage.setItem(THEME_KEY, "dark"); } catch (e) {}

  // ---------------------------------------------------------------- HTTP

  function api(path, options) {
    options = options || {};
    var headers = Object.assign({
      "Content-Type": "application/json",
      "X-Agy-Accounts": "1"
    }, options.headers || {});
    return fetch(path, {
      method: options.method || "GET",
      credentials: "same-origin",
      headers: headers,
      body: options.body
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (response.status === 401) {
          showLogin("登录已过期，请重新输入面板密码。");
          throw new Error("未登录");
        }
        if (!response.ok) {
          throw new Error(data.error || ("HTTP " + response.status));
        }
        return data;
      });
    });
  }

  function emitEvent(event, payload) {
    var ids = eventListeners.get(event);
    if (!ids) return;
    ids.forEach(function (eventId) {
      var meta = eventIds.get(eventId);
      if (!meta) return;
      var callback = callbacks[meta.handler];
      if (typeof callback !== "function") return;
      try {
        callback({ event: event, id: eventId, payload: payload });
      } catch (error) {
        console.warn("[agy-accounts] event handler failed:", event, error);
      }
    });
  }

  function scheduleQuotaRefresh() {
    if (!cache.status || quotaTimer || quotaInflight) return;
    var quotas = (cache.quotas && cache.quotas.quotas) || {};
    var now = Date.now();
    if (now - quotaRefreshAt < 15000) return;
    var needsRefresh = Object.keys(quotas).some(function (id) {
      var quota = quotas[id];
      if (!quota || quota.status === "loading" || !quota.fetched_at) return true;
      return now - Number(quota.fetched_at) * 1000 > 120000;
    });
    if (!needsRefresh && now - quotaRefreshAt < 60000) return;
    quotaRefreshAt = now;
    quotaTimer = window.setTimeout(function () {
      quotaTimer = null;
      quotaInflight = api("/api/quotas")
        .then(function (data) {
          cache.quotas = data;
          cache.at = Date.now();
          emitEvent("accounts://refreshed", {});
        })
        .catch(function () {})
        .finally(function () {
          quotaInflight = null;
        });
    }, 200);
  }

  function loadSnapshot(forceQuota) {
    if (inflight && !forceQuota) return inflight;
    var quotaPath = forceQuota ? "/api/quotas?refresh=1" : "/api/quotas?cached=1";
    inflight = Promise.all([
      api("/api/status"),
      api(quotaPath)
    ]).then(function (values) {
      cache = { at: Date.now(), status: values[0], quotas: values[1] };
      if (!forceQuota) scheduleQuotaRefresh();
      return cache;
    }).finally(function () {
      inflight = null;
    });
    return inflight;
  }

  // ------------------------------------------------------------- mapping

  function bucketId(key, window) {
    var prefix = key === "claude" ? "3p" : (key === "gemini" ? "gemini" : (key || "other"));
    return prefix + "-" + (window === "5h" ? "5h" : "weekly");
  }

  function emptyQuota() {
    return {
      models: [],
      last_updated: 0,
      is_forbidden: false,
      subscription_tier: null,
      quota_groups: []
    };
  }

  function mapQuota(quota) {
    if (!quota || quota.status === "error") return emptyQuota();
    var groups = (quota.groups || []).map(function (group) {
      var buckets = Object.keys(group.windows || {}).map(function (window) {
        var bucket = group.windows[window];
        return {
          bucket_id: bucketId(group.key, window),
          window: window,
          remaining_fraction: bucket.remaining,
          remaining_fraction_known: !bucket.disabled,
          reset_time: bucket.reset || "",
          display_name: group.label || "",
          description: bucket.disabled ? "does not currently apply" : ""
        };
      });
      return {
        display_name: group.label || group.key,
        description: "",
        buckets: buckets
      };
    });

    var models = [];
    var reported = Array.isArray(quota.models) ? quota.models : [];
    if (reported.length) {
      models = reported.map(function (model) {
        return {
          name: model.name,
          display_name: model.display_name || model.name,
          percentage: model.percentage,
          percentage_known: model.percentage !== null && model.percentage !== undefined,
          observed_remaining_fraction: model.remaining,
          reset_time: model.reset || "",
          inferred_bucket_id: null,
          supports_images: Boolean(model.supports_images),
          supports_thinking: Boolean(model.supports_thinking)
        };
      });
    } else {
      (quota.groups || []).forEach(function (group) {
        var source = group.windows && (group.windows["5h"] || group.windows.weekly);
        if (!source) return;
        var percentage = Math.round(Number(source.remaining) * 100);
        var names = group.key === "claude"
          ? ["claude-opus-4-6-thinking"]
          : (group.key === "gemini" ? ["gemini-3.8-flash"] : [group.key || "model"]);
        names.forEach(function (name) {
          models.push({
            name: name,
            display_name: group.label || name,
            percentage: percentage,
            percentage_known: true,
            reset_time: source.reset || "",
            inferred_bucket_id: null,
            supports_images: false,
            supports_thinking: false
          });
        });
      });
    }

    return {
      models: models,
      last_updated: quota.fetched_at || 0,
      is_forbidden: false,
      subscription_tier: quota.tier || null,
      quota_groups: groups
    };
  }

  function mapAccount(account, quotas) {
    var quota = quotas && quotas[account.id];
    var email = account.email || account.name || account.id;
    return {
      id: account.id,
      email: email,
      name: account.name,
      token: {
        access_token: "",
        refresh_token: "",
        expires_in: 0,
        expiry_timestamp: 0,
        token_type: "Bearer",
        email: email
      },
      quota: mapQuota(quota),
      disabled: false,
      protected_models: [],
      custom_label: account.name && account.name !== email ? account.name : null,
      created_at: account.created_at || 0,
      last_used: account.last_used_at || account.created_at || 0
    };
  }

  function accountOrder() {
    try {
      var value = JSON.parse(localStorage.getItem(ORDER_KEY) || "[]");
      return Array.isArray(value) ? value : [];
    } catch (e) {
      return [];
    }
  }

  function mapAccounts(status, quotas) {
    var order = accountOrder();
    var list = (status.accounts || []).slice().sort(function (a, b) {
      var ai = order.indexOf(a.id);
      var bi = order.indexOf(b.id);
      if (ai >= 0 || bi >= 0) {
        if (ai < 0) return 1;
        if (bi < 0) return -1;
        return ai - bi;
      }
      return (b.last_used_at || 0) - (a.last_used_at || 0);
    });
    return list.map(function (account) { return mapAccount(account, quotas); });
  }

  function dashboardQuota(account, quotas) {
    var quota = quotas && quotas[account.id];
    if (!quota || quota.status === "error") {
      return {
        last_updated: 0,
        is_forbidden: false,
        subscription_tier: null,
        provenance: "observed",
        models: [],
        groups: []
      };
    }
    var mapped = mapQuota(quota);
    return {
      last_updated: mapped.last_updated,
      is_forbidden: false,
      subscription_tier: mapped.subscription_tier,
      provenance: "observed",
      models: mapped.models.map(function (model) {
        return {
          name: model.name,
          display_name: model.display_name,
          percentage: model.percentage,
          reset_time: model.reset_time,
          inferred_bucket_id: null
        };
      }),
      groups: mapped.quota_groups.map(function (group) {
        return {
          display_name: group.display_name,
          buckets: group.buckets.map(function (bucket) {
            return {
              bucket_id: bucket.bucket_id,
              window: bucket.window,
              remaining_fraction: bucket.remaining_fraction,
              reset_time: bucket.reset_time
            };
          })
        };
      })
    };
  }

  // -------------------------------------------------------------- config

  function defaultConfig() {
    return {
      language: "zh",
      theme: "dark",
      check_updates_on_startup: false,
      auto_refresh: true,
      refresh_interval: 5,
      auto_sync: false,
      sync_interval: 5,
      dashboard: {
        cards: ["total_tokens", "input_tokens", "output_tokens", "cache_hit_rate", "account_status", "aggregate_quota"],
        order: ["total_tokens", "input_tokens", "output_tokens", "cache_hit_rate", "api_cost", "first_text_latency", "body_speed", "account_status", "aggregate_quota", "average_input"]
      },
      desktop: { launch_at_login: false, hide_dock_icon: false, start_minimized: false },
      menu_bar: {
        quota_scope: "other", display_scope: "other", hide_unavailable: true,
        label_style: "email_then_label", show_aggregate: true, show_session: true,
        show_weekly: true, show_icons: true, show_reset_on_hover: true,
        green_above: 60, red_below: 20
      },
      quota_protection: { enabled: false, threshold_percentage: 10, monitored_models: [] },
      pinned_quota_models: { models: ["gemini-3.8-flash", "claude-opus-4-6-thinking"] },
      network: { proxy_mode: "system", proxy_url: "" },
      quota_valuation: {
        enabled: false, currency: "usd", display_mode: "remaining",
        auto_exchange_rate: false, usd_to_cny_rate: 7.25,
        use_reference_cap: false, reference_caps_usd: {}
      }
    };
  }

  function loadConfig() {
    var base = defaultConfig();
    try {
      var saved = JSON.parse(localStorage.getItem(CONFIG_KEY) || "null");
      if (saved && typeof saved === "object") {
        var savedDashboard = Object.assign({}, saved.dashboard || {});
        if (Array.isArray(savedDashboard.cards) && savedDashboard.cards.length === 2 &&
            savedDashboard.cards.indexOf("account_status") >= 0 &&
            savedDashboard.cards.indexOf("aggregate_quota") >= 0) {
          savedDashboard.cards = base.dashboard.cards.slice();
          savedDashboard.order = base.dashboard.order.slice();
        }
        return Object.assign(base, saved, {
          dashboard: Object.assign(base.dashboard, savedDashboard),
          desktop: Object.assign(base.desktop, saved.desktop || {}),
          menu_bar: Object.assign(base.menu_bar, saved.menu_bar || {}),
          quota_protection: Object.assign(base.quota_protection, saved.quota_protection || {}),
          pinned_quota_models: Object.assign(base.pinned_quota_models, saved.pinned_quota_models || {}),
          network: Object.assign(base.network, saved.network || {}),
          quota_valuation: Object.assign(base.quota_valuation, saved.quota_valuation || {})
        });
      }
    } catch (e) {}
    return base;
  }

  function saveConfig(config) {
    try { localStorage.setItem(CONFIG_KEY, JSON.stringify(config)); } catch (e) {}
  }

  // ----------------------------------------------------------- constants

  function zeroTotals() {
    return { input_tokens: 0, output_tokens: 0, cached_tokens: 0, total_tokens: 0, request_count: 0 };
  }

  function usageSummary() {
    return {
      recent_performance: null,
      today: zeroTotals(), yesterday: zeroTotals(), last_3_days: zeroTotals(),
      last_7_days: zeroTotals(), last_30_days: zeroTotals(),
      daily: [], hourly: [], by_model_today: [], by_model_yesterday: [],
      by_model_3_days: [], by_model_7_days: [], by_model: [],
      databases_scanned: 0, generations_scanned: 0, skipped_large_records: 0,
      unreadable_databases: 0, generated_at: Math.floor(Date.now() / 1000)
    };
  }

  function autoSwitchConfig() {
    return {
      enabled: false, mode: "wait", strategy: "priority",
      reserve_percentage: 10, candidate_min_percentage: 10,
      monitored_model: "", candidate_account_ids: [], target: "app"
    };
  }

  function autoSwitchStatus() {
    return {
      phase: "disabled", reason: null, source_account_id: null, source_email: null,
      target_account_id: null, target_email: null, remaining_percentage: null,
      pending_id: null, mode: "wait", process_state: "unknown", last_checked: null
    };
  }

  function webLoginError() {
    return new Error("Web 版不在这里完成 Google 授权。请打开 Antigravity 页面登录，再回面板保存。");
  }

  // -------------------------------------------------------------- bridge

  async function bridge(cmd, args) {
    args = args || {};
    if (cmd.indexOf("plugin:event|") === 0) {
      if (cmd.indexOf("|listen") >= 0) {
        var eventName = String(args.event || "");
        var handler = args.handler;
        var eventId = nextEventId++;
        var list = eventListeners.get(eventName) || new Set();
        list.add(eventId);
        eventListeners.set(eventName, list);
        eventIds.set(eventId, { event: eventName, handler: handler });
        return eventId;
      }
      if (cmd.indexOf("|unlisten") >= 0) {
        var removeId = args.eventId;
        var meta = eventIds.get(removeId);
        if (meta) {
          var listeners = eventListeners.get(meta.event);
          if (listeners) listeners.delete(removeId);
          eventIds.delete(removeId);
        }
        return null;
      }
      return null;
    }
    if (cmd.indexOf("plugin:window|") === 0) {
      if (cmd.indexOf("outer_size") >= 0 || cmd.indexOf("inner_size") >= 0) return { width: 1200, height: 800 };
      if (cmd.indexOf("scale_factor") >= 0) return 1;
      if (cmd.indexOf("is_decorated") >= 0 || cmd.indexOf("is_resizable") >= 0 ||
          cmd.indexOf("is_visible") >= 0 || cmd.indexOf("is_maximized") >= 0) return true;
      if (cmd.indexOf("title") >= 0) return "Antigravity 账号面板";
      return null;
    }
    if (cmd.indexOf("plugin:dialog|") === 0) return null;
    if (cmd.indexOf("plugin:opener|") === 0) return null;
    if (cmd.indexOf("plugin:autostart|") === 0) return null;

    switch (cmd) {
      case "load_config":
        return loadConfig();
      case "save_config":
        saveConfig(args.config || {});
        return null;
      case "list_accounts": {
        var snapshot = await loadSnapshot(false);
        return { accounts: mapAccounts(snapshot.status, snapshot.quotas.quotas) };
      }
      case "get_current_account": {
        var snap = await loadSnapshot(false);
        var state = snap.status;
        if (state.current) {
          var found = (state.accounts || []).filter(function (a) { return a.id === state.current; })[0];
          if (found) return mapAccount(found, snap.quotas.quotas);
        }
        if (state.token_present) {
          var email = state.token_email || "当前登录";
          return {
            id: "__live__",
            email: email,
            name: "当前登录（未保存）",
            token: {
              access_token: "", refresh_token: "", expires_in: 0,
              expiry_timestamp: 0, token_type: "Bearer", email: email
            },
            quota: emptyQuota(),
            disabled: false,
            protected_models: [],
            custom_label: null,
            created_at: 0,
            last_used: Math.floor(Date.now() / 1000)
          };
        }
        return null;
      }
      case "get_account_dashboard_snapshot": {
        var snap = (await loadSnapshot(false));
        var status = snap.status;
        var quotas = snap.quotas.quotas || {};
        var accounts = (status.accounts || []).map(function (account) {
          return {
            id: account.id,
            email: account.email || account.name || account.id,
            name: account.name || null,
            custom_label: account.name && account.name !== account.email ? account.name : null,
            read_status: "loaded",
            read_error: null,
            disabled: false,
            validation_blocked: false,
            validation_blocked_until: null,
            protected_models: [],
            quota: dashboardQuota(account, quotas)
          };
        });
        return {
          indexed_total: accounts.length,
          loaded_count: accounts.length,
          failed_count: 0,
          current_account_id: status.current || null,
          current_identity_source: "tools_record",
          accounts: accounts
        };
      }
      case "fetch_account_quota": {
        var forced = await loadSnapshot(true);
        return mapQuota((forced.quotas.quotas || {})[args.accountId]);
      }
      case "refresh_all_quotas": {
        var refreshed = await loadSnapshot(true);
        var values = Object.keys(refreshed.quotas.quotas || {}).map(function (key) {
          return refreshed.quotas.quotas[key];
        });
        return {
          total: values.length,
          success: values.filter(function (q) { return q.status !== "error"; }).length,
          failed: values.filter(function (q) { return q.status === "error"; }).length,
          details: values.filter(function (q) { return q.status === "error"; })
            .map(function (q) { return q.error || "额度查询失败"; })
        };
      }
      case "switch_account": {
        await api("/api/accounts/" + encodeURIComponent(args.accountId) + "/switch", {
          method: "POST", body: "{}"
        });
        cache.at = 0;
        return null;
      }
      case "update_account_label": {
        if (!args.label) return null;
        await api("/api/accounts/" + encodeURIComponent(args.accountId) + "/rename", {
          method: "POST", body: JSON.stringify({ name: args.label })
        });
        cache.at = 0;
        return null;
      }
      case "delete_account": {
        await api("/api/accounts/" + encodeURIComponent(args.accountId) + "/delete", {
          method: "POST", body: "{}"
        });
        cache.at = 0;
        return null;
      }
      case "delete_accounts": {
        var ids = args.accountIds || [];
        for (var i = 0; i < ids.length; i += 1) {
          await api("/api/accounts/" + encodeURIComponent(ids[i]) + "/delete", {
            method: "POST", body: "{}"
          });
        }
        cache.at = 0;
        return null;
      }
      case "reorder_accounts": {
        try { localStorage.setItem(ORDER_KEY, JSON.stringify(args.accountIds || [])); } catch (e) {}
        return null;
      }
      case "get_account_quota_valuations":
        return {
          accounts: [], currency: "usd", display_mode: "remaining",
          usd_to_cny_rate: 7.25, rate_stale: true,
          calculated_at: Math.floor(Date.now() / 1000)
        };
      case "get_quota_cycle_history":
        return {
          cycles: [], accounts_summary: [], currency: "usd",
          usd_to_cny_rate: 7.25, queried_at: Math.floor(Date.now() / 1000)
        };
      case "get_local_token_usage":
        return api("/api/usage");
      case "get_api_pricing":
        return {
          prices: [], fetched_at: 0, stale: true, source: "unavailable",
          warning: "Web 版暂未接入本地 Token 用量与价格数据"
        };
      case "get_exchange_rate":
        return { rate: 7.25, fetched_at: Math.floor(Date.now() / 1000), stale: true };
      case "get_running_version":
        return "agy-accounts 1.3.0 (web)";
      case "get_data_dir_path":
        return "/root/.config/agy-accounts";
      case "get_desktop_settings":
        return {
          platform: "linux", tray_available: false, autostart_supported: false,
          launch_at_login: false, hide_dock_icon: false, start_minimized: false
        };
      case "get_menu_bar_appearance":
        return { platform: "linux", native_material: false, reduced_transparency: false, high_contrast: false };
      case "get_auto_switch_config":
        return autoSwitchConfig();
      case "set_auto_switch_config":
        return args.config || autoSwitchConfig();
      case "get_auto_switch_status":
      case "check_auto_switch_now":
      case "cancel_auto_switch":
        return autoSwitchStatus();
      case "get_app_experiments":
        return {};
      case "list_oauth_clients":
        return { clients: [] };
      case "get_active_oauth_client":
        return { client_key: "" };
      case "set_active_oauth_client":
      case "set_window_theme":
      case "set_app_translation":
      case "set_dashboard_cards":
      case "set_desktop_preferences":
      case "set_menu_bar_preferences":
      case "open_data_folder":
      case "main_window_ready":
      case "present_main_window":
      case "show_main_window":
      case "hide_menu_bar_dashboard":
      case "open_app_page":
      case "open_project_page":
      case "check_for_updates":
        return null;
      case "add_account":
      case "start_oauth_login":
      case "prepare_oauth_url":
      case "complete_oauth_login":
      case "submit_oauth_code":
      case "cancel_oauth_login":
      case "import_from_db":
      case "import_custom_db":
      case "sync_account_from_db":
      case "import_local_accounts_report":
      case "export_accounts":
      case "save_account_export":
      case "import_account_transfer":
      case "download_and_install_update":
      case "quit_app":
        throw webLoginError();
      default:
        console.warn("[agy-accounts] unhandled Tauri command:", cmd, args);
        return null;
    }
  }

  // ---------------------------------------------------------------- auth

  var gate = null;

  function showLogin(message) {
    if (!document.body) {
      window.addEventListener("DOMContentLoaded", function () { showLogin(message); }, { once: true });
      return;
    }
    document.documentElement.classList.add("agy-gate");
    if (gate) {
      var note = gate.querySelector(".agy-gate-note");
      if (note && message) note.textContent = message;
      return;
    }
    gate = document.createElement("div");
    gate.id = "agy-accounts-gate";
    gate.innerHTML = [
      '<form class="agy-gate-card">',
      '  <div class="agy-gate-mark">AG</div>',
      '  <h1>Antigravity 账号面板</h1>',
      '  <p>请输入面板密码。登录后即可管理账号、切换身份与查看额度。</p>',
      '  <label>面板密码</label>',
      '  <input name="password" type="password" autocomplete="current-password" required autofocus>',
      '  <button type="submit">解锁</button>',
      '  <div class="agy-gate-note">密码错误不会泄露任何账号信息。</div>',
      '</form>'
    ].join("");
    document.body.appendChild(gate);
    var form = gate.querySelector("form");
    var input = gate.querySelector("input");
    var note = gate.querySelector(".agy-gate-note");
    if (message) note.textContent = message;
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      note.textContent = "验证中…";
      fetch("/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Agy-Accounts": "1" },
        body: JSON.stringify({ password: input.value })
      }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) throw new Error(data.error || "登录失败");
          window.location.reload();
        });
      }).catch(function (error) {
        note.textContent = error.message || "登录失败";
        input.select();
      });
    });
  }

  // ------------------------------------------------------------- toolbar

  function addToolbar() {
    if (document.getElementById("agy-tools") || window.location.pathname.indexOf("menubar") >= 0) return;
    var bar = document.createElement("div");
    bar.id = "agy-tools";
    bar.innerHTML = [
      '<button type="button" data-agy-action="save" title="把当前 Antigravity 登录保存为账号快照">保存当前登录</button>',
      '<button type="button" data-agy-action="logout" title="退出面板">退出</button>'
    ].join("");
    document.body.appendChild(bar);
    bar.addEventListener("click", function (event) {
      var target = event.target.closest("[data-agy-action]");
      if (!target) return;
      var action = target.getAttribute("data-agy-action");
      if (action === "save") void saveCurrentLogin();
      if (action === "logout") void logout();
    });
  }

  async function saveCurrentLogin() {
    try {
      var status = await api("/api/status");
      if (!status.token_present) {
        window.alert("当前没有可保存的登录。请先在 Antigravity 页面完成 Google 登录。");
        return;
      }
      var suggested = (status.token_email || "").split("@")[0] || "我的账号";
      var name = window.prompt("给当前登录起个名字", suggested);
      if (name === null) return;
      name = name.trim();
      if (!name) return;
      await api("/api/accounts", {
        method: "POST",
        body: JSON.stringify({ name: name })
      });
      cache.at = 0;
      window.alert("已保存账号「" + name + "」");
      window.location.reload();
    } catch (error) {
      window.alert(error.message || "保存失败");
    }
  }

  window.__agySaveCurrent = saveCurrentLogin;

  async function logout() {
    try {
      await api("/api/logout", { method: "POST", body: "{}" });
    } catch (e) {}
    window.location.reload();
  }

  // --------------------------------------------------------------- boot

  var style = document.createElement("style");
  style.textContent = [
    "html.agy-gate #root{display:none!important}",
    "html.agy-gate body{overflow:hidden!important}",
    "#agy-accounts-gate{position:fixed;inset:0;z-index:2147483000;display:grid;place-items:center;padding:24px;",
    "background:radial-gradient(900px 500px at 80% -10%,rgba(85,214,190,.12),transparent 60%),#0b1017;",
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#e8eef7}",
    "#agy-accounts-gate .agy-gate-card{width:100%;max-width:380px;padding:30px 26px;border-radius:20px;",
    "background:#141e2b;border:1px solid #243247;box-shadow:0 24px 60px rgba(0,0,0,.45)}",
    "#agy-accounts-gate .agy-gate-mark{width:44px;height:44px;border-radius:13px;display:grid;place-items:center;",
    "background:linear-gradient(140deg,#55d6be,#4e7cff);color:#062019;font-weight:800;margin-bottom:20px}",
    "#agy-accounts-gate h1{font-size:21px;margin:0 0 6px}",
    "#agy-accounts-gate p{color:#8fa2bb;font-size:13px;line-height:1.6;margin:0 0 20px}",
    "#agy-accounts-gate label{display:block;color:#8fa2bb;font-size:12px;margin-bottom:6px}",
    "#agy-accounts-gate input{width:100%;box-sizing:border-box;padding:12px 13px;border-radius:10px;",
    "border:1px solid #243247;background:#0c141e;color:#e8eef7;font-size:15px;outline:none}",
    "#agy-accounts-gate input:focus{border-color:#2fb8a0;box-shadow:0 0 0 3px rgba(85,214,190,.14)}",
    "#agy-accounts-gate button{width:100%;margin-top:14px;padding:12px;border:0;border-radius:10px;",
    "background:linear-gradient(180deg,#55d6be,#2fb8a0);color:#04231d;font-weight:700;font-size:14px;cursor:pointer}",
    "#agy-accounts-gate .agy-gate-note{margin-top:12px;color:#64758c;font-size:11.5px;line-height:1.5}",
    "[data-tauri-drag-region]{display:none!important}",
    "#agy-tools{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:2147482000;display:flex;gap:6px;align-items:center;",
    "padding:6px;border-radius:12px;background:rgba(20,30,43,.92);border:1px solid #243247;",
    "box-shadow:0 10px 30px rgba(0,0,0,.35);backdrop-filter:blur(8px);font-size:12px}",
    "#agy-tools button,#agy-tools a{padding:6px 10px;border-radius:8px;border:0;background:transparent;",
    "color:#8fa2bb;font:inherit;cursor:pointer;text-decoration:none;white-space:nowrap}",
    "#agy-tools button:hover,#agy-tools a:hover{background:#1c2a3d;color:#e8eef7}",
    "#agy-tools button[data-agy-action=save]{background:rgba(85,214,190,.14);color:#55d6be;font-weight:700}",
    "#agy-tools button[data-agy-action=save]:hover{background:rgba(85,214,190,.22)}",
    "@media (max-width:560px){#agy-tools{bottom:10px;font-size:11px}#agy-tools button,#agy-tools a{padding:5px 8px}}"
  ].join("");
  document.head.appendChild(style);

  window.__TAURI_EVENT_PLUGIN_INTERNALS__ = {
    unregisterListener: function () {},
    emit: function () {}
  };

  window.__TAURI_INTERNALS__ = {
    invoke: function (cmd, args) { return bridge(cmd, args); },
    transformCallback: function (callback) {
      var id = nextCallback++;
      callbacks[id] = callback;
      return id;
    },
    unregisterCallback: function (id) { delete callbacks[id]; },
    convertFileSrc: function (path) { return path; },
    metadata: {
      currentWindow: { label: "main" },
      windows: [{ label: "main" }]
    }
  };

  document.documentElement.classList.add("agy-gate");
  fetch("/api/status", { credentials: "same-origin" }).then(function (response) {
    if (response.ok) {
      document.documentElement.classList.remove("agy-gate");
    } else {
      showLogin();
    }
  }).catch(function () {
    showLogin("无法连接面板服务。");
  });
})();
