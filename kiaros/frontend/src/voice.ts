/**
 * KIAROS voice I/O.
 *
 * Input:
 *   - Web (laptop): continuous Web Speech API recognition.
 *   - Web (phone browser): tap-to-talk with the Web Speech API.
 *   - Android app: tap-to-talk with the native speech recognizer (WebView has no Web Speech API).
 * Output:
 *   - MP3 from the server (Fish Audio) through an AudioContext + analyser for the orb.
 *   - Otherwise the device's own TTS (speechSynthesis, or the native TTS plugin in the app).
 */

import { SpeechRecognition as NativeSR } from "@capacitor-community/speech-recognition";
import { TextToSpeech } from "@capacitor-community/text-to-speech";
import { isNative } from "./config";

export const LANG = (() => {
  try {
    return localStorage.getItem("kiaros.lang") || navigator.language || "en-US";
  } catch {
    return navigator.language || "en-US";
  }
})();

// ---------------------------------------------------------------------------
// Speech recognition
// ---------------------------------------------------------------------------

export type ListenMode = "continuous" | "push";

export interface VoiceInput {
  readonly mode: ListenMode;
  readonly supported: boolean;
  start(): void;
  stop(): void;
  pause(): void;
  resume(): void;
  /** Push-to-talk: listen for one utterance. */
  listenOnce(): Promise<void>;
  isListening(): boolean;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRec = any;

export function createVoiceInput(
  mode: ListenMode,
  onTranscript: (text: string) => void,
  onError: (msg: string) => void,
  onListeningChange: (listening: boolean) => void = () => {}
): VoiceInput {
  if (isNative) return createNativeInput(onTranscript, onError, onListeningChange);

  const SR = (window as AnyRec).SpeechRecognition || (window as AnyRec).webkitSpeechRecognition;
  if (!SR) {
    onError("Speech recognition isn't supported in this browser. Use Chrome.");
    return {
      mode, supported: false,
      start() {}, stop() {}, pause() {}, resume() {}, async listenOnce() {}, isListening: () => false,
    };
  }

  const recognition = new SR();
  recognition.continuous = mode === "continuous";
  recognition.interimResults = false;
  recognition.lang = LANG;

  let shouldListen = false;
  let paused = false;
  let listening = false;

  const setListening = (v: boolean) => {
    if (listening !== v) {
      listening = v;
      onListeningChange(v);
    }
  };

  recognition.onstart = () => setListening(true);
  recognition.onresult = (event: AnyRec) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        const text = event.results[i][0].transcript.trim();
        if (text) onTranscript(text);
      }
    }
  };
  recognition.onend = () => {
    setListening(false);
    if (mode === "continuous" && shouldListen && !paused) {
      try { recognition.start(); } catch { /* already started */ }
    }
  };
  recognition.onerror = (event: AnyRec) => {
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      onError("Microphone access denied. Allow the microphone for this site.");
      shouldListen = false;
    } else if (!["no-speech", "aborted"].includes(event.error)) {
      console.warn("[voice] recognition error:", event.error);
    }
  };

  const tryStart = () => {
    try { recognition.start(); } catch { /* already started */ }
  };

  return {
    mode,
    supported: true,
    start() {
      shouldListen = true;
      paused = false;
      if (mode === "continuous") tryStart();
    },
    stop() {
      shouldListen = false;
      paused = false;
      recognition.stop();
    },
    pause() {
      paused = true;
      recognition.abort();
    },
    resume() {
      paused = false;
      if (mode === "continuous" && shouldListen) tryStart();
    },
    async listenOnce() {
      if (listening) {
        recognition.stop();
        return;
      }
      tryStart();
    },
    isListening: () => listening,
  };
}

function createNativeInput(
  onTranscript: (text: string) => void,
  onError: (msg: string) => void,
  onListeningChange: (listening: boolean) => void
): VoiceInput {
  let listening = false;
  let permitted = false;

  async function ensurePermission(): Promise<boolean> {
    if (permitted) return true;
    try {
      const { available } = await NativeSR.available();
      if (!available) {
        onError("Speech recognition isn't available on this phone. Install the Google app.");
        return false;
      }
      let status = await NativeSR.checkPermissions();
      if (status.speechRecognition !== "granted") status = await NativeSR.requestPermissions();
      permitted = status.speechRecognition === "granted";
      if (!permitted) onError("Microphone permission was denied.");
      return permitted;
    } catch (e) {
      onError("Couldn't start speech recognition.");
      console.error(e);
      return false;
    }
  }

  return {
    mode: "push",
    supported: true,
    start() { void ensurePermission(); },
    stop() { void NativeSR.stop().catch(() => {}); },
    pause() { if (listening) void NativeSR.stop().catch(() => {}); },
    resume() {},
    async listenOnce() {
      if (listening) {
        await NativeSR.stop().catch(() => {});
        return;
      }
      if (!(await ensurePermission())) return;
      listening = true;
      onListeningChange(true);
      try {
        const result = await NativeSR.start({ language: LANG, maxResults: 1, partialResults: false, popup: false });
        const text = result.matches?.[0]?.trim();
        if (text) onTranscript(text);
      } catch (e) {
        // "No match" / timeout errors are normal when the user says nothing.
        console.warn("[voice] native recognition:", e);
      } finally {
        listening = false;
        onListeningChange(false);
      }
    },
    isListening: () => listening,
  };
}

