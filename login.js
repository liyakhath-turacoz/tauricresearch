"use strict";
document.getElementById("loginForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = document.getElementById("loginSubmit");
  const error = document.getElementById("loginError");
  button.disabled = true;
  error.textContent = "";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Local-UI-Token": document.querySelector('meta[name="local-ui-token"]').content},
      body: JSON.stringify({email: document.getElementById("loginEmail").value.trim(), password: document.getElementById("loginPassword").value})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Sign in failed.");
    location.replace("/");
  } catch (problem) {
    error.textContent = problem.message;
  } finally {
    button.disabled = false;
  }
});
