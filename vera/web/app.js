/* VERA Control Panel PWA + Bidirectional Gemini Live Audio Client */

const state = {
  token: localStorage.getItem("vera_token") || "",
  activeDoc: "USER.md",
  docs: {},
  selectedFile: null,
  liveWs: null,
  liveActive: false,
  captureCtx: null,
  playCtx: null,
  micStream: null,
  processor: null,
  nextPlayTime: 0,
  activeSources: [],
  autoVoice: localStorage.getItem("vera_autovoice") !== "false",
  ttsVoice: localStorage.getItem("vera_tts_voice") || "tr-TR-AhmetNeural",
  currentAudio: null,
  wakeEnabled: localStorage.getItem("vera_wake_enabled") !== "false",
  wakeAwake: false,
  wakeBusy: false,
  wakeSpeaking: false,
  wakeRec: null,
  wakeTimer: null,
  ttsGenId: 0,
  currentSpokenText: "",
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  if (!(options.body instanceof FormData) && options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401) {
    logout();
    throw new Error("Oturum süresi doldu.");
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || "İstek başarısız.");
  }
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("application/json") ? resp.json() : resp;
}

function logout() {
  state.token = "";
  localStorage.removeItem("vera_token");
  stopLiveVoice();
  $("#app-shell").classList.add("hidden");
  $("#login-screen").classList.remove("hidden");
}

async function initApp() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  if (!state.token) {
    $("#login-screen").classList.remove("hidden");
    return;
  }
  try {
    await api("/api/system/status");
    $("#login-screen").classList.add("hidden");
    $("#app-shell").classList.remove("hidden");
    await Promise.all([loadChatHistory(), loadApprovals(), loadSystemStatus()]);
  } catch {
    logout();
  }
}

/* --- Login --- */
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("#login-password").value }),
    });
    if (!res.ok) throw new Error("Parola hatalı.");
    const data = await res.json();
    state.token = data.token;
    localStorage.setItem("vera_token", state.token);
    $("#login-password").value = "";
    await initApp();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#logout-btn").addEventListener("click", logout);

/* --- Navigation --- */
$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".nav-item").forEach((b) => b.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    $(`#tab-${tab}`).classList.add("active");
    if (tab === "memory") loadMemoryTab();
    if (tab === "jobs") loadJobsTab();
    if (tab === "skills") loadSkillsTab();
    if (tab === "system") loadSystemStatus();
  });
});

