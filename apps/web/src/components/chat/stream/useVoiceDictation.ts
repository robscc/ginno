import { useEffect, useRef, useState, type RefObject } from "react";
import { listen } from "@tauri-apps/api/event";
import { BASE } from "@/lib/api/core";
import { getVoiceSettings, watchVoiceShortcut, type VoiceShortcut } from "@/lib/api/voice";

export const VOICE_SETTINGS_PATH = "/settings/connections/voice";
type HoldShortcut = "fn" | "ctrl-alt-v";
type Phase = "idle" | "starting" | "recording" | "finishing" | "failed";
type Task = {
  stream?: MediaStream;
  context?: AudioContext;
  node?: AudioWorkletNode;
  socket?: WebSocket;
  timer?: ReturnType<typeof setTimeout>;
  firstFrame?: ReturnType<typeof setTimeout>;
  final: Map<number, string>;
  partial: Map<number, string>;
  insert: (text: string) => void;
};
const ordered = (parts: Map<number, string>) => [...parts].sort(([a], [b]) => a - b).map(([, text]) => text).join("");

function release(task: Task) {
  clearTimeout(task.timer);
  clearTimeout(task.firstFrame);
  task.stream?.getTracks().forEach((track) => track.stop());
  task.node?.disconnect();
  if (task.context && task.context.state !== "closed") void task.context.close().catch(() => {});
  task.socket?.close();
}

