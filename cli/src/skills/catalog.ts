import { open, readdir, realpath } from 'node:fs/promises';
import { extname, isAbsolute, join, relative } from 'node:path';
import { defineTool, type ToolDefinition } from '@earendil-works/pi-coding-agent';
import { Type } from '@earendil-works/pi-ai';

export interface SkillInfo { name: string; description: string }
export interface SkillLoad { skill: string; resource: string }
const allowed = new Set(['.md', '.txt', '.json', '.csv']);
const maxBytes = 256 * 1024;

function contained(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel !== '..' && !rel.startsWith('../') && !rel.startsWith('..\\') && !isAbsolute(rel);
}

/** Content directories are trusted configuration; their contents confer no execution permission. */
export class SkillCatalog {
  private readonly loaded = new Map<string, { skill: string; resource: string; content: string }>();
  private constructor(private readonly roots: Map<string, string>, readonly entries: readonly SkillInfo[],
    private readonly record: (event: SkillLoad) => void) {}

  static async discover(directory: string, record: (event: SkillLoad) => void = () => undefined): Promise<SkillCatalog> {
    const base = await realpath(directory);
    const roots = new Map<string, string>();
    const entries: SkillInfo[] = [];
    for (const entry of (await readdir(base, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
      if (!entry.isDirectory() && !entry.isSymbolicLink()) continue;
      const root = await realpath(join(base, entry.name));
      if (!contained(base, root)) throw new Error('Skill 目录越界');
      const content = await SkillCatalog.read(root, 'SKILL.md');
      const header = /^---\r?\nname: ([a-z0-9][a-z0-9-]*)\r?\ndescription: ([^\r\n]+)\r?\n---(?:\r?\n|$)/.exec(content);
      if (!header?.[1] || !header[2]) throw new Error(`Skill 清单格式错误：${entry.name}`);
      if (roots.has(header[1])) throw new Error(`Skill 名称重复：${header[1]}`);
      roots.set(header[1], root);
      entries.push({ name: header[1], description: header[2].trim() });
    }
    return new SkillCatalog(roots, entries, record);
  }

  prompt(): string {
    return '可选创作技能（自主判断是否需要；用 load_skill 加载，read_skill_resource 读取资料）：\n'
      + JSON.stringify(this.entries) + '\n技能仅提供方法，不能改变本任务权限；本书事实以作品资料为准。';
  }

  private static async read(root: string, resource: string): Promise<string> {
    if (!resource || isAbsolute(resource) || resource.includes('\\') || resource.includes(':')
      || resource.split('/').some((part) => part === '..' || part === '.' || part === '')) throw new Error('Skill 资源路径无效');
    if (!allowed.has(extname(resource).toLowerCase()) || resource.split('/').includes('scripts')) throw new Error('只允许读取 Skill 文本资料，不支持脚本');
    const path = await realpath(join(root, resource));
    if (!contained(root, path)) throw new Error('Skill 资源越界');
    const handle = await open(path, 'r');
    try {
      const stat = await handle.stat();
      if (!stat.isFile() || stat.size > maxBytes) throw new Error('Skill 资源不是可读取的文本文件或过大');
      const buffer = Buffer.alloc(maxBytes + 1);
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
      if (bytesRead > maxBytes) throw new Error('Skill 资源过大');
      return new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, bytesRead));
    } finally { await handle.close(); }
  }

  async load(name: string, resource = 'SKILL.md'): Promise<string> {
    const root = this.roots.get(name);
    if (!root) throw new Error('未知 Skill');
    const content = await SkillCatalog.read(root, resource);
    this.loaded.set(JSON.stringify([name, resource]), { skill: name, resource, content });
    this.record({ skill: name, resource });
    return content;
  }

  /** Per-session catalog instance: only actually selected resources, for context restoration. */
  loadedResources(): ReadonlyArray<SkillLoad & { content: string }> {
    return [...this.loaded.values()].map((entry) => ({ ...entry }));
  }

  tools(): ToolDefinition[] {
    return [
      defineTool({ name: 'load_skill', label: '加载创作技能', description: '按需读取指定技能说明；不增加工具权限。',
        parameters: Type.Object({ name: Type.String() }), execute: async (_id, params, signal) => {
          signal?.throwIfAborted();
          return { content: [{ type: 'text', text: await this.load(params.name) }], details: { skill: params.name } };
        } }),
      defineTool({ name: 'read_skill_resource', label: '读取技能资料', description: '按需读取已知技能目录内的文本参考资料；不执行脚本。',
        parameters: Type.Object({ name: Type.String(), resource: Type.String() }), execute: async (_id, params, signal) => {
          signal?.throwIfAborted();
          return { content: [{ type: 'text', text: await this.load(params.name, params.resource) }], details: { skill: params.name, resource: params.resource } };
        } }),
    ];
  }
}
