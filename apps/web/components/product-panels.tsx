"use client";

import {
  Check,
  CircleX,
  ClipboardCheck,
  Plus,
  Save,
  ShieldAlert,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";

import {
  createTodo,
  deletePersonalData,
  deleteProfileAttribute,
  deleteTodo,
  resolveProfileSuggestion,
  updateProfile,
  updateTodo,
} from "@/lib/api";
import type {
  ProfileAttribute,
  StudentProfile,
  UserTodo,
} from "@/lib/api";

const PROFILE_FIELDS: Array<{
  key: ProfileAttribute["attribute_key"];
  label: string;
  placeholder: string;
}> = [
  { key: "education_level", label: "培养层次", placeholder: "本科 / 研究生" },
  { key: "cohort", label: "入学年份", placeholder: "例如 2026" },
  { key: "college", label: "学院", placeholder: "例如 工程学院" },
  { key: "major", label: "专业", placeholder: "例如 电子信息工程" },
  { key: "goal", label: "发展目标", placeholder: "保研 / 就业 / 留学 / 探索中" },
  { key: "interest", label: "兴趣方向", placeholder: "竞赛、科研、社团……" },
];

type OnboardingProps = {
  profile: StudentProfile | null;
  onSaved: (profile: StudentProfile) => void;
};

function useDocumentScrollLock(locked: boolean) {
  useEffect(() => {
    if (!locked) return;

    const root = document.documentElement;
    const body = document.body;
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    const previousBodyStyles = {
      position: body.style.position,
      inset: body.style.inset,
      top: body.style.top,
      width: body.style.width,
    };

    root.classList.add("product-modal-open");
    body.classList.add("product-modal-open");
    body.style.position = "fixed";
    body.style.inset = "0";
    body.style.top = `-${scrollY}px`;
    body.style.width = "100%";

    return () => {
      root.classList.remove("product-modal-open");
      body.classList.remove("product-modal-open");
      body.style.position = previousBodyStyles.position;
      body.style.inset = previousBodyStyles.inset;
      body.style.top = previousBodyStyles.top;
      body.style.width = previousBodyStyles.width;
      window.scrollTo(scrollX, scrollY);
    };
  }, [locked]);
}

export function OnboardingPanel({ profile, onSaved }: OnboardingProps) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const open = Boolean(profile && !profile.onboarding_completed);

  useDocumentScrollLock(open);

  if (!open) return null;

  async function save(skip = false) {
    setBusy(true);
    try {
      const attributes = skip
        ? []
        : PROFILE_FIELDS.flatMap((field) => {
            const value = values[field.key]?.trim();
            return value
              ? [{ attribute_key: field.key, attribute_value: value }]
              : [];
          });
      onSaved(
        await updateProfile({
          onboarding_completed: true,
          attributes,
        }),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="product-backdrop onboarding-backdrop">
      <section className="product-panel onboarding-panel" role="dialog" aria-modal="true">
        <header>
          <span>00</span>
          <div>
            <p className="eyebrow">FIRST CONTACT / OPTIONAL</p>
            <h2>先让城知认识你一点</h2>
          </div>
        </header>
        <div className="onboarding-scroll-area">
          <p>
            这些信息只用于让建议更贴合你。可以留空、随时修改，也可以关闭个性化。
          </p>
          <div className="profile-grid">
            {PROFILE_FIELDS.map((field) => (
              <label key={field.key}>
                <span>{field.label}</span>
                <input
                  value={values[field.key] ?? ""}
                  placeholder={field.placeholder}
                  onChange={(event) =>
                    setValues((current) => ({
                      ...current,
                      [field.key]: event.target.value,
                    }))
                  }
                />
              </label>
            ))}
          </div>
        </div>
        <footer>
          <button type="button" disabled={busy} onClick={() => void save(true)}>
            先跳过
          </button>
          <button type="button" disabled={busy} onClick={() => void save()}>
            <Save size={16} /> 保存并开始
          </button>
        </footer>
      </section>
    </div>
  );
}

type SpaceProps = {
  open: boolean;
  profile: StudentProfile | null;
  todos: UserTodo[];
  onClose: () => void;
  onProfile: (profile: StudentProfile) => void;
  onTodosChanged: () => Promise<void>;
  onPersonalDataDeleted: () => void;
  onError: (message: string) => void;
};

export function MySpacePanel({
  open,
  profile,
  todos,
  onClose,
  onProfile,
  onTodosChanged,
  onPersonalDataDeleted,
  onError,
}: SpaceProps) {
  const [tab, setTab] = useState<"profile" | "todos" | "data">("profile");
  const [values, setValues] = useState<Record<string, string>>({});
  const [newTodo, setNewTodo] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const confirmed = Object.fromEntries(
      (profile?.confirmed ?? []).map((item) => [
        item.attribute_key,
        item.attribute_value,
      ]),
    );
    setValues(confirmed);
  }, [profile]);

  if (!open || !profile) return null;

  async function saveProfile() {
    setBusy(true);
    try {
      onProfile(
        await updateProfile({
          attributes: PROFILE_FIELDS.flatMap((field) => {
            const value = values[field.key]?.trim();
            return value
              ? [{ attribute_key: field.key, attribute_value: value }]
              : [];
          }),
        }),
      );
    } finally {
      setBusy(false);
    }
  }

  async function togglePersonalization() {
    onProfile(
      await updateProfile({
        personalization_enabled: !(profile?.personalization_enabled ?? true),
      }),
    );
  }

  async function resolveSuggestion(id: string, action: "confirm" | "reject") {
    await resolveProfileSuggestion(id, action);
    const next = await updateProfile({});
    onProfile(next);
  }

  async function addTodo() {
    const title = newTodo.trim();
    if (!title) return;
    await createTodo({ title });
    setNewTodo("");
    await onTodosChanged();
  }

  async function destroyPersonalData() {
    if (
      !window.confirm(
        "确定删除全部个人数据吗？会话、画像、待办和反馈都会清除，且无法恢复。",
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await deletePersonalData();
      onPersonalDataDeleted();
      onClose();
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : "个人数据删除失败，请稍后重试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="product-backdrop">
      <section className="product-panel space-panel" role="dialog" aria-modal="true">
        <header>
          <span>
            <SlidersHorizontal size={19} />
          </span>
          <div>
            <p className="eyebrow">PERSONAL WORKSPACE</p>
            <h2>我的空间</h2>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭我的空间">
            <X size={20} />
          </button>
        </header>
        <nav aria-label="我的空间分区">
          <button
            type="button"
            className={tab === "profile" ? "active" : ""}
            onClick={() => setTab("profile")}
          >
            画像
          </button>
          <button
            type="button"
            className={tab === "todos" ? "active" : ""}
            onClick={() => setTab("todos")}
          >
            待办 <i>{todos.filter((item) => item.status === "open").length}</i>
          </button>
          <button
            type="button"
            className={tab === "data" ? "active" : ""}
            onClick={() => setTab("data")}
          >
            数据
          </button>
        </nav>

        <div className="space-content">
          {tab === "profile" && (
            <div className="space-profile">
              <div className="preference-switch">
                <span>
                  <b>回答个性化</b>
                  <small>关闭后，画像不会进入 Agent 上下文</small>
                </span>
                <button
                  type="button"
                  className={profile.personalization_enabled ? "on" : ""}
                  onClick={() => void togglePersonalization()}
                  aria-pressed={profile.personalization_enabled}
                >
                  <i />
                </button>
              </div>
              <div className="profile-grid">
                {PROFILE_FIELDS.map((field) => (
                  <label key={field.key}>
                    <span>{field.label}</span>
                    <span className="profile-input-row">
                      <input
                        value={values[field.key] ?? ""}
                        placeholder={field.placeholder}
                        onChange={(event) =>
                          setValues((current) => ({
                            ...current,
                            [field.key]: event.target.value,
                          }))
                        }
                      />
                      {values[field.key] ? (
                        <button
                          className="profile-clear"
                          type="button"
                          aria-label={`删除${field.label}`}
                          onClick={async () => {
                            setBusy(true);
                            try {
                              await deleteProfileAttribute(field.key);
                              setValues((current) => {
                                const next = { ...current };
                                delete next[field.key];
                                return next;
                              });
                              onProfile(await updateProfile({}));
                            } finally {
                              setBusy(false);
                            }
                          }}
                        >
                          <Trash2 size={13} />
                        </button>
                      ) : null}
                    </span>
                  </label>
                ))}
              </div>
              {profile.suggestions.length > 0 && (
                <div className="profile-suggestions">
                  <p className="eyebrow">WAITING FOR CONFIRMATION</p>
                  {profile.suggestions.map((suggestion) => (
                    <article key={suggestion.attribute_id}>
                      <span>
                        <b>{suggestion.attribute_value}</b>
                        <small>来自“{suggestion.supporting_user_text}”</small>
                      </span>
                      <button
                        type="button"
                        onClick={() =>
                          void resolveSuggestion(suggestion.attribute_id, "confirm")
                        }
                        aria-label="确认画像建议"
                      >
                        <Check size={15} />
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          void resolveSuggestion(suggestion.attribute_id, "reject")
                        }
                        aria-label="拒绝画像建议"
                      >
                        <CircleX size={15} />
                      </button>
                    </article>
                  ))}
                </div>
              )}
              <button
                className="primary-product-action"
                type="button"
                disabled={busy}
                onClick={() => void saveProfile()}
              >
                <Save size={16} /> 保存画像
              </button>
            </div>
          )}

          {tab === "todos" && (
            <div className="space-todos">
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void addTodo();
                }}
              >
                <input
                  value={newTodo}
                  onChange={(event) => setNewTodo(event.target.value)}
                  placeholder="手动添加一项待办"
                />
                <button type="submit" disabled={!newTodo.trim()}>
                  <Plus size={17} /> 添加
                </button>
              </form>
              {todos.length ? (
                todos.map((todo) => (
                  <article key={todo.todo_id} className={todo.status}>
                    <button
                      type="button"
                      onClick={async () => {
                        await updateTodo(todo.todo_id, {
                          status: todo.status === "done" ? "open" : "done",
                        });
                        await onTodosChanged();
                      }}
                      aria-label={todo.status === "done" ? "恢复待办" : "完成待办"}
                    >
                      <ClipboardCheck size={17} />
                    </button>
                    <span>
                      <b>{todo.title}</b>
                      <small>
                        {todo.source_answer_id ? "来自一次回答" : "手动创建"}
                        {todo.due_at
                          ? ` · ${new Date(todo.due_at).toLocaleDateString("zh-CN")}`
                          : ""}
                      </small>
                    </span>
                    <button
                      type="button"
                      onClick={async () => {
                        await deleteTodo(todo.todo_id);
                        await onTodosChanged();
                      }}
                      aria-label="删除待办"
                    >
                      <Trash2 size={15} />
                    </button>
                  </article>
                ))
              ) : (
                <div className="space-empty">暂时没有待办。回答中的行动项也需要你确认后才会保存。</div>
              )}
            </div>
          )}

          {tab === "data" && (
            <div className="space-data">
              <ShieldAlert size={25} />
              <h3>数据控制权在你手里</h3>
              <p>
                此操作会删除当前主体的会话、画像、待办和反馈。校园登录本身不会被删除，
                但产品会回到全新状态。
              </p>
              <button
                type="button"
                disabled={busy}
                onClick={() => void destroyPersonalData()}
              >
                <Trash2 size={16} /> {busy ? "正在删除…" : "删除全部个人数据"}
              </button>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