export function useVoiceDictation(
  sessionId: string | undefined,
  composer: RefObject<HTMLDivElement>,
  insertAtSelection: () => Task["insert"],
) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState("");
  const [confirmed, setConfirmed] = useState("");
  const [preview, setPreview] = useState("");
  const taskRef = useRef<Task | null>(null);
  const phaseRef = useRef<Phase>("idle");
  const held = useRef<{ mode: HoldShortcut; timer: ReturnType<typeof setTimeout>; started: boolean } | null>(null);
  const shortcut = useRef<VoiceShortcut>("off");

  function transition(next: Phase) {
    phaseRef.current = next;
    setPhase(next);
  }

  function cancel() {
    clearTimeout(held.current?.timer);
    held.current = null;
    const task = taskRef.current;
    taskRef.current = null;
    if (task) release(task);
    setConfirmed(""); setPreview(""); setError("");
    transition("idle");
  }

  function fail(task: Task, code: string) {
    if (taskRef.current !== task) return;
    clearTimeout(held.current?.timer);
    held.current = null;
    transition("failed");
    release(task);
    setPreview("");
    setError(code);
  }

  function keep() {
    const task = taskRef.current;
    if (task) {
      const text = ordered(task.final).trim();
      if (text) task.insert(text);
    }
    cancel();
  }

  function stop() {
    const task = taskRef.current;
    if (!task) return;
    if (phaseRef.current === "starting") { cancel(); return; }
    if (phaseRef.current !== "recording") return;
    transition("finishing");
    clearTimeout(task.firstFrame);
    clearTimeout(task.timer);
    task.timer = setTimeout(() => fail(task, "connection"), 7000);
    task.node?.port.postMessage("stop");
  }

  async function start() {
    if (phaseRef.current === "failed" && !taskRef.current?.final.size) cancel();
    if (phaseRef.current !== "idle") return;
    const task: Task = { final: new Map(), partial: new Map(), insert: insertAtSelection() };
    taskRef.current = task;
    setError("");
    transition("starting");
    try {
      const settings = await getVoiceSettings();
      if (taskRef.current !== task) return;
      if (!settings.models.some((item) => item.id === settings.selected)) { fail(task, "configuration"); return; }
      if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) { fail(task, "unsupported"); return; }
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 }, video: false });
      if (taskRef.current !== task) { stream.getTracks().forEach((track) => track.stop()); return; }
      task.stream = stream;
      stream.getAudioTracks().forEach((track) => track.addEventListener("ended", () => fail(task, "microphone")));
      const context = task.context = new AudioContext();
      await context.audioWorklet.addModule("/voice/pcm-worklet.js");
      if (taskRef.current !== task) return;
      const node = task.node = new AudioWorkletNode(context, "ginno-pcm");
      // The worklet leaves its output silent. Connecting it keeps Safari pulling inputs.
      node.connect(context.destination);
      const socket = task.socket = new WebSocket(`${BASE.replace(/^http/, "ws")}/voice/dictation/ws`);
      task.timer = setTimeout(() => fail(task, "connection"), 12000);
      socket.onopen = () => socket.send(JSON.stringify({ type: "start", session_id: sessionId }));
      socket.onerror = socket.onclose = () => {
        if (phaseRef.current !== "failed") fail(task, "connection");
      };
      node.port.onmessage = ({ data }) => {
        if (taskRef.current !== task || phaseRef.current === "failed" || socket.readyState !== WebSocket.OPEN) return;
        if (data === "stopped") {
          stream.getTracks().forEach((track) => track.stop());
          socket.send("stop");
        } else if (socket.bufferedAmount > 32000) fail(task, "connection");
        else {
          clearTimeout(task.firstFrame);
          socket.send(data);
        }
      };
      socket.onmessage = ({ data }) => {
        if (taskRef.current !== task || phaseRef.current === "failed") return;
        const message = JSON.parse(data);
        if (message.type === "ready" && phaseRef.current === "starting") {
          clearTimeout(task.timer);
          context.createMediaStreamSource(stream).connect(node);
          transition("recording");
          task.firstFrame = setTimeout(() => fail(task, "capture"), 3000);
          void context.resume().catch(() => fail(task, "capture"));
          // Leave room to flush the final frame within the runtime's 60s audio limit.
          task.timer = setTimeout(stop, 59900);
        } else if (message.type === "final") {
          task.final.set(message.id, message.text);
          task.partial.delete(message.id);
          setConfirmed(ordered(task.final)); setPreview(ordered(task.partial));
        } else if (message.type === "partial" && !task.final.has(message.id)) {
          task.partial.set(message.id, message.text);
          setPreview(ordered(task.partial));
        } else if (message.type === "done") {
          if (ordered(task.final).trim()) keep();
          else fail(task, "empty");
        } else if (message.type === "error") fail(task, message.code);
      };
    } catch (err) {
      if (taskRef.current === task) fail(task, err instanceof DOMException ? "microphone" : "connection");
    }
  }

  function onShortcut(mode: HoldShortcut, down: boolean) {
    if (!down) {
      if (held.current?.mode !== mode) return;
      const { started, timer } = held.current;
      clearTimeout(timer); held.current = null;
      if (started) stop();
      return;
    }
    const retryable = phaseRef.current === "failed" && !taskRef.current?.final.size;
    if (shortcut.current !== mode || held.current || (!retryable && phaseRef.current !== "idle") || !ownsShortcut()) return;
    if (retryable) cancel();
    held.current = { mode, started: false, timer: setTimeout(() => {
      const press = held.current;
      if (!press) return;
      // getUserMedia is the permission authority. WKWebView does not always
      // expose the OS microphone grant through navigator.permissions.
      press.started = true;
      void start();
    }, 200) };
  }
  function ownsShortcut() {
    if (!document.hasFocus() || !composer.current?.getClientRects().length) return false;
    const composers = Array.from(document.querySelectorAll('[data-composer="1"]'));
    return composers.filter((el) => el.getClientRects().length > 0).length <= 1 ||
      composer.current.contains(document.activeElement);
  }
  const handlers = useRef({ onShortcut, cancel, ownsShortcut });
  handlers.current = { onShortcut, cancel, ownsShortcut };
  useEffect(() => {
    let mounted = true;
    let unlisten: (() => void) | undefined;
    let revision = 0;
    const refresh = () => {
      const request = ++revision;
      void getVoiceSettings().then((cfg) => {
        if (mounted && request === revision) shortcut.current = cfg.shortcut;
      }).catch(() => {});
    };
    const unwatch = watchVoiceShortcut((value) => {
      revision++;
      shortcut.current = value;
      if (held.current && held.current.mode !== value) handlers.current.cancel();
    });
    refresh();
    const down = (e: KeyboardEvent) => {
      if (e.code === "KeyV" && e.ctrlKey && e.altKey && !e.metaKey && !e.shiftKey && shortcut.current === "ctrl-alt-v" && handlers.current.ownsShortcut()) {
        e.preventDefault();
        if (!e.repeat) handlers.current.onShortcut("ctrl-alt-v", true);
      }
      if (e.key === "Escape" && (held.current || taskRef.current)) { e.preventDefault(); e.stopPropagation(); handlers.current.cancel(); }
    };
    const up = (e: KeyboardEvent) => { if (e.code === "KeyV" || e.key === "Control" || e.key === "Alt") handlers.current.onShortcut("ctrl-alt-v", false); };
    const blur = () => { if (held.current) handlers.current.cancel(); };
    window.addEventListener("keydown", down, true);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", blur);
    window.addEventListener("focus", refresh);
    if ("__TAURI_INTERNALS__" in window) {
      const subscribe = (mode: HoldShortcut, event: string) => listen<string>(event, ({ payload }) => {
        if (payload === "cancel") { if (held.current?.mode === mode) handlers.current.cancel(); }
        else handlers.current.onShortcut(mode, payload === "down");
      }).then((dispose) => {
        if (!mounted) { dispose(); return; }
        const previous = unlisten;
        unlisten = () => { previous?.(); dispose(); };
      }).catch(() => {});
      void subscribe("fn", "voice-fn");
      void subscribe("ctrl-alt-v", "voice-ctrl-alt-v");
    }
    return () => {
      mounted = false; unwatch(); unlisten?.();
      window.removeEventListener("keydown", down, true); window.removeEventListener("keyup", up);
      window.removeEventListener("blur", blur); window.removeEventListener("focus", refresh);
    };
  }, []);
  useEffect(() => {
    handlers.current.cancel();
    return () => {
      clearTimeout(held.current?.timer); held.current = null;
      const task = taskRef.current; taskRef.current = null;
      if (task) release(task);
    };
  }, [sessionId]);
  return { phase, error, confirmed, preview, start, stop, cancel, keep, blocked: () => phaseRef.current !== "idle" && phaseRef.current !== "failed" };
}
