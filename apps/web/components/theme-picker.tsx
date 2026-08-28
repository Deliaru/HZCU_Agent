"use client";

import { Check, Feather, Focus } from "lucide-react";

import type { ThemeId } from "@/lib/theme";
import { CongyuArtwork } from "./congyu-artwork";

type Props = {
  value?: ThemeId | null;
  heading?: string;
  description?: string;
  className?: string;
  compact?: boolean;
  onSelect: (theme: ThemeId) => void;
};

export function ThemePicker({
  value,
  heading = "界面主题",
  description = "主题跟随当前设备，不会写入校园账号或学生画像。",
  className = "",
  compact = false,
  onSelect,
}: Props) {
  const content = (
    <>
      <header className="theme-picker-heading">
        <span aria-hidden="true">00</span>
        <div>
          <p className="eyebrow">DISPLAY MODE / THIS DEVICE</p>
          <h2>{heading}</h2>
          <p>{description}</p>
        </div>
      </header>
      <div className="theme-options" role="radiogroup" aria-label="界面主题">
        <button
          type="button"
          className={value === "minimal" ? "selected" : ""}
          role="radio"
          aria-checked={value === "minimal"}
          onClick={() => onSelect("minimal")}
        >
          <span className="theme-preview theme-preview-minimal" aria-hidden="true">
            <i /><i /><i /><b>城知</b>
          </span>
          <span className="theme-option-copy">
            <i><Focus size={16} /></i>
            <span><b>简洁主题</b><small>清爽、克制，专注信息本身</small></span>
            {value === "minimal" ? <Check size={17} /> : null}
          </span>
        </button>
        <button
          type="button"
          className={value === "character" ? "selected" : ""}
          role="radio"
          aria-checked={value === "character"}
          onClick={() => onSelect("character")}
        >
          <span className="theme-preview theme-preview-character" aria-hidden="true">
            <span className="theme-character-title"><b>琮羽</b><small>CAMPUS INVESTIGATION</small></span>
            <CongyuArtwork scene="hello" sizes="220px" />
            <i className="theme-character-notebook" />
          </span>
          <span className="theme-option-copy">
            <i><Feather size={16} /></i>
            <span><b>琮羽主题</b><small>角色陪伴与状态反馈更完整</small></span>
            {value === "character" ? <Check size={17} /> : null}
          </span>
        </button>
      </div>
    </>
  );

  if (compact) {
    return <section className={`theme-picker-inline ${className}`.trim()}>{content}</section>;
  }

  return (
    <div className={`theme-picker-backdrop ${className}`.trim()}>
      <section className="theme-picker-panel" role="dialog" aria-modal="true">
        {content}
      </section>
    </div>
  );
}
