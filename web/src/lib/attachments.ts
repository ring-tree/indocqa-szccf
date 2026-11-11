/** 会话附件的前端校验、类型与大小格式化。 */

export const MAX_FILE_BYTES = 100 * 1024 * 1024;
export const MAX_FILES = 10;

export const ACCEPT_ATTR = [
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".bmp",
  ".svg",
  ".pdf",
  ".docx",
  ".md",
  ".markdown",
  ".txt",
  ".csv",
  ".xlsx",
  ".pptx",
  ".mp3",
  ".wav",
  ".m4a",
  ".aac",
  ".ogg",
  ".flac",
  ".mp4",
  ".webm",
  ".mov",
].join(",");

export type AttachmentKind = "image" | "document" | "audio" | "video";

const KIND_BY_EXT: Record<string, AttachmentKind> = {
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  bmp: "image",
  svg: "image",
  pdf: "document",
  docx: "document",
  md: "document",
  markdown: "document",
  txt: "document",
  csv: "document",
  xlsx: "document",
  pptx: "document",
  mp3: "audio",
  wav: "audio",
  m4a: "audio",
  aac: "audio",
  ogg: "audio",
  flac: "audio",
  mp4: "video",
  webm: "video",
  mov: "video",
};

export function extensionOf(filename: string): string {
  const index = filename.lastIndexOf(".");
  return index >= 0 ? filename.slice(index + 1).toLowerCase() : "";
}

export function kindOfFile(file: File): AttachmentKind | null {
  return KIND_BY_EXT[extensionOf(file.name)] ?? null;
}

export function validateFiles(
  files: File[],
  remaining: number,
): { accepted: File[]; errors: string[] } {
  const errors: string[] = [];
  const accepted: File[] = [];
  if (remaining <= 0) {
    return { accepted, errors: [`单条消息最多 ${MAX_FILES} 个附件`] };
  }
  if (files.length > remaining) {
    errors.push(`最多还能添加 ${remaining} 个附件，已忽略多余文件`);
  }
  for (const file of files.slice(0, remaining)) {
    if (!kindOfFile(file)) {
      errors.push(`${file.name}: 不支持的文件格式`);
      continue;
    }
    if (file.size <= 0) {
      errors.push(`${file.name}: 文件为空`);
      continue;
    }
    if (file.size > MAX_FILE_BYTES) {
      errors.push(`${file.name}: 超过 100MB 上限`);
      continue;
    }
    accepted.push(file);
  }
  return { accepted, errors };
}

export function formatSize(bytes: number): string {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value}B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)}KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)}MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)}GB`;
}

export function fileTypeLabel(name: string): string {
  const ext = extensionOf(name);
  return ext ? ext.toUpperCase() : "FILE";
}
