import type { MemoryRecord } from "./memoryApi";

export function replaceMemory(records: MemoryRecord[], next: MemoryRecord): MemoryRecord[] {
  return records.map((record) => (record.memory_id === next.memory_id ? next : record));
}

export function upsertMemory(records: MemoryRecord[], next: MemoryRecord): MemoryRecord[] {
  return records.some((record) => record.memory_id === next.memory_id)
    ? replaceMemory(records, next)
    : [...records, next];
}

export function removeMemory(records: MemoryRecord[], memoryId: string): MemoryRecord[] {
  return records.filter((record) => record.memory_id !== memoryId);
}

export function selectCurrentAnswerMemories(
  records: MemoryRecord[],
  memoryIds: string[],
): MemoryRecord[] {
  const allowed = new Set(memoryIds);
  return records.filter((record) => allowed.has(record.memory_id));
}
