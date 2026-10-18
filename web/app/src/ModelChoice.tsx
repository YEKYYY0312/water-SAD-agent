import type { ModelCatalogView } from "./modelChoiceApi.ts";
import {
  FOLLOW_DEFAULT,
  TIER_NOTE,
  followDefaultLabel,
  modelCardView,
  modelOptionLabel,
  selectableModels,
  sessionDefaultProblem,
  showModelPicker,
  type ModelCard,
} from "./modelChoiceState.ts";

/** 输入框旁的「本条消息用哪个模型」。目录为空或没有可选项时整个不渲染。禁用条件与输入框一致。 */
export function ComposerModelSelect({
  catalog,
  value,
  sessionDefault,
  disabled,
  onChange,
}: {
  catalog: ModelCatalogView | null;
  value: string;
  sessionDefault: string | null | undefined;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  if (!showModelPicker(catalog)) return null;
  return (
    <>
      <label htmlFor="composer-model" className="sr-only">本条消息使用的模型</label>
      <select
        id="composer-model"
        className="composer-model-select"
        value={value}
        disabled={disabled}
        title={TIER_NOTE}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value={FOLLOW_DEFAULT}>{followDefaultLabel(catalog, sessionDefault)}</option>
        {selectableModels(catalog).map((model) => (
          <option key={model.id} value={model.id}>{modelOptionLabel(model)}</option>
        ))}
      </select>
    </>
  );
}

/** 会话默认模型：设了之后每条消息默认用它；清除回到全局模型。 */
export function SessionDefaultModel({
  catalog,
  sessionDefault,
  disabled,
  onChange,
}: {
  catalog: ModelCatalogView | null;
  sessionDefault: string | null | undefined;
  disabled: boolean;
  onChange: (model: string | null) => void;
}) {
  const problem = sessionDefaultProblem(catalog, sessionDefault);
  // 目录没东西可选、且这个会话也没设过默认：不占位
  if (!showModelPicker(catalog) && !sessionDefault) return null;
  const options = selectableModels(catalog);
  const known = sessionDefault ? options.some((model) => model.id === sessionDefault) : true;
  return (
    <div className="session-default-model">
      <label htmlFor="session-default-model">本会话默认模型</label>
      <select
        id="session-default-model"
        value={sessionDefault ?? ""}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value === "" ? null : event.target.value)}
      >
        <option value="">全局模型</option>
        {!known && sessionDefault && <option value={sessionDefault}>{sessionDefault}（不可用）</option>}
        {options.map((model) => (
          <option key={model.id} value={model.id}>{modelOptionLabel(model)}</option>
        ))}
      </select>
      {problem && <p className="field-error" role="status">{problem}</p>}
    </div>
  );
}

/** Inspector「本次模型」：只在用户选了模型时出现。 */
export function ModelUsedSection({ model, catalog }: { model: ModelCard | null; catalog: ModelCatalogView | null }) {
  const view = modelCardView(model, catalog);
  if (!view) return null;
  return (
    <section className="inspector-section" aria-labelledby="model-used-heading">
      <h3 id="model-used-heading">本次模型</h3>
      <div className="capability-list">
        <div className="capability-row">
          <div><strong>{view.title}</strong><p>{view.detail}</p><p className="muted-copy">{view.note}</p></div>
        </div>
      </div>
    </section>
  );
}
