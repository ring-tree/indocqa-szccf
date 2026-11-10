import { useCallback, useEffect, useRef, useState } from "react";
import type {
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import Sidebar from "./components/Sidebar";
import ChatView from "./pages/ChatView";
import DocsView from "./pages/DocsView";
import { deleteSession, fetchSessions } from "./lib/api";
import type { Session } from "./lib/types";

const EXAMPLES = [
  "空压机报错 E-05 应该怎么处理？",
  "MTBF 和平均无故障时间是同一个指标吗？额定值是多少？",
  "储气罐的作用是什么？",
];

type Theme = "light" | "dark";

const SIDEBAR_MIN = 224;
const SIDEBAR_MAX = 520;
const SIDEBAR_DEFAULT = 272;
const SIDEBAR_STEP = 16;
const SIDEBAR_KEY = "indocqa-sidebar-width";

function clampSidebarWidth(value: number) {
  return Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, Math.round(value)));
}

function ThemeToggle({ theme, onToggle }: { theme: Theme; onToggle: () => void }) {
  const next = theme === "dark" ? "浅色" : "深色";
  return (
    <button
      type="button"
      onClick={onToggle}
      className="ui-theme-toggle"
      aria-pressed={theme === "dark"}
      aria-label={`切换到${next}主题`}
      title={`切换到${next}主题`}
    >
      <span className="ui-theme-dot" aria-hidden="true" />
      {next}
    </button>
  );
}

