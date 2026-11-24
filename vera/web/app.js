/* J.A.R.V.I.S. // V.E.R.A. — Tam Ekran Holografik Ses Çekirdeği (Masaüstü & Mobil Edge/Chrome/Safari) */

const state = {
  token: localStorage.getItem("vera_token") || "",
  ttsVoice: localStorage.getItem("vera_tts_voice") || "tr-TR-AhmetNeural",
  awake: true,
  busy: false,
  speaking: false,
  audioUnlocked: false,
  rec: null,
  mediaRec: null,
  mediaChunks: [],
  ttsGenId: 0,
  currentSpokenText: "",
  interimBuffer: "",
  silenceTimer: null,
  wakeLock: null,
};

const $ = (sel) => document.querySelector(sel);

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  if (!(options.body instanceof FormData) && options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401) {
    localStorage.removeItem("vera_token");
    location.reload();
    throw new Error("Oturum süresi doldu.");
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || "İstek başarısız.");
  }
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("application/json") ? resp.json() : resp;
}

/* Keep Phone Screen Awake (Screen Wake Lock API) */
async function requestPhoneWakeLock() {
  if ("wakeLock" in navigator && !state.wakeLock) {
    try {
      state.wakeLock = await navigator.wakeLock.request("screen");
      state.wakeLock.addEventListener("release", () => { state.wakeLock = null; });
    } catch {}
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    requestPhoneWakeLock();
    if (state.audioUnlocked && !state.busy && !state.speaking) {
      ensureMicListening();
    }
  }
});

/* Unlock Mobile Browser Audio on First Tap */
function unlockMobileAudio() {
  if (state.audioUnlocked) return;
  state.audioUnlocked = true;
  requestPhoneWakeLock();
  const player = $("#jarvis-player");
  if (player) {
    // 0.01s silent WAV data URI unlocks persistent <audio> on iOS/Android
    player.src = "data:audio/wav;base64,UklGRigAAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA";
    player.play().catch(() => {});
  }
}

function setCoreVisual(mode, statusText) {
  const core = $("#reactor-core");
  const st = $("#jarvis-state");
  if (core) {
    core.classList.remove("awake", "busy", "speaking");
    if (mode) core.classList.add(mode);
  }
  if (st && statusText) st.textContent = statusText;
}

function playChime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const now = ctx.currentTime;
    [660, 990].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const g = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, now + i * 0.055);
      g.gain.setValueAtTime(0.11, now + i * 0.055);
      g.gain.exponentialRampToValueAtTime(0.001, now + i * 0.055 + 0.11);
      osc.connect(g);
      g.connect(ctx.destination);
      osc.start(now + i * 0.055);
      osc.stop(now + i * 0.055 + 0.12);
    });
  } catch {}
}