/* --- Chat --- */
function appendMessage(role, content, metaText = "") {
  const box = $("#chat-messages");
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = content;

  if (role === "assistant") {
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const span = document.createElement("span");
    span.textContent = metaText || "VERA";
    const ttsBtn = document.createElement("button");
    ttsBtn.className = "tts-btn";
    ttsBtn.textContent = "🔊 Dinle";
    ttsBtn.onclick = () => playTTS(content);
    meta.append(span, ttsBtn);
    div.appendChild(meta);
  }
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function splitIntoSpeechSentences(rawText) {
  const cleaned = rawText
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/[#*_~>|`]+/g, " ")
    .replace(/\n+/g, ". ")
    .replace(/\s+/g, " ")
    .trim();
  if (!cleaned) return [];
  const rawParts = cleaned.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [cleaned];
  const chunks = [];
  let buf = "";
  for (const part of rawParts) {
    const s = part.trim();
    if (!s) continue;
    if ((buf + " " + s).trim().length <= 170) {
      buf = (buf + " " + s).trim();
    } else {
      if (buf) chunks.push(buf);
      buf = s;
    }
    if (chunks.length >= 6) break;
  }
  if (buf && chunks.length < 6) chunks.push(buf);
  return chunks;
}

function normalizeTr(str) {
  return (str || "")
    .toLowerCase()
    .replace(/[^\wçğıöşü\s]/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function isLikelySpeakerEcho(heardRaw, spokenRaw) {
  const heard = normalizeTr(heardRaw);
  if (!heard) return true;
  // Explicit interrupt commands always trigger barge-in immediately
  if (/\b(hey\s*vera|vera\s*dur|dur\s*vera|bekle|sus|kes|tamam\s*dur|jarvis\s*dur|hey\s*jarvis)\b/i.test(heard)) {
    return false;
  }
  const spoken = normalizeTr(spokenRaw);
  if (!spoken) return false;
  if (spoken.includes(heard)) return true;
  const heardWords = heard.split(" ").filter((w) => w.length > 2);
  if (!heardWords.length) return true;
  const matched = heardWords.filter((w) => spoken.includes(w));
  return matched.length / heardWords.length >= 0.55;
}

function interruptVeraSpeech() {
  state.ttsGenId += 1;
  if (state.currentAudio) {
    try { state.currentAudio.pause(); } catch {}
    state.currentAudio = null;
  }
  if (state.liveAudio) {
    try { state.liveAudio.pause(); } catch {}
    state.liveAudio = null;
  }
  state.wakeSpeaking = false;
  state.currentSpokenText = "";
  $("#voice-orb")?.classList.remove("speaking");
  state.wakeAwake = true;
  armWakeSleepTimer(30000);
  setWakeBannerState("awake", "🎙️ SÖZÜNÜZÜ DİNLİYORUM YUSUF BEY — Buyurun devam edin...");
}

async function playTTS(text, onDone = null) {
  const genId = ++state.ttsGenId;
  if (state.currentAudio) {
    try { state.currentAudio.pause(); } catch {}
    state.currentAudio = null;
  }

  const sentences = splitIntoSpeechSentences(text);
  if (!sentences.length) {
    if (onDone) onDone();
    return;
  }

  state.wakeSpeaking = true;
  state.currentSpokenText = sentences.join(" ");
  ensureWakeRecognizerRunning();
  setWakeBannerState("speaking", "🔊 VERA konuşuyor... (Konuşmaya başlarsanız anında susup sizi dinler)");

  // Pipeline prefetch all sentence audio blobs in parallel for 0ms gap between sentences
  const fetchPromises = sentences.map((sentence) =>
    api("/api/voice/tts", {
      method: "POST",
      body: JSON.stringify({ message: sentence, voice: state.ttsVoice }),
    })
      .then((r) => r.blob())
      .catch(() => null)
  );

  try {
    for (let i = 0; i < fetchPromises.length; i++) {
      if (state.ttsGenId !== genId) return; // Interrupted by barge-in!
      const blob = await fetchPromises[i];
      if (!blob || state.ttsGenId !== genId) continue;

      await new Promise((resolve) => {
        if (state.ttsGenId !== genId) return resolve();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        state.currentAudio = audio;
        const done = () => {
          URL.revokeObjectURL(url);
          resolve();
        };
        audio.onended = done;
        audio.onerror = done;
        audio.play().catch(done);
      });
    }
  } finally {
    if (state.ttsGenId === genId) {
      state.wakeSpeaking = false;
      state.currentAudio = null;
      state.currentSpokenText = "";
      if (state.wakeAwake) {
        setWakeBannerState("awake", "🎙️ VERA DİNLİYOR — Buyurun Yusuf Bey, komutunuzu söyleyin...");
        armWakeSleepTimer(30000);
      } else if (state.wakeEnabled) {
        setWakeBannerState("standby", '🟢 "Hey Vera" nöbeti aktif — "Hey Vera" diyerek seslenebilirsiniz.');
      }
      ensureWakeRecognizerRunning();
      if (onDone) onDone();
    }
  }
}

function playJarvisChime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const now = ctx.currentTime;
    [587.33, 880.0].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, now + i * 0.09);
      gain.gain.setValueAtTime(0.14, now + i * 0.09);
      gain.gain.exponentialRampToValueAtTime(0.001, now + i * 0.09 + 0.16);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now + i * 0.09);
      osc.stop(now + i * 0.09 + 0.17);
    });
  } catch {}
}

function setWakeBannerState(mode, text) {
  const banner = $("#wake-status-banner");
  const txt = $("#wake-status-text");
  if (!banner || !txt) return;
  banner.classList.remove("awake", "speaking");
  if (mode === "awake") banner.classList.add("awake");
  if (mode === "speaking") banner.classList.add("speaking");
  txt.textContent = text;
}

function armWakeSleepTimer(ms = 30000) {
  if (state.wakeTimer) clearTimeout(state.wakeTimer);
  state.wakeTimer = setTimeout(() => {
    if (state.wakeAwake && !state.wakeSpeaking && !state.wakeBusy) {
      state.wakeAwake = false;
      setWakeBannerState("standby", '🟢 "Hey Vera" nöbet moduna döndü — "Hey Vera" diyerek uyandırabilirsiniz.');
    }
  }, ms);
}

async function executeWakeVoiceCommand(commandText) {
  const cleanCmd = commandText.trim();
  if (!cleanCmd) return;

  // Check if user told VERA to go back to standby
  if (/^(teşekkürler|teşekkür ederim|tamamdır|beklemeye geç|uyku modu|kapat|görüşürüz)\b/i.test(cleanCmd)) {
    state.wakeAwake = false;
    if (state.wakeTimer) clearTimeout(state.wakeTimer);
    const bye = "Rica ederim Yusuf Bey, nöbet moduna geçiyorum. İhtiyacınız olduğunda Hey Vera demeniz yeterli.";
    appendMessage("assistant", bye, "J.A.R.V.I.S. Nöbet Modu");
    await playTTS(bye);
    return;
  }

  state.wakeBusy = true;
  setWakeBannerState("awake", `⚡ Emir işleniyor: "${cleanCmd}"`);
  appendMessage("user", `🎙️ ${cleanCmd}`);

  const thinking = document.createElement("div");
  thinking.className = "msg assistant muted";
  thinking.textContent = "J.A.R.V.I.S. emrinizi icra ediyor...";
  $("#chat-messages").appendChild(thinking);
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;

  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: cleanCmd, session_id: "web_main" }),
    });
    thinking.remove();
    const meta = data.tools_used?.length ? `Araçlar: ${data.tools_used.join(", ")}` : (data.model || "J.A.R.V.I.S.");
    appendMessage("assistant", data.reply, meta);
    state.wakeBusy = false;
    state.wakeAwake = true;
    await playTTS(data.reply);
    await loadApprovals();
  } catch (err) {
    thinking.textContent = "Hata: " + err.message;
    state.wakeBusy = false;
    ensureWakeRecognizerRunning();
  }
}

