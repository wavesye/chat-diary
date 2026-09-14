// Run with: node tests/test_web_drafts.js (no browser or packages required).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function element() {
  const classes = new Set();
  return {
    value: "", style: {}, dataset: {}, children: [], attributes: {}, hidden: false,
    textContent: "", disabled: false, scrollHeight: 100,
    classList: {
      add: key => classes.add(key), remove: key => classes.delete(key),
      toggle(key, on) { if (on ?? !classes.has(key)) classes.add(key); else classes.delete(key); },
    },
    set innerHTML(value) { this.html = value; this.children = []; },
    get innerHTML() { return this.html ?? this.textContent.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); },
    setAttribute(key, value) { this.attributes[key] = value; },
    append(child) { this.children.push(child); }, focus() {},
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
  };
}

async function fixture(server = {}) {
  Object.assign(server, {
    markdown: server.markdown ?? "# 今天\n\n- [x] 完成报告\n- [ ] 买牛奶\n\n最后一段。\n",
    edited: server.edited ?? false, stale: server.stale ?? false,
    messages: server.messages ?? [],
  });
  const nodes = new Map(), calls = [], events = {};
  const get = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const context = vm.createContext({
    document: { getElementById: get, createElement: element, querySelector: get,
      querySelectorAll: () => [], documentElement: element() },
    localStorage: { getItem() {}, setItem() {} }, window: { addEventListener(name, fn) { events[name] = fn; } },
    requestAnimationFrame: fn => fn(), setTimeout: () => 0, clearTimeout() {},
    innerWidth: 1200, confirm: () => true,
    fetch: async (path, options = {}) => {
      const method = options.method || "GET";
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ path, method, body });
      let data;
      if (path === "/v1/session") data = { day: "2026-09-14", greeting: "今天好吗", messages: server.messages };
      else if (path === "/v1/chat") { server.stale = true; data = { reply: "原话已保存" }; }
      else if (path === "/v1/finalize") data = { day: "2026-09-14" };
      else if (path.startsWith("/v1/preview")) {
        if (method === "PUT") {
          if (server.failSave) return { ok: false, status: 409, json: async () => ({ detail: "保存失败" }) };
          server.markdown = body.markdown; server.edited = true;
        }
        if (path.includes("refresh=true")) { server.markdown = "# 重新生成\n"; server.edited = false; server.stale = false; }
        data = { day: "2026-09-14", markdown: server.markdown, edited: server.edited, stale: server.stale };
      } else throw new Error(`Unexpected request ${method} ${path}`);
      return { ok: true, json: async () => data };
    },
  });
  vm.runInContext(fs.readFileSync("web/app.js", "utf8") +
    "\nglobalThis.ui={state,preview,finish,send,closePreview,draftDirty,markdown};", context);
  await new Promise(resolve => setImmediate(resolve));
  return { ui: context.ui, get, calls, events, context, server };
}

(async () => {
  const app = await fixture();
  await app.ui.preview();
  assert.match(app.get("paper").innerHTML, /☑ 完成报告/);
  assert.match(app.ui.markdown("> 原话 <tag>"), /<blockquote>原话 &lt;tag&gt;<\/blockquote>/);
  app.get("editDraft").onclick();
  assert.equal(app.get("draftEditor").selectionStart, 0, "first edit should open at the title");
  const exact = "  # 手动标题\n\n  原话保留空格。  \n\n";
  app.get("draftEditor").value = exact;
  app.get("draftEditor").oninput();
  app.ui.closePreview();
  const beforeReopen = app.calls.length;
  await app.ui.preview();
  assert.equal(app.calls.length, beforeReopen, "reopening must retain an unsaved edit");
  assert.equal(app.get("draftEditor").value, exact);
  let prevented = false;
  app.events.beforeunload({ preventDefault() { prevented = true; } });
  assert.ok(prevented, "unsaved edits require an unload warning");
  await app.ui.finish();
  const finalCalls = app.calls.slice(-2);
  assert.equal(finalCalls[0].method, "PUT");
  assert.equal(finalCalls[0].body.markdown, exact, "save must preserve whitespace exactly");
  assert.equal(finalCalls[1].path, "/v1/finalize", "finalize must follow successful save");
  assert.ok(!app.ui.draftDirty());

  const reloaded = await fixture(app.server);
  await reloaded.ui.preview();
  assert.equal(reloaded.get("draftEditor").value, exact, "saved draft survives page reload");
  const original = " /quote  第一行  \n 第二行 \n";
  reloaded.get("messageInput").value = original;
  await reloaded.ui.send();
  assert.equal(reloaded.calls.find(call => call.path === "/v1/chat").body.message, original);
  assert.equal(reloaded.ui.state.messages.at(-1).content, "原话已保存", "command replies must be visible without stored chat messages");
  assert.ok(reloaded.ui.state.draft.stale);
  assert.equal(reloaded.ui.state.draft.markdown, exact, "new records must not replace the draft");
  reloaded.context.confirm = () => false;
  await reloaded.ui.preview(true);
  assert.ok(!reloaded.calls.some(call => call.path.includes("refresh=true")));
  reloaded.context.confirm = () => true;
  await reloaded.ui.preview(true);
  assert.equal(reloaded.ui.state.draft.markdown, "# 重新生成\n");

  const failed = await fixture({ failSave: true });
  await failed.ui.preview();
  failed.get("draftEditor").value = "# 必须保存\n";
  failed.get("draftEditor").oninput();
  await failed.ui.finish();
  assert.ok(!failed.calls.some(call => call.path === "/v1/finalize"), "failed save must block finalization");
  assert.ok(failed.ui.draftDirty());
  assert.equal(failed.ui.state.busy, false);

  const onlyCommands = await fixture();
  await onlyCommands.ui.finish();
  assert.ok(onlyCommands.calls.some(call => call.path === "/v1/finalize"), "tasks or quotes can be finalized without user chat records");
  onlyCommands.ui.state.busy = true;
  const beforeBusy = onlyCommands.calls.length;
  await onlyCommands.ui.finish(); await onlyCommands.ui.preview(); await onlyCommands.ui.send();
  assert.equal(onlyCommands.calls.length, beforeBusy, "busy UI must reject concurrent operations");
  console.log("Web draft interactions passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