export default function App() {
  const [tab, setTab] = useState<"chat" | "docs">("chat");
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [kb, setKb] = useState<{ documents: number; chunks: number } | null>(null);
  const [theme, setTheme] = useState<Theme>(() =>
    localStorage.getItem("indocqa-theme") === "dark" ? "dark" : "light",
  );
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const stored = Number(localStorage.getItem(SIDEBAR_KEY));
    return Number.isFinite(stored) && stored > 0
      ? clampSidebarWidth(stored)
      : SIDEBAR_DEFAULT;
  });
  const bootstrapped = useRef(false);
  const resizeCleanup = useRef<(() => void) | null>(null);

  const refreshSessions = useCallback(async (selectFirst = false) => {
    try {
      const list = await fetchSessions();
      setSessions(list);
      if (selectFirst && !bootstrapped.current) {
        bootstrapped.current = true;
        if (list.length > 0) setActiveId(list[0].id);
      }
    } catch {
      /* 后端未就绪时保持空列表，用户仍可直接提问 */
    }
  }, []);

  const refreshSessionsOnly = useCallback(() => {
    void refreshSessions();
  }, [refreshSessions]);

  const refreshKb = useCallback(() => {
    fetch("/api/health")
      .then((r) => r.json())
      .then((d) => setKb(d?.kb ?? null))
      .catch(() => setKb(null));
  }, []);

  const newChat = useCallback(() => {
    setActiveId(null);
    setTab("chat");
    setSidebarOpen(false);
  }, []);

  const selectSession = useCallback((id: string) => {
    setActiveId(id);
    setTab("chat");
    setSidebarOpen(false);
  }, []);

  const changeTab = useCallback((next: "chat" | "docs") => {
    setTab(next);
    setSidebarOpen(false);
  }, []);

  const deleteActive = useCallback(
    async (id: string) => {
      try {
        await deleteSession(id);
      } catch {
        /* 会话可能已被清理，刷新即可 */
      }
      if (activeId === id) setActiveId(null);
      await refreshSessions();
    },
    [activeId, refreshSessions],
  );

  const handleDelete = useCallback(
    (id: string) => {
      void deleteActive(id);
    },
    [deleteActive],
  );

  const closeSidebar = useCallback(() => setSidebarOpen(false), []);
  const toggleTheme = useCallback(
    () => setTheme((current) => (current === "light" ? "dark" : "light")),
    [],
  );

  const startResize = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      e.preventDefault();
      const startX = e.clientX;
      const startWidth = sidebarWidth;
      const onMove = (event: PointerEvent) => {
        setSidebarWidth(clampSidebarWidth(startWidth + event.clientX - startX));
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        resizeCleanup.current = null;
      };
      resizeCleanup.current = onUp;
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [sidebarWidth],
  );

  const onResizeKeyDown = useCallback((e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      setSidebarWidth((current) =>
        clampSidebarWidth(
          current + (e.key === "ArrowRight" ? SIDEBAR_STEP : -SIDEBAR_STEP),
        ),
      );
    } else if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      setSidebarWidth(e.key === "Home" ? SIDEBAR_MIN : SIDEBAR_MAX);
    }
  }, []);

  const resetSidebarWidth = useCallback(() => setSidebarWidth(SIDEBAR_DEFAULT), []);

  useEffect(() => {
    localStorage.setItem(SIDEBAR_KEY, String(sidebarWidth));
    document.documentElement.style.setProperty("--sidebar-width", `${sidebarWidth}px`);
  }, [sidebarWidth]);

  useEffect(() => () => resizeCleanup.current?.(), []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", theme === "dark" ? "#070d18" : "#edf3f8");
    localStorage.setItem("indocqa-theme", theme);
  }, [theme]);

  useEffect(() => {
    void refreshSessions(true);
    refreshKb();
  }, [refreshSessions, refreshKb]);

  useEffect(() => {
    if (!sidebarOpen) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSidebarOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sidebarOpen]);

  return (
    <div className="app-shell flex h-screen overflow-hidden">
      <a
        href="#main-content"
        className="ui-btn ui-btn-primary fixed left-4 top-4 z-50 -translate-y-24 shadow-lg transition-transform focus:translate-y-0"
      >
        跳到主要内容
      </a>

      {sidebarOpen && (
        <div
          className="fixed inset-0 z-10 bg-slate-900/40 md:hidden"
          onClick={closeSidebar}
          aria-hidden="true"
        />
      )}
      <div
        id="app-sidebar"
        style={{ width: sidebarWidth, maxWidth: "86vw" }}
        className={`fixed inset-y-0 left-0 z-20 md:static md:z-auto ${
          sidebarOpen ? "" : "hidden md:block"
        }`}
      >
        <Sidebar
          sessions={sessions}
          activeId={activeId}
          tab={tab}
          subtitle="工业技术文档 · 自适应检索问答"
          onTab={changeTab}
          onSelect={selectSession}
          onNew={newChat}
          onDelete={handleDelete}
          onClose={closeSidebar}
          kb={kb}
        />
      </div>

      <div
        role="separator"
        aria-label="调整侧栏宽度"
        aria-controls="app-sidebar"
        aria-orientation="vertical"
        aria-valuemin={SIDEBAR_MIN}
        aria-valuemax={SIDEBAR_MAX}
        aria-valuenow={sidebarWidth}
        tabIndex={0}
        onPointerDown={startResize}
        onKeyDown={onResizeKeyDown}
        onDoubleClick={resetSidebarWidth}
        title="拖拽调整宽度；双击复位；方向键微调"
        style={{ left: sidebarWidth }}
        className="ui-resize-handle hidden md:block"
      />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="ui-topbar hidden items-center justify-between gap-4 px-6 py-3 md:flex">
          <div className="flex min-w-0 items-center gap-3">
            <div className="ui-brand-mark" aria-hidden="true">
              工
            </div>
            <div className="min-w-0">
              <div className="ui-kicker">Knowledge Workspace</div>
              <div className="ui-brand-title">工阅 IndocQA</div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {kb && (
              <span className="ui-status-pill">
                {kb.documents} 份文档 · {kb.chunks} 块
              </span>
            )}
            <ThemeToggle theme={theme} onToggle={toggleTheme} />
          </div>
        </header>

        <header className="ui-topbar flex items-center gap-3 px-4 py-2.5 md:hidden">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            className="ui-btn ui-btn-ghost ui-btn-sm"
            aria-label="打开侧栏"
            aria-controls="app-sidebar"
            aria-expanded={sidebarOpen}
          >
            菜单
          </button>
          <div className="ui-brand-mark ui-brand-mark-sm" aria-hidden="true">
            工
          </div>
          <span className="ui-brand-title truncate">工阅 IndocQA</span>
          <div className="ml-auto">
            <ThemeToggle theme={theme} onToggle={toggleTheme} />
          </div>
        </header>

        <main id="main-content" tabIndex={-1} className="ui-main flex min-h-0 flex-1 flex-col outline-none">
          <ChatView
            activeId={activeId}
            onActiveId={setActiveId}
            onRefreshSessions={refreshSessionsOnly}
            className={tab === "chat" ? "" : "hidden"}
            examples={EXAMPLES}
            active={tab === "chat"}
          />
          <DocsView className={tab === "docs" ? "" : "hidden"} onChanged={refreshKb} />
        </main>
      </div>
    </div>
  );
}
