/**
 * The short form of an entity ID shown in the UI (DESIGN §3.3: "short ID").
 *
 * IDs are UUIDv7 (CLAUDE §5): the leading 48 bits are a millisecond timestamp, so the first eight hex
 * characters only change every ~65 s — two runs planned a few seconds apart would read the same.
 * The trailing eight are random, hence distinct between entities created at the same moment.
 * A value that is not a canonical UUID keeps its first eight characters.
 */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function shortId(id: string): string {
  return UUID.test(id) ? id.slice(-8) : id.slice(0, 8);
}