function ensureWakeRecognizerRunning() {
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec || !state.wakeEnabled || state.liveActive || state.wakeBusy) return;
  if (state.wakeRec) return;

  try {
    const rec = new SpeechRec();
    state.wakeRec = rec;
    rec.lang = "tr-TR";
    rec.continuous = true;
    rec.interimResults = true;

    rec.onresult = async (ev) => {
      if (state.wakeBusy) return;
      const lastResult = ev.results[ev.results.length - 1];
      if (!lastResult) return;
      const spoken = (lastResult[0]?.transcript || "").trim();
      if (!spoken) return;

      // BARGE-IN: If VERA is currently speaking and user starts talking (non-echo), stop VERA immediately!
      if (state.wakeSpeaking) {
        if (!isLikelySpeakerEcho(spoken, state.currentSpokenText) && spoken.length >= 3) {
          interruptVeraSpeech();
        } else {
          return;
        }
      }

      if (!lastResult.isFinal) return;

      const wakeRegex = /\b(?:hey\s*vera|ey\s*vera|hei\s*vera|hey\s*bera|merhaba\s*vera|alo\s*vera|hey\s*jarvis|vera|jarvis)\b/i;

      if (!state.wakeAwake) {
        const match = spoken.match(wakeRegex);
        if (!match) return; // Ignore background speech that doesn't contain wake word
        playJarvisChime();
        state.wakeAwake = true;
        armWakeSleepTimer(30000);
        const afterWake = spoken.slice(match.index + match[0].length).replace(/^[\s,.:;!?-]+/, "").trim();
        if (afterWake.length >= 4) {
          await executeWakeVoiceCommand(afterWake);
        } else {
          const greeting = "Buyurun Yusuf Bey, sizi dinliyorum. Emriniz nedir?";
          appendMessage("assistant", greeting, "J.A.R.V.I.S. Sesli Uyandırma");
          await playTTS(greeting);
        }
      } else {
        // Already awake (or just interrupted VERA via barge-in) -> execute command!
        playJarvisChime();
        armWakeSleepTimer(30000);
        const stripped = spoken.replace(wakeRegex, "").replace(/^[\s,.:;!?-]+/, "").trim() || spoken;
        await executeWakeVoiceCommand(stripped);
      }
    };

    rec.onerror = (ev) => {
      state.wakeRec = null;
      if (ev.error === "not-allowed") {
        setWakeBannerState("standby", '⚠️ Mikrofon izni gerekli — "Hey Vera" nöbetini başlatmak için bu şeride dokunun.');
        return;
      }
      if (state.wakeEnabled && !state.wakeBusy) {
        setTimeout(ensureWakeRecognizerRunning, 600);
      }
    };

    rec.onend = () => {
      state.wakeRec = null;
      if (state.wakeEnabled && !state.wakeBusy && !state.liveActive) {
        setTimeout(ensureWakeRecognizerRunning, 250);
      }
    };

    rec.start();
    if (!state.wakeAwake && !state.wakeSpeaking) {
      setWakeBannerState("standby", '🟢 "Hey Vera" nöbeti aktif — "Hey Vera" diyerek seslenebilirsiniz.');
    }
  } catch {
    state.wakeRec = null;
  }
}

function updateHudControls() {
  const wwBtn = $("#btn-hud-wakeword");
  if (wwBtn) {
    wwBtn.textContent = state.wakeEnabled ? '📡 "Hey Vera": AÇIK' : '📡 "Hey Vera": KAPALI';
    wwBtn.classList.toggle("active", state.wakeEnabled);
  }
  const avBtn = $("#btn-hud-autovoice");
  if (avBtn) {
    avBtn.textContent = state.autoVoice ? "🔊 Oto-Ses: AÇIK" : "🔇 Oto-Ses: KAPALI";
    avBtn.classList.toggle("active", state.autoVoice);
  }
  const vsBtn = $("#btn-hud-voice-select");
  if (vsBtn) {
    const isJarvis = state.ttsVoice.includes("Ahmet");
    vsBtn.textContent = isJarvis ? "🗣️ Ses: JARVIS (Erkek)" : "🗣️ Ses: VERA (Kadın)";
  }
}

$("#btn-hud-wakeword")?.addEventListener("click", () => {
  state.wakeEnabled = !state.wakeEnabled;
  localStorage.setItem("vera_wake_enabled", String(state.wakeEnabled));
  if (!state.wakeEnabled) {
    state.wakeAwake = false;
    if (state.wakeRec) { try { state.wakeRec.abort(); } catch {} state.wakeRec = null; }
    setWakeBannerState("standby", '⚪ "Hey Vera" sesli nöbeti kapatıldı.');
  } else {
    ensureWakeRecognizerRunning();
  }
  updateHudControls();
});

