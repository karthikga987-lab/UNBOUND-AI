(() => {
  const $ = (id) => document.getElementById(id);
  const state = $("state");
  const stateDetail = $("stateDetail");
  const transcript = $("transcript");
  const partial = $("partial");
  const error = $("error");
  const response = $("response");
  const recognitionEnabled = $("recognitionEnabled");
  const speechEnabled = $("speechEnabled");
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const WakeWordController = window.UNBOUNDWakeWordController;
  const SESSION_STORAGE_KEY = "unbound-ai.conversation-session-id";
  const RECOGNITION_TIMEOUT_MS = 30_000;
  const LOCAL_PATH_DIAGNOSTICS = /(?:\?|&)debug_local_path=1(?:&|$)/.test(window.location?.search || "");

  let recognizer = null;
  let recognitionRun = 0;
  let recognitionTimer = null;
  let listening = false;
  let recognitionStopping = false;
  let recognitionError = false;
  let recognitionCancelled = false;
  let recordingFinalText = "";
  let transcriptBeforeRecording = "";
  let finalResultIndexes = new Set();
  let lastMessage = "";
  let lastResponse = "";
  let lastRequestId = null;
  let lastAutoSpokenRequestId = null;
  let sessionId = null;
  let activeTask = null;
  let taskBusy = false;
  let speechRun = 0;
  let speaking = false;
  let speechOutputPending = false;
  let wakeResumeRequested = false;
  let activeUtterance = null;
  let editingReminderId = null;
  const reminderTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const wakeWordController = WakeWordController ? new WakeWordController({
    onActivate: () => {
      wakeResumeRequested = true;
      $("wakeWordState").textContent = "ACTIVATED — starting the push-to-talk recognizer for your request.";
      beginRecognition();
    },
    onError: (wakeError) => {
      $("wakeWordState").textContent = `Wake word stopped: ${wakeError?.message || "check the microphone and local backend"}`;
    },
    onState: (next) => { $("wakeWordState").textContent = `Wake word: ${next.toUpperCase()}`; },
  }) : null;

  function setState(next, message = "") {
    state.textContent = next;
    state.setAttribute("aria-label", `Status: ${next.toLowerCase()}`);
    error.textContent = next === "ERROR" ? message : "";
    stateDetail.textContent = next === "ERROR" ? "" : message;
  }

  function api(path, options = {}) {
    const base = $("apiBase").value.trim().replace(/\/$/, "");
    if (LOCAL_PATH_DIAGNOSTICS && path === "/api/assistant/requests") {
      try {
        const request = JSON.parse(options.body || "{}");
        logLocalPathDiagnostic("browser.fetch.message", localPathCandidate(request.message));
      } catch (_) {
        // Diagnostic parsing must never interfere with the actual request.
      }
    }
    return fetch(`${base}${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    }).then(async (result) => {
      const body = result.status === 204 ? null : await result.json().catch(() => ({}));
      if (!result.ok) {
        const detail = body?.detail;
        throw new Error((typeof detail === "string" ? detail : detail?.message) || `Backend returned HTTP ${result.status}`);
      }
      return body;
    });
  }

  function createLocalWakeDetector() {
    let stopped = true;
    let timer = null;
    let lastEventId = 0;
    let onError = null;
    return {
      localOnly: true,
      async start(onDetection, reportError) {
        stopped = false;
        onError = reportError;
        try {
          const status = await api("/api/wake-word/start", { method: "POST" });
          lastEventId = Number(status.event_id || 0);
          const poll = async () => {
            if (stopped) return;
            try {
              const pollResult = await api(`/api/wake-word/events?after_event_id=${lastEventId}`);
              if (stopped) return;
              const found = pollResult?.event;
              if (found && found.event_id > lastEventId) {
                lastEventId = found.event_id;
                onDetection({ phrase: found.phrase, confidence: found.confidence });
                return;
              }
              if (pollResult?.status?.status !== "listening") {
                stopped = true;
                onError?.(new Error(pollResult?.status?.detail || "Local wake monitoring stopped."));
                return;
              }
              timer = setTimeout(poll, 350);
            } catch (error) {
              if (!stopped) {
                stopped = true;
                onError?.(error);
                try { await api("/api/wake-word/stop", { method: "POST" }); } catch (_) {}
              }
            }
          };
          poll();
        } catch (error) {
          stopped = true;
          throw error;
        }
      },
      async stop() {
        stopped = true;
        if (timer !== null) clearTimeout(timer);
        timer = null;
        try { await api("/api/wake-word/stop", { method: "POST" }); }
        catch (error) { if (onError && !String(error.message).toLowerCase().includes("failed to fetch")) onError(error); }
      },
    };
  }
  if (!window.UNBOUND_LOCAL_WAKE_WORD_FACTORY) window.UNBOUND_LOCAL_WAKE_WORD_FACTORY = createLocalWakeDetector;

  async function resumeWakeWordIfRequested() {
    if (!wakeResumeRequested || !wakeWordController?.enabled || wakeWordController.state === "listening") return;
    wakeResumeRequested = false;
    try {
      await wakeWordController.start(window.UNBOUND_LOCAL_WAKE_WORD_FACTORY());
    } catch (error) {
      $("wakeWordState").textContent = `Wake word paused: ${error.message}`;
    }
  }

  function showHistory(messages) {
    $("history").textContent = messages.length
      ? messages.map((item) => `${item.role === "user" ? "You" : "UNBOUND"}: ${item.content}`).join("\n\n")
      : "No messages yet.";
  }

  function isExplicitLocalFileRequest(message) {
    const actionAtStart = /^\s*(?:(?:please|can you|could you|would you)\s+)?(?:open|read|analy[sz]e|summari[sz]e|review|inspect)\b/i;
    // Send explicit drive-path requests to the deterministic backend validator,
    // including unsupported or malformed targets that need a safe clarification.
    return actionAtStart.test(message) && /\b[A-Za-z]:\\/.test(message);
  }

  function localPathCandidate(message) {
    if (typeof message !== "string") return null;
    const matches = message.match(/[A-Za-z]:\\[^\r\n<>"|?*]*?\.(?:pdf|docx|txt)\b/ig) || [];
    return matches.length === 1 ? matches[0].replace(/[.,;:!?)\u201d\u2019]+$/, "") : null;
  }

  function logLocalPathDiagnostic(stage, path) {
    if (!LOCAL_PATH_DIAGNOSTICS || !path) return;
    const characters = Array.from(path);
    const tildeContexts = [];
    characters.forEach((character, index) => {
      if (character !== "~") return;
      tildeContexts.push({
        tilde_index: index,
        preceding_character: index ? characters[index - 1] : null,
        preceded_by_backslash: index > 0 && characters[index - 1] === "\\",
        characters: characters.slice(Math.max(0, index - 3), index + 4)
          .map((value, offset) => [Math.max(0, index - 3) + offset, value, value.codePointAt(0)]),
      });
    });
    const backslashPositions = characters.flatMap((character, index) => character === "\\" ? [index] : []);
    console.info("UNBOUND local path diagnostic", JSON.stringify({
      stage, path, length: characters.length, backslash_positions: backslashPositions, tilde_contexts: tildeContexts,
    }));
  }

  function taskOutcomeMessage(task) {
    if (["created", "planning"].includes(task.status)) {
      const fileStep = task.plan?.find((step) => step.tool_name === "document.read_local_file");
      const filePath = fileStep?.input?.file_path;
      const focus = fileStep?.input?.analysis_request;
      return `Task prepared to read and analyse ${filePath || "the requested file"}${focus ? ` for: ${focus}` : ""}. Approval is required. No file has been read. Review the exact action below, then explicitly start it to request approval.`;
    }
    if (task.status === "awaiting_approval") {
      const pending = task.pending_approvals?.find((approval) => approval.tool_name === "document.read_local_file");
      const approvedPath = pending?.arguments?.file_path;
      return `Awaiting your explicit approval to read ${approvedPath || "the selected file"}. The file has not been read.`;
    }
    if (task.status === "completed") {
      const verifiedStep = task.plan?.find((step) => step.verification?.verified === true);
      const result = verifiedStep?.verification?.tool_result;
      const analysis = result?.analysis;
      if (!analysis?.summary || !Array.isArray(analysis.findings)) {
        return "The backend reports task completion, but did not return a verified document analysis. UNBOUND cannot present this as a successful analysis.";
      }
      const findings = analysis.findings.map((finding) => {
        const kind = finding.kind === "fact" ? "Source fact" : "Interpretation";
        return `${kind}: ${finding.statement}\nSource ${finding.source_chunk_id}; evidence ${finding.evidence_id}: “${finding.quote}”`;
      });
      const missing = analysis.missing_or_unreadable?.length
        ? `\nMissing or unreadable: ${analysis.missing_or_unreadable.join("; ")}` : "";
      return `Task completed and independently verified.\n\n${analysis.summary}${findings.length ? `\n\n${findings.join("\n\n")}` : ""}${missing}`;
    }
    if (task.status === "failed" || task.status === "needs_attention" || task.status === "cancelled") {
      const failedStep = task.plan?.find((step) => step.status === "failed");
      const details = failedStep?.verification?.details || {};
      const reason = task.error || failedStep?.error || task.status;
      const failureCode = details.failure_code ? ` (failure code: ${details.failure_code})` : "";
      return `Task ${task.status}: ${reason}${failureCode}. No successful analysis is being claimed.`;
    }
    return `Task status: ${task.status}. Review the task details and events below.`;
  }

  function localTomorrow() {
    const value = new Date();
    value.setDate(value.getDate() + 1);
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
  }

  async function loadReminders() {
    try {
      const reminders = await api("/api/reminders");
      const agentStatus = $("reminderAgentStatus");
      let health = null;
      try { health = await api("/api/health"); }
      catch { agentStatus.textContent = "Notification agent status is unavailable. Reminders remain saved; notifications require the UNBOUND Windows notification agent to be installed and running."; }
      if (health?.notification_agent_running) {
        agentStatus.textContent = "UNBOUND Windows notification agent is running. Windows may still suppress notifications based on its notification settings.";
      } else if (health?.notification_agent_installed) {
        agentStatus.textContent = "UNBOUND Windows notification agent is installed but not currently running. Reminders remain saved.";
      } else if (health) {
        agentStatus.textContent = "Notifications require the UNBOUND Windows notification agent to be installed and running. Reminders remain saved if the agent is unavailable.";
      }
      const container = $("reminderList");
      container.replaceChildren();
      if (!reminders.length) {
        container.textContent = "No reminders.";
        return;
      }
      for (const reminder of reminders) {
        const row = document.createElement("div");
        row.className = "reminder-row";
        const summary = document.createElement("p");
        const delivery = reminder.notification_status === "submitted"
          ? "Windows accepted toast submission (display not acknowledged)"
          : reminder.notification_status === "retry_wait"
            ? `Notification retry pending${reminder.notification_error_code ? ` (${reminder.notification_error_code})` : ""}`
            : reminder.notification_status === "claimed"
              ? "Notification submission in progress"
              : reminder.status === "active" ? "No notification submission yet" : "No notification while proposed";
        summary.textContent = `${reminder.title} — ${reminder.due_date}${reminder.due_time ? ` ${reminder.due_time}` : " (all day)"} ${reminder.timezone} — ${reminder.status} — ${delivery}`;
        row.append(summary);
        if (reminder.status === "proposed") {
          const confirm = document.createElement("button");
          confirm.textContent = "Confirm reminder";
          confirm.addEventListener("click", async () => {
            try { await api(`/api/reminders/${encodeURIComponent(reminder.reminder_id)}/confirm`, { method: "POST" }); await loadReminders(); }
            catch (e) { $("reminderStatus").textContent = `Could not confirm reminder: ${e.message}`; }
          });
          row.append(confirm);
        }
        if (reminder.status === "active") {
          const complete = document.createElement("button");
          complete.textContent = "Complete";
          complete.addEventListener("click", async () => {
            try { await api(`/api/reminders/${encodeURIComponent(reminder.reminder_id)}/complete`, { method: "POST" }); await loadReminders(); }
            catch (e) { $("reminderStatus").textContent = `Could not complete reminder: ${e.message}`; }
          });
          row.append(complete);
        }
        const edit = document.createElement("button");
        edit.textContent = "Edit";
        edit.className = "secondary";
        edit.addEventListener("click", () => {
          editingReminderId = reminder.reminder_id;
          $("reminderTitle").value = reminder.title;
          $("reminderDate").value = reminder.due_date;
          $("reminderTime").value = reminder.due_time ? reminder.due_time.slice(0, 5) : "";
          $("saveReminderButton").textContent = "Save reminder changes";
          $("cancelReminderEditButton").hidden = false;
          $("reminderStatus").textContent = "Editing reminder.";
        });
        const remove = document.createElement("button");
        remove.textContent = "Delete";
        remove.className = "danger";
        remove.addEventListener("click", async () => {
          try { await api(`/api/reminders/${encodeURIComponent(reminder.reminder_id)}`, { method: "DELETE" }); await loadReminders(); }
          catch (e) { $("reminderStatus").textContent = `Could not delete reminder: ${e.message}`; }
        });
        row.append(edit, remove);
        container.append(row);
      }
    } catch (e) {
      const status = $("reminderAgentStatus");
      if (status) status.textContent = "Notification agent status is unavailable. Reminders remain saved; notifications require the UNBOUND Windows notification agent to be installed and running.";
      $("reminderList").textContent = `Reminder storage unavailable: ${e.message}`;
    }
  }

  function cancelReminderEdit() {
    editingReminderId = null;
    $("reminderTitle").value = "";
    $("reminderDate").value = localTomorrow();
    $("reminderTime").value = "";
    $("saveReminderButton").textContent = "Propose reminder";
    $("cancelReminderEditButton").hidden = true;
  }

  async function saveReminder() {
    const payload = {
      title: $("reminderTitle").value.trim(),
      due_date: $("reminderDate").value,
      due_time: $("reminderTime").value || null,
      timezone: reminderTimezone,
    };
    if (!payload.title || !payload.due_date) {
      $("reminderStatus").textContent = "Enter a reminder title and due date.";
      return;
    }
    try {
      if (editingReminderId) {
        const id = editingReminderId;
        await api(`/api/reminders/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(payload) });
        $("reminderStatus").textContent = "Reminder updated.";
      } else {
        const proposed = await api("/api/reminders", { method: "POST", body: JSON.stringify({ ...payload, explicitly_confirmed: false }) });
        $("reminderStatus").textContent = `Reminder proposed (${proposed.status}). Confirm it below to activate it.`;
      }
      cancelReminderEdit();
      await loadReminders();
    } catch (e) {
      $("reminderStatus").textContent = `Could not save reminder: ${e.message}`;
    }
  }

  async function restoreOrCreateSession() {
    setState("PROCESSING", "Loading conversation history…");
    const savedId = localStorage.getItem(SESSION_STORAGE_KEY);
    try {
      if (savedId) {
        try {
          const history = await api(`/api/conversations/${encodeURIComponent(savedId)}`);
          sessionId = history.session_id;
          showHistory(history.messages);
        } catch (e) {
          if (!String(e.message).toLowerCase().includes("not found")) throw e;
          localStorage.removeItem(SESSION_STORAGE_KEY);
        }
      }
      if (!sessionId) await startNewConversation(false);
      $("sessionId").textContent = sessionId;
      setState("IDLE");
    } catch (e) {
      setState("ERROR", `Could not restore conversation: ${e.message}`);
    }
  }

  async function startNewConversation(deleteCurrent = false) {
    stopListening({ cancel: true, quiet: true });
    stopSpeaking(true);
    if (deleteCurrent && sessionId) {
      await api(`/api/conversations/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    }
    const created = await api("/api/conversations", { method: "POST", body: "{}" });
    sessionId = created.session_id;
    localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    lastRequestId = null;
    lastMessage = "";
    lastResponse = "";
    activeTask = null;
    transcript.value = "";
    response.textContent = "";
    $("sessionId").textContent = sessionId;
    $("speakButton").disabled = true;
    $("taskOutput").textContent = "No task has been prepared.";
    $("taskEvents").textContent = "";
    $("approvalList").replaceChildren();
    $("executeConsent").checked = false;
    $("executeTaskButton").disabled = true;
    showHistory([]);
  }

  function setRecognitionButtons() {
    $("startButton").disabled = listening || recognitionStopping || !Recognition || !recognitionEnabled.checked;
    $("stopButton").disabled = !listening || recognitionStopping;
    $("cancelRecordingButton").disabled = !listening && !recognitionStopping;
  }

  function clearRecognitionTimer() {
    if (recognitionTimer !== null) clearTimeout(recognitionTimer);
    recognitionTimer = null;
  }

  function stopListening({ cancel = false, quiet = false } = {}) {
    clearRecognitionTimer();
    if ((!listening && !recognitionStopping) || !recognizer) {
      setRecognitionButtons();
      return;
    }
    listening = false;
    if (cancel) {
      listening = false;
      recognitionStopping = false;
      recognitionCancelled = true;
      transcript.value = transcriptBeforeRecording;
      partial.textContent = "";
      recognitionError = false;
      try { recognizer.abort(); } catch (_) { /* The recognizer may already have ended. */ }
      if (!quiet) setState("IDLE");
    } else {
      recognitionStopping = true;
      clearRecognitionTimer();
      setRecognitionButtons();
      try { recognizer.stop(); } catch (e) {
        listening = false;
        recognitionStopping = false;
        recognitionError = true;
        setState("ERROR", `Could not stop speech recognition: ${e.message}`);
      }
    }
    setRecognitionButtons();
  }

  function recognitionFailure(message, currentRun) {
    if (currentRun !== recognitionRun || recognitionCancelled || recognitionError) return;
    recognitionError = true;
    listening = false;
    recognitionStopping = false;
    clearRecognitionTimer();
    setRecognitionButtons();
    setState("ERROR", message);
    try { recognizer?.abort(); } catch (_) { /* Ignore an already-ended recognizer. */ }
  }

  function beginRecognition() {
    setState("IDLE");
    if (!recognitionEnabled.checked) {
      setState("ERROR", "Speech recognition is disabled. Enable it or type your message.");
      resumeWakeWordIfRequested();
      return;
    }
    if (!Recognition) {
      setState("ERROR", "This browser does not support Web Speech recognition. Type your message instead.");
      resumeWakeWordIfRequested();
      return;
    }
    if (listening || recognitionStopping || $("sendButton").disabled || taskBusy) return;
    if (speaking) stopSpeaking(true);

    recognitionRun += 1;
    const currentRun = recognitionRun;
    recognitionError = false;
    recognitionCancelled = false;
    recordingFinalText = "";
    transcriptBeforeRecording = transcript.value;
    finalResultIndexes = new Set();
    partial.textContent = "";
    try {
      recognizer = new Recognition();
      recognizer.lang = navigator.language || "en-US";
      recognizer.continuous = false;
      recognizer.interimResults = true;
      recognizer.maxAlternatives = 1;
      recognizer.onstart = () => {
        if (currentRun !== recognitionRun) return;
        listening = true;
        recognitionTimer = setTimeout(() => recognitionFailure(
          "Listening timed out. Try again and speak after listening starts.", currentRun,
        ), RECOGNITION_TIMEOUT_MS);
        setState("LISTENING");
        setRecognitionButtons();
      };
      recognizer.onresult = (event) => {
        if (currentRun !== recognitionRun || (!listening && !recognitionStopping)) return;
        let interim = "";
        const addedFinal = [];
        for (let i = Math.max(0, event.resultIndex || 0); i < event.results.length; i += 1) {
          const result = event.results[i];
          const text = result?.[0]?.transcript || "";
          if (result.isFinal) {
            if (!finalResultIndexes.has(i)) {
              finalResultIndexes.add(i);
              if (text.trim()) addedFinal.push(text.trim());
            }
          } else {
            interim += text;
          }
        }
        if (addedFinal.length) transcript.value = [transcript.value.trim(), addedFinal.join(" ")].filter(Boolean).join(" ");
        if (addedFinal.length) recordingFinalText = [recordingFinalText, ...addedFinal].join(" ");
        partial.textContent = interim.trim();
      };
      recognizer.onerror = (event) => {
        const messages = {
          "not-allowed": "Microphone permission was denied. Allow microphone access in Edge site permissions, then retry.",
          "service-not-allowed": "Speech recognition is blocked by browser or device policy.",
          "audio-capture": "No microphone was found. Check the selected Windows input device.",
          "no-speech": "No speech was detected. Try again and speak after listening starts.",
          network: "Browser speech recognition reported a network/service error. It may require an online browser service.",
        };
        recognitionFailure(messages[event.error] || `Speech recognition failed (${event.error || "unknown error"}).`, currentRun);
      };
      recognizer.onend = () => {
        if (currentRun !== recognitionRun) return;
        listening = false;
        recognitionStopping = false;
        clearRecognitionTimer();
        setRecognitionButtons();
        partial.textContent = "";
        if (!recognitionError && state.textContent === "LISTENING") {
          if (!recordingFinalText && !transcriptBeforeRecording.trim()) setState("ERROR", "No finalized speech was captured. Try again or type your message.");
          else setState("IDLE");
        }
        if (recognitionCancelled || recognitionError) resumeWakeWordIfRequested();
      };
      listening = true;
      setRecognitionButtons();
      recognizer.start();
    } catch (e) {
      listening = false;
      recognitionError = true;
      clearRecognitionTimer();
      setRecognitionButtons();
      setState("ERROR", `Could not start microphone recognition: ${e.message}`);
      resumeWakeWordIfRequested();
    }
  }

  async function sendMessage() {
    const enteredText = transcript.value;
    logLocalPathDiagnostic("browser.transcript.value", localPathCandidate(enteredText));
    const message = enteredText.trim();
    const localFileHandoff = isExplicitLocalFileRequest(message);
    if (!message) return setState("ERROR", "Transcript is empty. Speak or type a message first.");
    if (!sessionId) return setState("ERROR", "No conversation is loaded. Retry after the backend is available.");
    if (listening || recognitionStopping || $("sendButton").disabled || taskBusy) return;
    stopSpeaking(true);
    const playbackTicket = speechRun;
    speechOutputPending = true;
    $("stopSpeakingButton").disabled = false;
    $("sendButton").disabled = true;
    $("retryButton").disabled = true;
    setState("PROCESSING");
    try {
      if (localFileHandoff) {
        taskBusy = true;
        logLocalPathDiagnostic("browser.transcript_before_fetch", localPathCandidate(message));
        const handoff = await api("/api/assistant/requests", {
          method: "POST",
          body: JSON.stringify({ message, session_id: sessionId }),
        });
        $("taskIntent").textContent = `Intent: ${handoff.intent.category}${handoff.intent.clarification_question ? ` — ${handoff.intent.clarification_question}` : ""}`;
        if (handoff.task) {
          const taskPath = handoff.task.plan?.find((step) => step.tool_name === "document.read_local_file")?.input?.file_path;
          logLocalPathDiagnostic("browser.task_plan_response", taskPath);
          renderTask(handoff.task);
          await refreshTaskEvents(handoff.task.task_id);
        } else {
          activeTask = null;
          $("taskOutput").textContent = "No task plan was created.";
          $("approvalList").replaceChildren();
          $("taskEvents").textContent = "";
        }
        lastResponse = handoff.task ? taskOutcomeMessage(handoff.task) : handoff.reply;
        $("taskReply").textContent = lastResponse;
        response.textContent = lastResponse;
        speechOutputPending = false;
        if (playbackTicket === speechRun && speechEnabled.checked && lastAutoSpokenRequestId !== `task:${handoff.task?.task_id || message}`) {
          lastAutoSpokenRequestId = `task:${handoff.task?.task_id || message}`;
          speakResponse();
        } else if (playbackTicket === speechRun) {
          setState("IDLE");
        }
        return;
      }

      if (!lastRequestId || message !== lastMessage) lastRequestId = crypto.randomUUID();
      const requestId = lastRequestId;
      lastMessage = message;
      const body = await api(`/api/conversations/${encodeURIComponent(sessionId)}/messages`, {
        method: "POST",
        body: JSON.stringify({ message, request_id: requestId, timezone: reminderTimezone }),
      });
      const reply = body.reply || "";
      if (!reply.trim()) throw new Error("Backend returned an empty response.");
      const history = await api(`/api/conversations/${encodeURIComponent(sessionId)}`);
      showHistory(history.messages);
      lastResponse = reply;
      response.textContent = reply;
      lastRequestId = null;
      $("speakButton").disabled = !speechEnabled.checked || !("speechSynthesis" in window);
      speechOutputPending = false;
      if (playbackTicket !== speechRun) {
        if (state.textContent === "PROCESSING") setState("IDLE");
      } else if (speechEnabled.checked && lastAutoSpokenRequestId !== requestId) {
        lastAutoSpokenRequestId = requestId;
        speakResponse();
      } else {
        setState("IDLE");
      }
    } catch (e) {
      speechOutputPending = false;
      $("retryButton").disabled = localFileHandoff;
      setState("ERROR", `${localFileHandoff ? "Could not prepare the local-file task" : "Could not reach the conversation service"}: ${e.message}`);
    } finally {
      taskBusy = false;
      if (playbackTicket === speechRun && !activeUtterance) $("stopSpeakingButton").disabled = true;
      $("sendButton").disabled = false;
      if (!speaking && !speechOutputPending) resumeWakeWordIfRequested();
    }
  }

  function stopSpeaking(quiet = false) {
    const wasSpeaking = speaking || activeUtterance !== null;
    const shouldLeaveProcessing = speechOutputPending && state.textContent === "PROCESSING";
    speechRun += 1;
    speechOutputPending = false;
    activeUtterance = null;
    if ("speechSynthesis" in window) {
      try {
        // cancel() aborts the current utterance and clears queued utterances.
        // Invalidate callbacks first because browsers may dispatch a late
        // `end`/`error` event after cancellation.
        window.speechSynthesis.cancel();
      } catch (_) {
        // Keep the text response usable and leave the UI out of SPEAKING even
        // if an implementation throws while clearing its native queue.
      }
    }
    speaking = false;
    $("stopSpeakingButton").disabled = true;
    if (!quiet && (state.textContent === "SPEAKING" || shouldLeaveProcessing)) setState("IDLE");
    if (!quiet && wasSpeaking) resumeWakeWordIfRequested();
  }

  function chooseVoice(voices) {
    if (!voices.length) return null;
    const language = (navigator.language || "en-US").toLowerCase();
    const baseLanguage = language.split("-")[0];
    return voices.find((voice) => voice.lang?.toLowerCase() === language)
      || voices.find((voice) => voice.lang?.toLowerCase().startsWith(`${baseLanguage}-`))
      || voices[0];
  }

  function speakResponse() {
    if (!lastResponse || !speechEnabled.checked) return;
    if (!("speechSynthesis" in window) || typeof window.SpeechSynthesisUtterance !== "function") {
      setState("ERROR", "Speech synthesis is unavailable in this browser. The response remains available as text.");
      return;
    }
    stopSpeaking(true);
    const currentRun = speechRun;
    try {
      const utterance = new window.SpeechSynthesisUtterance(lastResponse);
      activeUtterance = utterance;
      const voice = chooseVoice(window.speechSynthesis.getVoices());
      if (voice) utterance.voice = voice;
      utterance.onstart = () => {
        if (currentRun !== speechRun) return;
        speaking = true;
        $("stopSpeakingButton").disabled = false;
        setState("SPEAKING");
      };
      utterance.onend = () => {
        if (currentRun !== speechRun) return;
        activeUtterance = null;
        speaking = false;
        $("stopSpeakingButton").disabled = true;
        setState("IDLE");
        resumeWakeWordIfRequested();
      };
      utterance.onerror = () => {
        if (currentRun !== speechRun) return;
        activeUtterance = null;
        speaking = false;
        $("stopSpeakingButton").disabled = true;
        setState("ERROR", "Speech synthesis failed. You can still read the response.");
        resumeWakeWordIfRequested();
      };
      // Enable interruption before `onstart`: native speech events may arrive
      // after the utterance has entered the browser queue.
      speaking = true;
      $("stopSpeakingButton").disabled = false;
      setState("SPEAKING");
      window.speechSynthesis.speak(utterance);
    } catch (_) {
      activeUtterance = null;
      speaking = false;
      $("stopSpeakingButton").disabled = true;
      setState("ERROR", "Speech synthesis could not start. You can still read the response.");
    }
  }

  function renderTask(task) {
    activeTask = task;
    if (task?.plan) {
      const step = task.plan.find((item) => item.tool_name === "document.read_local_file");
      logLocalPathDiagnostic("browser.render_task", step?.input?.file_path);
      const approval = (task.pending_approvals || []).find((item) => item.tool_name === "document.read_local_file");
      logLocalPathDiagnostic("browser.render_approval", approval?.arguments?.file_path);
    }
    const lines = [
      `Task ${task.task_id} — ${task.status}`,
      task.user_request ? `Request: ${task.user_request}` : "",
      task.current_step ? `Current step: ${task.current_step}` : "No step is currently running.",
      task.error ? `Task error: ${task.error}` : "",
      task.result ? `Result: ${JSON.stringify(task.result)}` : "",
      `Steps: ${task.plan.map((step) => `${step.step_id} · ${step.status} · ${step.tool_name}${step.requires_approval ? " · approval required" : ""} — ${step.description}${step.error ? ` · error: ${step.error}` : ""}${step.verification ? ` · verification: ${JSON.stringify(step.verification)}` : ""}`).join(" | ")}`,
      `Evidence references (not proof of success by themselves): ${task.evidence.length ? task.evidence.map((item) => `${item.evidence_id} · ${item.source_name} · ${item.source_url || item.file_path || "source reference unavailable"} · supports: ${item.supports_claim}`).join(" | ") : "none returned"}`,
    ].filter(Boolean);
    $("taskOutput").textContent = lines.join("\n");
    const approvalList = $("approvalList");
    approvalList.replaceChildren();
    for (const approval of task.pending_approvals || []) {
      const card = document.createElement("div");
      card.className = "approval-card";
      const description = document.createElement("p");
      description.textContent = `Approval required for ${approval.tool_name} (${approval.step_id}): ${approval.description}`;
      const args = document.createElement("pre");
      args.textContent = Object.entries(approval.arguments || {}).map(([name, value]) => {
        // Show filesystem paths as literal text. JSON.stringify escapes each
        // backslash, which is valid JSON but makes Windows paths look altered.
        return `${name}: ${name === "file_path" && typeof value === "string" ? value : JSON.stringify(value)}`;
      }).join("\n");
      card.append(description, args);
      for (const decision of ["APPROVED", "REJECTED"]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = decision === "APPROVED" ? "Approve this action" : "Reject this action";
        button.className = decision === "REJECTED" ? "danger" : "secondary";
        button.addEventListener("click", () => resolveApproval(approval.approval_id, decision));
        card.append(button);
      }
      approvalList.append(card);
    }
    const hasPendingApproval = (task.pending_approvals || []).length > 0;
    const canStart = ["created", "executing"].includes(task.status);
    $("executeConsent").checked = false;
    $("executeConsent").disabled = !canStart || hasPendingApproval;
    $("executeTaskButton").disabled = true;
    $("executeTaskButton").textContent = hasPendingApproval ? "Resolve approval before execution" : canStart ? "Execute task (explicit)" : "Task cannot be executed in this state";
  }

  async function refreshTaskEvents(taskId) {
    try {
      const events = await api(`/api/tasks/${encodeURIComponent(taskId)}/events`);
      $("taskEvents").textContent = events.length
        ? events.map((event) => `${event.timestamp} · ${event.status} · ${event.message}${event.evidence_ids?.length ? ` · evidence ${event.evidence_ids.join(", ")}` : ""}`).join("\n")
        : "No execution events yet.";
    } catch (e) {
      $("taskEvents").textContent = `Task events unavailable: ${e.message}`;
    }
  }

  async function prepareTask() {
    const message = transcript.value.trim();
    if (!message) return setState("ERROR", "Enter or recognize a request before preparing a task plan.");
    if (!sessionId) return setState("ERROR", "No conversation session is loaded.");
    if (listening || recognitionStopping || taskBusy || $("sendButton").disabled) return;
    taskBusy = true;
    $("prepareTaskButton").disabled = true;
    setState("PROCESSING", "Classifying the request and preparing a plan. No task will execute yet.");
    try {
      const result = await api("/api/assistant/requests", {
        method: "POST",
        body: JSON.stringify({ message, session_id: sessionId }),
      });
      $("taskIntent").textContent = `Intent: ${result.intent.category}${result.intent.clarification_question ? ` — ${result.intent.clarification_question}` : ""}`;
      $("taskReply").textContent = result.reply;
      if (result.task) {
        renderTask(result.task);
        await refreshTaskEvents(result.task.task_id);
      } else {
        activeTask = null;
        $("taskOutput").textContent = "No task plan was created.";
        $("approvalList").replaceChildren();
        $("taskEvents").textContent = "";
      }
      setState("IDLE");
    } catch (e) {
      setState("ERROR", `Could not prepare a task: ${e.message}`);
    } finally {
      taskBusy = false;
      $("prepareTaskButton").disabled = false;
    }
  }

  async function executeTask() {
    if (!activeTask || !["created", "executing"].includes(activeTask.status) || !$("executeConsent").checked || taskBusy) return;
    if (!window.confirm("Start this task now? Consequential actions may pause for separate approval.")) return;
    const taskId = activeTask.task_id;
    taskBusy = true;
    $("executeTaskButton").disabled = true;
    $("executeConsent").checked = false;
    setState("PROCESSING", "Executing the explicitly selected task and checking its outcomes.");
    response.textContent = "Task execution requested. Waiting for backend status and verification…";
    try {
      const task = await api(`/api/tasks/${encodeURIComponent(taskId)}/execute`, { method: "POST" });
      renderTask(task);
      await refreshTaskEvents(taskId);
      lastResponse = taskOutcomeMessage(task);
      response.textContent = lastResponse;
      setState(task.status === "completed" ? "IDLE" : task.status === "failed" ? "ERROR" : "IDLE",
        task.status === "completed" ? "Task completed according to the backend verification flow." : task.status === "failed" ? (task.error || "Task failed or could not be verified.") : task.status === "awaiting_approval" ? "The action is paused until you approve or reject the exact request." : "Task stopped before completion; review its status and events.");
    } catch (e) {
      setState("ERROR", `Task execution request failed: ${e.message}`);
      response.textContent = `Task execution request failed: ${e.message}`;
      if (activeTask) renderTask(activeTask);
    } finally {
      taskBusy = false;
    }
  }

  async function resolveApproval(approvalId, decision) {
    if (!activeTask || taskBusy) return;
    const action = decision === "APPROVED" ? "approve" : "reject";
    if (!window.confirm(`Do you want to ${action} this exact proposed action?`)) return;
    const taskId = activeTask.task_id;
    taskBusy = true;
    setState("PROCESSING", `Recording your ${action} decision. The task will not execute automatically.`);
    try {
      const task = await api(`/api/tasks/${encodeURIComponent(taskId)}/approvals/${encodeURIComponent(approvalId)}`, {
        method: "POST", body: JSON.stringify({ decision }),
      });
      renderTask(task);
      await refreshTaskEvents(taskId);
      lastResponse = decision === "APPROVED"
        ? `Approval recorded for the exact proposed action. No file has been read yet. Explicitly execute the task below to continue.`
        : `The proposed action was rejected. No file was read.`;
      response.textContent = lastResponse;
      setState("IDLE", `Approval decision recorded: ${decision}. Explicit execution is still required.`);
    } catch (e) {
      setState("ERROR", `Could not record approval decision: ${e.message}`);
      response.textContent = `Could not record approval decision: ${e.message}`;
    } finally {
      taskBusy = false;
    }
  }

  $("startButton").addEventListener("click", async () => {
    if (wakeWordController?.state === "listening") {
      wakeResumeRequested = wakeWordController.enabled;
      await wakeWordController.mute();
    }
    beginRecognition();
  });
  $("stopButton").addEventListener("click", () => stopListening());
  $("cancelRecordingButton").addEventListener("click", () => stopListening({ cancel: true }));
  $("sendButton").addEventListener("click", sendMessage);
  $("retryButton").addEventListener("click", () => {
    if (lastMessage) transcript.value = lastMessage;
    sendMessage();
  });
  $("speakButton").addEventListener("click", speakResponse);
  $("stopSpeakingButton").addEventListener("click", () => stopSpeaking());
  $("wakeWordStartButton").disabled = typeof window.UNBOUND_LOCAL_WAKE_WORD_FACTORY !== "function" || !wakeWordController;
  $("wakeWordStartButton").addEventListener("click", async () => {
    try {
      const detector = window.UNBOUND_LOCAL_WAKE_WORD_FACTORY?.();
      await wakeWordController?.start(detector);
      wakeResumeRequested = true;
    } catch (e) {
      $("wakeWordState").textContent = `Wake word unavailable: ${e.message}`;
    }
  });
  $("wakeWordMuteButton").addEventListener("click", () => {
    wakeResumeRequested = false;
    wakeWordController?.mute();
  });
  $("wakeWordEnableButton").addEventListener("click", () => wakeWordController?.enable());
  $("wakeWordDisableButton").addEventListener("click", () => {
    wakeResumeRequested = false;
    wakeWordController?.disable();
  });
  $("saveReminderButton").addEventListener("click", saveReminder);
  $("cancelReminderEditButton").addEventListener("click", cancelReminderEdit);
  $("prepareTaskButton").addEventListener("click", prepareTask);
  $("executeTaskButton").addEventListener("click", executeTask);
  $("executeConsent").addEventListener("change", () => {
    $("executeTaskButton").disabled = !$("executeConsent").checked || !activeTask || taskBusy || (activeTask.pending_approvals || []).length > 0;
  });

  speechEnabled.addEventListener("change", () => {
    if (!speechEnabled.checked) stopSpeaking();
    $("speakButton").disabled = !speechEnabled.checked || !lastResponse || !("speechSynthesis" in window);
  });
  recognitionEnabled.addEventListener("change", () => {
    if (!recognitionEnabled.checked) {
      stopListening({ cancel: true });
      wakeResumeRequested = false;
      wakeWordController?.mute();
    }
    setRecognitionButtons();
  });
  $("newConversationButton").addEventListener("click", async () => {
    try { await startNewConversation(false); setState("IDLE"); }
    catch (e) { setState("ERROR", `Could not start a new conversation: ${e.message}`); }
  });
  $("deleteConversationButton").addEventListener("click", async () => {
    if (!window.confirm("Delete this conversation and its messages from local storage?")) return;
    try { await startNewConversation(true); setState("IDLE"); }
    catch (e) { setState("ERROR", `Could not delete the conversation: ${e.message}`); }
  });

  window.addEventListener("pagehide", () => {
    stopListening({ cancel: true, quiet: true });
    stopSpeaking(true);
    wakeWordController?.disable();
  });
  $("speakButton").disabled = !("speechSynthesis" in window);
  $("reminderTimezone").textContent = reminderTimezone;
  $("reminderDate").value = localTomorrow();
  if (!Recognition) $("startButton").title = "Web Speech recognition is unsupported; manual text entry is available.";
  setRecognitionButtons();
  restoreOrCreateSession();
  loadReminders();
})();
