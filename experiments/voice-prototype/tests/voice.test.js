const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { WakeWordController } = require("../wake-word-controller.js");

const source = fs.readFileSync(path.join(__dirname, "..", "voice.js"), "utf8");

class Element {
  constructor(id = "") {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.checked = false;
    this.disabled = false;
    this.listeners = {};
    this.children = [];
    this.attributes = {};
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  setAttribute(name, value) { this.attributes[name] = value; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  click() { for (const fn of this.listeners.click || []) fn({ target: this }); }
  change() { for (const fn of this.listeners.change || []) fn({ target: this }); }
}

function makeHarness({ recognition = true, synthesis = true, speechStartImmediate = true, localWakeDetector = null, wakeEvent = null, fetch: fetchMock, storedSession = null, confirm = true, pendingApproval = false, localPathDiagnostics = false } = {}) {
  const ids = [
    "state", "stateDetail", "transcript", "partial", "error", "response", "recognitionEnabled", "speechEnabled", "apiBase",
    "stopButton", "startButton", "cancelRecordingButton", "history", "sessionId", "newConversationButton",
    "deleteConversationButton", "sendButton", "retryButton", "speakButton", "stopSpeakingButton", "taskOutput",
    "taskEvents", "approvalList", "executeConsent", "executeTaskButton", "prepareTaskButton", "taskIntent", "taskReply",
    "wakeWordState", "wakeWordStartButton", "wakeWordMuteButton", "wakeWordEnableButton", "wakeWordDisableButton",
    "reminderTimezone", "reminderDate", "reminderTime", "reminderTitle", "saveReminderButton", "cancelReminderEditButton", "reminderStatus", "reminderList", "reminderAgentStatus",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
  elements.apiBase.value = "http://127.0.0.1:8000";
  elements.recognitionEnabled.checked = true;
  elements.speechEnabled.checked = true;
  const local = new Map(storedSession ? [["unbound-ai.conversation-session-id", storedSession]] : []);
  const calls = [];
  const debugLogs = [];
  const instances = [];
  const timers = [];
  let utterances = [];
  const reminders = [];
  const speechQueue = [];
  let wakeStarts = 0;
  let wakeStops = 0;
  let speechCancelCount = 0;
  const recognizerClass = recognition ? class {
    constructor() { instances.push(this); }
    start() { this.onstart?.(); }
    stop() { this.onend?.(); }
    abort() { this.onend?.(); }
  } : undefined;
  class Utterance { constructor(text) { this.text = text; utterances.push(this); } }
  const speech = synthesis ? {
    getVoices: () => [{ lang: "en-US", name: "Test voice" }],
    speak(utterance) { speechQueue.push(utterance); if (speechStartImmediate) utterance.onstart?.(); },
    cancel() { speechCancelCount += 1; speechQueue.splice(0); },
  } : undefined;
  const window = {
    SpeechRecognition: recognizerClass,
    UNBOUNDWakeWordController: WakeWordController,
    UNBOUND_LOCAL_WAKE_WORD_FACTORY: localWakeDetector ? () => localWakeDetector : undefined,
    speechSynthesis: speech,
    SpeechSynthesisUtterance: synthesis ? Utterance : undefined,
    location: localPathDiagnostics ? { search: "?debug_local_path=1" } : undefined,
    addEventListener() {},
    confirm: () => confirm,
  };
  const defaultFetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (String(url).endsWith("/api/wake-word/start")) { wakeStarts += 1; return jsonResponse({ status: "listening", event_id: 0 }); }
    if (String(url).endsWith("/api/wake-word/stop")) { wakeStops += 1; return jsonResponse({ status: "disabled", event_id: wakeEvent?.event_id || 0 }); }
    if (String(url).includes("/api/wake-word/events")) return jsonResponse({ event: wakeEvent, status: { status: "listening", detail: "Listening locally" } });
    if (String(url).endsWith("/api/health")) return jsonResponse({ notification_agent_installed: false, notification_agent_running: false });
    if (String(url).endsWith("/api/reminders") && (!options.method || options.method === "GET")) return jsonResponse(reminders);
    if (String(url).endsWith("/api/reminders") && options.method === "POST") {
      const reminder = { reminder_id: "reminder-1", status: "proposed", ...JSON.parse(options.body) };
      reminders.push(reminder);
      return jsonResponse(reminder, 201);
    }
    if (String(url).endsWith("/api/reminders/reminder-1/confirm") && options.method === "POST") {
      reminders[0].status = "active";
      return jsonResponse(reminders[0]);
    }
    if (String(url).endsWith("/api/conversations") && options.method === "POST") {
      return jsonResponse({ session_id: "session-1" }, 201);
    }
    if (String(url).endsWith("/api/conversations/session-1") && (!options.method || options.method === "GET")) {
      return jsonResponse({ session_id: "session-1", messages: [] });
    }
    if (String(url).includes("/messages")) return jsonResponse({ reply: "A useful answer." });
    if (String(url).includes("/api/assistant/requests")) {
      return jsonResponse({
        intent: { category: "action_planning", current_turn_authorizes_execution: false, clarification_question: null },
        reply: "Plan ready; nothing ran.",
        task: {
          task_id: "task-1", status: "created", current_step: null, error: null, result: null,
          plan: [{ step_id: "step-1", status: "pending", tool_name: "browser.navigate", description: "Open the requested page" }],
          evidence: [], pending_approvals: pendingApproval ? [{ approval_id: "approval-1", task_id: "task-1", step_id: "step-1", tool_name: "browser.click", description: "Submit the consequential action", arguments: { target: "synthetic" }, status: "pending" }] : [],
        },
      });
    }
    if (String(url).includes("/events")) return jsonResponse([]);
    if (String(url).includes("/approvals/approval-1")) return jsonResponse({
      task_id: "task-1", status: "executing", current_step: "step-1", error: null, result: null,
      plan: [{ step_id: "step-1", status: "awaiting_approval", tool_name: "browser.click", description: "Submit the consequential action" }],
      evidence: [], pending_approvals: [],
    });
    if (String(url).endsWith("/execute")) return jsonResponse({
      task_id: "task-1", status: "completed", current_step: null, error: null, result: { verified: true },
      plan: [{ step_id: "step-1", status: "completed", tool_name: "browser.navigate", description: "Open the requested page" }],
      evidence: [], pending_approvals: [],
    });
    return jsonResponse({ detail: { message: "not found" } }, 404);
  };
  const sandbox = {
    document: { getElementById: (id) => elements[id] || (elements[id] = new Element(id)), createElement: () => new Element() },
    window,
    localStorage: { getItem: (key) => local.get(key) || null, setItem: (key, value) => local.set(key, value), removeItem: (key) => local.delete(key) },
    navigator: { language: "en-US" },
    crypto: { randomUUID: () => "request-1" },
    fetch: fetchMock || defaultFetch,
    setTimeout: (fn, delay) => { timers.push({ fn, delay, cleared: false }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].cleared = true; },
    console: { info: (...values) => debugLogs.push(values.join(" ")) },
  };
  vm.runInNewContext(source, sandbox, { filename: "voice.js" });
  return {
    elements, calls, instances, utterances, timers, local, window, speechQueue, reminders, debugLogs,
    get speechCancelCount() { return speechCancelCount; },
    get wakeStarts() { return wakeStarts; },
    get wakeStops() { return wakeStops; },
    click: (id) => elements[id].click(),
    fireTimer: async (delay) => {
      const timer = timers.find((item) => item.delay === delay && !item.cleared);
      if (!timer) throw new Error(`No active ${delay}ms timer`);
      timer.cleared = true;
      timer.fn();
      await Promise.resolve();
      await Promise.resolve();
    },
    flush: async () => {
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    },
  };
}

function jsonResponse(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}

test("unsupported recognition leaves text fallback usable and reports the issue", async () => {
  const h = makeHarness({ recognition: false });
  await h.flush();
  assert.equal(h.elements.startButton.disabled, true);
  h.elements.transcript.value = "Explain this concept";
  h.click("sendButton");
  await h.flush();
  assert.match(h.elements.response.textContent, /useful answer/, `${h.elements.state.textContent}: ${h.elements.error.textContent}; calls=${h.calls.map((call) => call.url).join(",")}`);
  assert.equal(h.calls.filter((call) => call.url.includes("/messages")).length, 1);
});

test("recognition keeps partial text separate, deduplicates final events and can cancel", async () => {
  const h = makeHarness();
  await h.flush();
  assert.match(h.elements.reminderAgentStatus.textContent, /require the UNBOUND Windows notification agent/);
  assert.match(h.elements.reminderAgentStatus.textContent, /Reminders remain saved/);
  h.elements.transcript.value = "Keep this";
  h.click("startButton");
  assert.equal(h.instances.length, 1);
  h.click("startButton");
  assert.equal(h.instances.length, 1, "a second recognition instance must not start");
  const instance = h.instances[0];
  instance.onresult({ resultIndex: 0, results: [{ isFinal: false, 0: { transcript: "partial phrase" } }] });
  assert.equal(h.elements.transcript.value, "Keep this");
  assert.equal(h.elements.partial.textContent, "partial phrase");
  const finalResult = { isFinal: true, 0: { transcript: "final phrase" } };
  instance.onresult({ resultIndex: 0, results: [finalResult] });
  instance.onresult({ resultIndex: 0, results: [finalResult] });
  assert.equal(h.elements.transcript.value, "Keep this final phrase");
  h.click("cancelRecordingButton");
  assert.equal(h.elements.transcript.value, "Keep this");
  assert.equal(h.elements.state.textContent, "IDLE");
});

test("recognition errors remain visible after the recognizer ends", async () => {
  const h = makeHarness();
  await h.flush();
  h.click("startButton");
  h.instances[0].onerror({ error: "audio-capture" });
  h.instances[0].onend();
  assert.equal(h.elements.state.textContent, "ERROR");
  assert.match(h.elements.error.textContent, /No microphone/);
});

test("empty messages are not submitted and denied microphone permission is actionable", async () => {
  const h = makeHarness();
  await h.flush();
  h.click("sendButton");
  assert.equal(h.calls.some((call) => call.url.includes("/messages")), false);
  assert.match(h.elements.error.textContent, /empty/);
  h.click("startButton");
  h.instances[0].onerror({ error: "not-allowed" });
  assert.match(h.elements.error.textContent, /permission was denied/);
  assert.equal(h.elements.state.textContent, "ERROR");
});

test("silence timeout stops recognition and explains the retry", async () => {
  const h = makeHarness();
  await h.flush();
  h.click("startButton");
  const timeout = h.timers.find((timer) => timer.delay === 30_000 && !timer.cleared);
  assert.ok(timeout, "a bounded listening timeout should be armed");
  timeout.fn();
  assert.equal(h.elements.state.textContent, "ERROR");
  assert.match(h.elements.error.textContent, /timed out/);
});

test("chat sends once, displays the response, and speech can be interrupted", async () => {
  const h = makeHarness();
  await h.flush();
  h.elements.transcript.value = "Hello";
  h.click("sendButton");
  h.click("sendButton");
  await h.flush();
  assert.equal(h.elements.response.textContent, "A useful answer.", `${h.elements.state.textContent}: ${h.elements.error.textContent}; calls=${h.calls.map((call) => call.url).join(",")}`);
  assert.equal(h.calls.filter((call) => call.url.includes("/messages")).length, 1, "a concurrent double click must not submit twice");
  assert.equal(h.elements.state.textContent, "SPEAKING");
  assert.equal(h.utterances.length, 1);
  h.click("stopSpeakingButton");
  assert.equal(h.elements.state.textContent, "IDLE");
  h.click("speakButton");
  assert.equal(h.elements.state.textContent, "SPEAKING");
  h.elements.speechEnabled.checked = false;
  h.elements.speechEnabled.change();
  assert.equal(h.elements.state.textContent, "IDLE");
  assert.equal(h.elements.speakButton.disabled, true);
});

test("speech synthesis failure keeps the text reply visible", async () => {
  const h = makeHarness();
  await h.flush();
  h.elements.transcript.value = "Hello";
  h.click("sendButton");
  await h.flush();
  h.utterances[0].onerror();
  assert.equal(h.elements.state.textContent, "ERROR");
  assert.match(h.elements.error.textContent, /still read/);
  assert.equal(h.elements.response.textContent, "A useful answer.");
});

test("stop cancels active and queued speech, resets state, and ignores stale callbacks", async () => {
  const h = makeHarness({ speechStartImmediate: false });
  await h.flush();
  h.elements.transcript.value = "Hello";
  h.click("sendButton");
  await h.flush();
  const first = h.utterances[0];
  assert.equal(h.elements.state.textContent, "SPEAKING");
  assert.equal(h.elements.stopSpeakingButton.disabled, false, "queued speech can be stopped before onstart fires");
  h.window.speechSynthesis.speak(new h.window.SpeechSynthesisUtterance("synthetic queued utterance"));
  assert.equal(h.speechQueue.length, 2);
  h.click("stopSpeakingButton");
  h.click("stopSpeakingButton");
  assert.equal(h.speechQueue.length, 0);
  assert.equal(h.elements.state.textContent, "IDLE");
  assert.equal(h.elements.response.textContent, "A useful answer.");
  first.onend?.();
  first.onerror?.();
  first.onstart?.();
  assert.equal(h.elements.state.textContent, "IDLE", "late native callbacks must not restore stale state");
  h.click("speakButton");
  assert.equal(h.utterances.length, 3, "a new utterance can start immediately after cancellation");
  assert.equal(h.speechQueue.length, 1);
  assert.equal(h.elements.state.textContent, "SPEAKING");
});

test("stopping during a pending conversation response prevents later automatic speech", async () => {
  let resolveMessage;
  const fetchMock = async (url, options = {}) => {
    if (String(url).endsWith("/api/conversations") && options.method === "POST") return jsonResponse({ session_id: "session-1" }, 201);
    if (String(url).endsWith("/api/conversations/session-1") && (!options.method || options.method === "GET")) return jsonResponse({ session_id: "session-1", messages: [] });
    if (String(url).includes("/messages")) return new Promise((resolve) => { resolveMessage = () => resolve(jsonResponse({ reply: "A delayed answer." })); });
    return jsonResponse({ detail: { message: "not found" } }, 404);
  };
  const h = makeHarness({ fetch: fetchMock });
  await h.flush();
  h.elements.transcript.value = "Tell me something";
  h.click("sendButton");
  await h.flush();
  assert.equal(h.elements.state.textContent, "PROCESSING");
  assert.equal(h.elements.stopSpeakingButton.disabled, false, "the user can cancel pending auto-speech before a reply arrives");
  h.click("stopSpeakingButton");
  assert.equal(h.elements.state.textContent, "IDLE");
  resolveMessage();
  await h.flush();
  assert.equal(h.elements.response.textContent, "A delayed answer.");
  assert.equal(h.utterances.length, 0, "late assistant output remains visible as text and is not spoken after cancellation");
  assert.equal(h.elements.state.textContent, "IDLE");
});

test("speech synthesis unavailable leaves conversation text visible and recoverable", async () => {
  const h = makeHarness({ synthesis: false });
  await h.flush();
  h.elements.transcript.value = "Hello";
  h.click("sendButton");
  await h.flush();
  assert.equal(h.elements.response.textContent, "A useful answer.");
  assert.equal(h.elements.state.textContent, "ERROR");
  assert.match(h.elements.error.textContent, /unavailable/);
});

test("reminder UI creates a proposal and requires a separate confirmation without task calls", async () => {
  const h = makeHarness();
  await h.flush();
  h.elements.reminderTitle.value = "Finish synthetic presentation";
  h.elements.reminderDate.value = "2030-02-02";
  h.click("saveReminderButton");
  await h.flush();
  assert.equal(h.reminders[0].status, "proposed");
  assert.match(h.elements.reminderStatus.textContent, /Confirm it below/);
  const confirm = h.elements.reminderList.children[0].children.find((element) => element.textContent === "Confirm reminder");
  confirm.click();
  await h.flush();
  assert.equal(h.reminders[0].status, "active");
  assert.equal(h.calls.some((call) => call.url.includes("/api/tasks")), false);
  assert.equal(h.calls.some((call) => call.url.includes("/assistant/requests")), false);
});

test("a local wake detection starts the existing speech recognizer but never submits or executes", async () => {
  const detector = {
    localOnly: true,
    onDetection: null,
    async start(onDetection) { detector.onDetection = onDetection; },
    async stop() {},
  };
  const h = makeHarness({ localWakeDetector: detector });
  await h.flush();
  assert.equal(h.elements.wakeWordStartButton.disabled, false);
  h.click("wakeWordStartButton");
  await h.flush();
  detector.onDetection({ phrase: "hey unbound", confidence: 0.95 });
  await h.flush();
  assert.equal(h.instances.length, 1, "wake phrase hands off to the existing recognition flow");
  assert.equal(h.elements.state.textContent, "LISTENING");
  assert.equal(h.calls.some((call) => call.url.includes("/messages")), false);
  assert.equal(h.calls.some((call) => call.url.includes("/api/tasks")), false);
});

test("wake controls start the local backend only on click and activate one-shot recognition from its event", async () => {
  const h = makeHarness({ wakeEvent: { event_id: 1, phrase: "hey unbound", confidence: 1 } });
  await h.flush();
  assert.equal(h.wakeStarts, 0, "page load must not open the microphone");
  h.click("wakeWordStartButton");
  await h.flush();
  assert.equal(h.wakeStarts, 1);
  assert.equal(h.instances.length, 1);
  assert.equal(h.elements.state.textContent, "LISTENING");
  assert.equal(h.calls.some((call) => call.url.endsWith("/api/tasks") || call.url.includes("/assistant/requests")), false);
  assert.equal(h.calls.some((call) => call.url.includes("/messages")), false);
  assert.ok(h.wakeStops >= 1, "the backend detector is stopped/released before browser recognition continues");
});

test("push-to-talk stops backend wake capture before browser microphone recognition", async () => {
  const h = makeHarness();
  await h.flush();
  h.click("wakeWordStartButton");
  await h.flush();
  h.click("startButton");
  await h.flush();
  const stopIndex = h.calls.findIndex((call) => call.url.endsWith("/api/wake-word/stop"));
  assert.ok(stopIndex >= 0);
  assert.equal(h.instances.length, 1, "browser recognition starts after awaited local recorder stop");
  assert.equal(h.elements.state.textContent, "LISTENING");
});

test("wake monitoring resumes after the voice reply only when the user did not mute it", async () => {
  const detector = {
    localOnly: true,
    starts: 0,
    onDetection: null,
    async start(onDetection) { this.starts += 1; this.onDetection = onDetection; },
    async stop() {},
  };
  const h = makeHarness({ localWakeDetector: detector });
  await h.flush();
  h.click("wakeWordStartButton");
  await h.flush();
  detector.onDetection({ phrase: "hey unbound", confidence: 0.99 });
  await h.flush();
  h.click("stopButton");
  h.elements.transcript.value = "Hello after wake";
  h.click("sendButton");
  await h.flush();
  h.utterances[0].onend();
  await h.flush();
  assert.equal(detector.starts, 2, "enabled wake listening resumes after playback ends");

  detector.onDetection({ phrase: "hey unbound", confidence: 0.99 });
  await h.flush();
  h.click("stopButton");
  h.click("wakeWordMuteButton");
  h.elements.transcript.value = "Reply after explicit mute";
  h.click("sendButton");
  await h.flush();
  if (!h.utterances[1]) h.click("speakButton");
  await h.flush();
  h.utterances[1].onend();
  await h.flush();
  assert.equal(detector.starts, 2, "explicit mute suppresses automatic wake rearming");
});

test("task classification is a deliberate separate action and execution needs explicit consent", async () => {
  const h = makeHarness();
  await h.flush();
  h.click("startButton");
  h.instances[0].onresult({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: "Open the site" } }] });
  h.click("stopButton");
  assert.equal(h.calls.some((call) => call.url.includes("/assistant/requests")), false);
  assert.equal(h.calls.some((call) => call.url.endsWith("/execute")), false);
  h.click("prepareTaskButton");
  await h.flush();
  assert.match(h.elements.taskOutput.textContent, /task-1/);
  assert.equal(h.calls.some((call) => call.url.endsWith("/execute")), false);
  assert.equal(h.elements.executeTaskButton.disabled, true);
  h.elements.executeConsent.checked = true;
  h.elements.executeConsent.change();
  assert.equal(h.elements.executeTaskButton.disabled, false);
  h.click("executeTaskButton");
  await h.flush();
  assert.equal(h.calls.filter((call) => call.url.endsWith("/execute")).length, 1);
  assert.match(h.elements.taskOutput.textContent, /completed/);
});

