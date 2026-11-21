/** Virtual User selection, sent as the `X-Virtual-User-Id` request header. */
export type VirtualUserSelection = { virtualUserId?: string };

export function selectedVirtualUser({ virtualUserId }: VirtualUserSelection = {}): string | undefined {
  if (virtualUserId !== undefined && (typeof virtualUserId !== 'string' || !virtualUserId || /[\s\x00-\x1f\x7f]/.test(virtualUserId)))
    throw new TypeError('virtualUserId must be a nonempty header value');
  return virtualUserId;
}