function normalizeTr(str) {
  return (str || "")
    .toLowerCase()
    .replace(/[^\wçğıöşü\s]/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function isEcho(heardRaw, spokenRaw) {
  const heard = normalizeTr(heardRaw);
  if (!heard) return true;
  if (/\b(hey\s*vera|vera|jarvis|dur|bekle|sus|kes|iptal|hayır)\b/i.test(heard)) {
    return false;
  }
  const spoken = normalizeTr(spokenRaw);
  if (!spoken) return false;
  if (spoken.includes(heard)) return true;
  const words = heard.split(" ").filter((w) => w.length > 2);
  if (!words.length) return true;
  const hits = words.filter((w) => spoken.includes(w));
  return hits.length / words.length >= 0.5;
}

function interruptSpeech() {
  state.ttsGenId += 1;
  const player = $("#jarvis-player");
  if (player) {
    try { player.pause(); player.removeAttribute("src"); } catch {}
  }
  if (window.speechSynthesis) {
    try { window.speechSynthesis.cancel(); } catch {}
  }
  state.speaking = false;
  state.currentSpokenText = "";
  state.awake = true;
  setCoreVisual("awake", "DİNLİYORUM YUSUF BEY");
}

async function speakFast(text) {
  const genId = ++state.ttsGenId;
  const player = $("#jarvis-player");
  if (player) {
    try { player.pause(); } catch {}
  }

  const clean = (text || "").trim();
  if (!clean) return;

  state.speaking = true;
  state.currentSpokenText = clean;
  setCoreVisual("speaking", "J.A.R.V.I.S. KONUŞUYOR");
  ensureMicListening();

  try {
    const resp = await api("/api/voice/tts", {
      method: "POST",
      body: JSON.stringify({ message: clean, voice: state.ttsVoice }),
    });
    if (state.ttsGenId !== genId) return;
    const blob = await resp.blob();
    if (state.ttsGenId !== genId) return;

    await new Promise((resolve) => {
      const url = URL.createObjectURL(blob);
      const audioEl = player || new Audio();
      audioEl.src = url;
      const done = () => {
        URL.revokeObjectURL(url);
        resolve();
      };
      audioEl.onended = done;
      audioEl.onerror = done;
      audioEl.play().catch(done);
    });
  } catch {
    if (window.speechSynthesis && state.ttsGenId === genId) {
      const u = new SpeechSynthesisUtterance(clean);
      u.lang = "tr-TR";
      u.rate = 1.15;
      window.speechSynthesis.speak(u);
    }
  } finally {
    if (state.ttsGenId === genId) {
      state.speaking = false;
      state.currentSpokenText = "";
      setCoreVisual(state.awake ? "awake" : "", state.awake ? "DİNLİYORUM YUSUF BEY" : '"HEY VERA" BEKLENİYOR');
      ensureMicListening();
    }
  }
}

async function processSpokenUtterance(rawSpoken) {
  if (state.silenceTimer) {
    clearTimeout(state.silenceTimer);
    state.silenceTimer = null;
  }
  state.interimBuffer = "";

  const spoken = (rawSpoken || "").trim();
  if (!spoken || state.busy) return;

  const wakeRegex = /\b(?:hey\s*vera|ey\s*vera|hei\s*vera|hey\s*bera|merhaba\s*vera|alo\s*vera|hey\s*jarvis|vera|jarvis)\b/i;

  if (!state.awake) {
    const match = spoken.match(wakeRegex);
    if (!match) return;
    playChime();
    state.awake = true;
    const afterWake = spoken.slice(match.index + match[0].length).replace(/^[\s,.:;!?-]+/, "").trim();
    if (afterWake.length < 3) {
      const greet = "Emrinizdeyim Yusuf Bey, sizi dinliyorum.";
      $("#jarvis-heard").textContent = `"${spoken}"`;
      $("#jarvis-reply").textContent = greet;
      await speakFast(greet);
      return;
    }
    await sendVoiceCommand(afterWake);
    return;
  }

  if (/^(uyku moduna geç|beklemeye geç|nöbete geç)\b/i.test(spoken)) {
    state.awake = false;
    const msg = "Nöbet moduna geçtim Efendim. Hey Vera diyerek uyandırabilirsiniz.";
    $("#jarvis-heard").textContent = `"${spoken}"`;
    $("#jarvis-reply").textContent = msg;
    await speakFast(msg);
    return;
  }

  const match = spoken.match(wakeRegex);
  if (match && spoken.replace(wakeRegex, "").trim().length < 2) {
    playChime();
    const greet = "Sizi dinliyorum Yusuf Bey, buyurun.";
    $("#jarvis-heard").textContent = `"${spoken}"`;
    $("#jarvis-reply").textContent = greet;
    await speakFast(greet);
    return;
  }

  const cmd = spoken.replace(/^(?:hey\s*vera|ey\s*vera|vera|hey\s*jarvis|jarvis)[\s,.:;!?-]*/i, "").trim() || spoken;
  await sendVoiceCommand(cmd);
}

async function sendVoiceCommand(cmd) {
  state.busy = true;
  $("#jarvis-heard").textContent = `"${cmd}"`;
  $("#jarvis-reply").textContent = "";
  setCoreVisual("busy", "İCRA EDİLİYOR...");

  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: cmd, session_id: "voice_jarvis" }),
    });
    state.busy = false;
    $("#jarvis-reply").textContent = data.reply;
    await speakFast(data.reply);
  } catch (err) {
    state.busy = false;
    $("#jarvis-reply").textContent = "Bağlantı hatası: " + err.message;
    setCoreVisual("awake", "DİNLİYORUM YUSUF BEY");
    ensureMicListening();
  }
}