$("#wake-status-banner")?.addEventListener("click", () => {
  if (state.currentAudio) {
    try { state.currentAudio.pause(); } catch {}
    state.currentAudio = null;
    state.wakeSpeaking = false;
  }
  state.wakeEnabled = true;
  state.wakeAwake = true;
  updateHudControls();
  playJarvisChime();
  setWakeBannerState("awake", "🎙️ VERA DİNLİYOR — Buyurun Yusuf Bey, komutunuzu söyleyin...");
  armWakeSleepTimer(30000);
  ensureWakeRecognizerRunning();
});

document.addEventListener("click", () => {
  if (state.wakeEnabled && !state.wakeRec && !state.wakeSpeaking && !state.wakeBusy) {
    ensureWakeRecognizerRunning();
  }
}, { passive: true });

$("#btn-hud-autovoice")?.addEventListener("click", () => {
  state.autoVoice = !state.autoVoice;
  localStorage.setItem("vera_autovoice", String(state.autoVoice));
  if (!state.autoVoice && state.currentAudio) {
    try { state.currentAudio.pause(); } catch {}
  }
  updateHudControls();
});

$("#btn-hud-voice-select")?.addEventListener("click", () => {
  state.ttsVoice = state.ttsVoice.includes("Ahmet") ? "tr-TR-EmelNeural" : "tr-TR-AhmetNeural";
  localStorage.setItem("vera_tts_voice", state.ttsVoice);
  updateHudControls();
});

setInterval(() => {
  const el = $("#hud-clock");
  if (el) {
    const now = new Date().toLocaleTimeString("tr-TR", { timeZone: "Europe/Istanbul" });
    el.textContent = `SİSTEM AKTİF • İSTANBUL ${now}`;
  }
}, 1000);

$$(".tactical-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const cmd = chip.dataset.cmd;
    if (!cmd) return;
    $("#chat-input").value = cmd;
    $("#chat-form").requestSubmit();
  });
});

async function loadChatHistory() {
  updateHudControls();
  ensureWakeRecognizerRunning();
  const data = await api("/api/chat/history?session_id=web_main");
  const box = $("#chat-messages");
  box.innerHTML = "";
  if (location.search.includes("google=connected")) {
    appendMessage("assistant", "✅ Google Workspace (Gmail, Takvim, Drive, Sheets, Docs) bağlantısı başarıyla doğrulandı ve kalıcı bulut hafızasına kaydedildi, Yusuf Bey. Emrinizdeyim!");
    history.replaceState({}, "", "/");
  }
  if (!data.messages.length) {
    appendMessage("assistant", "Sistemler çevrimiçi, Yusuf Bey. V.E.R.A. // J.A.R.V.I.S. Taktiksel Komuta Merkezi 7/24 emrinizde.");
    return;
  }
  data.messages.forEach((m) => appendMessage(m.role, m.content));
}

$("#chat-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (file) {
    state.selectedFile = file;
    $("#file-chip").textContent = file.name;
    $("#file-chip").classList.remove("hidden");
  }
});

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text && !state.selectedFile) return;

  const userDisplay = state.selectedFile ? `[Dosya: ${state.selectedFile.name}] ${text}` : text;
  appendMessage("user", userDisplay);
  input.value = "";

  const thinking = document.createElement("div");
  thinking.className = "msg assistant muted";
  thinking.textContent = "VERA düşünüyor ve araçları kontrol ediyor...";
  $("#chat-messages").appendChild(thinking);
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;

  try {
    let data;
    if (state.selectedFile) {
      const fd = new FormData();
      fd.append("file", state.selectedFile);
      fd.append("message", text || "Bu dosyayı detaylı analiz et ve özetle.");
      fd.append("session_id", "web_main");
      data = await api("/api/chat/upload", { method: "POST", body: fd });
      state.selectedFile = null;
      $("#chat-file").value = "";
      $("#file-chip").classList.add("hidden");
    } else {
      data = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({ message: text, session_id: "web_main" }),
      });
    }
    thinking.remove();
    const meta = data.tools_used?.length ? `Araçlar: ${data.tools_used.join(", ")}` : (data.model || "VERA");
    appendMessage("assistant", data.reply, meta);
    if (state.autoVoice) playTTS(data.reply);
    await loadApprovals();
  } catch (err) {
    thinking.textContent = "Hata: " + err.message;
  }
});

$("#btn-run-briefing-chat").addEventListener("click", async () => {
  appendMessage("user", "☀️ Sabah Brifingimi getir");
  const data = await api("/api/briefing/run", { method: "POST" });
  appendMessage("assistant", data.briefing, "Sabah Brifingi");
});

