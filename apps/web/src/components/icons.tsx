import {
  Terminal,
  Search,
  PenLine,
  MessageSquare,
  BookOpen,
  Settings as SettingsIcon,
  Star,
  Plus,
  Paperclip,
  Keyboard,
  ArrowUp,
  MoreVertical,
  ChevronDown,
  ListChecks,
  Workflow as WorkflowIcon,
  Boxes,
  LogOut,
  Eye,
  EyeOff,
  Zap,
  Clock,
  Slash,
  type LucideProps,
} from "lucide-react";

// Claude Code 专属星芒标:委托子会话(type=delegation, backend=claude-code)
// 的 icon,runtime 侧按后端下发同名 icon 名,其余后端沿用 terminal。
function ClaudeCodeMark(props: LucideProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      {...props}
    >
      <path d="M12 7.5V2.5" />
      <path d="M16.5 12h5" />
      <path d="M12 16.5v5" />
      <path d="M7.5 12h-5" />
      <path d="M15.2 8.8l1.75-1.75" />
      <path d="M8.8 8.8 7.05 7.05" />
      <path d="M8.8 15.2l-1.75 1.75" />
      <path d="M15.2 15.2l1.75 1.75" />
    </svg>
  );
}

// codex:六边形节点(OpenAI 几何风);pi:Π 字形——委托子会话按后端使用
// 专属图标,与 claude-code 星芒同一套 24x24 描边风格。
function CodexMark(props: LucideProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="M12 3.5 19.4 7.75v8.5L12 20.5 4.6 16.25v-8.5Z" />
      <circle cx="12" cy="12" r="2" />
    </svg>
  );
}

function PiMark(props: LucideProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      {...props}
    >
      <path d="M4.5 6.5h15" />
      <path d="M7 6.5V19" />
      <path d="M17 6.5V19" />
    </svg>
  );
}

const MAP: Record<string, React.ComponentType<LucideProps>> = {
  terminal: Terminal,
  search: Search,
  "pen-line": PenLine,
  pen: PenLine,
  "message-square": MessageSquare,
  message: MessageSquare,
  book: BookOpen,
  settings: SettingsIcon,
  star: Star,
  plus: Plus,
  paperclip: Paperclip,
  keyboard: Keyboard,
  "arrow-up": ArrowUp,
  more: MoreVertical,
  chevron: ChevronDown,
  list: ListChecks,
  workflow: WorkflowIcon,
  boxes: Boxes,
  logout: LogOut,
  eye: Eye,
  "eye-off": EyeOff,
  zap: Zap,
  clock: Clock,
  slash: Slash,
  "claude-code": ClaudeCodeMark,
  codex: CodexMark,
  pi: PiMark,
};

export function Icon({
  name,
  ...rest
}: { name: string } & LucideProps) {
  const Cmp = MAP[name] || MessageSquare;
  return <Cmp {...rest} />;
}
