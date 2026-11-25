/* V.E.R.A. — Evrensel Mobil (Edge/Chrome/Safari) & Masaüstü Tam Ekran Ses Çekirdeği (v6) */

const isMobile = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent) || (navigator.maxTouchPoints > 1 && window.innerWidth < 960);

const state = {
  token: localStorage.getItem("vera_token") || "",
  ttsVoice: localStorage.getItem("vera_tts_voice") || "tr-TR-AhmetNeural",
  awake: true,
  busy: false,
  speaking: false,
  audioUnlocked: false,
  useVadRecorder: isMobile, // Mobilde bozuk webkitSpeechRecognition yerine kesintisiz WebAudio VAD + MediaRecorder kullan!
  rec: null,
  micStream: null,
  audioCtx: null,
  analyser: null,
  vadInterval: null,
  mediaRec: null,
  mediaChunks: [],
  isRecordingClip: false,
  speechStartAt: 0,
  lastVoiceAt: 0,
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
  }
});

function unlockMobileAudio() {
  requestPhoneWakeLock();
  if (!state.audioCtx) {
    try {
      state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    } catch {}
  }
  if (state.audioCtx && state.audioCtx.state === "suspended") {
    state.audioCtx.resume().catch(() => {});
  }
  if (state.audioUnlocked) return;
  state.audioUnlocked = true;
  const player = $("#jarvis-player");
  if (player) {
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
    if (!state.audioCtx) state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const ctx = state.audioCtx;
    if (ctx.state === "suspended") ctx.resume();
    const now = ctx.currentTime;
    [660, 990].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const g = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, now + i * 0.055);
      g.gain.setValueAtTime(0.1, now + i * 0.055);
      g.gain.exponentialRampToValueAtTime(0.001, now + i * 0.055 + 0.11);
      osc.connect(g);
      g.connect(ctx.destination);
      osc.start(now + i * 0.055);
      osc.stop(now + i * 0.055 + 0.12);
    });
  } catch {}
}

function interruptSpeech() {
  state.ttsGenId += 1;
  const player = $("#jarvis-player");
  if (player) {
    try { player.pause(); } catch {}
  }
  if (window.speechSynthesis) {
    try { window.speechSynthesis.cancel(); } catch {}
  }
  state.speaking = false;
  state.currentSpokenText = "";
  state.awake = true;
  setCoreVisual("awake", "DİNLİYORUM YUSUF BEY — KONUŞABİLİRSİNİZ");
}

async function playAudioBase64OrFetch(text, audioB64 = "") {
  const genId = ++state.ttsGenId;
  const player = $("#jarvis-player");
  if (player) {
    try { player.pause(); } catch {}
  }

  const clean = (text || "").trim();
  if (!clean) return;

  state.speaking = true;
  state.currentSpokenText = clean;
  setCoreVisual("speaking", "V.E.R.A. KONUŞUYOR (KESMEK İÇİN KONUŞUN VEYA DOKUNUN)");

  try {
    let playUrl = "";
    let revokeAfter = false;

    if (audioB64) {
      playUrl = `data:audio/mpeg;base64,${audioB64}`;
    } else {
      const resp = await api("/api/voice/tts", {
        method: "POST",
        body: JSON.stringify({ message: clean, voice: state.ttsVoice }),
      });
      if (state.ttsGenId !== genId) return;
      const blob = await resp.blob();
      if (state.ttsGenId !== genId) return;
      playUrl = URL.createObjectURL(blob);
      revokeAfter = true;
    }

    await new Promise((resolve) => {
      if (state.ttsGenId !== genId) return resolve();
      const audioEl = player || new Audio();
      audioEl.src = playUrl;
      const done = () => {
        if (revokeAfter) URL.revokeObjectURL(playUrl);
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
      const dial = $("#reactor-core");
      if (dial) dial.style.transform = "";
      setCoreVisual("awake", "DİNLİYORUM YUSUF BEY — KONUŞABİLİRSİNİZ");
      if (!state.useVadRecorder) ensureDesktopSpeechRec();
    }
  }
}

/* ============================================================================
   ENGINE A: MOBILE & UNIVERSAL WEB-AUDIO VAD + MEDIARECORDER (Zero Beep / Edge Compatible)
   ============================================================================ */
function getSupportedMimeType() {
  const types = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  for (const t of types) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) {
      return t;
    }
  }
  return "";
}