/* --- Chat Voice Command Button (🎙️) --- */
let chatRec = null;
$("#btn-chat-mic").addEventListener("click", () => {
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) {
    alert("Tarayıcınız doğrudan ses tanımayı desteklemiyor; Canlı Ses sekmesini kullanabilirsiniz.");
    return;
  }
  if (chatRec) {
    try { chatRec.stop(); } catch {}
    chatRec = null;
    $("#btn-chat-mic").textContent = "🎙️";
    return;
  }
  chatRec = new SpeechRec();
  chatRec.lang = "tr-TR";
  chatRec.interimResults = false;
  $("#btn-chat-mic").textContent = "🔴";
  chatRec.onresult = async (ev) => {
    const spoken = ev.results[0][0].transcript.trim();
    $("#btn-chat-mic").textContent = "🎙️";
    chatRec = null;
    if (!spoken) return;
    $("#chat-input").value = spoken;
    appendMessage("user", `🎙️ ${spoken}`);
    $("#chat-input").value = "";
    const thinking = document.createElement("div");
    thinking.className = "msg assistant muted";
    thinking.textContent = "VERA sesli komutunu işliyor...";
    $("#chat-messages").appendChild(thinking);
    try {
      const data = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({ message: spoken, session_id: "web_main" }),
      });
      thinking.remove();
      const meta = data.tools_used?.length ? `Araçlar: ${data.tools_used.join(", ")}` : (data.model || "VERA");
      appendMessage("assistant", data.reply, meta);
      playTTS(data.reply);
      await loadApprovals();
    } catch (err) {
      thinking.textContent = "Hata: " + err.message;
    }
  };
  chatRec.onerror = () => { $("#btn-chat-mic").textContent = "🎙️"; chatRec = null; };
  chatRec.onend = () => { $("#btn-chat-mic").textContent = "🎙️"; chatRec = null; };
  chatRec.start();
});

/* --- Approvals --- */
async function loadApprovals() {
  const data = await api("/api/approvals?status_filter=pending");
  const banner = $("#approvals-banner");
  if (!data.approvals.length) {
    banner.classList.add("hidden");
    banner.innerHTML = "";
    return;
  }
  banner.classList.remove("hidden");
  banner.innerHTML = "<strong>⚠️ Onay Bekleyen Kritik İşlemler</strong>";
  data.approvals.forEach((ap) => {
    const row = document.createElement("div");
    row.className = "approval-row";
    row.innerHTML = `<span>${ap.summary}</span>`;
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const okBtn = document.createElement("button");
    okBtn.className = "btn-success";
    okBtn.textContent = "✅ Onayla";
    okBtn.onclick = async () => {
      const res = await api(`/api/approvals/${ap.id}`, {
        method: "POST",
        body: JSON.stringify({ approved: true }),
      });
      appendMessage("assistant", `✅ ${res.message}`);
      loadApprovals();
    };
    const noBtn = document.createElement("button");
    noBtn.className = "btn-danger";
    noBtn.textContent = "❌ İptal";
    noBtn.onclick = async () => {
      await api(`/api/approvals/${ap.id}`, {
        method: "POST",
        body: JSON.stringify({ approved: false }),
      });
      loadApprovals();
    };
    actions.append(okBtn, noBtn);
    row.appendChild(actions);
    banner.appendChild(row);
  });
}

/* --- LIVE VOICE (GEMINI LIVE WEBSOCKET + 16kHz/24kHz PCM AUDIO) --- */
function downsampleTo16k(float32, inputRate) {
  if (inputRate === 16000) return float32;
  const ratio = inputRate / 16000;
  const newLen = Math.round(float32.length / ratio);
  const out = new Float32Array(newLen);
  for (let i = 0; i < newLen; i++) {
    out[i] = float32[Math.min(float32.length - 1, Math.round(i * ratio))];
  }
  return out;
}

