import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { isMap, parseDocument } from 'yaml';
import { fail, RecipeMetadataSchema } from './models.js';

export async function readRecipeFile(path: string) {
  if (typeof path !== 'string' || !path.trim()) {
    return fail('invalid_argument', 'recipe_insert requires a file path');
  }
  let markdown: string;
  try {
    const file = await open(path, constants.O_RDONLY | constants.O_NONBLOCK);
    try {
      if (!(await file.stat()).isFile()) throw new Error('Not a regular file');
      markdown = new TextDecoder('utf-8', {
        fatal: true,
        ignoreBOM: true,
      }).decode(await file.readFile());
    } finally {
      await file.close();
    }
  } catch {
    return fail(
      'invalid_argument',
      'recipe_insert requires a readable UTF-8 Markdown file',
    );
  }

  const header =
    /^\uFEFF?---[ \t]*\r?\n((?:[^\n]*\n)*?)---[ \t]*(?:\r?\n|$)/u.exec(markdown);
  if (!header) {
    return fail('invalid_argument', 'Recipe requires YAML frontmatter delimited by ---');
  }

  let metadata: unknown;
  try {
    const document = parseDocument(header[1], { uniqueKeys: true });
    if (
      document.errors.length ||
      document.warnings.length ||
      !isMap(document.contents)
    ) {
      throw new Error('Invalid YAML mapping');
    }
    metadata = document.toJS();
  } catch {
    return fail('invalid_argument', 'Recipe frontmatter must be a valid YAML mapping');
  }

  const parsed = RecipeMetadataSchema.safeParse(metadata);
  if (!parsed.success) {
    return fail(
      'invalid_argument',
      'Recipe frontmatter requires nonempty string fields name and description',
    );
  }
  return {
    content: {
      ...parsed.data,
      instructions: markdown.slice(header[0].length).replace(/^\r?\n/u, ''),
    },
    markdown,
  };
}