// ---------------------------------------------------------------------------
// Device text-to-speech (fallback when the server sends no audio)
// ---------------------------------------------------------------------------

let webVoice: SpeechSynthesisVoice | null = null;

function pickWebVoice(): SpeechSynthesisVoice | null {
  if (webVoice || !("speechSynthesis" in window)) return webVoice;
  const voices = speechSynthesis.getVoices();
  const lang = LANG.toLowerCase();
  webVoice =
    voices.find((v) => v.lang.toLowerCase() === lang && /google|natural|neural/i.test(v.name)) ||
    voices.find((v) => v.lang.toLowerCase() === lang) ||
    voices.find((v) => v.lang.toLowerCase().startsWith("en") && /google|natural|neural/i.test(v.name)) ||
    voices.find((v) => v.lang.toLowerCase().startsWith("en")) ||
    null;
  return webVoice;
}
if ("speechSynthesis" in window) speechSynthesis.onvoiceschanged = () => { webVoice = null; pickWebVoice(); };

export async function speakText(text: string): Promise<void> {
  if (!text) return;
  if (isNative) {
    try {
      await TextToSpeech.speak({ text, lang: LANG, rate: 1.0, pitch: 1.0, volume: 1.0 });
    } catch (e) {
      console.warn("[tts] native speak failed:", e);
    }
    return;
  }
  if (!("speechSynthesis" in window)) return;
  await new Promise<void>((resolve) => {
    const u = new SpeechSynthesisUtterance(text);
    const v = pickWebVoice();
    if (v) u.voice = v;
    u.lang = v?.lang || LANG;
    u.rate = 1.03;
    u.onend = () => resolve();
    u.onerror = () => resolve();
    speechSynthesis.speak(u);
    // Chrome sometimes never fires onend; don't hang forever.
    setTimeout(resolve, Math.max(4000, text.length * 120));
  });
}

export function stopSpeaking() {
  if (isNative) void TextToSpeech.stop().catch(() => {});
  else if ("speechSynthesis" in window) speechSynthesis.cancel();
}

// ---------------------------------------------------------------------------
// Audio Player (server MP3)
// ---------------------------------------------------------------------------

export interface AudioPlayer {
  enqueue(base64: string): Promise<void>;
  stop(): void;
  getAnalyser(): AnalyserNode;
  onFinished(cb: () => void): void;
  resume(): Promise<void>;
}

export function createAudioPlayer(): AudioPlayer {
  const audioCtx = new AudioContext();
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.8;
  analyser.connect(audioCtx.destination);

  const queue: AudioBuffer[] = [];
  let isPlaying = false;
  let currentSource: AudioBufferSourceNode | null = null;
  let finishedCallback: (() => void) | null = null;

  function playNext() {
    if (queue.length === 0) {
      isPlaying = false;
      currentSource = null;
      finishedCallback?.();
      return;
    }
    isPlaying = true;
    const source = audioCtx.createBufferSource();
    source.buffer = queue.shift()!;
    source.connect(analyser);
    currentSource = source;
    source.onended = () => {
      if (currentSource === source) playNext();
    };
    source.start();
  }

  return {
    async enqueue(base64: string) {
      if (audioCtx.state === "suspended") await audioCtx.resume();
      try {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        queue.push(await audioCtx.decodeAudioData(bytes.buffer.slice(0)));
        if (!isPlaying) playNext();
      } catch (err) {
        console.error("[audio] decode error:", err);
        if (!isPlaying) finishedCallback?.();
      }
    },
    stop() {
      queue.length = 0;
      if (currentSource) {
        try { currentSource.stop(); } catch { /* already stopped */ }
        currentSource = null;
      }
      isPlaying = false;
    },
    getAnalyser: () => analyser,
    onFinished(cb) { finishedCallback = cb; },
    async resume() {
      if (audioCtx.state === "suspended") await audioCtx.resume();
    },
  };
}
