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
};

export function Icon({
  name,
  ...rest
}: { name: string } & LucideProps) {
  const Cmp = MAP[name] || MessageSquare;
  return <Cmp {...rest} />;
}
