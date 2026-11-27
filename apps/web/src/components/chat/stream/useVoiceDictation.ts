import { useEffect, useRef, useState, type RefObject } from "react";
import { listen } from "@tauri-apps/api/event";
import { BASE } from "@/lib/api/core";
import { getVoiceSettings } from "@/lib/api/voice";

export const VOICE_SETTINGS_PATH = "/settings/connections/voice";
type Phase = "idle" | "starting" | "recording" | "finishing" | "failed";
type Task = {
  stream?: MediaStream;
  context?: AudioContext;
  node?: AudioWorkletNode;
  socket?: WebSocket;
  timer?: ReturnType<typeof setTimeout>;
  final: Map<number, string>;
  partial: Map<number, string>;
  insert: (text: string) => void;
};
const ordered = (parts: Map<number, string>) => [...parts].sort(([a], [b]) => a - b).map(([, text]) => text).join("");

function release(task: Task) {
  clearTimeout(task.timer);
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
  const held = useRef<{ mode: "fn" | "ctrl-alt-v"; timer: ReturnType<typeof setTimeout>; started: boolean } | null>(null);
  const [shortcut, setShortcut] = useState("off");

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
    clearTimeout(task.timer);
    task.timer = setTimeout(() => fail(task, "connection"), 7000);
    task.node?.port.postMessage("stop");
  }

  async function start() {
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
      micAuthorized.current = true;
      stream.getAudioTracks().forEach((track) => track.addEventListener("ended", () => fail(task, "microphone")));
      const context = task.context = new AudioContext();
      await context.audioWorklet.addModule("/voice/pcm-worklet.js");
      if (taskRef.current !== task) return;
      const node = task.node = new AudioWorkletNode(context, "ginno-pcm", { numberOfOutputs: 0 });
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
        else socket.send(data);
      };
      socket.onmessage = ({ data }) => {
        if (taskRef.current !== task || phaseRef.current === "failed") return;
        const message = JSON.parse(data);
        if (message.type === "ready" && phaseRef.current === "starting") {
          clearTimeout(task.timer);
          context.createMediaStreamSource(stream).connect(node);
          transition("recording");
          void context.resume().catch(() => fail(task, "microphone"));
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

  function onShortcut(mode: "fn" | "ctrl-alt-v", down: boolean) {
    if (!down) {
      if (held.current?.mode !== mode) return;
      const { started, timer } = held.current;
      clearTimeout(timer); held.current = null;
      if (started) stop();
      return;
    }
    if (shortcut !== mode || held.current || phaseRef.current !== "idle" || !ownsShortcut()) return;
    // Permissions are requested through the microphone button first.
    held.current = { mode, started: false, timer: setTimeout(async () => {
      const press = held.current;
      if (!press) return;
      let granted = false;
      try { granted = (await navigator.permissions.query({ name: "microphone" as PermissionName })).state === "granted"; } catch { /* WKWebView may not expose Permissions API. */ }
      if (held.current !== press) return;
      if (!granted && !micAuthorized.current) { held.current = null; setError("authorize"); return; }
      press.started = true;
      void start();
    }, 200) };
  }
  function ownsShortcut() {
    if (!document.hasFocus() || !composer.current?.getClientRects().length) return false;
    if (document.querySelectorAll('[data-composer="1"]').length <= 1) return true;
    return composer.current.contains(document.activeElement);
  }
  const micAuthorized = useRef(false);
  const handlers = useRef({ onShortcut, cancel, ownsShortcut });
  handlers.current = { onShortcut, cancel, ownsShortcut };
  useEffect(() => {
    let mounted = true;
    let unlisten: (() => void) | undefined;
    const refresh = () => { void getVoiceSettings().then((cfg) => { if (mounted) setShortcut(cfg.shortcut); }).catch(() => {}); };
    refresh();
    const down = (e: KeyboardEvent) => {
      if (e.code === "KeyV" && e.ctrlKey && e.altKey && !e.metaKey && !e.shiftKey && shortcut === "ctrl-alt-v" && handlers.current.ownsShortcut()) {
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
    if ("__TAURI_INTERNALS__" in window) void listen<string>("voice-fn", ({ payload }) => {
      if (payload === "cancel") { if (held.current?.mode === "fn") handlers.current.cancel(); }
      else handlers.current.onShortcut("fn", payload === "down");
    })
      .then((dispose) => { if (mounted) unlisten = dispose; else dispose(); }).catch(() => {});
    return () => {
      mounted = false; unlisten?.();
      window.removeEventListener("keydown", down, true); window.removeEventListener("keyup", up);
      window.removeEventListener("blur", blur); window.removeEventListener("focus", refresh);
    };
  }, [shortcut]);
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
