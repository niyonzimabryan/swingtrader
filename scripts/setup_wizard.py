"""
Local setup wizard for Swing Trader.

Run:
    python -m scripts.setup_wizard
Then open:
    http://localhost:8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import httpx
from aiohttp import web

from config.onboarding import (
    FIELD_BY_NAME,
    ENV_GROUPS,
    completion_counts,
    merged_env_values,
    public_schema,
    read_env_file,
    write_env_file,
)
from scripts.doctor import run_doctor, summarize_results


class WizardState:
    def __init__(self, env_path: str | Path):
        self.env_path = Path(env_path)

    def values(self) -> dict[str, str]:
        return merged_env_values(self.env_path)

    def public_state(self) -> dict:
        values = self.values()
        return {
            "env_path": str(self.env_path.resolve()),
            "schema": public_schema(values),
            "groups": ENV_GROUPS,
            "field_values": {
                name: value
                for name, value in values.items()
                if name in FIELD_BY_NAME and not FIELD_BY_NAME[name].secret
            },
            "secret_placeholders": {
                name: FIELD_BY_NAME[name].to_public_dict(values.get(name, "")).get("masked_value", "")
                for name in FIELD_BY_NAME
                if FIELD_BY_NAME[name].secret
            },
        }


def create_app(env_path: str | Path = ".env") -> web.Application:
    state = WizardState(env_path)
    app = web.Application()
    app["wizard_state"] = state
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_post("/api/save", handle_save)
    app.router.add_post("/api/doctor", handle_doctor)
    app.router.add_post("/api/telegram/discover", handle_telegram_discover)
    app.router.add_post("/api/telegram/test", handle_telegram_test)
    return app


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=HTML, content_type="text/html")


async def handle_state(request: web.Request) -> web.Response:
    state: WizardState = request.app["wizard_state"]
    return web.json_response(state.public_state())


async def handle_save(request: web.Request) -> web.Response:
    state: WizardState = request.app["wizard_state"]
    payload = await request.json()
    submitted = payload.get("values", {})
    current = read_env_file(state.env_path)
    values = current.copy()

    for name, field in FIELD_BY_NAME.items():
        incoming = submitted.get(name)
        if incoming is None:
            continue
        incoming = str(incoming).strip()
        if not incoming and current.get(name):
            continue
        if not incoming and field.default:
            values[name] = field.default
        else:
            values[name] = incoming

    write_env_file(values, state.env_path)
    return web.json_response({"ok": True, "state": state.public_state()})


async def handle_doctor(request: web.Request) -> web.Response:
    state: WizardState = request.app["wizard_state"]
    payload = await request.json()
    submitted = {
        key: str(value).strip()
        for key, value in payload.get("values", {}).items()
        if key in FIELD_BY_NAME and str(value).strip()
    }
    skip_live = bool(payload.get("skip_live", False))
    results = await run_doctor(state.env_path, skip_live=skip_live, provided_values=submitted)
    merged = state.values()
    merged.update(submitted)
    return web.json_response(
        {
            "summary": summarize_results(results),
            "completion": completion_counts(merged),
            "results": [asdict(result) for result in results],
        }
    )


async def handle_telegram_discover(request: web.Request) -> web.Response:
    state: WizardState = request.app["wizard_state"]
    payload = await request.json()
    token = str(payload.get("token") or state.values().get("TELEGRAM_BOT_TOKEN", "")).strip()
    if not token:
        return web.json_response({"ok": False, "error": "Paste TELEGRAM_BOT_TOKEN first."}, status=400)

    async with httpx.AsyncClient(timeout=10) as client:
        bot_response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        bot_payload = bot_response.json()
        if not bot_payload.get("ok"):
            return web.json_response(
                {"ok": False, "error": "Telegram rejected this bot token.", "payload": bot_payload},
                status=400,
            )
        bot = bot_payload.get("result", {})

        chats = []
        seen = set()
        for _ in range(8):
            response = await client.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"timeout": 2, "allowed_updates": json.dumps(["message", "channel_post"])},
            )
            payload = response.json()
            if payload.get("ok"):
                for update in payload.get("result", []):
                    message = update.get("message") or update.get("channel_post") or {}
                    chat = message.get("chat") or {}
                    chat_id = chat.get("id")
                    if chat_id is None or chat_id in seen:
                        continue
                    seen.add(chat_id)
                    chats.append(
                        {
                            "id": str(chat_id),
                            "title": chat.get("title") or chat.get("username") or chat.get("first_name") or "Telegram chat",
                            "type": chat.get("type", "chat"),
                        }
                    )
            if chats:
                break
            await asyncio.sleep(1)

    return web.json_response(
        {
            "ok": True,
            "bot": {"username": bot.get("username", ""), "first_name": bot.get("first_name", "")},
            "chats": chats,
        }
    )


async def handle_telegram_test(request: web.Request) -> web.Response:
    state: WizardState = request.app["wizard_state"]
    payload = await request.json()
    values = state.values()
    token = str(payload.get("token") or values.get("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = str(payload.get("chat_id") or values.get("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        return web.json_response({"ok": False, "error": "Telegram token and chat ID are required."}, status=400)

    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "Swing Trader setup test passed. Next step: run /test AAPL after startup.",
            },
        )
    payload = response.json()
    if not payload.get("ok"):
        return web.json_response({"ok": False, "error": "Telegram test message failed.", "payload": payload}, status=400)
    return web.json_response({"ok": True, "message_id": payload.get("result", {}).get("message_id")})


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the local Swing Trader setup wizard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()

    app = create_app(args.env)
    print(f"Swing Trader setup wizard running at http://{args.host}:{args.port}")
    print("Secrets stay local and are written to .env only.")
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Swing Trader Setup</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0e10;
      --panel: #11171a;
      --panel-2: #151d21;
      --line: #2a363c;
      --muted: #8fa0a7;
      --text: #edf5f2;
      --green: #38e28a;
      --cyan: #4fc3ff;
      --amber: #ffbd4a;
      --coral: #ff6b5f;
      --ink: #07100d;
      --shadow: 0 24px 80px rgba(0, 0, 0, .38);
    }

    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    html { min-height: 100%; background: var(--bg); }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      letter-spacing: 0;
      background:
        linear-gradient(135deg, rgba(79, 195, 255, .08), transparent 28%),
        linear-gradient(225deg, rgba(56, 226, 138, .07), transparent 32%),
        radial-gradient(circle at 50% 0%, rgba(255, 189, 74, .08), transparent 28%),
        var(--bg);
    }

    button, input { font: inherit; letter-spacing: 0; }
    a { color: inherit; }

    .shell {
      display: grid;
      grid-template-columns: 286px minmax(0, 1fr) 330px;
      min-height: 100vh;
    }

    .rail {
      border-right: 1px solid var(--line);
      padding: 22px 18px;
      background: rgba(11, 14, 16, .78);
      position: sticky;
      top: 0;
      height: 100vh;
      overflow: auto;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 24px;
    }

    .mark {
      width: 42px;
      height: 42px;
      border: 1px solid #35505a;
      display: grid;
      place-items: center;
      background:
        linear-gradient(135deg, rgba(56, 226, 138, .20), transparent),
        #0f1719;
      border-radius: 8px;
      box-shadow: inset 0 0 22px rgba(79, 195, 255, .12);
      font-family: Georgia, serif;
      font-weight: 700;
      color: var(--green);
    }

    .brand h1 {
      margin: 0;
      font-size: 17px;
      line-height: 1.1;
      font-weight: 750;
    }

    .brand p {
      margin: 3px 0 0;
      color: var(--muted);
      font-size: 12px;
    }

    .step {
      width: 100%;
      border: 1px solid transparent;
      background: transparent;
      color: var(--muted);
      display: grid;
      grid-template-columns: 28px 1fr auto;
      align-items: center;
      gap: 10px;
      padding: 10px 9px;
      border-radius: 8px;
      text-align: left;
      cursor: pointer;
      margin: 3px 0;
    }

    .step:hover { background: rgba(255, 255, 255, .04); color: var(--text); }
    .step.active {
      color: var(--text);
      border-color: #2f4f59;
      background: linear-gradient(90deg, rgba(79, 195, 255, .12), rgba(56, 226, 138, .06));
    }

    .step-index {
      width: 28px;
      height: 28px;
      border-radius: 6px;
      display: grid;
      place-items: center;
      background: #10191d;
      border: 1px solid #263941;
      font-size: 12px;
      color: var(--cyan);
    }

    .step strong {
      display: block;
      font-size: 13px;
      font-weight: 700;
    }

    .step > span:nth-child(2) {
      min-width: 0;
    }

    .step small {
      display: block;
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .pill {
      font-size: 10px;
      color: var(--ink);
      background: var(--green);
      border-radius: 5px;
      padding: 3px 5px;
      font-weight: 800;
      text-transform: uppercase;
    }

    .pill.optional { color: #0f1416; background: var(--amber); }

    .main {
      padding: 26px;
      min-width: 0;
    }

    .topbar {
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      margin-bottom: 22px;
    }

    .headline h2 {
      margin: 0;
      font-size: 31px;
      font-weight: 760;
      line-height: 1;
    }

    .headline p {
      margin: 9px 0 0;
      color: var(--muted);
      max-width: 740px;
      line-height: 1.45;
      font-size: 14px;
    }

    .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .button {
      height: 38px;
      border: 1px solid #34515b;
      color: var(--text);
      background: #10191d;
      border-radius: 7px;
      padding: 0 13px;
      cursor: pointer;
      display: inline-flex;
      gap: 8px;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 13px;
      box-shadow: none;
      text-decoration: none;
    }

    .button:hover { border-color: #527784; background: #142127; }
    .button.primary {
      color: #07100d;
      background: linear-gradient(135deg, var(--green), #b6f16b);
      border-color: transparent;
    }

    .button.warn {
      color: #140d07;
      background: linear-gradient(135deg, var(--amber), #ffdd75);
      border-color: transparent;
    }

    .board {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 14px;
    }

    .panel {
      border: 1px solid var(--line);
      background:
        linear-gradient(180deg, rgba(255, 255, 255, .035), transparent),
        rgba(17, 23, 26, .88);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .panel-head {
      min-height: 58px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 16px;
    }

    .panel-head strong {
      font-size: 15px;
    }

    .panel-head span {
      color: var(--muted);
      font-size: 12px;
    }

    .field-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0;
    }

    .field {
      padding: 16px;
      border-right: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      min-height: 148px;
      background: rgba(8, 12, 13, .16);
    }

    .field:nth-child(2n) { border-right: 0; }

    .field-top {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 9px;
    }

    label {
      display: block;
      font-weight: 760;
      font-size: 13px;
    }

    .status {
      font-size: 10px;
      text-transform: uppercase;
      color: #08100d;
      background: var(--green);
      border-radius: 4px;
      padding: 3px 5px;
      height: 19px;
      font-weight: 800;
      white-space: nowrap;
    }

    .status.missing { color: #1b0c08; background: var(--coral); }
    .status.optional { color: #130f07; background: var(--amber); }

    .field p {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.42;
      margin: 0 0 12px;
      min-height: 34px;
    }

    .input-row {
      display: flex;
      gap: 8px;
    }

    input {
      min-width: 0;
      flex: 1;
      height: 38px;
      border: 1px solid #2e4148;
      background: #090e10;
      color: var(--text);
      border-radius: 6px;
      padding: 0 11px;
      outline: none;
    }

    input:focus {
      border-color: var(--cyan);
      box-shadow: 0 0 0 3px rgba(79, 195, 255, .10);
    }

    .launch {
      width: 38px;
      min-width: 38px;
      padding: 0;
      font-weight: 900;
    }

    .telegram-tools {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      padding: 16px;
      border-top: 1px solid var(--line);
      background: rgba(79, 195, 255, .035);
    }

    .side {
      border-left: 1px solid var(--line);
      background: rgba(8, 11, 12, .72);
      padding: 22px 18px;
      height: 100vh;
      position: sticky;
      top: 0;
      overflow: auto;
    }

    .radar {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 14px;
      box-shadow: var(--shadow);
    }

    .ring {
      --pct: 0deg;
      width: 176px;
      aspect-ratio: 1;
      margin: 8px auto 14px;
      border-radius: 50%;
      background:
        radial-gradient(circle at center, #11171a 0 54%, transparent 55%),
        conic-gradient(var(--green) var(--pct), #263238 0);
      position: relative;
      display: grid;
      place-items: center;
    }

    .ring::before {
      content: "";
      position: absolute;
      width: 92px;
      height: 92px;
      border-radius: 50%;
      border: 1px solid rgba(79, 195, 255, .28);
      box-shadow: 0 0 42px rgba(56, 226, 138, .10);
    }

    .ring span {
      position: relative;
      z-index: 1;
      font-size: 30px;
      font-weight: 800;
    }

    .radar h3, .checks h3 {
      margin: 0 0 9px;
      font-size: 14px;
    }

    .metric {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      padding: 7px 0;
      border-top: 1px solid rgba(255, 255, 255, .06);
    }

    .metric strong { color: var(--text); }

    .checks {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 14px;
    }

    .check-list {
      display: grid;
      gap: 7px;
      margin-top: 12px;
      max-height: 46vh;
      overflow: auto;
    }

    .check {
      border: 1px solid #263941;
      border-radius: 7px;
      padding: 9px;
      background: #0b1113;
      font-size: 12px;
    }

    .check strong {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      font-size: 12px;
      margin-bottom: 4px;
    }

    .check p { margin: 0; color: var(--muted); line-height: 1.35; }
    .check.pass { border-color: rgba(56, 226, 138, .42); }
    .check.fail { border-color: rgba(255, 107, 95, .54); }
    .check.warn { border-color: rgba(255, 189, 74, .48); }
    .check.info { border-color: rgba(79, 195, 255, .38); }

    .toast {
      position: fixed;
      left: 50%;
      bottom: 18px;
      transform: translateX(-50%);
      background: #eaf8ef;
      color: #07100d;
      border-radius: 7px;
      padding: 10px 13px;
      font-size: 13px;
      font-weight: 750;
      box-shadow: var(--shadow);
      opacity: 0;
      pointer-events: none;
      transition: opacity .2s ease, transform .2s ease;
    }

    .toast.show {
      opacity: 1;
      transform: translateX(-50%) translateY(-4px);
    }

    @media (max-width: 1120px) {
      .shell { grid-template-columns: 230px minmax(0, 1fr); }
      .side { grid-column: 1 / -1; height: auto; position: static; border-left: 0; border-top: 1px solid var(--line); }
      .radar { display: grid; grid-template-columns: 220px 1fr; gap: 18px; align-items: center; }
      .ring { margin: 0 auto; }
    }

    @media (max-width: 820px) {
      .shell { display: block; }
      .rail, .side { height: auto; position: static; }
      .rail { overflow: visible; }
      .step { grid-template-columns: 28px minmax(0, 1fr) max-content; }
      .step small { display: none; }
      .field-grid { grid-template-columns: 1fr; }
      .field { border-right: 0; }
      .topbar { display: block; }
      .actions { justify-content: flex-start; margin-top: 14px; }
      .radar { display: block; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside class="rail">
      <div class="brand">
        <div class="mark">ST</div>
        <div>
          <h1>Swing Trader</h1>
          <p>local setup cockpit</p>
        </div>
      </div>
      <nav id="steps"></nav>
    </aside>

    <main class="main">
      <div class="topbar">
        <div class="headline">
          <h2 id="groupTitle">Setup</h2>
          <p id="groupSummary">Loading configuration.</p>
        </div>
        <div class="actions">
          <button class="button" id="saveBtn">Save .env</button>
          <button class="button warn" id="validateLocalBtn">Presence Check</button>
          <button class="button primary" id="validateLiveBtn">Full Check</button>
        </div>
      </div>

      <section class="board">
        <div class="panel">
          <div class="panel-head">
            <div>
              <strong id="panelTitle">Provider keys</strong><br>
              <span id="panelMeta">Secrets stay in your local .env file.</span>
            </div>
            <span id="envPath"></span>
          </div>
          <div class="field-grid" id="fields"></div>
          <div class="telegram-tools" id="telegramTools" hidden>
            <a class="button" href="https://t.me/BotFather" target="_blank" rel="noreferrer">Open BotFather</a>
            <button class="button" id="discoverTelegramBtn">Discover chat ID</button>
            <button class="button primary" id="testTelegramBtn">Send test message</button>
          </div>
        </div>
      </section>
    </main>

    <aside class="side">
      <section class="radar">
        <div class="ring" id="ring"><span id="ringText">0%</span></div>
        <div>
          <h3>Readiness</h3>
          <div class="metric"><span>Required keys</span><strong id="requiredMetric">0/0</strong></div>
          <div class="metric"><span>Gemini search</span><strong id="geminiMetric">Skipped</strong></div>
          <div class="metric"><span>Last check</span><strong id="lastCheckMetric">Not run</strong></div>
          <div class="metric"><span>Next command</span><strong>python main.py</strong></div>
        </div>
      </section>

      <section class="checks">
        <h3>Validation Feed</h3>
        <div class="check-list" id="checks">
          <div class="check info">
            <strong><span>Ready</span><span>INFO</span></strong>
            <p>Fill each required step, save .env, then run a full check.</p>
          </div>
        </div>
      </section>
    </aside>
  </div>
  <div class="toast" id="toast"></div>

  <script>
    const app = {
      groups: [],
      fields: [],
      values: {},
      placeholders: {},
      activeGroup: "core",
      checks: [],
    };

    const $ = (id) => document.getElementById(id);

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Request failed");
      return data;
    }

    function fieldValue(name) {
      const input = document.querySelector(`[data-field="${name}"]`);
      if (input) return input.value.trim();
      return app.values[name] || "";
    }

    function collectValues() {
      const values = { ...app.values };
      document.querySelectorAll("[data-field]").forEach((input) => {
        values[input.dataset.field] = input.value.trim();
      });
      return values;
    }

    async function loadState() {
      const data = await api("/api/state");
      app.groups = data.groups;
      app.fields = data.schema.fields;
      app.values = data.field_values || {};
      app.placeholders = data.secret_placeholders || {};
      $("envPath").textContent = data.env_path;
      render();
    }

    function render() {
      renderSteps();
      renderFields();
      renderRadar();
      renderChecks();
    }

    function renderSteps() {
      $("steps").innerHTML = app.groups.map((group, index) => {
        const fields = app.fields.filter((field) => field.group === group.id);
        const required = fields.some((field) => field.required);
        const complete = fields.filter((field) => field.required && field.configured).length;
        const total = fields.filter((field) => field.required).length;
        const badge = required ? `${complete}/${total}` : "add-on";
        return `
          <button class="step ${group.id === app.activeGroup ? "active" : ""}" data-step="${group.id}">
            <span class="step-index">${String(index + 1).padStart(2, "0")}</span>
            <span><strong>${group.label}</strong><small>${group.summary}</small></span>
            <span class="pill ${required ? "" : "optional"}">${badge}</span>
          </button>
        `;
      }).join("");
      document.querySelectorAll("[data-step]").forEach((btn) => {
        btn.addEventListener("click", () => {
          app.activeGroup = btn.dataset.step;
          render();
        });
      });
    }

    function renderFields() {
      const group = app.groups.find((item) => item.id === app.activeGroup) || app.groups[0];
      const fields = app.fields.filter((field) => field.group === group.id);
      $("groupTitle").textContent = group.label;
      $("groupSummary").textContent = group.summary;
      $("panelTitle").textContent = group.id === "gemini" ? "Optional Gemini add-on" : "Required setup inputs";
      $("telegramTools").hidden = group.id !== "telegram";

      $("fields").innerHTML = fields.map((field) => {
        const configured = field.configured;
        const status = configured ? "Configured" : (field.required ? "Missing" : "Optional");
        const statusClass = configured ? "" : (field.required ? "missing" : "optional");
        const type = field.secret ? "password" : "text";
        const existing = field.secret ? "" : (app.values[field.name] || "");
        const placeholder = field.secret && app.placeholders[field.name]
          ? `${app.placeholders[field.name]} saved`
          : (field.placeholder || field.default || "");
        const link = field.signup_url
          ? `<a class="button launch" href="${field.signup_url}" target="_blank" rel="noreferrer" title="Open provider">Go</a>`
          : "";
        return `
          <div class="field">
            <div class="field-top">
              <label for="${field.name}">${field.label}</label>
              <span class="status ${statusClass}">${status}</span>
            </div>
            <p>${field.description}</p>
            <div class="input-row">
              <input id="${field.name}" data-field="${field.name}" type="${type}" placeholder="${placeholder}" value="${escapeAttr(existing)}" autocomplete="off" spellcheck="false">
              ${link}
            </div>
          </div>
        `;
      }).join("");
    }

    function renderRadar() {
      const required = app.fields.filter((field) => field.required);
      const complete = required.filter((field) => field.configured).length;
      const pct = required.length ? Math.round((complete / required.length) * 100) : 0;
      $("ring").style.setProperty("--pct", `${pct * 3.6}deg`);
      $("ringText").textContent = `${pct}%`;
      $("requiredMetric").textContent = `${complete}/${required.length}`;
      const gemini = app.fields.find((field) => field.name === "GEMINI_API_KEY");
      $("geminiMetric").textContent = gemini && gemini.configured ? "Enabled" : "Skipped";
    }

    function renderChecks() {
      if (!app.checks.length) return;
      $("checks").innerHTML = app.checks.slice(0, 36).map((check) => `
        <div class="check ${check.status}">
          <strong><span>${check.name}</span><span>${check.status.toUpperCase()}</span></strong>
          <p>${check.message}${check.detail ? "<br>" + escapeHtml(check.detail) : ""}</p>
        </div>
      `).join("");
    }

    async function saveEnv() {
      const data = await api("/api/save", {
        method: "POST",
        body: JSON.stringify({ values: collectValues() }),
      });
      toast("Saved .env");
      await loadState();
      return data;
    }

    async function runCheck(skipLive) {
      const data = await api("/api/doctor", {
        method: "POST",
        body: JSON.stringify({ values: collectValues(), skip_live: skipLive }),
      });
      app.checks = data.results;
      const summary = data.summary;
      $("lastCheckMetric").textContent = `${summary.fail} fail / ${summary.warn} warn`;
      renderChecks();
      toast(skipLive ? "Presence check complete" : "Full check complete");
    }

    async function discoverTelegram() {
      const data = await api("/api/telegram/discover", {
        method: "POST",
        body: JSON.stringify({ token: fieldValue("TELEGRAM_BOT_TOKEN") }),
      });
      if (data.chats && data.chats.length) {
        const chat = data.chats[0];
        const input = document.querySelector('[data-field="TELEGRAM_CHAT_ID"]');
        if (input) input.value = chat.id;
        toast(`Found chat: ${chat.title}`);
      } else {
        const username = data.bot && data.bot.username ? `@${data.bot.username}` : "your bot";
        toast(`Message ${username}, then click Discover again`);
      }
    }

    async function testTelegram() {
      await api("/api/telegram/test", {
        method: "POST",
        body: JSON.stringify({
          token: fieldValue("TELEGRAM_BOT_TOKEN"),
          chat_id: fieldValue("TELEGRAM_CHAT_ID"),
        }),
      });
      toast("Telegram test sent");
    }

    function toast(message) {
      const el = $("toast");
      el.textContent = message;
      el.classList.add("show");
      setTimeout(() => el.classList.remove("show"), 2200);
    }

    function escapeAttr(value) {
      return String(value || "").replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
    }

    function escapeHtml(value) {
      return String(value || "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
    }

    $("saveBtn").addEventListener("click", () => saveEnv().catch((err) => toast(err.message)));
    $("validateLocalBtn").addEventListener("click", () => runCheck(true).catch((err) => toast(err.message)));
    $("validateLiveBtn").addEventListener("click", () => runCheck(false).catch((err) => toast(err.message)));
    $("discoverTelegramBtn").addEventListener("click", () => discoverTelegram().catch((err) => toast(err.message)));
    $("testTelegramBtn").addEventListener("click", () => testTelegram().catch((err) => toast(err.message)));

    loadState().catch((err) => toast(err.message));
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
