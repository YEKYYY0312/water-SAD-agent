import { useCallback, useEffect, useState } from "react";

import { ModelCatalogApi, type CatalogModel, type ModelCatalogView } from "./modelChoiceApi.ts";
import {
  MAX_CATALOG_ENTRIES,
  TIER_NOTE,
  draftChanged,
  draftFromView,
  draftToPayload,
  modelErrorMessage,
  newDraftRow,
  thinkingLabel,
  tierLabel,
  unselectableReason,
  validateDraft,
  type CatalogDraftRow,
} from "./modelChoiceState.ts";
import { SettingsApiError } from "./settingsApi.ts";

const api = new ModelCatalogApi();

function errorText(error: unknown): string {
  if (error instanceof SettingsApiError) return modelErrorMessage(error.code, error.message);
  return "请求未完成，请稍后重试。";
}

function statusText(model: CatalogModel | undefined, locked: ModelCatalogView["locked_reason"]): string {
  if (!model) return "保存后才能检查";
  return unselectableReason(model, locked) ?? "可选";
}

/**
 * 「模型与密钥」页里的模型目录（管理员）。同一端点、同一把 Key 下的模型白名单：
 * 这里不收地址与密钥；新增的模型要先通过兼容性检查才会出现在用户的选择器里。
 */
export function ModelCatalogPanel() {
  const [view, setView] = useState<ModelCatalogView | null>(null);
  const [rows, setRows] = useState<CatalogDraftRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.get();
      setView(data);
      setRows(draftFromView(data));
      setError(null);
      return data;
    } catch (caught) {
      setError(errorText(caught));
      return null;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 检查在后台跑：轮询目录直到被检查的模型 verified 变化或超时
  useEffect(() => {
    if (!checking) return;
    let ticks = 0;
    const timer = window.setInterval(() => {
      ticks += 1;
      void load().then((data) => {
        const model = data?.models.find((item) => item.id === checking);
        if (model?.verified || ticks >= 40) setChecking(null);
      });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [checking, load]);

  // 成员（读到 can_manage=false）与读失败：整块不渲染
  if (!view || !view.can_manage) return null;

  const editable = view.editable;
  const validation = validateDraft(rows);
  const changed = draftChanged(rows, view);

  const patch = (key: string, update: Partial<CatalogDraftRow>) =>
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...update } : row)));

  const save = async () => {
    if (validation) return;
    setBusy(true);
    setError(null);
    try {
      const data = await api.put(draftToPayload(rows));
      setView(data);
      setRows(draftFromView(data));
    } catch (caught) {
      setError(errorText(caught));
    } finally {
      setBusy(false);
    }
  };

  const check = async (id: string) => {
    if (
      !window.confirm(
        `对「${id}」运行兼容性检查会向对话端点发出约五次计费请求（同端点、同 Key）。继续？`,
      )
    ) {
      return;
    }
    setError(null);
    try {
      await api.runCompatibility(id);
      setChecking(id);
    } catch (caught) {
      setError(errorText(caught));
    }
  };

  return (
    <section className="settings-card" aria-labelledby="model-catalog-heading">
      <h3 id="model-catalog-heading" className="settings-subheading">可选模型目录</h3>
      <p className="muted-copy">
        同一对话端点、同一把 Key 下允许用户选用的模型。{TIER_NOTE}未通过兼容性检查的模型不会出现在选择器里；
        目录为空时用户看不到任何选择器，行为与此前相同。
      </p>
      {view.locked_reason !== null && (
        <p className="warning-state" role="status">对话端点由部署配置或本地模型路径管理，目录只读，用户也不能按会话 / 按消息选择模型。</p>
      )}
      {error && <div className="dialog-error" role="alert"><span>{error}</span></div>}
      {view.error && <div className="dialog-error" role="alert"><span>{view.error.message}</span></div>}

      <div className="model-catalog-rows">
        {rows.map((row) => {
          const stored = view.models.find((item) => item.id === row.id);
          return (
            <div className="model-catalog-row" key={row.key}>
              <input
                aria-label="模型 id"
                value={row.id}
                readOnly={row.saved}
                disabled={!editable}
                placeholder="模型 id"
                onChange={(event) => patch(row.key, { id: event.target.value })}
              />
              <input
                aria-label="显示名称"
                value={row.label}
                disabled={!editable}
                placeholder="显示名称"
                onChange={(event) => patch(row.key, { label: event.target.value })}
              />
              <select
                aria-label="档位"
                value={row.tier}
                disabled={!editable}
                onChange={(event) => patch(row.key, { tier: event.target.value as CatalogDraftRow["tier"] })}
              >
                <option value="fast">{tierLabel("fast")}</option>
                <option value="advanced">{tierLabel("advanced")}</option>
              </select>
              <select
                aria-label="思考配置"
                value={row.thinking}
                disabled={!editable}
                onChange={(event) => patch(row.key, { thinking: event.target.value as CatalogDraftRow["thinking"] })}
              >
                <option value="default">{thinkingLabel("default")}</option>
                <option value="off">{thinkingLabel("off")}</option>
                <option value="roundtrip">{thinkingLabel("roundtrip")}</option>
              </select>
              <label className="model-catalog-enabled">
                <input
                  type="checkbox"
                  checked={row.enabled}
                  disabled={!editable}
                  onChange={(event) => patch(row.key, { enabled: event.target.checked })}
                />
                启用
              </label>
              <span className="model-catalog-status">{statusText(stored, view.locked_reason)}</span>
              <button
                type="button"
                className="button-secondary"
                disabled={!editable || !row.saved || checking !== null || changed}
                title={changed ? "先保存目录的改动" : undefined}
                onClick={() => void check(row.id)}
              >
                {checking === row.id ? "检查中…" : "运行兼容性检查"}
              </button>
              {editable && (
                <button
                  type="button"
                  className="quiet-button"
                  onClick={() => setRows((current) => current.filter((item) => item.key !== row.key))}
                >
                  移除
                </button>
              )}
            </div>
          );
        })}
        {rows.length === 0 && <p className="muted-copy">目录为空。</p>}
      </div>

      {validation && editable && changed && <p className="field-error" role="status">{validation}</p>}
      {editable && (
        <div className="settings-card-actions">
          <button
            type="button"
            className="button-secondary"
            disabled={rows.length >= MAX_CATALOG_ENTRIES}
            onClick={() => setRows((current) => [...current, newDraftRow()])}
          >
            添加模型
          </button>
          <button
            type="button"
            className="button-primary"
            disabled={busy || !changed || validation !== null}
            onClick={() => void save()}
          >
            {busy ? "保存中…" : "保存目录"}
          </button>
        </div>
      )}
    </section>
  );
}
