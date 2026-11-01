// A small generic cache and helpers.

export type Result<T, E = Error> = { ok: true; value: T } | { ok: false; error: E };

export interface Entity {
  id: string;
}

export class LruCache<K, V extends object = Record<string, unknown>> {
  private items = new Map<K, V>();

  constructor(private readonly capacity: number) {}

  get(key: K): V | undefined {
    const value = this.items.get(key);
    if (value !== undefined) {
      this.items.delete(key);
      this.items.set(key, value);
    }
    return value;
  }

  set(key: K, value: V): void {
    this.items.set(key, value);
    if (this.items.size > this.capacity) {
      const oldest = this.items.keys().next().value as K;
      this.items.delete(oldest);
    }
  }
}

export function memoize<A extends unknown[], R>(fn: (...args: A) => R): (...args: A) => R {
  const seen = new Map<string, R>();
  return (...args: A): R => {
    const key = JSON.stringify(args);
    if (!seen.has(key)) seen.set(key, fn(...args));
    return seen.get(key) as R;
  };
}

export const mapValues = <K extends string, V, R>(obj: Record<K, V>, fn: (value: V) => R): Record<K, R> => {
  const out = {} as Record<K, R>;
  for (const key of Object.keys(obj) as K[]) out[key] = fn(obj[key]);
  return out;
};

export class InMemoryRepo<T extends Entity> {
  private rows: T[] = [];

  add(row: T): void {
    this.rows.push(row);
  }

  async findById(id: string): Promise<Result<T, string>> {
    const row = this.rows.find((r) => r.id === id);
    return row ? { ok: true, value: row } : { ok: false, error: `no row ${id}` };
  }
}
