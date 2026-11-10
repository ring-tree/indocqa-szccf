import { memo, useEffect, useState } from "react";
import type { Session } from "../lib/types";

interface Props {
  sessions: Session[];
  activeId: string | null;
  tab: "chat" | "docs";
  subtitle?: string;
  onTab: (tab: "chat" | "docs") => void;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onDeleteMany: (ids: string[]) => void;
  onClose?: () => void;
  kb?: { documents: number; chunks: number } | null;
}

function Sidebar({
  sessions,
  activeId,
  tab,
  subtitle = "企业知识库智能问答",
  onTab,
  onSelect,
  onNew,
  onDelete,
  onDeleteMany,
  onClose,
  kb,
}: Props) {
  const [managing, setManaging] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    setSelected((current) => {
      const next = new Set([...current].filter((id) => sessions.some((s) => s.id === id)));
      return next.size === current.size ? current : next;
    });
    if (sessions.length === 0) setManaging(false);
  }, [sessions]);

  const toggleSelected = (id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setSelected((current) =>
      current.size === sessions.length ? new Set() : new Set(sessions.map((s) => s.id)),
    );
  };

  const deleteSelected = () => {
    const ids = [...selected];
    if (ids.length === 0) return;
    if (!window.confirm(`删除选中的 ${ids.length} 个会话？该操作不可撤销。`)) return;
    onDeleteMany(ids);
    setSelected(new Set());
    setManaging(false);
  };

  return (
    <aside
      aria-label="会话与导航"
      className="ui-sidebar flex h-full w-full shrink-0 flex-col"
    >
      <div className="ui-sidebar-header px-4 pb-3 pt-5">
        <div className="flex min-w-0 items-start gap-3">
          <div className="ui-brand-mark" aria-hidden="true">
            工
          </div>
          <div className="min-w-0">
            <h1 className="ui-sidebar-title truncate">工阅 IndocQA</h1>
            <p className="ui-sidebar-subtitle mt-1">{subtitle}</p>
          </div>
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="ui-sidebar-close md:hidden"
            aria-label="关闭侧栏"
          >
            ×
          </button>
        )}
      </div>

      <div className="px-3">
        <button type="button" onClick={onNew} className="ui-sidebar-primary">
          <span className="text-base leading-none" aria-hidden="true">
            +
          </span>
          新对话
        </button>
      </div>

      <nav aria-label="主视图切换" className="ui-sidebar-nav mx-3 mt-3">
        <button
          type="button"
          onClick={() => onTab("chat")}
          aria-current={tab === "chat" ? "page" : undefined}
        >
          问答
        </button>
        <button
          type="button"
          onClick={() => onTab("docs")}
          aria-current={tab === "docs" ? "page" : undefined}
        >
          文档管理
        </button>
      </nav>

      <div className="mt-5 flex min-h-0 flex-1 flex-col">
        <div className="ui-sidebar-section flex items-center justify-between gap-2 px-4 pb-2">
          <span>最近会话</span>
          {sessions.length > 0 && (
            <button
              type="button"
              className="ui-session-manage"
              onClick={() => {
                setManaging((value) => !value);
                setSelected(new Set());
              }}
            >
              {managing ? "完成" : "管理"}
            </button>
          )}
        </div>
        {managing && sessions.length > 0 && (
          <div className="ui-session-batch">
            <span>已选 {selected.size} 项</span>
            <button type="button" onClick={toggleAll}>
              {selected.size === sessions.length ? "取消全选" : "全选"}
            </button>
            <button
              type="button"
              disabled={selected.size === 0}
              onClick={deleteSelected}
              className="ui-session-batch-delete"
            >
              删除
            </button>
          </div>
        )}
        <ul className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-3">
          {sessions.length === 0 && (
            <li className="ui-sidebar-subtitle px-3 py-7 text-center">
              还没有会话，直接提问即可开始
            </li>
          )}
          {sessions.map((s) => {
            const active = s.id === activeId;
            const checked = selected.has(s.id);
            return (
              <li
                key={s.id}
                className={`group ui-session-item ${active ? "is-active" : ""} ${
                  managing ? "is-managing" : ""
                }`}
              >
                {managing && (
                  <button
                    type="button"
                    role="checkbox"
                    aria-checked={checked}
                    aria-label={`选择会话 ${s.title}`}
                    className="ui-session-check"
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleSelected(s.id);
                    }}
                  >
                    <span className={`ui-session-checkbox ${checked ? "is-checked" : ""}`} />
                  </button>
                )}
                <div
                  role="button"
                  tabIndex={0}
                  aria-current={active ? "true" : undefined}
                  aria-label={managing ? `选择会话 ${s.title}` : `打开会话 ${s.title}`}
                  onClick={() => (managing ? toggleSelected(s.id) : onSelect(s.id))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      if (managing) toggleSelected(s.id);
                      else onSelect(s.id);
                    }
                  }}
                  className="min-w-0 flex-1 cursor-pointer rounded-md px-2 py-2 text-left"
                >
                  <div className="ui-session-title truncate">{s.title}</div>
                  <div className="ui-session-meta mt-1 truncate">
                    {s.lastMessage || `${s.messageCount ?? 0} 条消息`}
                  </div>
                </div>
                {!managing && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      if (window.confirm(`删除会话「${s.title}」？`)) onDelete(s.id);
                    }}
                    className="ui-session-delete md:opacity-60 md:group-hover:opacity-100"
                    aria-label={`删除会话 ${s.title}`}
                    title="删除会话"
                  >
                    ×
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      {kb && (
        <div className="ui-sidebar-footer px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="h-1.5 w-1.5 rounded-full bg-[var(--brand)]" aria-hidden="true" />
            知识库已就绪
          </div>
          <div className="mt-1">
            {kb.documents} 份文档 / {kb.chunks} 块
          </div>
        </div>
      )}
    </aside>
  );
}

export default memo(Sidebar);
