import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type { ModelProfile } from "../lib/types";

interface Props {
  models: ModelProfile[];
  profile: string;
  onSelect: (id: string) => void;
  disabled: boolean;
}

export default function ModelPicker({ models, profile, onSelect, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const pillRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const selected = models.find((m) => m.id === profile) ?? models[0] ?? null;
  const label = selected?.model ?? "未配置输出模型";

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        pillRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const first = menuRef.current?.querySelector<HTMLElement>('[role="option"]');
    first?.focus();
  }, [open]);

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  function choose(id: string) {
    onSelect(id);
    setOpen(false);
    pillRef.current?.focus();
  }

  function onMenuKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const items = [
        ...(menuRef.current?.querySelectorAll<HTMLElement>('[role="option"]') ?? []),
      ];
      const index = items.indexOf(document.activeElement as HTMLElement);
      const next =
        event.key === "ArrowDown"
          ? Math.min(items.length - 1, index + 1)
          : Math.max(0, index - 1);
      items[next]?.focus();
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const items = [
        ...(menuRef.current?.querySelectorAll<HTMLElement>('[role="option"]') ?? []),
      ];
      (event.key === "Home" ? items[0] : items[items.length - 1])?.focus();
    }
  }

  return (
    <div ref={wrapRef} className="ui-model-picker">
      <button
        ref={pillRef}
        type="button"
        className="ui-model-pill"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls="model-menu"
        aria-label={`输出模型：${label}`}
        title={disabled ? label : "点击切换输出模型，仅影响后续消息"}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="ui-model-pill-dot" aria-hidden="true" />
        <span className="ui-model-pill-label">模型</span>
        <span className="ui-model-pill-value">{label}</span>
        <span className="ui-model-pill-chevron" aria-hidden="true" />
      </button>

      {open && !disabled && (
        <div
          ref={menuRef}
          id="model-menu"
          role="listbox"
          aria-label="输出模型"
          className="ui-model-menu"
          onKeyDown={onMenuKeyDown}
        >
          {models.map((m) => (
            <button
              key={m.id}
              type="button"
              role="option"
              aria-selected={m.id === profile}
              className={`ui-model-option ${m.id === profile ? "is-active" : ""}`}
              onClick={() => choose(m.id)}
            >
              <span className="ui-model-option-name">{m.model}</span>
              <span className="ui-model-option-check" aria-hidden="true" />
            </button>
          ))}
        </div>
      )}

      {/* 保持原有 #model-select / options / value 契约，仅供自动化与无障碍回退读取 */}
      <select
        id="model-select"
        className="ui-model-native-select sr-only"
        tabIndex={-1}
        aria-hidden="true"
        aria-label="输出模型"
        value={profile}
        disabled={disabled}
        onChange={(e) => onSelect(e.target.value)}
      >
        {models.length === 0 && <option value="">未配置输出模型</option>}
        {models.map((m) => (
          <option key={m.id} value={m.id}>
            {m.model}
          </option>
        ))}
      </select>
    </div>
  );
}
