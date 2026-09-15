import { CURSOR_MARKER, Input, matchesKey, truncateToWidth, visibleWidth, type Component, type TuiMouseEvent } from '@earendil-works/pi-tui';
import type { EffectiveModel, ModelProfile, ModelScope, ModelSlot } from '../runtime/pi/model-config.js';
import { createNovelTheme } from './theme.js';

export interface ModelFormActions {
  load(scope: ModelScope, slot: ModelSlot): Promise<{ profile: ModelProfile | null; source: string; hasApiKey: boolean }>;
  save(value: { scope: ModelScope; slot: ModelSlot; profile: ModelProfile; apiKey: string }): Promise<EffectiveModel>;
}
type Field = 'scope' | 'slot' | 'provider' | 'api' | 'baseUrl' | 'id' | 'contextWindow' | 'maxTokens' | 'thinkingLevel' | 'apiKey' | 'save';
const fields: Field[] = ['scope', 'slot', 'provider', 'api', 'baseUrl', 'id', 'contextWindow', 'maxTokens', 'thinkingLevel', 'apiKey', 'save'];
const labels: Record<Field, string> = { scope: '保存范围', slot: '使用角色', provider: '服务名称', api: '接口类型', baseUrl: '服务地址', id: '模型标识', contextWindow: '上下文容量', maxTokens: '输出上限', thinkingLevel: '推理强度', apiKey: 'API Key', save: '保存当前配置' };
const choices: Partial<Record<Field, { value: string; label: string }[]>> = {
  scope: [{ value: 'book', label: '仅本书' }, { value: 'global', label: '全局默认' }],
  slot: [{ value: 'default', label: '默认模型' }, { value: 'planner', label: '规划' }, { value: 'writer', label: '写作' }, { value: 'editor', label: '评审' }],
  api: [{ value: 'openai-completions', label: 'OpenAI Chat Completions' }, { value: 'openai-responses', label: 'OpenAI Responses' }, { value: 'anthropic-messages', label: 'Anthropic Messages' }],
  thinkingLevel: [{ value: 'off', label: '关闭' }, { value: 'minimal', label: '最低' }, { value: 'low', label: '低' }, { value: 'medium', label: '中' }, { value: 'high', label: '高' }],
};
const safe = (value: string) => value.replace(/[\x00-\x1f\x7f-\x9f]/g, '');
const identity = (p: ModelProfile) => JSON.stringify([p.provider, p.api, p.baseUrl.replace(/\/$/, ''), p.id]);

