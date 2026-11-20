/**
 * Virtual User selection. `virtual_user_id` is accepted as an alias for
 * `virtualUserId` because it is the Python, MCP and HTTP name.
 */
export type VirtualUserSelection = { virtualUserId?: string; virtual_user_id?: string };

function checked(value: unknown, name: string): string | undefined {
  if (value !== undefined && (typeof value !== 'string' || !value || /[\s\x00-\x1f\x7f]/.test(value)))
    throw new TypeError(`${name} must be a nonempty header value`);
  return value;
}

export function selectedVirtualUser(selection: VirtualUserSelection = {}): string | undefined {
  const camel = checked(selection.virtualUserId, 'virtualUserId');
  const snake = checked(selection.virtual_user_id, 'virtual_user_id');
  if (camel !== undefined && snake !== undefined && camel !== snake)
    throw new TypeError('virtualUserId and virtual_user_id differ; give only one');
  return camel ?? snake;
}
