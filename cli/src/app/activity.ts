import type { HostEvent } from './host.js';

export interface ToolActivity { id: string; name: string; role: string; status: 'running' | 'completed' | 'failed'; at: number }
interface ToolRow extends ToolActivity { started?: number; ended?: number }
interface Group { taskId?: string; label?: string; role: string; status: string; started?: number; ended?: number; tools: Map<string, ToolRow>; note?: string }
const clean = (value: string, max = 100) => value.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/[\x00-\x1f\x7f]/g, ' ').trim().slice(0, max);
const roles: Record<string, string> = { planner: '规划', writer: '写作', editor: '评审', discussion: '共创', arbiter: '意见裁定', host: '运行' };
const states: Record<string, string> = { running: '进行中', completed: '完成', complete: '完成', failed: '失败', incomplete: '未完成', cancelled: '已取消', paused: '已暂停', discussion: '待讨论' };
const clock = (at?: number) => at === undefined ? '--:--:--' : new Date(at).toLocaleTimeString('zh-CN', { hour12: false });
const duration = (start?: number, end?: number) => start !== undefined && end !== undefined && end >= start ? ` · ${((end - start) / 1000).toFixed(1)}s` : '';

/** A bounded projection of observed events, never tool arguments/results or invented task progress. */
export class ActivityTimeline {
  private readonly groups: Group[] = [];
  private readonly active = new Map<string, Group>();
  private readonly toolOwners = new Map<string, Group>();
  constructor(private readonly maxRows = 120) {
    if (!Number.isSafeInteger(maxRows) || maxRows < 2) throw new Error('活动记录容量至少为 2');
  }
  recordTask(event: HostEvent): void {
    if (!['task_start', 'task_end'].includes(event.type)) return;
    const role = clean(event.role), taskId = clean(event.taskId, 180);
    const parsed = Date.parse(event.time), at = Number.isFinite(parsed) ? parsed : undefined;
    let group = [...this.groups].reverse().find((g) => g.role === role && g.taskId === taskId);
    // Re-dispatch of one logical task is a new observed run, not a continuation of its old duration.
    if (event.type === 'task_start' && group?.ended !== undefined && at !== undefined && at > group.ended) group = undefined;
    if (!group) { group = { role, taskId, status: event.status, tools: new Map() }; this.groups.push(group); }
    if (event.type === 'task_start') {
      group.label = clean(event.reason, 72);
      if (group.started === undefined && at !== undefined) group.started = at;
      if (group.ended === undefined) group.status = 'running';
      this.active.set(role, group);
    } else {
      group.status = clean(event.status);
      if (at !== undefined) group.ended = at;
      if (this.active.get(role) === group) this.active.delete(role);
    }
    this.trim();
  }
  recordTool(event: ToolActivity): void {
    if (!Number.isFinite(event.at) || !event.id || !event.name) return;
    const role = clean(event.role), key = `${role}\0${event.id}`;
    const priorOwner = this.toolOwners.get(key);
    let group = event.status === 'running' ? this.active.get(role) ?? priorOwner : priorOwner ?? this.active.get(role);
    if (!group) { group = { role, status: '', tools: new Map() }; this.groups.push(group); }
    const prior = group.tools.get(event.id);
    const row: ToolRow = prior ?? { ...event, role, name: clean(event.name), id: event.id };
    if (event.status === 'running') {
      row.started ??= event.at;
      if (row.ended === undefined) row.status = 'running';
    } else { row.status = event.status; row.ended = event.at; }
    group.tools.set(event.id, row); this.toolOwners.set(key, group); this.trim();
  }
  recordNote(role: string, text: string): void {
    const note = clean(text, 240), safeRole = clean(role);
    if (!note) return;
    const last = this.groups.at(-1);
    if (last?.note === note && last.role === safeRole) return;
    this.groups.push({ role: safeRole, status: '', note, tools: new Map() }); this.trim();
  }
  lines(): string[] {
    return this.groups.flatMap((group) => {
      const role = roles[group.role] ?? group.role;
      if (group.note) return [`${role} · ${group.note}`];
      const head = group.taskId ? `${clock(group.started ?? group.ended)} ${role} · ${group.label || '当前任务'} · ${states[group.status] ?? group.status}${duration(group.started, group.ended)}` : `${role} · 工具调用`;
      const rows = [...group.tools.values()];
      return [head, ...rows.map((row, i) => `  ${i === rows.length - 1 ? '└─' : '├─'} ${clock(row.started ?? row.ended)} ${row.name} · ${states[row.status]}${duration(row.started, row.ended)}`)];
    });
  }
  private trim(): void {
    const count = () => this.groups.reduce((sum, group) => sum + 1 + group.tools.size, 0);
    while (count() > this.maxRows) {
      if (this.groups.length > 1) {
        const removed = this.groups.shift()!;
        if (this.active.get(removed.role) === removed) this.active.delete(removed.role);
        for (const [key, owner] of this.toolOwners) if (owner === removed) this.toolOwners.delete(key);
      } else {
        const group = this.groups[0]!; const id = group.tools.keys().next().value;
        if (id === undefined) break;
        group.tools.delete(id); this.toolOwners.delete(`${group.role}\0${id}`);
      }
    }
  }
}