test("explicit local-file request from chat prepares the existing task without reading or executing", async () => {
  const h = makeHarness();
  await h.flush();
  const exactPath = String.raw`C:\Users\KARTHI~1\AppData\Local\Temp\unbound-file-access-test.txt`;
  const message = `Read ${exactPath} and analyse the key findings, costs, risks and recommendations.`;
  h.elements.transcript.value = message;
  h.click("sendButton");
  await h.flush();

  assert.equal(h.calls.filter((call) => call.url.includes("/api/assistant/requests")).length, 1);
  assert.equal(h.calls.some((call) => call.url.includes("/api/conversations/session-1/messages")), false);
  assert.equal(h.calls.some((call) => call.url.endsWith("/execute")), false);
  const request = h.calls.find((call) => call.url.includes("/api/assistant/requests"));
  assert.equal(JSON.parse(request.options.body).message, message);
  assert.match(h.elements.response.textContent, /Plan ready|Task prepared/);
});

test("chat handoff serializes Windows short, spaced, and nested paths without rewriting them", async () => {
  const paths = [
    String.raw`C:\Users\KARTHI~1\AppData\Local\Temp\file.txt`,
    String.raw`C:\Users\Example User\AppData\Local\Temp\file.txt`,
    String.raw`D:\Workspaces\UNBOUND AI\Project Files\Reports\2026\file.txt`,
  ];
  for (const exactPath of paths) {
    const h = makeHarness();
    await h.flush();
    const message = `Read ${exactPath} and analyse it.`;
    h.elements.transcript.value = message;
    h.click("sendButton");
    await h.flush();
    const request = h.calls.find((call) => call.url.includes("/api/assistant/requests"));
    assert.ok(request, `expected task preparation for ${exactPath}`);
    assert.equal(JSON.parse(request.options.body).message, message);
    assert.equal(h.calls.some((call) => call.url.includes("/api/conversations/session-1/messages")), false);
    assert.equal(h.calls.some((call) => call.url.endsWith("/execute")), false);
  }
});

