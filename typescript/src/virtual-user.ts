/** Virtual User selection, sent as the `X-Virtual-User-Id` request header. */
export type VirtualUserSelection = { virtualUserId?: string };

/**
 * Returns the selected Virtual User, or undefined for the authenticated
 * account. `virtual_user_id` is not an option: it is rejected, never read, so
 * a snake_case selection cannot quietly use the account's own space instead.
 */
export function selectedVirtualUser(selection: VirtualUserSelection = {}): string | undefined {
  if ('virtual_user_id' in selection)
    throw new TypeError('virtual_user_id is not an option; use virtualUserId (sent as X-Virtual-User-Id)');
  const { virtualUserId } = selection;
  if (virtualUserId !== undefined && (typeof virtualUserId !== 'string' || !virtualUserId || /[\s\x00-\x1f\x7f]/.test(virtualUserId)))
    throw new TypeError('virtualUserId must be a nonempty header value');
  return virtualUserId;
}

/** Throws when an operation without Virtual User support is given a selection. */
export function rejectVirtualUser(selection: VirtualUserSelection, operation: string): void {
  if (selectedVirtualUser(selection) !== undefined)
    throw new TypeError(`Virtual User selection is not supported for ${operation}`);
}
