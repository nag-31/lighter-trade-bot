(function () {
  "use strict";

  var form = document.getElementById("login-form");
  var password = document.getElementById("password");
  var submit = document.getElementById("login-submit");
  var error = document.getElementById("login-error");
  var toggle = document.getElementById("password-toggle");

  fetch("/api/auth/status", { cache: "no-store" }).then(function (response) {
    return response.json();
  }).then(function (body) {
    if (body.authenticated) window.location.replace("/");
  }).catch(function () {});

  toggle.addEventListener("click", function () {
    var visible = password.type === "text";
    password.type = visible ? "password" : "text";
    toggle.setAttribute("aria-label", visible ? "Show password" : "Hide password");
    toggle.setAttribute("title", visible ? "Show password" : "Hide password");
    password.focus();
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    error.textContent = "";
    if (!password.value) {
      error.textContent = "Enter your password.";
      password.focus();
      return;
    }
    submit.disabled = true;
    submit.textContent = "Signing in...";
    fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password.value })
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) throw new Error(body.error || "Sign in failed");
        window.location.replace("/");
      });
    }).catch(function (failure) {
      error.textContent = failure.message;
      password.select();
      submit.disabled = false;
      submit.textContent = "Sign in";
    });
  });
})();