test("opt-in browser diagnostics report path characters only at transcript and fetch stages", async () => {
  const h = makeHarness({ localPathDiagnostics: true });
  await h.flush();
  const path = String.raw`C:\Users\KARTHI~1\AppData\Local\Temp\file.txt`;
  h.elements.transcript.value = `Read ${path} and inspect private surrounding request text.`;
  h.click("sendButton");
  await h.flush();
  assert.ok(h.debugLogs.some((line) => line.includes('"stage":"browser.transcript.value"')));
  assert.ok(h.debugLogs.some((line) => line.includes('"stage":"browser.transcript_before_fetch"')));
  assert.ok(h.debugLogs.some((line) => line.includes('"stage":"browser.fetch.message"')));
  assert.ok(h.debugLogs.some((line) => line.includes('"preceded_by_backslash":false')));
  assert.ok(h.debugLogs.some((line) => line.includes('"~",126')));
  assert.ok(h.debugLogs.every((line) => !line.includes("private surrounding request text")));
});

test("normal conversation remains response-only and does not invoke the task handoff", async () => {
  const h = makeHarness();
  await h.flush();
  h.elements.transcript.value = "How should I organize a project folder?";
  h.click("sendButton");
  await h.flush();
  assert.equal(h.calls.filter((call) => call.url.includes("/api/conversations/session-1/messages")).length, 1);
  assert.equal(h.calls.some((call) => call.url.includes("/api/assistant/requests")), false);
});

