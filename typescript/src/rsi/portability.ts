import { basename, extname } from 'node:path';
import { read_snapshot } from './materials.js';
import type { EvidenceFile, RubricsContent } from './models.js';

const recordId =
  /\b[A-Za-z][A-Za-z0-9]*[-_][0-9][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*\b/g;
const materialLocator = /\bmaterial_[a-f0-9]{32}(?:\/\d+)?\b/i;

function normalizeRow(row: string) {
  return row
    .trim()
    .toLowerCase()
    .replace(/[ \t]*([,\t])[ \t]*/g, '$1');
}

export class LearningLiterals {
  private ids = new Set<string>();
  private paths = new Set<string>();
  private rows = new Set<string>();
  private filenames: RegExp[];

  constructor(files: EvidenceFile[], allowedPaths: string[]) {
    const allowedNames = new Set(
      allowedPaths.map((path) => basename(path).toLowerCase()),
    );
    const names = new Set(
      files.map((file) => basename(file.path).toLowerCase()),
    );
    this.filenames = [...names]
      .filter((name) => name && !allowedNames.has(name))
      .map((name) => {
        const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        return new RegExp(
          `(?<![\\p{L}\\p{N}_.-])${escaped}(?![\\p{L}\\p{N}_.-])`,
          'iu',
        );
      });
    for (const file of files) {
      this.paths.add(file.path);
      this.paths.add(file.snapshot);
      if (!['.csv', '.tsv'].includes(extname(file.path).toLowerCase()))
        continue;

      let text: string;
      try {
        ({ text } = read_snapshot(file));
      } catch {
        // Unreadable files can only contribute their catalog metadata.
        continue;
      }
      for (const line of text.split(/\r?\n/)) {
        if (!/[,\t]/.test(line) || !/\d/.test(line)) continue;
        this.rows.add(normalizeRow(line));
        const key = line.split(/[,\t]/, 1)[0].trim().replace(/^"|"$/g, '');
        if (key.match(recordId)?.[0] === key) this.ids.add(key.toLowerCase());
      }
    }
  }

  foundIn(fields: Iterable<string>) {
    for (const field of fields) {
      if (
        materialLocator.test(field) ||
        this.filenames.some((name) => name.test(field))
      )
        return true;
      for (const path of this.paths) if (field.includes(path)) return true;
      for (const id of field.toLowerCase().match(recordId) ?? [])
        if (this.ids.has(id)) return true;
      const normalized = normalizeRow(field);
      for (const row of this.rows) if (normalized.includes(row)) return true;
    }
    return false;
  }
}

export function rubricRuleText(rubrics: RubricsContent) {
  return rubrics.criteria.flatMap(({ id, description }) => [id, description]);
}