function floatToPcm16Base64(float32) {
  const buf = new ArrayBuffer(float32.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < float32.length; i++) {
    let s = Math.max(-1, Math.min(1, float32[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  let binary = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function playPcm24kBase64(b64) {
  if (!state.playCtx) state.playCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
  const binary = atob(b64);
  const len = binary.length / 2;
  const audioBuf = state.playCtx.createBuffer(1, len, 24000);
  const channel = audioBuf.getChannelData(0);
  for (let i = 0; i < len; i++) {
    const lo = binary.charCodeAt(i * 2);
    const hi = binary.charCodeAt(i * 2 + 1);
    let val = (hi << 8) | lo;
    if (val >= 0x8000) val -= 0x10000;
    channel[i] = val / 32768.0;
  }
  const src = state.playCtx.createBufferSource();
  src.buffer = audioBuf;
  src.connect(state.playCtx.destination);
  const now = state.playCtx.currentTime;
  const startAt = Math.max(now, state.nextPlayTime);
  src.start(startAt);
  state.nextPlayTime = startAt + audioBuf.duration;
  state.activeSources.push(src);
  $("#voice-orb").classList.add("speaking");
  src.onended = () => {
    state.activeSources = state.activeSources.filter((s) => s !== src);
    if (!state.activeSources.length) $("#voice-orb").classList.remove("speaking");
  };
}

function clearPlaybackQueue() {
  state.activeSources.forEach((s) => { try { s.stop(); } catch {} });
  state.activeSources = [];
  state.nextPlayTime = 0;
  $("#voice-orb").classList.remove("speaking");
}

function addVoiceTranscript(role, text) {
  const el = $("#voice-transcript");
  const last = el.lastElementChild;
  if (last && last.dataset.role === role) {
    last.textContent += text;
  } else {
    const p = document.createElement("div");
    p.dataset.role = role;
    p.className = role === "user" ? "muted" : "";
    p.textContent = (role === "user" ? "Sen: " : "VERA: ") + text;
    el.appendChild(p);
  }
  el.scrollTop = el.scrollHeight;
}

async function startLiveVoice() {
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  $("#voice-transcript").innerHTML = "";
  state.liveActive = true;
  $("#voice-orb").classList.add("active");
  $("#btn-voice-toggle").textContent = "🔴 Görüşmeyi Bitir";
  $("#btn-voice-toggle").className = "btn-danger large";

  if (SpeechRec) {
    const listenTurn = () => {
      if (!state.liveActive) return;
      $("#voice-status").textContent = "🎙️ VERA seni dinliyor — doğal Türkçe konuşabilirsin";
      const rec = new SpeechRec();
      state.liveRec = rec;
      rec.lang = "tr-TR";
      rec.interimResults = true;
      rec.onresult = async (ev) => {
        const lastRes = ev.results[ev.results.length - 1];
        if (!lastRes) return;
        const text = (lastRes[0]?.transcript || "").trim();
        if (!text || !state.liveActive) return;
        if (state.wakeSpeaking) {
          if (!isLikelySpeakerEcho(text, state.currentSpokenText) && text.length >= 3) {
            interruptVeraSpeech();
            $("#voice-status").textContent = "🎙️ Sözünüzü dinliyorum Yusuf Bey...";
          } else {
            return;
          }
        }
        if (!lastRes.isFinal) return;
        addVoiceTranscript("user", text);
        $("#voice-status").textContent = "⚡ VERA düşünüyor ve araçları çalıştırıyor...";
        try {
          const data = await api("/api/chat", {
            method: "POST",
            body: JSON.stringify({ message: text, session_id: "live_voice" }),
          });
          if (!state.liveActive) return;
          if (data.tools_used?.length) {
            const pill = $("#voice-tool-pill");
            pill.textContent = `⚡ Kullanılan araçlar: ${data.tools_used.join(", ")}`;
            pill.classList.remove("hidden");
          }
          addVoiceTranscript("assistant", data.reply);
          $("#voice-status").textContent = "🔊 VERA konuşuyor... (Konuşmaya başlarsanız anında susup sizi dinler)";
          $("#voice-orb").classList.add("speaking");
          listenTurn();
          await playTTS(data.reply, () => {
            $("#voice-orb").classList.remove("speaking");
            if (state.liveActive) {
              $("#voice-status").textContent = "🎙️ VERA seni dinliyor — doğal Türkçe konuşabilirsin";
            }
          });
        } catch (err) {
          $("#voice-status").textContent = "Hata: " + err.message;
          if (state.liveActive) setTimeout(listenTurn, 1500);
        }
      };
      rec.onerror = (ev) => {
        if (state.liveActive && ev.error !== "aborted") setTimeout(listenTurn, 800);
      };
      rec.onend = () => {
        if (state.liveActive && !$("#voice-orb").classList.contains("speaking") && $("#voice-status").textContent.includes("dinliyor")) {
          try { rec.start(); } catch {}
        }
      };
      try { rec.start(); } catch {}
    };
    listenTurn();
    return;
  }

  $("#voice-status").textContent = "Tarayıcı ses tanıma desteklemiyor; lütfen Chrome veya Edge kullanın.";
}

function stopLiveVoice(resetStatus = true) {
  state.liveActive = false;
  clearPlaybackQueue();
  if (state.liveRec) { try { state.liveRec.abort(); } catch {} state.liveRec = null; }
  if (state.liveAudio) { try { state.liveAudio.pause(); } catch {} state.liveAudio = null; }
  if (state.liveWs) { try { state.liveWs.close(); } catch {} state.liveWs = null; }
  if (state.processor) { try { state.processor.disconnect(); } catch {} state.processor = null; }
  if (state.captureCtx) { try { state.captureCtx.close(); } catch {} state.captureCtx = null; }
  if (state.micStream) { state.micStream.getTracks().forEach((t) => t.stop()); state.micStream = null; }
  $("#voice-orb").classList.remove("active", "speaking");
  $("#voice-tool-pill").classList.add("hidden");
  $("#btn-voice-toggle").textContent = "📞 Canlı Görüşmeyi Başlat";
  $("#btn-voice-toggle").className = "btn-primary large";
  if (resetStatus) $("#voice-status").textContent = "Görüşme sonlandırıldı. Tekrar konuşmak için dokun.";
}

$("#btn-voice-toggle").addEventListener("click", () => {
  if (state.liveActive) stopLiveVoice();
  else startLiveVoice();
});

/* --- Hermes Memory --- */
async function loadMemoryTab() {
  state.docs = await api("/api/memory/docs");
  $("#memory-editor").value = state.docs[state.activeDoc] || "";
  await loadMemoryEntries("");
}

$$(".doc-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".doc-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.activeDoc = btn.dataset.doc;
    $("#memory-editor").value = state.docs[state.activeDoc] || "";
  });
});

$("#btn-save-doc").addEventListener("click", async () => {
  const content = $("#memory-editor").value;
  await api(`/api/memory/docs/${state.activeDoc}`, {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
  state.docs[state.activeDoc] = content;
  $("#memory-save-status").textContent = `${state.activeDoc} kaydedildi!`;
  setTimeout(() => ($("#memory-save-status").textContent = ""), 2500);
});

async function loadMemoryEntries(query) {
  const data = await api(`/api/memory/entries?q=${encodeURIComponent(query)}`);
  const box = $("#memory-entries-list");
  box.innerHTML = "";
  if (!data.items.length) {
    box.innerHTML = '<div class="muted">Kayıtlı anı bulunamadı.</div>';
    return;
  }
  data.items.forEach((m) => {
    const item = document.createElement("div");
    item.className = "list-item";
    item.innerHTML = `
      <div>
        <div class="list-item-title">${m.content}</div>
        <div class="list-item-sub">${m.category} • önem:${m.importance} • ${(m.created_at || "").slice(0, 16)}</div>
      </div>
    `;
    const del = document.createElement("button");
    del.className = "btn-ghost";
    del.textContent = "🗑️";
    del.onclick = async () => {
      await api(`/api/memory/entries/${m.id}`, { method: "DELETE" });
      loadMemoryEntries($("#memory-search-input").value);
    };
    item.appendChild(del);
    box.appendChild(item);
  });
}

$("#btn-memory-search").addEventListener("click", () => loadMemoryEntries($("#memory-search-input").value));
$("#btn-dream").addEventListener("click", async () => {
  $("#btn-dream").disabled = true;
  $("#btn-dream").textContent = "🌙 İşleniyor...";
  try {
    const res = await api("/api/memory/dream", { method: "POST" });
    alert("Dream Konsolidasyonu: " + (res.summary || res.reason || res.status));
    await loadMemoryTab();
  } finally {
    $("#btn-dream").disabled = false;
    $("#btn-dream").textContent = "🌙 Dream Konsolidasyonu";
  }
});

/* --- Jobs & Watchers --- */
async function loadJobsTab() {
  const data = await api("/api/jobs");
  const jBox = $("#jobs-list");
  jBox.innerHTML = "";
  data.jobs.forEach((j) => {
    const el = document.createElement("div");
    el.className = "list-item";
    el.innerHTML = `
      <div>
        <div class="list-item-title">${j.name} <span class="badge">${j.schedule_expr}</span></div>
        <div class="list-item-sub">${j.job_type} • Son çalışma: ${j.last_run_at ? j.last_run_at.slice(0, 16) : "Henüz"}</div>
      </div>
    `;
    const del = document.createElement("button");
    del.className = "btn-ghost";
    del.textContent = "🗑️";
    del.onclick = async () => { await api(`/api/jobs/${j.id}`, { method: "DELETE" }); loadJobsTab(); };
    el.appendChild(del);
    jBox.appendChild(el);
  });

  const wBox = $("#watchers-list");
  wBox.innerHTML = "";
  data.watchers.forEach((w) => {
    const el = document.createElement("div");
    el.className = "list-item";
    el.innerHTML = `
      <div>
        <div class="list-item-title">${w.name} <span class="badge">${w.interval_minutes} dk</span></div>
        <div class="list-item-sub">${w.url}<br/>${w.instruction}</div>
      </div>
    `;
    const del = document.createElement("button");
    del.className = "btn-ghost";
    del.textContent = "🗑️";
    del.onclick = async () => { await api(`/api/watchers/${w.id}`, { method: "DELETE" }); loadJobsTab(); };
    el.appendChild(del);
    wBox.appendChild(el);
  });
}

$("#job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/jobs", {
    method: "POST",
    body: JSON.stringify({
      name: $("#job-name").value,
      job_type: "agent_cron",
      schedule_expr: $("#job-cron").value,
      prompt: $("#job-prompt").value,
      send_voice: $("#job-voice").checked,
    }),
  });
  e.target.reset();
  loadJobsTab();
});

$("#watcher-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/watchers", {
    method: "POST",
    body: JSON.stringify({
      name: $("#watch-name").value,
      url: $("#watch-url").value,
      instruction: $("#watch-inst").value,
      interval_minutes: Number($("#watch-interval").value || 60),
    }),
  });
  e.target.reset();
  loadJobsTab();
});

