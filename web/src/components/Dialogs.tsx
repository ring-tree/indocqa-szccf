import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

interface ConfirmProps {
  open: boolean;
  title: string;
  children?: ReactNode;
  confirmText?: string;
  danger?: boolean;
  busy?: boolean;
  error?: string;
  onCancel: () => void;
  onConfirm: () => void;
}

/** 应用内确认弹窗：替代 window.confirm，避免嵌入式浏览器阻塞主线程。 */
export function ConfirmModal({
  open,
  title,
  children,
  confirmText = "确认",
  danger = false,
  busy = false,
  error = "",
  onCancel,
  onConfirm,
}: ConfirmProps) {
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    confirmRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, busy, onCancel]);

  if (!open) return null;
  return (
    <div className="ui-modal-backdrop" role="presentation" onClick={busy ? undefined : onCancel}>
      <div
        className="ui-modal ui-confirm"
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="ui-modal-head">
          <h3>{title}</h3>
        </div>
        {children && <div className="ui-dialog-text">{children}</div>}
        {error && <p className="ui-form-error">{error}</p>}
        <div className="ui-modal-actions">
          <button type="button" className="ui-btn ui-btn-ghost" disabled={busy} onClick={onCancel}>
            取消
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={`ui-btn ${danger ? "ui-btn-danger" : "ui-btn-primary"}`}
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? "处理中…" : confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}

interface PromptProps {
  open: boolean;
  title: string;
  description?: string;
  label?: string;
  placeholder?: string;
  initialValue?: string;
  confirmText?: string;
  busy?: boolean;
  error?: string;
  selectOnOpen?: boolean;
  canSubmit?: (value: string) => boolean;
  renderHint?: (value: string) => ReactNode;
  onCancel: () => void;
  onSubmit: (value: string) => void;
}
/** 应用内输入弹窗：替代 window.prompt，支持 Enter 提交与 Esc 取消。 */
export function PromptModal({
  open,
  title,
  description,
  label = "目录路径",
  placeholder = "例如 设备手册/新机组",
  initialValue = "",
  confirmText = "确定",
  busy = false,
  error = "",
  selectOnOpen = true,
  canSubmit,
  renderHint,
  onCancel,
  onSubmit,
}: PromptProps) {
  const [value, setValue] = useState(initialValue);
  const inputRef = useRef<HTMLInputElement>(null);
  const enabled = canSubmit ? canSubmit(value) : Boolean(value.trim());

  useEffect(() => {
    if (!open) return;
    setValue(initialValue);
    inputRef.current?.focus();
    if (selectOnOpen) {
      inputRef.current?.select();
    } else {
      const length = inputRef.current?.value.length ?? 0;
      inputRef.current?.setSelectionRange(length, length);
    }
  }, [open, initialValue, selectOnOpen]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, busy, onCancel]);

  if (!open) return null;
  return (
    <div className="ui-modal-backdrop" role="presentation" onClick={busy ? undefined : onCancel}>
      <form
        className="ui-modal ui-confirm"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
        onSubmit={(event) => {
          event.preventDefault();
          const next = value.trim();
          if (next && enabled && !busy) onSubmit(next);
        }}
      >
        <div className="ui-modal-head">
          <h3>{title}</h3>
        </div>
        {description && <p className="ui-dialog-text">{description}</p>}
        <label className="ui-dialog-field">
          <span>{label}</span>
          <input
            ref={inputRef}
            type="text"
            className="ui-search w-full px-3 py-2 text-xs outline-none"
            placeholder={placeholder}
            value={value}
            disabled={busy}
            onChange={(event) => setValue(event.target.value)}
          />
        </label>
        {renderHint && <div className="ui-dialog-hint">{renderHint(value)}</div>}
        {error && <p className="ui-form-error">{error}</p>}
        <div className="ui-modal-actions">
          <button type="button" className="ui-btn ui-btn-ghost" disabled={busy} onClick={onCancel}>
            取消
          </button>
          <button
            type="submit"
            className="ui-btn ui-btn-primary"
            disabled={busy || !enabled}
          >
            {busy ? "处理中…" : confirmText}
          </button>
        </div>
      </form>
    </div>
  );
}

export interface ChoiceAction {
  label: string;
  tone?: "ghost" | "primary" | "danger";
  onClick: () => void;
}

const TONE_CLASS: Record<NonNullable<ChoiceAction["tone"]>, string> = {
  ghost: "ui-btn-ghost",
  primary: "ui-btn-primary",
  danger: "ui-btn-danger",
};

interface ChoiceProps {
  open: boolean;
  title: string;
  children?: ReactNode;
  actions: ChoiceAction[];
  busy?: boolean;
  error?: string;
  onCancel: () => void;
}

/** 多选项确认弹窗：用于删除目录时选择“结构上移 / 级联删除”。 */
export function ChoiceModal({
  open,
  title,
  children,
  actions,
  busy = false,
  error = "",
  onCancel,
}: ChoiceProps) {
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    dialogRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, busy, onCancel]);

  if (!open) return null;
  return (
    <div className="ui-modal-backdrop" role="presentation" onClick={busy ? undefined : onCancel}>
      <div
        ref={dialogRef}
        className="ui-modal ui-confirm"
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="ui-modal-head">
          <h3>{title}</h3>
        </div>
        {children && <div className="ui-dialog-text">{children}</div>}
        {error && <p className="ui-form-error">{error}</p>}
        {busy && <p className="ui-confirm-hint">处理中…</p>}
        <div className="ui-modal-actions">
          {actions.map((action) => (
            <button
              key={action.label}
              type="button"
              className={`ui-btn ${TONE_CLASS[action.tone ?? "ghost"]}`}
              disabled={busy}
              onClick={action.onClick}
            >
              {action.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