async function startMobileVadEngine() {
  if (state.micStream) {
    setCoreVisual("awake", "DİNLİYORUM YUSUF BEY — DOĞRUDAN KONUŞUN");
    return;
  }
  try {
    state.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    if (!state.audioCtx) {
      state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (state.audioCtx.state === "suspended") {
      await state.audioCtx.resume();
    }
    const source = state.audioCtx.createMediaStreamSource(state.micStream);
    state.analyser = state.audioCtx.createAnalyser();
    state.analyser.fftSize = 512;
    source.connect(state.analyser);

    const dataArray = new Uint8Array(state.analyser.fftSize);
    setCoreVisual("awake", "DİNLİYORUM YUSUF BEY — DOĞRUDAN KONUŞUN");

    if (state.vadInterval) clearInterval(state.vadInterval);
    state.vadInterval = setInterval(() => {
      if (!state.analyser || state.busy) return;
      state.analyser.getByteTimeDomainData(dataArray);
      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) {
        const v = (dataArray[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / dataArray.length);
      const now = Date.now();

      // Subtle visual ring scale reacting to user's voice on mobile
      const dial = $("#reactor-core");
      if (dial && !state.speaking && !state.busy) {
        const scale = 1 + Math.min(0.14, rms * 1.1);
        dial.style.transform = `scale(${scale.toFixed(3)})`;
      }

      // If V.E.R.A. is speaking and user speaks clearly into mic -> Barge-In!
      if (state.speaking) {
        if (rms > 0.11) {
          interruptSpeech();
          beginClipRecording();
        }
        return;
      }

      // Hands-free Voice Activity Detection
      const SPEECH_THRESHOLD = 0.026;
      const SILENCE_HOLD_MS = 520;
      const MAX_CLIP_MS = 14000;

      if (rms > SPEECH_THRESHOLD) {
        state.lastVoiceAt = now;
        if (!state.isRecordingClip) {
          beginClipRecording();
        }
      } else if (state.isRecordingClip) {
        if (now - state.lastVoiceAt > SILENCE_HOLD_MS || now - state.speechStartAt > MAX_CLIP_MS) {
          finishClipRecording();
        }
      }
    }, 45);
  } catch (err) {
    setCoreVisual("", "MİKROFON İZNİ GEREKLİ: " + err.message);
  }
}

function beginClipRecording() {
  if (!state.micStream || state.isRecordingClip || state.busy) return;
  try {
    state.mediaChunks = [];
    const mime = getSupportedMimeType();
    let mr;
    try {
      mr = mime
        ? new MediaRecorder(state.micStream, { mimeType: mime, audioBitsPerSecond: 24000 })
        : new MediaRecorder(state.micStream, { audioBitsPerSecond: 24000 });
    } catch {
      mr = mime ? new MediaRecorder(state.micStream, { mimeType: mime }) : new MediaRecorder(state.micStream);
    }
    state.mediaRec = mr;
    state.isRecordingClip = true;
    state.speechStartAt = Date.now();
    state.lastVoiceAt = Date.now();
    setCoreVisual("awake", "SESİNİZ ALGILANDI — DİNLİYORUM...");

    mr.ondataavailable = (ev) => {
      if (ev.data && ev.data.size > 0) state.mediaChunks.push(ev.data);
    };

    mr.onstop = async () => {
      state.isRecordingClip = false;
      const dial = $("#reactor-core");
      if (dial) dial.style.transform = "";
      const dur = Date.now() - state.speechStartAt;
      const blob = new Blob(state.mediaChunks, { type: mr.mimeType || "audio/webm" });
      if (dur < 280 || blob.size < 600 || state.busy) {
        if (!state.busy && !state.speaking) {
          setCoreVisual("awake", "DİNLİYORUM YUSUF BEY — DOĞRUDAN KONUŞUN");
        }
        return;
      }
      await sendAudioClipToVera(blob, mr.mimeType || "audio/webm");
    };

    mr.start();
  } catch {
    state.isRecordingClip = false;
  }
}

function finishClipRecording() {
  if (state.mediaRec && state.mediaRec.state === "recording") {
    try { state.mediaRec.stop(); } catch {}
  }
  state.isRecordingClip = false;
}

async function sendAudioClipToVera(blob, mimeType) {
  state.busy = true;
  setCoreVisual("busy", "İCRA EDİLİYOR...");
  $("#jarvis-heard").textContent = "Sesli komut işleniyor...";
  $("#jarvis-reply").textContent = "";

  try {
    const ext = mimeType.includes("mp4") ? "mp4" : mimeType.includes("ogg") ? "ogg" : "webm";
    const fd = new FormData();
    fd.append("file", blob, `voice.${ext}`);
    fd.append("session_id", "voice_jarvis");
    fd.append("voice", state.ttsVoice);

    const data = await api("/api/voice/converse", { method: "POST", body: fd });
    state.busy = false;
    if (data.transcript) {
      $("#jarvis-heard").textContent = `"${data.transcript}"`;
    } else {
      $("#jarvis-heard").textContent = "";
    }
    if (data.reply) {
      $("#jarvis-reply").textContent = data.reply;
      await playAudioBase64OrFetch(data.reply, data.audio_b64 || "");
    } else {
      setCoreVisual("awake", "DİNLİYORUM YUSUF BEY — DOĞRUDAN KONUŞUN");
    }
  } catch (err) {
    state.busy = false;
    $("#jarvis-reply").textContent = "Hata: " + err.message;
    setCoreVisual("awake", "DİNLİYORUM YUSUF BEY");
  }
}

/* ============================================================================
   ENGINE B: DESKTOP SPEECHRECOGNITION (With Auto-Fallback to Engine A)
   ============================================================================ */
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
  if (/\b(hey\s*vera|vera|jarvis|dur|bekle|sus|kes|iptal|hayır)\b/i.test(heard)) return false;
  const spoken = normalizeTr(spokenRaw);
  if (!spoken) return false;
  if (spoken.includes(heard)) return true;
  const words = heard.split(" ").filter((w) => w.length > 2);
  if (!words.length) return true;
  const hits = words.filter((w) => spoken.includes(w));
  return hits.length / words.length >= 0.5;
}

async function sendTextVoiceCommand(rawSpoken) {
  if (state.silenceTimer) {
    clearTimeout(state.silenceTimer);
    state.silenceTimer = null;
  }
  state.interimBuffer = "";
  const spoken = (rawSpoken || "").trim();
  if (!spoken || state.busy) return;

  const cmd = spoken.replace(/^(?:hey\s*vera|ey\s*vera|vera|hey\s*jarvis|jarvis)[\s,.:;!?-]*/i, "").trim() || spoken;
  state.busy = true;
  $("#jarvis-heard").textContent = `"${cmd}"`;
  $("#jarvis-reply").textContent = "";
  setCoreVisual("busy", "İCRA EDİLİYOR...");

  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        message: cmd,
        session_id: "voice_jarvis",
        voice: state.ttsVoice,
        include_audio: true,
      }),
    });
    state.busy = false;
    $("#jarvis-reply").textContent = data.reply;
    await playAudioBase64OrFetch(data.reply, data.audio_b64 || "");
  } catch (err) {
    state.busy = false;
    $("#jarvis-reply").textContent = "Hata: " + err.message;
    setCoreVisual("awake", "DİNLİYORUM YUSUF BEY");
    ensureDesktopSpeechRec();
  }
}

