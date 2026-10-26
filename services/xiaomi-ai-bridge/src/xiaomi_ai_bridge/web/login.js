"use strict";

const form = document.querySelector("#panel-login-form");
const username = document.querySelector("#panel-username");
const password = document.querySelector("#panel-password");
const submit = document.querySelector("#panel-login-submit");
const message = document.querySelector("#panel-login-error");
const toggle = document.querySelector("#panel-password-toggle");
const setupStatus = document.querySelector("#panel-setup-status");
const capsLock = document.querySelector("#panel-caps-lock");
let submitting = false;
let setupRequired = false;

async function checkSetup() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 6000);
  try {
    const response = await fetch("/api/auth/session", {
      credentials: "same-origin", cache: "no-store", signal: controller.signal,
    });
    if (!response.ok) return;
    const session = await response.json();
    if (session.enabled === false || session.authenticated === true) {
      location.replace(safeReturn(new URLSearchParams(location.search).get("next") || "/"));
      return;
    }
    if (session.enabled === true && session.configured === false) {
      setupRequired = true;
      submit.disabled = true;
      setupStatus.textContent = "管理员账号尚未创建。请由服务器管理员完成账号初始化，再刷新此页。";
      setupStatus.hidden = false;
      document.querySelector(".login-help").open = true;
    }
  } catch (_) {
    // Login remains available if this advisory status check is unavailable.
  } finally {
    clearTimeout(timeout);
  }
}

password.addEventListener("keyup", (event) => {
  capsLock.hidden = !event.getModifierState?.("CapsLock");
});
password.addEventListener("blur", () => { capsLock.hidden = true; });

function safeReturn(value) {
  return value === "/" || /^\/#[a-zA-Z0-9_-]+$/.test(value || "") ? value : "/";
}

toggle.addEventListener("click", () => {
  const visible = password.type === "password";
  password.type = visible ? "text" : "password";
  toggle.textContent = visible ? "隐藏" : "显示";
  toggle.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
  toggle.setAttribute("aria-pressed", String(visible));
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submitting || setupRequired) return;
  submitting = true;
  submit.disabled = true;
  submit.textContent = "正在登录…";
  form.setAttribute("aria-busy", "true");
  message.hidden = true;
  username.removeAttribute("aria-invalid");
  password.removeAttribute("aria-invalid");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const next = safeReturn(new URLSearchParams(location.search).get("next") || "/" + location.hash);
    const response = await fetch("/api/auth/login", {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value.trim(), password: password.value, next }),
      signal: controller.signal,
    });
    const result = await response.json().catch(() => ({error: "服务暂时不可用，请稍后重试"}));
    if (!response.ok || !result.ok) {
      if (response.status === 429) {
        const seconds = Math.min(600, Math.max(1, Number(result.retry_after || response.headers.get("Retry-After")) || 60));
        throw new Error(`登录尝试过多，请在 ${seconds} 秒后重试`);
      }
      if (response.status === 401) {
        username.setAttribute("aria-invalid", "true");
        password.setAttribute("aria-invalid", "true");
      }
      throw new Error(result.error || "暂时无法登录，请稍后重试");
    }
    password.value = "";
    location.replace(safeReturn(result.next));
  } catch (error) {
    message.textContent = error.name === "AbortError" ? "登录请求超时，请稍后重试" : error instanceof TypeError ? "无法连接到中枢，请检查网络后重试" : error.message;
    message.hidden = false;
  } finally {
    clearTimeout(timeout);
    submitting = false;
    submit.disabled = setupRequired;
    submit.textContent = "登录 →";
    form.removeAttribute("aria-busy");
  }
});

void checkSetup();