/* --- Skills & Tools --- */
async function loadSkillsTab() {
  const data = await api("/api/skills");
  const sBox = $("#skills-list");
  sBox.innerHTML = "";
  data.skills.forEach((s) => {
    const el = document.createElement("div");
    el.className = "list-item";
    el.innerHTML = `<div><div class="list-item-title">${s.name} <span class="badge">${s.slug}</span></div><div class="list-item-sub">${s.description}</div></div>`;
    sBox.appendChild(el);
  });

  const tBox = $("#tools-list");
  tBox.innerHTML = "";
  data.tools.forEach((t) => {
    const el = document.createElement("div");
    el.className = "list-item";
    el.innerHTML = `
      <div>
        <div class="list-item-title">${t.name} <span class="badge">${t.category}</span> ${t.requires_approval ? '<span class="badge warn">Onaylı</span>' : ""}</div>
        <div class="list-item-sub">${t.description}</div>
      </div>
    `;
    const toggle = document.createElement("button");
    toggle.className = t.enabled ? "btn-success" : "btn-ghost";
    toggle.textContent = t.enabled ? "Aktif" : "Kapalı";
    toggle.onclick = async () => {
      await api(`/api/tools/${t.name}/toggle?enabled=${!t.enabled}`, { method: "POST" });
      loadSkillsTab();
    };
    el.appendChild(toggle);
    tBox.appendChild(el);
  });
}