function ensureDesktopSpeechRec() {
  if (state.useVadRecorder) {
    startMobileVadEngine();
    return;
  }
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) {
    state.useVadRecorder = true;
    startMobileVadEngine();
    return;
  }
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
        sendTextVoiceCommand(text);
      } else {
        state.interimBuffer = text;
        if (state.silenceTimer) clearTimeout(state.silenceTimer);
        state.silenceTimer = setTimeout(() => {
          const pending = state.interimBuffer;
          if (pending && !state.busy && !state.speaking) {
            try { rec.stop(); } catch {}
            sendTextVoiceCommand(pending);
          }
        }, 420);
      }
    };

    rec.onerror = (ev) => {
      state.rec = null;
      if (ev.error === "service-not-allowed" || ev.error === "network" || ev.error === "language-not-supported") {
        // Auto-switch to universal WebAudio VAD + MediaRecorder if browser SpeechRecognition service fails!
        state.useVadRecorder = true;
        startMobileVadEngine();
        return;
      }
      if (ev.error === "not-allowed") {
        setCoreVisual("", "MİKROFONU AÇMAK İÇİN HALKAYA DOKUNUN");
        return;
      }
      if (!state.busy) setTimeout(ensureDesktopSpeechRec, 350);
    };

    rec.onend = () => {
      state.rec = null;
      if (!state.busy && !state.useVadRecorder) setTimeout(ensureDesktopSpeechRec, 180);
    };

    rec.start();
    if (!state.speaking && !state.busy) {
      setCoreVisual("awake", "DİNLİYORUM YUSUF BEY — DOĞRUDAN KONUŞABİLİRSİNİZ");
    }
  } catch {
    state.rec = null;
    state.useVadRecorder = true;
    startMobileVadEngine();
  }
}

/* Tap Handler on the Full-Screen V.E.R.A. Interface */
$("#app-shell")?.addEventListener("click", () => {
  unlockMobileAudio();
  if (state.speaking) {
    interruptSpeech();
    return;
  }
  if (state.isRecordingClip) {
    // Tapping while speaking on mobile immediately commits the voice command!
    finishClipRecording();
    return;
  }
  playChime();
  if (state.useVadRecorder) {
    startMobileVadEngine();
  } else {
    ensureDesktopSpeechRec();
  }
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
    await bootVera();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

async function bootVera() {
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
    if (state.useVadRecorder) {
      setCoreVisual("", "MİKROFONU AÇMAK İÇİN V.E.R.A. HALKASINA BİR KEZ DOKUNUN");
    } else {
      ensureDesktopSpeechRec();
    }
  } catch {
    localStorage.removeItem("vera_token");
    $("#login-screen").classList.remove("hidden");
  }
}

bootVera();