/* Fallback Mobile MediaRecorder if Web Speech API is unavailable */
async function toggleMobileRecorderFallback() {
  if (state.mediaRec && state.mediaRec.state === "recording") {
    state.mediaRec.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    state.mediaChunks = [];
    const mr = new MediaRecorder(stream);
    state.mediaRec = mr;
    setCoreVisual("awake", "KONUŞUN — BİTİNCE HALKAYA DOKUNUN");
    mr.ondataavailable = (e) => { if (e.data.size > 0) state.mediaChunks.push(e.data); };
    mr.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(state.mediaChunks, { type: mr.mimeType || "audio/webm" });
      if (blob.size < 500) return;
      state.busy = true;
      setCoreVisual("busy", "SES ÇÖZÜMLENİYOR...");
      try {
        const fd = new FormData();
        fd.append("file", blob, "mobile_voice.webm");
        fd.append("session_id", "voice_jarvis");
        const data = await api("/api/voice/converse", { method: "POST", body: fd });
        state.busy = false;
        if (data.transcript) $("#jarvis-heard").textContent = `"${data.transcript}"`;
        if (data.reply) {
          $("#jarvis-reply").textContent = data.reply;
          await speakFast(data.reply);
        }
      } catch (err) {
        state.busy = false;
        setCoreVisual("awake", "HATA: " + err.message);
      }
    };
    mr.start();
  } catch (err) {
    setCoreVisual("", "MİKROFON İZNİ GEREKLİ: " + err.message);
  }
}

function ensureMicListening() {
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) return;
  if (state.busy || state.rec) return;

  try {
    const rec = new SpeechRec();
    state.rec = rec;
    rec.lang = "tr-TR";
    rec.continuous = true;
    rec.interimResults = true;

    rec.onresult = (ev) => {
      if (state.busy) return;
      const last = ev.results[ev.results.length - 1];
      if (!last) return;
      const text = (last[0]?.transcript || "").trim();
      if (!text) return;

      if (state.speaking) {
        if (!isEcho(text, state.currentSpokenText) && text.length >= 2) {
          interruptSpeech();
        } else {
          return;
        }
      }

      $("#jarvis-heard").textContent = `"${text}..."`;

      if (last.isFinal) {
        processSpokenUtterance(text);
      } else {
        state.interimBuffer = text;
        if (state.silenceTimer) clearTimeout(state.silenceTimer);
        state.silenceTimer = setTimeout(() => {
          const pending = state.interimBuffer;
          if (pending && !state.busy && !state.speaking) {
            try { rec.stop(); } catch {}
            processSpokenUtterance(pending);
          }
        }, 700);
      }
    };

    rec.onerror = (ev) => {
      state.rec = null;
      if (ev.error === "not-allowed") {
        setCoreVisual("", "MİKROFONU AÇMAK İÇİN HALKAYA DOKUNUN");
        return;
      }
      if (!state.busy) setTimeout(ensureMicListening, 350);
    };

    rec.onend = () => {
      state.rec = null;
      if (!state.busy) setTimeout(ensureMicListening, 180);
    };

    rec.start();
    if (!state.speaking && !state.busy) {
      setCoreVisual(state.awake ? "awake" : "", state.awake ? "DİNLİYORUM YUSUF BEY — DOĞRUDAN KONUŞABİLİRSİNİZ" : '"HEY VERA" BEKLENİYOR');
    }
  } catch {
    state.rec = null;
  }
}

/* Tap anywhere on the J.A.R.V.I.S. screen */
$("#app-shell")?.addEventListener("click", () => {
  unlockMobileAudio();
  if (state.speaking) {
    interruptSpeech();
    return;
  }
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) {
    toggleMobileRecorderFallback();
    return;
  }
  state.awake = true;
  playChime();
  setCoreVisual("awake", "DİNLİYORUM YUSUF BEY — DOĞRUDAN KONUŞABİLİRSİNİZ");
  ensureMicListening();
});

/* Login & Boot */
$("#login-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  unlockMobileAudio();
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
    await bootJarvis();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

async function bootJarvis() {
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
    ensureMicListening();
  } catch {
    localStorage.removeItem("vera_token");
    $("#login-screen").classList.remove("hidden");
  }
}

bootJarvis();