$("#skill-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/skills", {
    method: "POST",
    body: JSON.stringify({
      slug: $("#skill-slug").value,
      name: $("#skill-name").value,
      description: $("#skill-desc").value,
      instructions: $("#skill-inst").value,
    }),
  });
  e.target.reset();
  loadSkillsTab();
});

/* --- System & Google Workspace --- */
async function loadSystemStatus() {
  const [st, logData] = await Promise.all([api("/api/system/status"), api("/api/logs?limit=25")]);
  const h = st.health;
  $("#mini-ram").textContent = `RAM: ${Math.round(h.ram_used_mb)}/${Math.round(h.ram_total_mb)} MB`;
  if ($("#hud-cpu")) $("#hud-cpu").textContent = `CPU: %${h.cpu_percent}`;
  if ($("#hud-ram")) $("#hud-ram").textContent = `RAM: %${h.ram_percent}`;
  if ($("#hud-google")) {
    $("#hud-google").textContent = st.integrations.google_connected ? "GOOGLE: BAĞLI 🟢" : "GOOGLE: BEKLİYOR 🟡";
  }

  $("#health-cards").innerHTML = `
    <div class="stat-card glass"><div class="stat-label">CPU</div><div class="stat-value">%${h.cpu_percent}</div><div class="progress-bar"><div class="progress-fill" style="width:${h.cpu_percent}%"></div></div></div>
    <div class="stat-card glass"><div class="stat-label">RAM</div><div class="stat-value">${Math.round(h.ram_used_mb)} MB</div><div class="muted">/ ${Math.round(h.ram_total_mb)} MB (%${h.ram_percent})</div><div class="progress-bar"><div class="progress-fill" style="width:${h.ram_percent}%"></div></div></div>
    <div class="stat-card glass"><div class="stat-label">Swap</div><div class="stat-value">${Math.round(h.swap_used_mb)} MB</div><div class="muted">/ ${Math.round(h.swap_total_mb)} MB</div></div>
    <div class="stat-card glass"><div class="stat-label">Disk</div><div class="stat-value">${h.disk_used_gb} GB</div><div class="muted">/ ${h.disk_total_gb} GB (%${h.disk_percent})</div></div>
    <div class="stat-card glass"><div class="stat-label">Hafıza & Mesaj</div><div class="stat-value">${st.stats.memories_count} / ${st.stats.messages_count}</div><div class="muted">Aktif görev: ${st.stats.active_jobs_count}</div></div>
  `;

  const integ = st.integrations;
  const rows = [
    ["Telegram 7/24 Bot", integ.telegram_running],
    ["Google Gemini Beyin & Canlı Ses", integ.gemini_configured],
    ["Groq Whisper Türkçe Ses", integ.groq_configured],
    ["OpenRouter Yedek Model", integ.openrouter_configured],
    ["Google Workspace (Gmail/Takvim/Drive)", integ.google_connected],
    [`Bulut Hafıza Yedeği (${integ.cloud_backup_status || "-"})`, integ.cloud_backup_enabled],
  ];
  $("#integrations-list").innerHTML = rows
    .map(([label, ok]) => `<div class="list-item"><span class="list-item-title">${label}</span><span class="badge ${ok ? "ok" : "warn"}">${ok ? "Bağlı" : "Yapılandırılmadı"}</span></div>`)
    .join("");

  $("#logs-list").innerHTML = logData.logs
    .map((l) => `<div class="list-item"><div><div class="list-item-title">${l.event_type}</div><div class="list-item-sub">${l.detail}</div></div><span class="muted">${(l.created_at || "").slice(11, 19)}</span></div>`)
    .join("");
}

$("#btn-refresh-system").addEventListener("click", loadSystemStatus);
$("#btn-google-connect").addEventListener("click", async () => {
  try {
    const res = await api("/api/google/connect");
    window.location.href = res.auth_url;
  } catch (err) {
    alert(err.message);
  }
});

initApp();