/** A single-page draft editor. No configuration is written until Save is selected. */
export function createModelSettingsForm(options: { actions: ModelFormActions; theme?: ReturnType<typeof createNovelTheme>; requestRender: () => void; rows: () => number }) {
  const theme = options.theme ?? createNovelTheme();
  const result = Promise.withResolvers<EffectiveModel | null>();
  let scope: ModelScope = 'book'; let slot: ModelSlot = 'default';
  const empty = (): ModelProfile => ({ provider: 'custom', api: 'openai-completions', baseUrl: '', id: '', contextWindow: 32768, maxTokens: 4096, reasoning: false, thinkingLevel: 'off' });
  type Draft = { profile: ModelProfile; original: ModelProfile; apiKey: string; source: string; hasApiKey: boolean };
  let draft: Draft = { profile: empty(), original: empty(), apiKey: '', source: '未配置', hasApiKey: false };
  const drafts = new Map<string, Draft>();
  const editor = new Input();
  let selected = 0; let editing: Field | undefined; let busy = false; let closed = false; let cancelRequested = false;
  let error = ''; let rowStart = 0; let visibleStart = 0; let visibleCount = 0;
  const repaint = () => { if (!closed) options.requestRender(); };
  const finish = (value: EffectiveModel | null) => {
    if (closed) return;
    closed = true; editor.setValue(''); draft.apiKey = ''; for (const d of drafts.values()) d.apiKey = '';
    result.resolve(value);
  };
  const key = () => `${scope}:${slot}`;
  async function load() {
    const cached = drafts.get(key());
    if (cached) { draft = cached; repaint(); return; }
    busy = true; repaint();
    try {
      const value = await options.actions.load(scope, slot);
      const profile = structuredClone(value.profile ?? empty());
      draft = { profile, original: structuredClone(profile), apiKey: '', source: value.source, hasApiKey: value.hasApiKey };
      drafts.set(key(), draft);
    } catch (e) { error = e instanceof Error ? e.message : String(e); }
    finally { busy = false; if (cancelRequested) finish(null); else repaint(); }
  }
  function raw(field: Field): string {
    if (field === 'scope') return scope;
    if (field === 'slot') return slot;
    if (field === 'apiKey') return draft.apiKey;
    if (field === 'save') return '';
    return String(draft.profile[field]);
  }
  function set(field: Field, value: string) {
    if (field === 'scope') { scope = value as ModelScope; void load(); }
    else if (field === 'slot') { slot = value as ModelSlot; void load(); }
    else if (field === 'apiKey') draft.apiKey = value;
    else if (field === 'contextWindow' || field === 'maxTokens') {
      const match = /^(\d+(?:\.\d+)?)\s*([km])?$/i.exec(value.trim());
      if (!match) throw new Error('容量请填写正整数，支持 32K / 128K / 1M');
      const number = Number(match[1]) * (match[2]?.toLowerCase() === 'm' ? 1_000_000 : match[2] ? 1000 : 1);
      if (!Number.isSafeInteger(number) || number < 1) throw new Error('容量必须是正整数');
      draft.profile[field] = number;
    } else if (field === 'thinkingLevel') { draft.profile.thinkingLevel = value as ModelProfile['thinkingLevel']; draft.profile.reasoning = value !== 'off'; }
    else if (field === 'api') draft.profile.api = value as ModelProfile['api'];
    else if (field !== 'save') draft.profile[field] = value;
  }
  function cycle(field: Field, direction: number) {
    const values = choices[field]; if (!values) return;
    const index = Math.max(0, values.findIndex((v) => v.value === raw(field)));
    set(field, values[(index + direction + values.length) % values.length]!.value); error = ''; repaint();
  }
  function commitEdit() {
    if (!editing) return;
    try { set(editing, editor.getValue().trim()); editing = undefined; editor.setValue(''); error = ''; }
    catch (e) { error = e instanceof Error ? e.message : String(e); }
    repaint();
  }
  async function save() {
    if (busy || closed) return;
    if (editing) { commitEdit(); if (editing) return; }
    busy = true; error = ''; repaint();
    try {
      const profile = structuredClone(draft.profile);
      if (identity(profile) !== identity(draft.original)) delete profile.pricing;
      const saved = await options.actions.save({ scope, slot, profile, apiKey: draft.apiKey });
      finish(saved); // Saving may already have happened before a later cancellation; retain that fact.
    } catch (e) { error = e instanceof Error ? e.message : String(e); }
    finally { busy = false; if (cancelRequested) finish(null); else repaint(); }
  }
  function activate() {
    const field = fields[selected]!;
    if (field === 'save') { void save(); return; }
    if (choices[field]) { cycle(field, 1); return; }
    editing = field; editor.focused = true; editor.setValue(raw(field)); editor.handleInput('\x05'); error = ''; repaint();
  }
  function cancel() {
    cancelRequested = true;
    if (!busy) finish(null);
  }
  editor.onSubmit = commitEdit;
  editor.onEscape = () => { editing = undefined; editor.setValue(''); error = ''; repaint(); };
  const component: Component = {
    handleInput(data) {
      if (closed) return;
      if (matchesKey(data, 'ctrl+c')) { cancel(); return; }
      if (busy) return;
      if (matchesKey(data, 'ctrl+s')) { void save(); return; }
      if (editing) { editor.handleInput(data); repaint(); return; }
      if (matchesKey(data, 'escape')) { cancel(); return; }
      if (matchesKey(data, 'up') || matchesKey(data, 'shift+tab')) selected = (selected + fields.length - 1) % fields.length;
      else if (matchesKey(data, 'down') || matchesKey(data, 'tab')) selected = (selected + 1) % fields.length;
      else if (matchesKey(data, 'left') || matchesKey(data, 'right')) { cycle(fields[selected]!, matchesKey(data, 'left') ? -1 : 1); return; }
      else if (matchesKey(data, 'enter')) { activate(); return; }
      repaint();
    },
    handleMouse(event: TuiMouseEvent) {
      if (busy || editing || event.type !== 'click' || event.button !== 'left') return undefined;
      const index = event.y - rowStart;
      if (index >= 0 && index < visibleCount) { selected = visibleStart + index; activate(); return { handled: true }; }
      return undefined;
    },
    invalidate() { editor.invalidate(); },
    render(width) {
      const inner = Math.max(1, width - 4);
      const pad = (line: string) => { const clipped = truncateToWidth(line, inner); return clipped + ' '.repeat(Math.max(0, inner - visibleWidth(clipped))); };
      const row = (line: string) => theme.line('│') + ' ' + pad(line) + ' ' + theme.line('│');
      const lines = [theme.line('╭') + theme.gold(' 模型设置 ') + theme.line('─'.repeat(Math.max(0, width - 12)) + '╮'),
        row(theme.muted('选中字段后原位编辑；所有改动仅在保存时生效。')), row('')];
      rowStart = lines.length;
      visibleCount = Math.min(fields.length, Math.max(3, options.rows() - 9));
      visibleStart = Math.min(Math.max(0, selected - visibleCount + 1), fields.length - visibleCount);
      for (const field of fields.slice(visibleStart, visibleStart + visibleCount)) {
        const index = fields.indexOf(field); const active = index === selected;
        const label = labels[field]; const prefix = (active ? theme.gold('› ') : '  ') + theme.muted(label) + ' '.repeat(Math.max(1, 14 - visibleWidth(label)));
        let value: string;
        if (editing === field) {
          if (field === 'apiKey') value = '•'.repeat(Math.min([...editor.getValue()].length, Math.max(1, inner - 20))) + CURSOR_MARKER;
          else value = editor.render(Math.max(1, inner - visibleWidth(prefix)))[0] ?? '';
        } else if (field === 'apiKey') value = draft.apiKey ? '••••••••（已输入，待保存）' : draft.hasApiKey ? '已保存 · 留空保留，不显示原密钥' : '未设置 · Enter 输入';
        else if (field === 'save') value = busy ? '正在保存…' : 'Enter / Ctrl+S';
        else value = choices[field]?.find((v) => v.value === raw(field))?.label ?? (raw(field) || '未填写');
        lines.push(row(prefix + (editing === field ? value : (active ? theme.gold : theme.text)(safe(value)))));
      }
      lines.push(row(''), row(error ? theme.error(safe(error)) : theme.muted(busy ? '正在读取或保存，请稍候…' : `当前来源：${draft.source}  ·  保存当前选中的角色配置`)),
        row(theme.muted(editing ? 'Enter 确认字段 · Esc 撤销字段' : '↑↓ / Tab 移动 · ←→ 切换 · Enter 编辑 · Ctrl+S 保存 · Esc 取消')),
        theme.line('╰' + '─'.repeat(Math.max(0, width - 2)) + '╯'));
      return lines;
    },
  };
  void load();
  return { component, result: result.promise, cancel };
}