test("local-file chat flow shows preparation, approval pause, and verified evidence-backed analysis", async () => {
  const exactPath = String.raw`C:\Users\KARTHI~1\AppData\Local\Temp\unbound-file-access-test.txt`;
  const taskId = "task-local-file-1";
  const task = (status, overrides = {}) => ({
    task_id: taskId, user_request: `Read ${exactPath}`, status, current_step: null, error: null, result: null,
    plan: [{
      step_id: "read_and_analyse_file", status: "pending", tool_name: "document.read_local_file",
      description: `Read ${exactPath}`, input: { file_path: exactPath, analysis_request: "the key findings, costs and risks" }, requires_approval: true, verification: null,
    }],
    evidence: [], pending_approvals: [], ...overrides,
  });
  let executionCount = 0;
  let preparedRequest;
  const fetchMock = async (url, options = {}) => {
    const href = String(url);
    if (href.endsWith("/api/conversations") && options.method === "POST") return jsonResponse({ session_id: "session-1" }, 201);
    if (href.endsWith("/api/conversations/session-1")) return jsonResponse({ session_id: "session-1", messages: [] });
    if (href.endsWith("/api/health")) return jsonResponse({ notification_agent_installed: false, notification_agent_running: false });
    if (href.endsWith("/api/reminders")) return jsonResponse([]);
    if (href.endsWith("/api/assistant/requests")) {
      preparedRequest = JSON.parse(options.body);
      return jsonResponse({
        intent: { category: "action_execution", current_turn_authorizes_execution: true, clarification_question: null },
        reply: "Task ready.",
        task: task("created"),
      });
    }
    if (href.endsWith(`/api/tasks/${taskId}/events`)) return jsonResponse([]);
    if (href.endsWith(`/api/tasks/${taskId}/execute`)) {
      executionCount += 1;
      if (executionCount === 1) {
        return jsonResponse(task("awaiting_approval", {
          pending_approvals: [{
            approval_id: "approval-local-file-1", task_id: taskId, step_id: "read_and_analyse_file",
            tool_name: "document.read_local_file", description: `Read ${exactPath}`,
            arguments: { file_path: exactPath }, status: "pending",
          }],
          plan: [{ step_id: "read_and_analyse_file", status: "awaiting_approval", tool_name: "document.read_local_file", description: "Read selected file", input: { file_path: exactPath } }],
        }));
      }
      const analysis = {
        summary: "The synthetic report lists a setup cost and a staged rollout recommendation.",
        findings: [{ kind: "fact", statement: "Setup costs are 125 USD.", source_chunk_id: "chunk-1", quote: "setup costs 125 USD", evidence_id: "evidence-1" }],
        missing_or_unreadable: ["No risk figures were present in the excerpt."],
      };
      return jsonResponse(task("completed", {
        evidence: [{ evidence_id: "evidence-1", source_name: "unbound-file-access-test.txt", supports_claim: "Quoted source excerpt" }],
        plan: [{
          step_id: "read_and_analyse_file", status: "completed", tool_name: "document.read_local_file",
          description: "Read and analyse selected file", input: { file_path: exactPath },
          verification: { verified: true, tool_result: { analysis_status: "verified_quotes", analysis } },
        }],
      }));
    }
    if (href.includes(`/approvals/approval-local-file-1`)) {
      return jsonResponse(task("executing", { plan: [{ step_id: "read_and_analyse_file", status: "pending", tool_name: "document.read_local_file", description: "Read selected file", input: { file_path: exactPath } }] }));
    }
    return jsonResponse({ detail: { message: "not found" } }, 404);
  };

  const h = makeHarness({ fetch: fetchMock });
  await h.flush();
  h.elements.transcript.value = `Read ${exactPath} and analyse the key findings, costs, risks and recommendations.`;
  h.click("sendButton");
  await h.flush();
  assert.equal(preparedRequest.message, h.elements.transcript.value);
  assert.equal(preparedRequest.session_id, "session-1");
  assert.match(h.elements.response.textContent, /Task prepared/);
  assert.match(h.elements.response.textContent, /Approval is required/);
  assert.match(h.elements.response.textContent, /key findings, costs and risks/);
  assert.match(h.elements.response.textContent, new RegExp(exactPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(h.elements.taskOutput.textContent, /created/);
  assert.ok(h.elements.taskOutput.textContent.includes(`Request: Read ${exactPath}`), "task.user_request should render literally");
  assert.ok(h.elements.taskOutput.textContent.includes(exactPath), "task request/description should render the path literally");
  assert.equal(h.elements.taskOutput.textContent.includes(String.raw`KARTHI\~1`), false);
  assert.equal(executionCount, 0, "preparation does not start a file read");
  assert.equal(h.elements.executeConsent.disabled, false);

  h.elements.executeConsent.checked = true;
  h.elements.executeConsent.change();
  h.click("executeTaskButton");
  await h.flush();
  assert.equal(executionCount, 1);
  assert.equal(h.elements.response.textContent.includes("has not been read"), true);
  assert.equal(h.elements.approvalList.children.length, 1);
  assert.equal(h.elements.approvalList.children[0].children[1].textContent, `file_path: ${exactPath}`,
    "approval path should render as literal DOM text rather than escaped JSON");
  assert.equal(h.elements.approvalList.children[0].children[1].textContent.includes(String.raw`KARTHI\~1`), false);

  h.elements.approvalList.children[0].children[2].click();
  await h.flush();
  assert.equal(executionCount, 1, "approval decision does not execute the action");
  assert.match(h.elements.response.textContent, /(?:file has not been read|No file has been read yet)/i);
  assert.equal(h.elements.executeConsent.disabled, false);

  h.elements.executeConsent.checked = true;
  h.elements.executeConsent.change();
  h.click("executeTaskButton");
  await h.flush();
  assert.equal(executionCount, 2);
  assert.match(h.elements.response.textContent, /independently verified/);
  assert.match(h.elements.response.textContent, /Setup costs are 125 USD/);
  assert.match(h.elements.response.textContent, /evidence evidence-1/);
  assert.match(h.elements.response.textContent, /No risk figures were present/);
});

test("invalid local-file path clarification is shown without entering conversation generation", async () => {
  let handoffCalls = 0;
  let conversationCalls = 0;
  const fetchMock = async (url, options = {}) => {
    const href = String(url);
    if (href.endsWith("/api/conversations") && options.method === "POST") return jsonResponse({ session_id: "session-1" }, 201);
    if (href.endsWith("/api/conversations/session-1")) return jsonResponse({ session_id: "session-1", messages: [] });
    if (href.endsWith("/api/health")) return jsonResponse({ notification_agent_installed: false, notification_agent_running: false });
    if (href.endsWith("/api/reminders")) return jsonResponse([]);
    if (href.endsWith("/api/assistant/requests")) {
      handoffCalls += 1;
      return jsonResponse({
        intent: { category: "clarification_required", current_turn_authorizes_execution: false, clarification_question: "Please provide an existing absolute Windows document path." },
        reply: "Please provide an existing absolute Windows document path.", task: null,
      });
    }
    if (href.includes("/api/conversations/session-1/messages")) conversationCalls += 1;
    return jsonResponse({ detail: { message: "not found" } }, 404);
  };
  const h = makeHarness({ fetch: fetchMock });
  await h.flush();
  const fileMessage = String.raw`Read C:\Users\Example\Temp\report.xlsx and analyse it.`;
  h.elements.transcript.value = fileMessage;
  h.click("sendButton");
  await h.flush();
  assert.equal(handoffCalls, 1);
  assert.equal(conversationCalls, 0);
  assert.match(h.elements.response.textContent, /Please provide an existing absolute Windows document path/);
  assert.equal(h.elements.taskOutput.textContent, "No task plan was created.");
});

test("failed local-file analysis shows the backend failure and never claims success", async () => {
  const filePath = "C:\\Users\\Example\\Temp\\report.txt";
  const taskId = "task-failed-file";
  const task = (status, extras = {}) => ({
    task_id: taskId, user_request: `Read ${filePath}`, status, current_step: null, result: null,
    error: null, evidence: [], pending_approvals: [],
    plan: [{ step_id: "read_file", status: "pending", tool_name: "document.read_local_file", description: "Read selected file", input: { file_path: filePath } }],
    ...extras,
  });
  const fetchMock = async (url, options = {}) => {
    const href = String(url);
    if (href.endsWith("/api/conversations") && options.method === "POST") return jsonResponse({ session_id: "session-1" }, 201);
    if (href.endsWith("/api/conversations/session-1")) return jsonResponse({ session_id: "session-1", messages: [] });
    if (href.endsWith("/api/health")) return jsonResponse({ notification_agent_installed: false, notification_agent_running: false });
    if (href.endsWith("/api/reminders")) return jsonResponse([]);
    if (href.endsWith("/api/assistant/requests")) return jsonResponse({
      intent: { category: "action_execution", current_turn_authorizes_execution: true }, reply: "Prepared.", task: task("created"),
    });
    if (href.endsWith(`/api/tasks/${taskId}/events`)) return jsonResponse([]);
    if (href.endsWith(`/api/tasks/${taskId}/execute`)) return jsonResponse(task("failed", {
      error: "The document was extracted and stored, but evidence-grounded analysis could not be verified",
      plan: [{
        step_id: "read_file", status: "failed", tool_name: "document.read_local_file", description: "Read selected file",
        input: { file_path: filePath }, error: "The document was extracted and stored, but evidence-grounded analysis could not be verified",
        verification: { verified: false, details: { failure_code: "analysis_schema_validation_failed", analysis_status: "failed" } },
      }],
    }));
    return jsonResponse({ detail: { message: "not found" } }, 404);
  };
  const h = makeHarness({ fetch: fetchMock });
  await h.flush();
  h.elements.transcript.value = `Read ${filePath} and analyse it.`;
  h.click("sendButton");
  await h.flush();
  h.elements.executeConsent.checked = true;
  h.elements.executeConsent.change();
  h.click("executeTaskButton");
  await h.flush();
  assert.match(h.elements.response.textContent, /Task failed/);
  assert.match(h.elements.response.textContent, /evidence-grounded analysis could not be verified/);
  assert.match(h.elements.response.textContent, /analysis_schema_validation_failed/);
  assert.doesNotMatch(h.elements.response.textContent, /Task completed|independently verified/);
});

test("session restoration uses the saved ID and deletion starts a fresh session", async () => {
  const fetchMock = async (url, options = {}) => {
    if (String(url).endsWith("/api/conversations/saved-session") && options.method === "DELETE") return jsonResponse(null, 204);
    if (String(url).endsWith("/api/conversations/saved-session")) return jsonResponse({ session_id: "saved-session", messages: [{ role: "user", content: "Earlier" }] });
    if (String(url).endsWith("/api/conversations") && options.method === "POST") return jsonResponse({ session_id: "fresh-session" }, 201);
    return jsonResponse({ detail: { message: "not found" } }, 404);
  };
  const h = makeHarness({ fetch: fetchMock, storedSession: "saved-session" });
  await h.flush();
  assert.equal(h.elements.sessionId.textContent, "saved-session");
  assert.match(h.elements.history.textContent, /Earlier/);
  h.window.confirm = () => true;
  h.click("deleteConversationButton");
  await h.flush();
  assert.equal(h.elements.sessionId.textContent, "fresh-session");
  assert.equal(h.local.get("unbound-ai.conversation-session-id"), "fresh-session");
});

test("approval decision remains separate from execution", async () => {
  const h = makeHarness({ pendingApproval: true });
  await h.flush();
  h.elements.transcript.value = "Submit the synthetic request";
  h.click("prepareTaskButton");
  await h.flush();
  assert.equal(h.elements.executeConsent.disabled, true);
  assert.equal(h.elements.approvalList.children.length, 1);
  h.elements.approvalList.children[0].children[2].click();
  await h.flush();
  assert.ok(h.calls.some((call) => call.url.includes("/approvals/approval-1")));
  assert.equal(h.calls.some((call) => call.url.endsWith("/execute")), false);
  assert.match(h.elements.stateDetail.textContent, /Approval decision recorded/);
});
